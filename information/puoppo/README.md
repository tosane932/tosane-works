# Puoppo 🕊

![トップ画像](https://raw.githubusercontent.com/tosane932/tosane-works/main/information/puoppo/file_00000000e1dc820982ad82ea885d80c7.png)

## 🚀 概要：なぜ「Puoppo」なのか

**「現場には、現場の最適解がある。」**

プログラミング実務未経験の状態からPython学習を開始し、当時の開発記録では、学習開始から42時間目の段階でPuoppoの開発に着手。初期版を1日、約10時間で作成した情報収集・AI分析システム――それが『Puoppo（ポッポ）』です。その後、Google OAuth関連処理やFlask-Loginの導入、認証設定の見直しなどを行っています。
名前の由来は伝書鳩🕊️、そして鳩といえば平和の象徴、『public opinion poll(世論調査)』の "pu"・"op"・"po" を組み合わせ、鳴き声の「ポッポ」と名付けました。
記事収集・履歴保存・AI分析の処理を分け、変更箇所を追いやすい構成を意識して作りました。

---

## 🚀 オンラインデモ
以下のURLから、ローカル環境の構築なしで、ブラウザ上で実際のアプリケーションを体験いただけます。（スマホ対応）

**👉 [Puoppo（Render）](https://puoppo.onrender.com/)**

**👉 [ソースコード（Tosane Works / information/puoppo）](https://github.com/tosane932/tosane-works/tree/main/information/puoppo)**

現在は[Tosane Works](https://github.com/tosane932/tosane-works)の `information/puoppo/` に配置しています。

### 📊 システム体験の手順
初めてアプリを触る方は、ぜひ以下の手順に沿って、公式RSSからのデータ収集とAIによる要約ロジックを体験してみてください。

1. **検索キーワードを入力する**
   - テキストボックスに知りたい情報を入力します。
   - スペースで区切った複数ワード（例：`サッカー ワールドカップ`）も検索キーワードとして送信できます。
2. **「調査開始」ボタンを押す**
   - ボタンを押すと、GoogleニュースのRSSから関連記事の見出しを収集し、Gemini APIによる分析・要約がスタートします。
3. **要約結果を確認する**
   - RSS内の最大100件を対象に見出しを取り出し、その一覧をもとに分析した結果を1カラムで表示します。記事本文は取得していません。取得件数・通信環境・APIの応答によって処理時間は変わります。

---

※公開先の起動待ちや通信状況により、表示に時間がかかる場合があります。待つ間に、上記の体験手順や以下のデモ映像（YouTube）もご確認いただけます。

---


## 🎥 実際の動作デモ映像（YouTube）

「忙しい毎朝、ニュースを1つずつ調べて時間を使っていませんか？」
RSSから集めた最大100件の見出しをもとに、AIが情報をまとめて分析します。撮影時点の動作の様子はこちらの動画でご確認ください。

[![Puoppoデモ映像](https://img.youtube.com/vi/8iM_egbN_Cw/maxresdefault.jpg)](https://youtu.be/8iM_egbN_Cw)

(画像をクリックするとYouTubeでデモ映像が流れます / 7分49秒)

---

## 💡 特徴

### **「キーワードからの自動収集＆要約」**
  テキストボックスに「知りたい情報（例：政治 物価高 最新音楽ニュース等）」を入力すると、アプリがGoogleニュースのRSSから最大100件を対象に見出しを取り出し、Gemini APIへ分析・要約を依頼します。検索キーワードは前後の空白を除去し、最大100文字で扱います。*※スペースを入れて打てば複数ワードにも対応しています*

### **「シンプルな1カラムUI」**
  検索 → 結果という流れを一直線に設計し、迷わず使えることを意識したインターフェースです。

### **「GoogleニュースのRSSフィードを使ったデータ取得」**
  大手ニュースサイトへの直接スクレイピングを20回近く試みましたが、ことごとくブロックされました。[詳細はQiita記事に。](https://qiita.com/tosane932/items/974e12e369eda378549b)

記事本文の直接スクレイピングから、GoogleニュースのRSSに含まれる見出しを取得する設計へ切り替えました。RSS側の配信状況や通信エラーによって取得に失敗する場合はあります。

  *(余談ですが、物流の現場で「無理に近道を狙うより、整備された道を行く方が結局早い」と感じてきた経験が、この方法に行き着く後押しになった気がしています。)*

### **「検索履歴とGoogleログイン関連処理」**
  SQLiteの `history` テーブルへ検索キーワード・日時を保存し、一覧表示・削除ができます。現在の履歴は利用者別に分離していません。

  AuthlibによるGoogle OAuthとFlask-Loginによるログイン状態管理のコードを残しています。AI分析には `GOOGLE_LOGIN_ENABLED=true`、`SECRET_KEY`、`GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET` と `PUOPPO_ALLOWED_EMAILS`（カンマ区切りの許可メール）がすべて必要です。未設定なら公開POST `/search` は403を返し、Geminiを呼びません。認証済みの許可ユーザーでも1日5回・最短60秒間隔、CSRF付きPOSTのみです。既存の共有検索履歴は表示・新規保存・削除を停止します。既存DBの行は削除しません。実際のOAuth利用可否とGoogle側の請求上限は本番環境で別途確認が必要です。

  Bakery Hubからの連携は検索語をPuoppoのトップへGETで引き渡し、入力欄へ入れるだけです。GETではRSSやGeminiを呼びません。許可アカウントでログイン後、本人がPuoppo側の分析ボタンを押したときだけ費用のかかる処理を実行します。Renderの無料ディスクを使うSQLiteの利用回数記録はサービス再作成時に失われるため、Google側のAPI予算上限も別途設定してください。

---

## ⚙️ セットアップと起動方法

PCのローカル環境に直接インストールして動かす方法と、環境を汚さずにコマンド一発で動かせる Docker を使った方法（推奨）の2通りに対応しています。

まずTosane Worksをcloneし、Puoppoのディレクトリへ移動します。

```bash
git clone https://github.com/tosane932/tosane-works.git
cd tosane-works/information/puoppo
```

### 事前準備

#### 1. Puoppoのディレクトリ（`information/puoppo/`）に `.env` ファイルを作成し、Gemini の API キーを設定します。

```text
GEMINI_API_KEY=あなたのAPIキーを貼り付け
```

#### 2. APIキーは [Google AI Studio](https://aistudio.google.com/apikey) にアクセス。
Googleアカウントでログイン →  「Get API key」またはスマホの場合は左メニューからメールアドレス上にある🔑鍵マークを選択して、［APIキーを作成］をタッチすると発行されます。
#### 3. 発行されたキーを `.env` の `GEMINI_API_KEY` =の後に貼り付け

```text
GEMINI_API_KEY=あなたのAPIキーを貼り付け
```

#### ※⚠️セキュリティ保持（機密情報の流出防止）のため、.env ファイルは絶対に GitHub 等のリモートリポジトリにプッシュしないでください。⚠️

---

### 🐳 A. Docker での起動方法（推奨）
ローカル環境に Python や依存ライブラリを直接インストールすることなく、密閉されたコンテナ環境で安全・簡単に起動できます。

#### 1. Docker イメージのビルド
以下のコマンドを実行し、ローカルのソースコードをベースにコンテナイメージを構築します。
末尾のドット（ビルドコンテキスト）により、手元の最新情報がそのまま出荷されます。

```bash
docker build -t puoppo-app .
```

#### 2. コンテナの起動
ビルドしたイメージを指定し、事前準備した環境変数ファイルをコンテナ内に積み込んでエンジンをかけます。

```bash
docker run -p 5000:5000 --env-file .env puoppo-app
```

起動後、ブラウザで http://localhost:5000 にアクセスすると、アプリケーションをご利用いただけます。

---

### 🐍 B. ローカル環境（Python）での起動方法
仮想環境（.venv）を作成してローカル環境で直接実行する場合の手順です。

#### 1. 仮想環境の作成と有効化

```bash
python3 -m venv .venv
source .venv/bin/activate
```

#### 2. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

#### 3. アプリケーションの起動

```bash
python app.py
```

起動後、ブラウザで http://localhost:5000 にアクセスしてください。

---

### 🛠️ システム環境と修得技術

```text
[言語・フレームワーク]: Python / Flask（マイクロフレームワーク）

[コアロジック]: RSSフィード解析（公式エンドポイントを使った記事収集）

[AI連携]: Gemini API（Google GenAI SDK）を使った記事要約・分析ロジック

[データ保存]: SQLite（検索履歴・Googleユーザー情報）

[認証関連]: Authlib / Flask-Login / Google OAuth（環境変数による任意有効化）

[コンテナ技術]: Docker（Dockerfile / .dockerignore によるビルド軽量化・最適化）

[フロントエンド]: HTML / CSS / Jinja2テンプレート / JavaScript / シンプルな1カラムUI

[公開先・CI]: Render / GitHub Actions（Python構文・Flask画面のsmoke check）

[開発環境]: Ubuntu（Lubuntu） / VS Code / .venv（仮想環境）
```

DockerfileとCIはPython 3.11を使用し、Dockerは `python app.py` で起動します。Flaskのdebugは既定で無効で、`FLASK_DEBUG` の明示指定で切り替えます。モノレポのPuoppo CIは `information/puoppo/` を作業ディレクトリとしています。Renderの管理画面上のRoot Directory・環境変数・永続ディスク設定はリポジトリからは確認できません。

### 🔧 これから
知りたい最新情報はたくさんあるけれど、一つずつ調べる時間も、長々と意見を聞く時間もない。
Puoppoは、そんな悩みを少しでも減らせたらと思って作りました。
気になるキーワードを入力すれば、AIが関連記事の見出しをもとに分析・要約してくれます。
浮いた時間で何をするか、考えてみるのも楽しいかもしれません。

