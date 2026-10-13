from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
load_dotenv()
import os
import sqlite3
import secrets
import hmac
import datetime as dt
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import closing

from flask import Flask, abort, render_template, request, redirect, url_for, session
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user

# 🤖 新しい公式パッケージから Client を読み込みます
from google import genai

ALLOWED_EMAILS = {email.strip().lower() for email in os.environ.get("PUOPPO_ALLOWED_EMAILS", "").split(",") if email.strip()}
DAILY_ANALYSIS_LIMIT = 5
MIN_ANALYSIS_INTERVAL_SECONDS = 60


def can_analyze():
    return bool(app.config["GOOGLE_LOGIN_AVAILABLE"] and ALLOWED_EMAILS and
                current_user.is_authenticated and current_user.email.lower() in ALLOWED_EMAILS)


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def verify_csrf():
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        abort(400)

TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def is_env_flag_enabled(name):
    value = os.environ.get(name, "")
    return value.strip().lower() in TRUE_ENV_VALUES


GOOGLE_LOGIN_ENABLED = is_env_flag_enabled("GOOGLE_LOGIN_ENABLED")
GOOGLE_LOGIN_CONFIGURED = all(
    os.environ.get(setting_name, "").strip()
    for setting_name in (
        "SECRET_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    )
)
GOOGLE_LOGIN_AVAILABLE = GOOGLE_LOGIN_ENABLED and GOOGLE_LOGIN_CONFIGURED and bool(ALLOWED_EMAILS)


# 🔑 余計な固定を外し、自動で最適な接続先を選ばせます
client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'), http_options={"timeout": 15000})

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY"),
    GOOGLE_LOGIN_ENABLED=GOOGLE_LOGIN_ENABLED,
    GOOGLE_LOGIN_AVAILABLE=GOOGLE_LOGIN_AVAILABLE,
    SESSION_COOKIE_SECURE=not is_env_flag_enabled("LOCAL_DEVELOPMENT"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
app.jinja_env.globals["csrf_token"] = csrf_token

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

oauth = OAuth(app)

google = None
if GOOGLE_LOGIN_AVAILABLE:
    google = oauth.register(
        name="google",
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        server_metadata_url=(
            "https://accounts.google.com/"
            ".well-known/openid-configuration"
        ),
        client_kwargs={
            "scope": "openid email profile"
        }
    )

# ⚙️ コードのあちこちにあった固定値をここにまとめる（変更時はここだけ直せばOK）
DB_NAME = 'puoppo.db'
RSS_BASE_URL = "https://news.google.com/rss/search"
MAX_NEWS_ITEMS = 100
HOST = '0.0.0.0'
PORT = 5000
DEBUG = is_env_flag_enabled("FLASK_DEBUG")
app.config["DEBUG"] = DEBUG


def get_db_connection():
    """DB接続をここに集約。接続先を変える時もここだけ直せばいい。"""
    return sqlite3.connect(DB_NAME)


# 🗄️ データベースの初期設定を行う関数
def init_db():
    # puoppo.db というデータベースファイルに接続します（なければ自動作成されます）
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()
        # 検索履歴を保存する「history」テーブルを作成します
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_sub TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                profile_image TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS analysis_usage (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL,
                last_at REAL NOT NULL,
                PRIMARY KEY (user_id, day)
            )
        ''')

        conn.commit()


# アプリ起動時にデータベースを作成
init_db()

class User(UserMixin):
    def __init__(self, user_id, google_sub, email, name, profile_image=None):
        self.id = str(user_id)
        self.google_sub = google_sub
        self.email = email
        self.name = name
        self.profile_image = profile_image

@login_manager.user_loader
def load_user(user_id):
    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT
                id,
                google_sub,
                email,
                name,
                profile_image
            FROM users
            WHERE id = ?
            ''',
            (user_id,)
        )
        row = cursor.fetchone()

    if row is None:
        return None

    return User(
        user_id=row[0],
        google_sub=row[1],
        email=row[2],
        name=row[3],
        profile_image=row[4]
    )


@app.route("/login")
def login():
    if not app.config["GOOGLE_LOGIN_AVAILABLE"] or google is None:
        abort(404)

    redirect_uri = url_for(
        "google_callback",
        _external=True
    )

    return google.authorize_redirect(
        redirect_uri
    )


@app.route("/login/google/callback")
def google_callback():
    if not app.config["GOOGLE_LOGIN_AVAILABLE"] or google is None:
        abort(404)

    token = google.authorize_access_token()
    user_info = token.get("userinfo")

    if not user_info:
        return "Googleアカウント情報を取得できませんでした", 400

    if user_info.get("email_verified") is not True or (user_info.get("email") or "").lower() not in ALLOWED_EMAILS:
        abort(403)

    google_sub = user_info.get("sub")
    email = user_info.get("email")
    name = user_info.get("name")
    profile_image = user_info.get("picture")

    with closing(get_db_connection()) as conn:
        cursor = conn.cursor()

        cursor.execute(
            '''
            SELECT
                id,
                google_sub,
                email,
                name,
                profile_image
            FROM users
            WHERE google_sub = ?
            ''',
            (google_sub,)
        )
        row = cursor.fetchone()

        if row is None:
            cursor.execute(
                '''
                INSERT INTO users (
                    google_sub,
                    email,
                    name,
                    profile_image
                )
                VALUES (?, ?, ?, ?)
                ''',
                (
                    google_sub,
                    email,
                    name,
                    profile_image
                )
            )
            conn.commit()

            user_id = cursor.lastrowid

            user = User(
                user_id=user_id,
                google_sub=google_sub,
                email=email,
                name=name,
                profile_image=profile_image
            )
        else:
            cursor.execute(
                "UPDATE users SET email=?, name=?, profile_image=? WHERE id=?",
                (email, name, profile_image, row[0]),
            )
            conn.commit()
            user = User(
                user_id=row[0],
                google_sub=row[1],
                email=email,
                name=name,
                profile_image=profile_image
            )

    login_user(user)

    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    if not app.config["GOOGLE_LOGIN_AVAILABLE"]:
        abort(404)

    verify_csrf()
    logout_user()
    return redirect(url_for("index"))


# 🏠 メイン画面（履歴の一覧表示）
@app.route('/')
def index():
    # GETは検索語の引き渡しだけ。AI処理や共有履歴の読み出しを行わない。
    keyword = request.args.get("keyword", "")
    if keyword and (len(keyword) > 100 or not keyword.strip() or any(ord(c) < 32 for c in keyword)):
        abort(400)
    if keyword and app.config["GOOGLE_LOGIN_AVAILABLE"]:
        session["pending_keyword"] = keyword.strip()
    prefill = keyword.strip() or (session.pop("pending_keyword", "") if app.config["GOOGLE_LOGIN_AVAILABLE"] else "")
    return render_template(
        'index.html',
        keyword=prefill,
        analysis_available=can_analyze(),
        google_login_available=app.config["GOOGLE_LOGIN_AVAILABLE"],
    )


def reserve_analysis_slot(user_id):
    """同一ユーザーの連続・日次分析をSQLiteの書込ロックで制御する。"""
    now = dt.datetime.now(dt.timezone.utc)
    day = now.date().isoformat()
    with closing(get_db_connection()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT count, last_at FROM analysis_usage WHERE user_id=? AND day=?",
            (user_id, day),
        ).fetchone()
        if row and (row[0] >= DAILY_ANALYSIS_LIMIT or now.timestamp() - row[1] < MIN_ANALYSIS_INTERVAL_SECONDS):
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO analysis_usage (user_id, day, count, last_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1, last_at=excluded.last_at",
            (user_id, day, now.timestamp()),
        )
        conn.commit()
    return True


# 📥 検索窓から送信されたキーワードを処理し、分析結果画面を表示する
@app.route('/search', methods=['POST'])
def search():
    if not can_analyze():
        abort(403)
    verify_csrf()
    keyword = request.form.get('keyword', '').strip()
    if not keyword or len(keyword) > 100 or any(ord(character) < 32 for character in keyword):
        abort(400)
    if not reserve_analysis_slot(int(current_user.id)):
        return "分析回数の上限または連続操作の制限に達しました。時間を置いてください。", 429

    # 2. 📰 以前作成したロジックでRSSからニュースを取得
    # 例として、GoogleニュースのRSSからキーワードに関連する記事を最大100件取得します
    news_titles = []
    try:
        # urlencodeでパラメータの組み立てとエンコードをまとめて行う
        query_params = urllib.parse.urlencode({
            'q': keyword, 'hl': 'ja', 'gl': 'JP', 'ceid': 'JP:ja'
        })
        url = f"{RSS_BASE_URL}?{query_params}"

        # RSSのXMLデータをダウンロードして解析
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as response:
            xml_data = response.read(512 * 1024 + 1)
        if len(xml_data) > 512 * 1024:
            raise ValueError("RSS response too large")

        root = ET.fromstring(xml_data)
        # XMLから記事のタイトル（title）を抽出
        for item in root.findall('.//item')[:MAX_NEWS_ITEMS]:  # 直近100件
            # titleタグが無い記事でも落ちないようにNoneチェックを挟む
            title_elem = item.find('title')
            if title_elem is not None and title_elem.text:
                news_titles.append(title_elem.text)
    except Exception:
        news_titles = []

    if not news_titles:
        return "ニュース見出しを取得できず、AI分析を実行できませんでした。", 503

    # 3. 🤖 ニュース記事を元にGeminiに世論・不満分析を依頼
    analysis_result = ""
    try:
        # ニュースのタイトルを1つの文章にまとめます
        news_context = "\n".join([f"- {t}" for t in news_titles])

        # 以前使用した分析用のプロンプト（指示文）を作成
        prompt = f"""
        あなたは世論および不満トレンドの分析AI「Puoppo（ポッポ）」です。
        以下のキーワードに関する最新ニュースのタイトルを元に、現在の世論の動向や、人々が抱いている「不満・課題」を親しみやすい日本語で分析・要約してください。

        キーワード: {keyword}

        対象ニュース:
        {news_context}

        出力は、箇条書きなどを交えて分かりやすくまとめてください。
        """

        # 🤖 確実なモデル名に修正します
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )

        # 外部生成テキストはJinjaの自動エスケープで表示する。
        analysis_result = response.text or "分析結果を取得できませんでした。"

    except Exception:
        analysis_result = "AIによる分析中にエラーが発生しました。APIキーや通信環境を確認してください。"

    # 4. 📄 結果画面（result.html）にデータを渡して表示！
    return render_template('result.html', keyword=keyword, result=analysis_result, news=news_titles)


# 🗑️ 検索履歴を削除する処理
@app.route('/delete', methods=['POST'])
def delete_history():
    # 旧共有履歴は画面から参照・削除できない。既存DBの行は保持する。
    abort(404)


if __name__ == '__main__':
    app.run(debug=DEBUG, host=HOST, port=PORT)
