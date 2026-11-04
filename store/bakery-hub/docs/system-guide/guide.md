# Bakery Hub システム教科書

機能・設計・技術・安全性を理解するための解説書

更新：2026年11月4日。対象：Bakery Hub v3.2実装を含むmain。
基準コミット：6f61e12c5927d31a0be31a5a1e2f89a15b36b859

この本はBakery Hubという店舗運営システムを理解するための資料です。一般的なPython入門書ではありません。画面で行った操作が、どの処理を経てデータとなり、安全に保存・表示されるのかを読み進めます。コードの住所はすべて store/bakery-hub/ を基点とします。

公開状態は上記コミットのREADMEと公開準備記録に基づきます。本書の制作で本番の設定・稼働状態を再取得していません。本番で利用できると記す項目も記録上の状態であり、現在の接続成功を保証するものではありません。資料の説明と公開操作の承認は別です。

## 第1章｜Bakery Hubの全体像

### お店の仕事をつなぐ

想定する利用者は小さなベーカリーの店長とスタッフです。商品を登録し、販売数を記録し、売れ行きを確かめ、翌日の仕込みや材料を考える。さらに出勤予定や店舗のメモも必要になります。Bakery Hubはこれらを店舗ホームから辿れるようにまとめ、紙や別の画面への転記・探し直しを減らすことを目指します。

この目的は「ボタンを増やすこと」ではありません。利用者が今の登録状態と次の操作を把握できることが基準です。業務の最終判断は人が行います。AIの提案も、予報も、販売実績そのものと同じ確実性では扱いません。

### システム構成図（概念図）

ブラウザ（HTML・CSS・JavaScript）
↓ HTTPリクエスト：画面を開く／入力を送る
Python・Flask（ルート・権限確認・検証・計算）
↕ SQLAlchemy（モデルを通して問い合わせる）
PostgreSQL（商品・会計・予定などを永続保存）
↑ HTMLまたはJSONの応答を画面へ返す

外部サービスはサーバー側の処理につながります。天気情報・公開RSSは取得して表示し、Gemini APIは必要な分析時に呼び出します。Puoppoは別アプリであり、Bakery HubのDBを共有する分析処理ではありません。

### 使用技術は仕事の分担

| 技術 | Bakery Hubでの役割 |
| --- | --- |
| Python / Flask | URLと処理を結び、入力・権限・業務条件を確認する |
| Jinja / HTML / CSS | データをHTMLへ埋め込み、表示順・余白・押しやすさを整える |
| JavaScript / Chart.js | 入力操作、JSON取得、グラフ、レジの端末側操作を担当する |
| SQLAlchemy / PostgreSQL | データをモデルとして扱い、制約付きで永続保存する |
| SQLite | 通常のpytestで使う隔離されたメモリ内DB。PostgreSQL固有の保証は別試験 |
| Alembic / Flask-Migrate | DB構造の変更履歴を管理する |
| Docker / Gunicorn / Render | 実行環境を組み立て、Webリクエストを処理・公開する |
| pytest / Node / Playwright | Python・JavaScript・ブラウザ操作の異なる層を検証する |

### 実装と公開を分けて読む

| 状態 | 基準コミットで確認した範囲 |
| --- | --- |
| 本番利用可能と記録 | 従来のAdmin・Guest日次入力、商品・分析、店舗ホーム等。機能ごとの権限・外部設定条件あり |
| 実装済み・本番未公開 | v3.2 Guestの商品・写真・オンライン会計・返金・分析・スタッフ／シフト・仕込み |
| 実装済み・公開判定NO-GO | Admin正式レジ。非保存デモ・監査画面も有効化条件を持つ |
| 開発・検証上の残課題 | Guest公開の実機・PWA確認、正式レジ実機、未同期会計のログアウト案内 |
| 未対応・今後の課題 | Guest廃棄ロス、Guest仕込みへの販売実績参考連携 |

根拠：README.md、docs/v3.2-guest-launch-readiness.md、docs/v3.2-phase5b-g580-and-rollback.md。11月2日のバックアップ・隔離復元はREADMEに利用者報告として追記されています。古い公開準備記録の「未実行」と同一時点の記述ではありません。

## 第2章｜店舗運営を支える機能

### 商品管理：現在の設定と過去の売上を分ける

利用者は商品を登録し、名前・価格・写真・販売状態を管理します。従来の月次Productと、継続して使うRegisterProductは役割が異なります。月次商品は年月とDatasetを持ち、日次数量DailySalesとつながります。レジ商品は会計明細から参照され、名前と単価は会計時点の値も明細に保存されます。後で値上げしても、過去会計の単価を書き換えない設計です。

商品一覧は今月の登録と過去のCatalogを区別します。標準18商品のテンプレートと、Guest v3.2開始時の公開可能な初期商品は同じ意味ではありません。GuestはAdminの実商品を無断で複製しません。写真は許可された素材・アップロード経路で扱い、画像名だけを信用して任意ファイルを読ませません。

担当：app.py、catalog_repository.py、register_product_routes.py、register_products.py、register_images.py、models.py。検証：tests/test_register_product_routes.py、tests/test_register_products.py、tests/test_guest_register_v32.py。入力上限・写真・別Datasetへのアクセス・失敗時のrollbackが確認点です。

### レジ・会計・返金：金額の正本をサーバーに置く

写真タイルで商品を選び、数量と預かり金を確認して会計を完了します。返金は販売と別会計として確認します。これは実装済みレジの動作説明であり、正式Admin本番レジの公開済みを意味しません。

ブラウザが送る合計金額を正本にはしません。register_service.pyがDBの商品価格を読み直し、数量から合計・お釣りを計算します。表示時から価格が変わった場合、営業日が変わった場合、旧日次売上が同日にある場合には自動的に保存を押し通しません。返金は負の数量を使い、販売との混在や未確認の返金を拒否します。

担当：register_routes.py、register_service.py、register_products.py、static/register_core.js、static/register_offline_store.jsなど。検証：tests/test_register_routes.py、tests/test_register_foundation.py、tests/test_register_service_postgresql.py、tests_js/register_core.test.cjs、tests_e2e/guest_register_v32.spec.cjs。再送・競合・価格差・期限・保存失敗までが検証対象です。

### 売上分析：保存した事実を集計する

Dashboardでは期間を選び、売上や数量、商品順位を確認します。ホームの本日売上も同じ利用者のDatasetの集計を使います。記録がない「未入力」と、記録がある合計0円は別の状態です。

旧日次と新レジは単純に足し合わせません。切替情報に基づいて集計元を選び、返金も反映します。会計が端末に残っているだけではサーバー集計へ入りません。v3.2 Guestの分析導線は保存照合後に表示する考え方です。

担当：register_sales_aggregation.py、register_sales_view.py、sales_period_summary.py、app.py、templates/dashboard.html。検証：tests/test_register_sales_aggregation.py、tests/test_register_dashboard_integration.py、tests/test_register_sales_aggregation_postgresql.py。画面のグラフだけでなく、合計の根拠とDataset境界を確認します。

### 仕込み・レシピ・廃棄ロス：予定と実績を混同しない

仕込みではレシピを選び、作る数量を入力して必要材料を合算します。production_prep.pyのrequired_ingredients()やcombined_ingredients()が材料量を求めます。入力をwhole_number()やingredient_amount()で検証し、不正な数量を計算へ流しません。レシピ・材料・日付別計画は別モデルで管理します。

廃棄は /prep/waste で日付・商品・数量等を記録するAdmin向け実装です。本番利用可否は今回未確認です。READMEの旧節に「Draft PR・本番未適用」が残る一方、最新の復元報告は後続revisionを示しており、古い説明だけから公開済みとも未適用とも断定しません。waste_records.pyのadmin_dataset()が対象を限定します。仕込んだ予定数だけから「店頭に残っている数」を断定する機能ではありません。Guestの廃棄管理と仕込み販売実績参考連携は未対応です。

担当：production_prep.py、waste_records.py、PrepRecipe・PrepIngredient・PrepPlan・WasteRecord。検証：tests/test_production_prep.py、tests/test_guest_prep_v32.py、tests/test_waste_records.py、tests/test_prep_postgresql.py、tests_e2e/guest_prep.spec.cjs。材料計算と認可を分けて調べ、複数計画の保存失敗で一部だけ残らないことも確認します。

### スタッフ・シフト・予定・店舗ツール

スタッフを登録し出勤予定を入力すると、ホームから当日の予定を確認できます。スケジュールは日時のある作業を、メモは共有情報を、タスクは対応状況を表します。用途を分けることで、情報を一つの自由記入欄に詰め込まずに済みます。

担当：shop_staff_shifts.py、shop_schedules.py、shop_memos.py、shop_tasks.py、material_orders.py。モデルはShopStaff・ShopShift・ShopSchedule・ShopMemo・ShopTask・MaterialOrderItem。更新時も認可されたDatasetに絞ります。v3.2 Guestスタッフ／シフトは別の有効化条件を持ち、本番未公開です。

検証：tests/test_shop_staff_shifts.py、tests/test_home_staff.py、tests/test_guest_staff_v32.py、tests/test_guest_staff_hardening.py、tests/test_dataset_authorization.py。存在する他人のIDをURLへ入れた場合も、自分の記録として扱わないことが重要です。

### 天気・ニュース・AI：情報の由来と限界を表示する

ホームと /weather は地域の予報と対象時刻を表示します。current_weather.pyとhome_forecast.pyが取得・検証・集計を担当します。未提供値を0や推測値で埋めません。DailyWeatherの観測・再解析データとDailyForecastの予報記録は別物です。MET Norwayの取得時刻別データから求めた最大・最小と、全日の日最高・最低も同じ意味とは限りません。

bakery_news.pyは公開RSSの見出し・日付・出典・リンクを取得し、キャッシュや重複除外を行います。本文転載や見出しからの世論断定はしません。Puoppo連携は独立したログイン・分析のサービスへ検索語を渡す仕組みで、初期は無効、Guestからの引き渡しも無効です。

売上AIはapp.pyとprompts.pyが担当し、許可されたDatasetの集計を基にGeminiへ依頼します。Guestには回数制限があります。外部応答の失敗も業務保存の成功とは分けます。検証では外部サービスをモックし、実Geminiの呼び出しを必要としません。tests_js/home_weather.test.cjsとtests_e2e/home_weather.spec.cjsも表示と欠損時の確認に使います。

## 第3章｜画面操作からデータ保存まで

### 現行の日次入力を最後まで追う

GET /input は画面を開くリクエストです。app.pyのinput_sales()が認証を確認し、require_current_dataset()を通じて許可された領域を決め、年月に合う商品と数量を取得してinput.htmlへ渡します。Jinjaは商品を繰り返し配置してHTMLを作ります。

入力後のPOST /inputではrequest.formから日付、getlist()で商品IDと数量の列を受けます。日付の変換、列の長さ、重複ID、数量の範囲、商品のDatasetと年月・販売状態を検証します。ifはここでは「不正な状態なら保存しない」分岐、forは「送られた全商品を調べる」繰り返しです。returnは応答を返して処理を終えるため、不正な行の後に保存処理へ進むことを防ぎます。

検証済みの全行をDBへ送り、同じ商品・日付がある場合は数量を更新します。PostgreSQLとSQLiteではon_conflict_do_update()を使います。commit()でまとめて確定し、DB例外ならrollback()で取り消します。保存成功後はsuccess=Trueを指定して入力画面のHTMLを返します。この処理はDashboardへ自動リダイレクトしません。利用者が分析リンクを開くGET /dashboardは別リクエストです。

Dashboardは集計結果を表示し、/api/dashboard-dataなどのJSONをJavaScriptが使います。request.formは送信された入力、モデルはDBの記録、JinjaはHTML生成、JSONは画面側へ渡すデータ表現です。似た辞書状の形でも責任が違います。

### 実装済みレジ会計の往復（概念図）

商品選択 → 数量・預かり金の確認 → 同じ会計キーを付けてJSON送信
↓ POST /api/register/transactions
認証・公開条件・Dataset・JSON項目の確認
↓ register_service.save_register_transaction()
入力正規化 → Datasetロック → 既存会計照合 → 価格・営業日確認
↓ 商品と会計明細の計算・保存
1回のcommit → 会計ID等のJSON応答 → 端末側の保存照合・表示

register_routes.pyはJSONの必須項目が過不足なく揃っているか調べます。サービスは認可済みDatasetを受け取ります。入力されたdataset_idだけで権限を決めません。データが不正なら400、権限や公開条件に合わなければ403、価格変更・同じキーの内容違いなどは409、一時的なDB障害は503として返す実装です。これらは同じ「エラー」でも、利用者が取り直すべき入力や再送判断が異なります。

### 実コードから読む、再送の早期終了

以下はregister_service.pyのsave_register_transaction()からの連続した抜粋です。前後の入力正規化・ロック取得・例外処理は省略しています。

```python
previous = _existing(owner.id, key, digest)
if previous is not None:
    db.session.commit()
    return previous
```

_existing()はDatasetと会計キーに加え、内容のdigestを照合します。同じ会計があれば、その記録を返して終了します。returnは「会計をもう一つ追加するところへ進まない」ために働きます。違う内容を同じキーで送れば成功として使い回しません。新しいキーを勝手に作って再送すると、この仕組みの前提を崩すので、端末は元のキーを保持します。

### JavaScriptは何を担当するか

画面では商品タイルや数量入力、預かり金、通信状態、未同期表示を更新します。JSONを受け取るfetchは通信の窓口で、DBへ直接保存する命令ではありません。画面の「完了」表示だけで保存の正しさを判定せず、サーバーの結果・保存照合を確認します。これが通信切断時に「押せたつもり」を売上にしないための境界です。

## 第4章｜データ管理と安全設計

### モデルのつながり（概要・全テーブルではない）

| 主体 | 関連する記録 | 分離・整合性の要点 |
| --- | --- | --- |
| Dataset | Product、RegisterProduct、スタッフ、予定等 | AdminとGuestごとの領域 |
| Product | DailySales | 月次商品と日次数量。商品・日付の重複を制約で管理 |
| RegisterTransaction | RegisterTransactionLine | 1会計と複数明細。会計時点の商品名・単価も保持 |
| RegisterProduct | 価格履歴、会計明細 | 現在価格と過去の事実を分ける |
| PrepRecipe | PrepIngredient、PrepPlan | レシピ定義と日付ごとの計画を分ける |
| ShopStaff | ShopShift | スタッフと出勤予定。別Datasetの結合を防ぐ |

SQLAlchemyのモデル定義には型・外部キー・一意制約・CHECK制約があります。アプリの入力検証に加え、DB自身も不正な状態の保存を拒否します。例えばRegisterTransactionLineは明細金額が単価×数量であることをCHECKし、会計と商品との複合外部キーにdataset_idを含めます。Pythonのフィルターだけを唯一の境界にしません。

### 認証と認可は別の確認

認証は誰として利用しているか、認可はその人が何に触れてよいかです。Flask-Loginのログイン情報だけで、任意IDの商品へアクセスしてよいわけではありません。require_current_dataset()などで現在の領域を解決し、取得・更新クエリを限定します。Admin・Guest A・Guest Bを別の主体として検証します。

Admin認証にはadmin_sessions.pyとAdminAuthSessionを使います。サーバー側で有効期限と失効状態を都度確認し、ログアウトで失効させます。生トークンはDBへ保存せずhashで照合します。Cookieの存在だけで無期限の認証を成立させません。秘密値や実Cookieは本書にもリポジトリにも掲載しません。

Guestは無操作30分・最長2時間などの制限を持ち、AIは合計3回などの上限があります。期限が切れた瞬間に全行が物理削除される保証とは違います。次のGuest開始等のcleanup時に対象を再確認して削除します。期限・上限を撤廃して使いやすくする設計ではありません。

### トランザクションと競合

会計本体だけ保存され、明細が欠けると合計の根拠が壊れます。save_register_transaction()は会計と全明細を一つのトランザクションで確定し、例外ではrollbackします。flush()はIDや制約をDBへ反映する途中操作で、最終的なcommit()と同義ではありません。

同時に二つのリクエストが到着する可能性があります。DatasetロックやPostgreSQLの商品行ロックに加え、UNIQUE(dataset_id, submission_key)が最後の重複防止になります。IntegrityErrorの後はrollbackして、新しいトランザクションで同じ会計を再照合します。すべてのIntegrityErrorを「再送成功」と決めつけない設計です。

### オフライン保存と再同期

IndexedDBに会計を残すことは、サーバーへ売上を登録したこととは異なります。レジの端末側保存領域は認証・Datasetのscopeと結びつきます。register_offline_service.pyとregister_offline_store.jsなどが、許可・期限・営業日・価格・再送を扱います。通常のPWAインストールだけで、誰でもオフライン会計ができるわけではありません。

再接続後は同じ会計キーで送信し、サーバーに保存されたか照合します。価格変更や過去営業日など自動保存できないものは復旧確認へ回し、端末の記録を勝手に消しません。Guest v3.2で実装したオンライン会計の公開と、Adminの正式オフライン運用の承認は別です。

未同期会計のあるログアウト時の案内は残課題です。「ログアウトすれば安全に全件同期される」「端末のデータは常に消えない」と断定しないでください。tests/test_register_offline.py、tests/test_register_recovery.py、tests_js/register_offline.test.cjs、tests_e2e/offline_register.spec.cjsで、正常再送だけでなく拒否・復旧・scope境界を検証します。

## 第5章｜UI/UXと設計思想

### 状態を見せ、次の操作を伝える

登録済み・未入力・保存できない等を明示し、色に文字とアイコンを添えます。入力に失敗したときは、どこを直せばよいか分かる説明が必要です。危険な操作の意味を利用者の記憶に頼らず、確認・入力制約・サーバー検証を組み合わせます。

ホームの主要カードは空白部分からも目的の一覧へ移動でき、内側の個別リンクはそれぞれの行き先を優先します。CSSで主リンクのクリック領域を広げ、リンクの入れ子やJavaScriptによる二重遷移を避けています。「本日の売上」と「今日の天気」は単なる装飾ではなく、確認作業の入口です。

### 端末に合わせて操作領域を変える

レジはスマートフォンでは商品2列と縦スクロール、横長タッチ端末では商品2行の横スクロールと右の会計領域、PCでは高さに応じた2〜4行を使います。register_layout.jsは画面の実測寸法と入力方式を見て調整します。数量操作のたびに全商品DOMを作り直さず、位置やフォーカスの安定を守ります。

小さい空白タイルには商品ID・価格・入力を与えず、会計や商品数に数えません。押しやすい寸法、文字が折り返す場合、タブレット回転時にも操作を隠さないことが確認点です。端末差を吸収する処理は、会計の計算処理とは分けてあります。

### システム概要の読み方

Web版は5章で基本情報を紹介し、詳しい解説は本書へ分けています。現在の章名を押して目次を開き、章へ移動できます。タッチ端末の条件を満たす画面では、スクロール中にスクラバーが表示され、ドラッグやキーで章を選べます。章数は文書から読み取ります。

JavaScriptが無効でも本文と通常の目次リンクを読めます。JavaScriptは基本情報を隠すためではなく、現在地表示・目次操作・章移動を補助します。動きを減らす設定、見出しへのフォーカス、文字拡大時の見出し位置も確認対象です。

### PWAの役割と制限

PWAはホーム画面への追加やキャッシュによる表示を助けます。通常PWAとレジのオフライン専用資産・保存領域は分けて考えます。インストール済みでも通信・認証・期限・公開条件が不要になるわけではありません。本書はmanifest・Service Worker・IndexedDB形式を変更しません。端末の更新やログアウトを含む実機検証は、ブラウザ自動試験とは別に必要です。

## 第6章｜品質管理・テスト

### 何を壊さないためのテストか

テスト件数の多さだけでは、事故を検知する力は分かりません。「他GuestのIDを送る」「保存の途中でDBエラー」「同じ会計を同時に送る」など、失敗しやすい条件と守る事実を結びます。修正が入ったら、その不具合が再び起きたときに失敗する検証を残します。

| 層 | 主な目的 | 例 |
| --- | --- | --- |
| pytest / SQLite | 入力・権限・状態・失敗経路を素早く調べる | Dataset越境、期限、rollback |
| NodeのJavaScript試験 | 計算や端末状態を小さく検証 | 会計、数量、再送queue、天気書式 |
| 隔離PostgreSQL | DB固有の制約・ロック・競合を調べる | 複合FK、同時会計、migration |
| Chromium E2E | 実際のDOMと操作のつながりを確認 | スマホ表示、章移動、入力と保存 |
| 実機・運用確認 | 端末・設定・公開段階の条件を確認 | PWA更新、全端末の未同期照合 |

conftest.pyはテスト専用のメモリ内SQLiteを設定し、fixtureで表を準備・片付けます。本番DBにデータがあることを前提にしません。authenticated_client等で認証された主体を用意し、Admin・Guest・匿名それぞれの応答を調べます。時間依存はJSTの営業日とUTCの期限を取り違えないようにします。

### システム概要も境界を守る

/system-overviewは公開のGETページです。匿名閲覧で業務DBやAIを呼ばないこと、POSTを許可しないこと、秘密の商品名や任意query値を出さないこと、no-storeやフレーム防御ヘッダーを確認します。説明ページだからといって認可の検証を外しません。

今回の5章化では旧クイズ数・旧9章の期待値を仕様に合わせて変えます。一方、現在地更新、目次の開閉、スクラバードラッグ、キーボード・縮小動作設定・JS無効の基本閲覧は維持すべき保証です。PDFも単に存在するだけでなく、テキストが読めることとレンダリングを確認します。

### 検証結果と公開判断を分ける

mainに記録された過去のCI合格と、この資料更新で実際に実行した試験は別の証拠です。今回のコマンド・結果・未検証範囲はdocs/system-guide/README.mdに記録します。PostgreSQLや実機を今回実行していなければ、実行済みとして扱いません。

## 第7章｜公開・運用・復旧

### 通常起動に保守操作を混ぜない

DockerfileはPython 3.12のマルチステージ構成で依存を組み込みます。通常起動はstartup_guard.pyがrevision・必要table/columnを読み取り確認した後にGunicornを起動します。起動のたびにcreate_all・migration・seedを走らせる設計ではありません。

Renderは記録上、自動デプロイOFFで手動公開を判断します。GitHub mainへ入ったことと本番に反映されたことは違います。公開前には対象コミット、起動設定、DB revision、非秘密の公開フラグ、バックアップと承認を照合します。今回の資料変更にmigrationは不要です。

### migrationはデータを守る変更履歴

Alembicのrevisionとdown_revisionが構造変更の順序を表します。モデルファイルを書き換えただけで本番の表が自動で一致するわけではありません。既存の表・外部キー・制約・indexとデータ保全を調べてから変更します。資料制作のためにschemaやrevisionを変える必要はありません。

### バックアップは復元確認までを区別する

production_backup_guard.pyと関連手順は接続先の明示確認、暗号化バックアップ、暗号文のSHA-256照合、復号・アーカイブ確認、独立したDBへの復元を扱います。SHA一致はファイルが同じことの確認で、DBが復旧できる証明ではありません。

隔離復元では本番と異なる空の接続先を用意し、復元後にrevision、表・件数、Dataset参照不整合、外部キー孤児、未検証制約、無効indexを確認します。本番の後刻の件数と一致するだけで、取得時点の全データ完全一致とは断定しません。同じsnapshotによる比較がなければ検証の限界を記録します。

READMEにある11月2日のG580検証は利用者報告です。本書制作中に本番backupや実復元を実行したわけではありません。実DB・鍵・パスワード・実データを教材へ添付しません。

### 商品CSVと全量DBバックアップは用途が違う

data_vault.pyは商品・写真・レシピ・材料等の設定をCSVとManifestへ保存する独立CLIです。空のAdmin Datasetへの復元を前提に、既定dry-run、明示確認後の書き込み、途中失敗時のrollback、保存後の読み戻しを行います。売上・会計・返金・認証・未同期会計は対象外です。DB全量バックアップの代替にはなりません。

### 公開を止める条件

認可越境、二重会計、集計不整合、cleanupの誤削除、起動や認証の障害があれば先へ進みません。Guest公開とAdmin正式レジ切替を独立して判断します。Guestの公開フラグを変えるためにAdminゲートを解除しません。

旧版へ戻すだけでも、新版で追加されたGuestの仕込み等とcleanupの互換性に問題が出る可能性があります。旧コードへの切り戻しは合成データで確認し、DB自動復元や未同期queueの削除で解決しません。本番変更・merge・deployは別承認です。具体的な操作は既存runbookを参照してください。

## 第8章｜設計の改善と今後の課題

### 主な設計変更の理由

月次商品と日次数量の方式から、継続商品・会計本体・明細・価格履歴を持つレジへ設計を広げています。これにより会計時点の価格や返金を扱えます。一方、旧方式と新方式を同日に足すと二重計上になるため、切替情報と旧売上の競合確認が必要になりました。

GuestはAdminに似た流れで商品から会計・分析へ進む設計へ変わりました。ただし、画面を共通にすることはデータ領域や公開条件の共通化ではありません。Dataset分離・期限・容量・AI回数を保ちながら体験を整えます。

システム概要も、長い技術教材と第三者向け紹介が同じ場所にあった状態を見直しました。紹介を5章に整理し、実装を読みたい人向けには本書とコードの住所を残します。理解度8問・Python基礎15問のクイズは撤廃し、解答欄も設けません。

### 残された課題

v3.2 Guestは本番未公開です。公開条件・実機確認・承認が揃って初めて公開判断ができます。Admin正式レジはNO-GOを維持します。Guest廃棄ロスと仕込みの販売実績参考連携、ログアウト時の未同期会計案内、実機PWA更新等は残課題です。厨房とレジのリアルタイム在庫連携等の将来案を、現在の完成機能として記載しません。

### コードと資料の読み順

まずREADME.mdの公開状態を確認し、models.pyで記録の関係を読み、対象ルートとサービス、その失敗を調べるテストへ進みます。行番号は更新でずれるため、ファイル名と関数・クラス名を住所にします。基準版はGitHubの以下のコミットから辿れます。

https://github.com/tosane932/tosane-works/tree/6f61e12c5927d31a0be31a5a1e2f89a15b36b859/store/bakery-hub

関連資料：docs/v3-register-design.md、docs/v3-register-cutover-runbook.md、docs/v3-register-device-test-sheet.md、docs/v3-register-price-approval.md、docs/v3.2-guest-launch-readiness.md、docs/startup-maintenance.md、docs/data-vault-guide.md、docs/g580-restore-verification.md。

本書の生成方法と今回の検証結果：docs/system-guide/README.md。次の付録は旧システム概要から移した実装読解の参照資料です。機能の説明を補う住所として使い、第1章の公開状態と合わせて読んでください。
