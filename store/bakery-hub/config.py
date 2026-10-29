import os


def _environment_flag_is_true(raw_value):
    """明示的なtrueだけを有効として扱う。"""
    return (
        isinstance(raw_value, str)
        and raw_value.strip().lower() == "true"
    )


# ==========================================
# システム全体の設定管理ファイル値
# ==========================================

LOCAL_DEVELOPMENT = _environment_flag_is_true(
    os.environ.get("LOCAL_DEVELOPMENT")
)
REGISTER_V3_PRODUCTION_ENABLED = _environment_flag_is_true(
    os.environ.get("REGISTER_V3_PRODUCTION_ENABLED")
)
REGISTER_V3_CUTOVER_ENABLED = _environment_flag_is_true(
    os.environ.get("REGISTER_V3_CUTOVER_ENABLED")
)
REGISTER_V3_RECOVERY_ENABLED = _environment_flag_is_true(
    os.environ.get("REGISTER_V3_RECOVERY_ENABLED")
)
REGISTER_V3_OFFLINE_MODE_ENABLED = _environment_flag_is_true(
    os.environ.get("REGISTER_V3_OFFLINE_MODE_ENABLED")
)
_register_runtime = LOCAL_DEVELOPMENT or REGISTER_V3_PRODUCTION_ENABLED

# 各フラグの単独trueでは無効。ローカル開発または本番用の独立ゲートも要求する。
REGISTER_V3_PREVIEW_ENABLED = _register_runtime and _environment_flag_is_true(
    os.environ.get("REGISTER_V3_PREVIEW_ENABLED")
)
REGISTER_V3_SAVE_ENABLED = _register_runtime and _environment_flag_is_true(
    os.environ.get("REGISTER_V3_SAVE_ENABLED")
)
# 集計表示はレジの保存・プレビューとは独立。通常の本番既定値は無効。
REGISTER_V3_AGGREGATION_ENABLED = _register_runtime and _environment_flag_is_true(
    os.environ.get("REGISTER_V3_AGGREGATION_ENABLED")
)
REGISTER_V3_PRODUCTS_PREVIEW_ENABLED = _register_runtime and _environment_flag_is_true(
    os.environ.get("REGISTER_V3_PRODUCTS_PREVIEW_ENABLED")
)
REGISTER_V3_PRODUCTS_WRITE_ENABLED = _register_runtime and _environment_flag_is_true(
    os.environ.get("REGISTER_V3_PRODUCTS_WRITE_ENABLED")
)
# Admin専用の非保存デモ。LOCAL_DEVELOPMENTと正式レジの各フラグから独立。
REGISTER_V3_PUBLIC_DEMO_ENABLED = _environment_flag_is_true(
    os.environ.get("REGISTER_V3_PUBLIC_DEMO_ENABLED")
)
# Phase 5Bの監査・明示対応画面。migration未適用の本番では常に無効。
REGISTER_V3_READINESS_ENABLED = _environment_flag_is_true(
    os.environ.get("REGISTER_V3_READINESS_ENABLED")
)

# AIアドバイス生成で使用するGeminiの最新モデル名
GEMINI_MODEL = "gemini-3.8-flash"

# ==========================================
# 単一管理者認証の設定
# ==========================================
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")
ADMIN_SESSION_MAX_AGE_SECONDS = 12 * 60 * 60

# HTTPを使うLocal Development ModeだけSecure属性を無効化する。
# 未設定・誤値を含む通常環境では必ずTrueへ倒す。
SESSION_COOKIE_SECURE = not LOCAL_DEVELOPMENT
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
MAX_CONTENT_LENGTH = 256 * 1024

# Adminログイン失敗rate limit（5回 / 15分）
ADMIN_LOGIN_RATE_LIMIT_MAX_FAILURES = os.environ.get(
    "ADMIN_LOGIN_RATE_LIMIT_MAX_FAILURES",
    "5",
)
ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS = os.environ.get(
    "ADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS",
    "900",
)

# Guest Session作成rate limit（本番値は環境変数で明示する）
GUEST_CREATION_RATE_LIMIT_MAX_ATTEMPTS = os.environ.get(
    "GUEST_CREATION_RATE_LIMIT_MAX_ATTEMPTS"
)
GUEST_CREATION_RATE_LIMIT_WINDOW_SECONDS = os.environ.get(
    "GUEST_CREATION_RATE_LIMIT_WINDOW_SECONDS"
)

# 同時に存在できる有効なGuest Dataset数
GUEST_ACTIVE_DATASET_LIMIT = os.environ.get(
    "GUEST_ACTIVE_DATASET_LIMIT",
    "10",
)

# ==========================================
# データベース接続設定
# ==========================================
# RenderのPostgreSQL自動生成URL（postgres://）をSQLAlchemy対応形式（postgresql://）に変換する対策
raw_db_url = os.environ.get("DATABASE_URL")

if raw_db_url:
    # 先頭が「postgres://」で始まっている場合は「postgresql://」に置換
    if raw_db_url.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URI = raw_db_url.replace("postgres://", "postgresql://", 1)
    else:
        SQLALCHEMY_DATABASE_URI = raw_db_url
else:
    # 環境変数がない（ローカル開発環境）場合はSQLiteを使用
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(os.path.dirname(__file__), 'local.db')}"

SQLALCHEMY_TRACK_MODIFICATIONS = False

# 店舗の気象履歴を記録する場合のみ設定
WEATHER_LATITUDE = os.environ.get("WEATHER_LATITUDE")
WEATHER_LONGITUDE = os.environ.get("WEATHER_LONGITUDE")
WEATHER_API_MODE = os.environ.get("WEATHER_API_MODE")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY")
WEATHER_LOCATION_NAME = os.environ.get("WEATHER_LOCATION_NAME")
WEATHER_JMA_AREA_CODE = os.environ.get("WEATHER_JMA_AREA_CODE")

# Puoppo側で許可アカウントと分析回数上限を設定済みのときだけ有効化する。
PUOPPO_INTEGRATION_ENABLED = _environment_flag_is_true(
    os.environ.get("PUOPPO_INTEGRATION_ENABLED")
)
