# Bakery Hub v3.0 レジ会計設計・影響調査（Phase 0）

調査基準: `tosane932/tosane-works` の `main`、`923734b5627b9c13ee919f1c67c0f7185e141284`（PR #106 マージ済み）。本書は実装前の提案であり、DBスキーマ、既存画面、実データを変更しない。

## 1. 目的と確定仕様

判断基準は「誰でも使いやすいシステム。裏側ではしっかり仕事をするシステム」。スマートフォンと横向きタブレットで、説明なしに商品写真を選び、間違いを訂正し、会計できることを目指す。保存、認可、金額検証、競合防止はサーバーとDBが担う。

| 項目 | 確定した方向 |
| --- | --- |
| 入力単位 | 従来の商品・日付別の一括入力を撤廃し、お客さん1会計ずつ記録。サイドメニューは「レジ会計」から専用画面へ直行。Admin/Guest共通。店長・販売員のモード選択なし。 |
| 会計操作 | 商品写真をタッチして＋1、商品・数量・合計を確認、お預かりを入力、お釣りを表示し、明示的に「会計完了」を押す。DB保存成功後に次の会計へ進む。レジ締め・営業終了ボタンなし。 |
| 商品 | 標準18種類をすべて表示し、独自商品を追加。標準品を使うための毎月の登録作業は不要。画像のない独自商品も名前・価格の大きなタイルとして使える。 |
| 数量 | タイルのタッチで＋1、左側の明確な−で減算。数量を大きく表示。1商品・1会計につき −50〜＋50 個。直接数量入力も用意。0から負へ移る際には返金確認。 |
| 返金 | 専用ページと元顧客への紐付けなし。負の会計を保存し、通常販売とDBで識別。返金も会計件数および平均会計額の分母・分子に含む。在庫復元なし。例の660＋720−220＋840＝2,000円、4件、平均500円。 |
| 戻る | 体験版レジ左上に「← 戻る」。入力中は「入力中の会計があります。保存せずに戻りますか？」と「会計を続ける／破棄して戻る」。未入力は確認不要。「※ 実店舗でレジ専用運用する際は、『戻る』ボタンは表示されません。」と案内する予定。 |
| 商品管理 | 商品登録をレジ利用の前提にしない。独自商品の新規登録は名前・価格を先に扱い、Admin/Guestとも画像アップロード必須にしない。後から商品一覧の編集・テンプレート画像選択を可能にする。 |
| 対象外 | 電子決済、レシート印刷、売切表示、在庫管理、レジ締め、実店舗用販売員ログインの実装。 |

### UIの骨格

- タブレット横向きは左側にスクロール可能な商品タイル群、右側に常時見える会計明細・合計・お預かり／お釣り・会計完了。スマホはタイルと明細を読みやすい縦構成にする。18品を**表示対象**とし、狭い画面に全品を無理に同時表示しない。
- タイルは画像の有無にかかわらず、名前、単価、大きな選択数量、＋のタッチ領域と独立した−ボタンを持つ。−の誤タッチを避ける配置・押下領域、色以外の符号と「返金」表示を用いる。
- 金額欄は「合計」「お預かり」「お釣り」を別ラベルで表示。返金時の金銭授受表示は後述の未決定事項。操作途中の戻る・画面遷移だけを必要最小限で確認し、会計完了は必ず店員の明示操作とする。
- 保存中は会計完了を無効化して状態を表示。ただしこれは補助策で、DBで同時POSTを防ぐ。成功応答を受けたときだけ新しい会計キーで次の入力へ進む。失敗時は入力を保持して再試行可能にする。

## 2. 現行実装の調査結果

| 現行の場所 | 確認した事実・v3への影響 |
| --- | --- |
| `models.py` の `Product`（436行付近） | `dataset_id`（NOT NULL）、`year`、`month`、`name`、`price`、任意の`image_key`、`is_active`。月次行で、価格・名前は編集可能。月をまたいだ常設の販売商品マスタではない。DB上の価格範囲・月次一意性の制約はモデルにない。 |
| `models.py` の `DailySales`（531行付近） | `product_id`、`date`、`quantity`。`(product_id, date)` 一意。会計ID、日時、お預かり、返金区分、販売時単価、直接のDataset列はない。Dataset境界はProductへの結合から得る。 |
| `app.py` の `index()`（1406行付近） | `/` で年月別の商品を登録・更新。送信されない同月の既存商品を論理無効化する。Guestは1回30行、Dataset生涯で新規30商品。商品名1〜100字、価格0〜9,990円／10円単位（`PRODUCT_PRICE_MAX`等）。画像キー・Guest画像と名前の組合せを検証。後からの価格編集は過去の`DailySales`の金額表示に影響する。 |
| `product_images.py` | `PRODUCT_IMAGE_OPTIONS`は18品の画像・名称・初期価格・読み・Guest許可を持つ**ファイル上のテンプレート**。画像が存在する候補だけを返す。標準18品が各Datasetの`Product`として自動保存される構造ではない。独自商品向け承認カタログも読み込む。 |
| `app.py` の `input_sales()`（2059行付近） | `/input` は当月・有効商品のみ選択し、日付一致・Dataset一致を検証。`DailySales`に`(product_id,date)`のUPSERTで数量を**置換**し、一括commit／失敗時rollback。Guestの1回30行、数量0〜1,000の検証はレジの±50とは異なる。 |
| `app.py` の `product_catalog()`（1793行付近） | `/products` は当月の有効商品と、名前で重複排除した過去月の最新行を表示。画像なし表示はあるが、商品一覧からの編集操作はない。名前だけでの同一視は改名・同名別商品に弱い。 |
| `app.py` のダッシュボード | `dashboard()`と`api_dashboard_data()`は`_get_sales_from_db()`で`DailySales`の数量を商品名単位に合計。`_get_today_sales_amount()`と`home()`は`Product.price * DailySales.quantity`。`_has_today_sales_record()`は0円記録と未入力を区別。年月候補も`DailySales`から取得。`api_ai_advice()`も同じ数量ランキングを使用。 |
| `sales_period_summary.py` | 日・週・月・年の期間境界、前期間比較、日別推移と商品別ランキングを計算。`DailySales`を`Product`に結び、現在の`Product.price * quantity`で金額を計算。商品名で集約し、既存データは会計件数を復元できない。 |
| `app.py` のGuest境界 | `load_user()`はGuest期限を検査し、`require_current_dataset()`は認証主体からDatasetを決定する。フォームのDataset値を信じない。`admin_or_guest_required`と`admin_required`を使い分ける。`CSRFProtect(app)`を使用。 |
| Guest cleanup | `_cleanup_expired_guest_datasets()`は期限切れGuestを再確認してロックし、現在は`DailySales → Product → Dataset`を明示的に削除。新しい会計明細・会計・商品マスタも順序を守って対象Guestだけ消す必要がある。 |
| 仕込み・発注 | `production_prep.py`の仕込みはAdmin専用。`PrepRecipe.product_key`は`template:<key>`または`product:<旧Product ID>`であり、月次Productから独立。`PrepPlan`は営業日別の製造予定数、`required_ingredients()`／`combined_ingredients()`はレシピの基準数量から材料試算。`material_orders.py`はAdmin/Guest共通のDataset分離された発注メモ。 |
| カタログ投稿 | `submit_product_catalog()`（Admin専用）から`catalog_repository.py`の検証・GitHub PR公開へ進み、承認・デプロイ後の画像と商品候補が`product_catalog/admin_products.json`に載る。これは店舗内の「商品名・価格の登録」より重い、画像素材の公開経路。勝手に削除しない。 |
| migration | 初期テーブル`b7e2c4a91f30`、日次一意制約`9d3c1b7e5a42`、Dataset導入`c7a1d9e4f2b6`、Product Dataset NOT NULL`f2b6c8d4e1a9`、画像キー`c4e8a1d7b2f6`、仕込み`e2a7c4d9b105`を確認。リポジトリ内の最新の単一headは`e2a7c4d9b105`。次のmigrationはこの`down_revision`を前提に、着手時に再確認する。 |

`DailySales`への既存テストは`tests/test_sales.py`、集計は`test_dashboard.py`・`test_sales_period_summary.py`、Guest分離・期限切れは`test_dataset_authorization.py`・`test_guest_cleanup.py`、PostgreSQL経路は`test_sales_postgresql.py`・`test_guest_cleanup_postgresql.py`、仕込みは`test_production_prep.py`・`test_prep_migration.py`等にある。旧仕様を守る間はこれらを削除・弱体化しない。

## 3. 推奨するDB構成

**推奨: 会計ヘッダー＋会計明細を新設し、`DailySales`は旧実績として読み取り専用で残す。** 新しい会計を`DailySales`へ複写しない。日次合算が必要なら、後述の共通集計層で二系統を一度ずつ足す。集計キャッシュを追加するのは性能測定後とする。

| 方式 | 利点 | 問題／判断 |
| --- | --- | --- |
| 会計テーブル＋明細 | 1件の境界、返金、時点単価、再送防止を表せる | 採用。新規集計の追加は必要。 |
| 会計を保存し`DailySales`にも加算 | 既存グラフを一部流用できる | 会計と日次行が二重の正本になり、再試行・返品・再集計で不一致や二重計上。採用しない。 |
| 旧`DailySales`を残し新会計と統合集計 | 既存行を無改変で守れる | 採用。Dataset・営業日の排他的な切替を必須とする。 |

提案する新テーブルと主要制約（名前は実装Phaseで確定する）。金額は円の整数、時刻はUTC aware、営業日はAsia/Tokyoの日付とし、ブラウザの時刻・価格・合計を正本としない。

| テーブル | カラムと制約案 |
| --- | --- |
| `register_products` | `id` PK、`dataset_id` NOT NULL FK `datasets.id`、`template_key` NULL（標準18品なら安定したキー）、`name` NOT NULL、`price_yen` NOT NULL、`image_key` NULL、`is_active` NOT NULL、表示順、作成・更新日時。`UNIQUE(id,dataset_id)`、`UNIQUE(dataset_id,template_key)`（NULLの独自品は重複可能）、価格・名前・キー長のCHECK、Datasetと有効状態の索引。Phase 1Aでは表と制約のみ作り、標準18品の生成・整備はPhase 1BでDatasetごとに冪等に行う。毎月再生成しない。Guest上限は標準18品をどう数えるか別途決める。 |
| `register_cutovers` | `dataset_id` PK/FK、`first_register_date` NOT NULL。Datasetごとに営業日境界を一度確定し、不用意に更新・前倒ししない。Adminは切替前日までを旧実績、Guestの新規Datasetはレジ開始日から新実績。切替前・当日の旧行有無を監査する。 |
| `register_transactions` | `id` UUID PK、`dataset_id` NOT NULL FK、`submission_key` UUID NOT NULL、`request_digest` NOT NULL、`completed_at` NOT NULL、`business_date` NOT NULL、`kind` (`sale`/`refund`)、`total_yen`（返金時は負）、`received_yen` NULL可、`change_yen` NULL可。`UNIQUE(id,dataset_id)`、`UNIQUE(dataset_id,submission_key)`、`INDEX(dataset_id,business_date,completed_at)`、kindと符号・金銭授受の整合CHECK。商品明細の合計一致はCHECKのみでは表現できず、アプリの同一transactionで検証する。 |
| `register_transaction_lines` | `id` PK、`transaction_id` UUID、`dataset_id`、`register_product_id`、`product_name_snapshot`、`unit_price_yen`、`quantity`、`line_total_yen`。`FK(transaction_id,dataset_id) → register_transactions(id,dataset_id)`、`FK(register_product_id,dataset_id) → register_products(id,dataset_id)`、`UNIQUE(transaction_id,register_product_id)`、数量−50〜−1または1〜50、単価範囲と`line_total_yen = unit_price_yen * quantity`のCHECK、索引 `(dataset_id,register_product_id)`。数量0の行は保存しない。名称・単価は会計時点で固定し、商品の改名・値上げ・論理無効化が過去実績を変えない。 |

### Phase 1Aのmigration順序とデータ保全

現行のAlembicは直列のrevisionで、調査時の単一headは`e2a7c4d9b105`。同revisionの`prep_ingredients`も`(recipe_id,dataset_id)`の複合FKを親テーブル作成後に定義している。Phase 1Aの4表は互いにFKで依存するため、**同一の追加migrationにまとめるのが合理的**。次のmigrationの`down_revision`は着手時のheadを再確認して指定し、`models.py`の定義と手書きDDLを照合する。DBが本番でどのrevisionかは未確認であり、適用は本Phase 0.1の対象外。

1. `register_products`（親は既存`datasets`）を作り、PKの`id`に加えて複合FKの参照先となる`UNIQUE(id,dataset_id)`、`UNIQUE(dataset_id,template_key)`、名前の空白除外・価格の非負と安全な整数範囲・有効状態のCHECKを定義する（販売上限値と10円刻みをDBで固定するかはPhase 1A着手前に確定）。
2. `register_cutovers`（`dataset_id`がPKかつ`datasets.id`へのFK）を作る。**Phase 1Aでは切替行を挿入しない**。初回切替日と旧売上行の有無を運用前に確認する。DBのCHECKだけで旧売上・新会計の日付重複は防げないため、保存経路と旧POSTの遮断も必要。
3. `register_transactions`を作り、`dataset_id`を`datasets.id`に結ぶ。`register_cutovers`へのFKは張らず、切替行の存在・`business_date >= first_register_date`は保存サービスで同一transaction内に検証する。`UNIQUE(id,dataset_id)`と`UNIQUE(dataset_id,submission_key)`を定義し、販売／返金の区分、合計の正負、預かり・お釣りの組合せをCHECKで制限する。切替日以降にしか新会計を記録できないことはCHECKやDataset FKだけでは保証できず、Phase 2Aの保存テストで確認する。
4. `register_transaction_lines`を作り、`(transaction_id,dataset_id)`から会計へ、`(register_product_id,dataset_id)`から商品へ複合FKで結ぶ。全FK列をNOT NULLとし、`UNIQUE(transaction_id,register_product_id)`、`quantity BETWEEN -50 AND 50 AND quantity <> 0`、単価の非負・安全な整数範囲と`line_total_yen = unit_price_yen * quantity`のCHECKを置く。同じ会計内で販売と返金を混在させない推奨案はアプリの明細検証でも守る。

明細の`dataset_id`は、会計と商品が**同じDataset**に属することをDBで保証するために必要。FK参照先の複合UNIQUEと子のNOT NULLが揃わなければこの保証は成立しない。各表のDataset FK・会計明細のFKは`RESTRICT`相当を基本とし、Admin商品と会計履歴の物理削除を防ぐ。Guest期限切れ時だけ、認可済み対象Guestの明細→会計→レジ商品→切替行→旧売上→旧商品→Datasetを明示的に消す。独立したGuestへの影響と削除途中の失敗時rollbackを検証する。FKの多経路cascade任せにはしない。

Phase 1AのDDL確定前に未決定の0円会計・価格上限を確認し、採用するCHECKとサーバー検証の境界をテストで固定する。追加migrationの`upgrade`は空の4表・制約・索引のみ作成し、**既存の`products`、`daily_sales`、`datasets`の構造・行に触れない**。`downgrade`は明細→会計→切替→商品という逆順で表を落とすが、4表のいずれかに行があればデータ消失を防ぐため明示的に停止する。Phase 1B以降にデータが入った環境では通常のdowngradeを安全な復旧手段と見なさない。SQLiteでFK有効（`PRAGMA foreign_keys=ON`）の接続、PostgreSQLでFK・CHECK・一意制約の適用、両DBでupgrade→空表のみdowngrade、行のあるdowngrade拒否をテストする。価格上限など未決定の仕様値をmigrationで先走って固定しない。

### 保存と二重登録の防止

1. 画面は**会計を開始するとき**に予測不能な`submission_key`を発行し、失敗／通信切断後の再送でも同じキーを保持する。次の会計には新キーを発行する。CSRFトークンとは別物。
2. POST時にログイン主体のDatasetを取得。Guestは**同じDB transaction内**でDataset行をロックし、期限と利用上限を再確認する。キーと生の入力から正規化した商品ID／数量／預かり／返金確認等のダイジェストを求め、**先に既存キーを照会**する。同一入力の再送は、初回保存後に商品価格・有効状態が変わっていても元の会計を返す。異なる入力なら409。新規キーだけ、入力の商品IDをそのDatasetの有効なレジ商品から引き直し、重複ID・0・範囲外・行数超過・巨大ボディを拒否する。単価・明細金額・合計・お釣りはサーバーで再計算。クライアント送信金額を信用しない。
3. ヘッダーにダイジェストを記録し、全明細とともに**一回のDB transaction**で保存。`UNIQUE(dataset_id,submission_key)`が二重タップ・並行POSTの最終防壁となる。ダイジェストにはサーバーが後から変え得る商品価格を含めない。
4. 同じキーの再送でダイジェストも同一なら、既存会計を返して「保存済み」を確認可能にする。同じキーで異なる内容は409として新規保存しない。ユニーク競合後は必ずrollbackして別transactionで既存行を読み直す。通信切断時も保存状態の照会または同一キーの安全な再送で解決し、新キーを自動生成して重複会計にしない。
5. DB例外はrollbackし、入力内容を保持。保存成功の確証が得られるまで「次の会計」に進まない。PostgreSQLでは行ロック／一意制約、SQLiteでは一意制約に加え書込直列化・busy時の失敗処理を検証する。

UIの「0から負へ」の確認を飛ばす直接POSTにも備え、負数の会計にはサーバーが検証できる明示的な返金確認値を要求する。確認値はユーザー意思の再確認であり、認可・金額検証の代わりではない。

## 4. 切替・互換性と集計

**最大の危険は旧日次行と新会計を同じ営業日について両方計上すること。** Datasetごとに`first_register_date`を一度記録し、`DailySales.date < first_register_date`と`register_transactions.business_date >= first_register_date`を排他的なデータ源にする。切替日は原則として翌営業日など**旧行が一件もない日**を選ぶ。既に当日の旧行があれば翌日へ延期する。既存の有効Guestが旧売上を持つ場合も同じ規則を適用するか、期限満了後の新規Guestだけを先に切り替える。過去にさかのぼる会計POSTや、切替日以降の旧`/input`へのPOSTをサーバーで遮断する。旧画面の表示停止だけでは不十分。デプロイ中の旧プロセスからの書込も想定し、切替前に実行中ワーカー・ルート・日付条件を運用確認する。段階的リリース時の二重書込を許さない。

既存`Product`と`DailySales`は移動・上書き・削除せず、旧日付の参照を続ける。独自品は同じDataset内の旧月次行から候補を提示し、店長が採用する行と現行価格を確認して新マスタへ**コピー**する。名前だけを使った自動統合や、旧Product IDの再利用はしない。標準18品はテンプレートの安定キーで一度だけ生成し、同名の旧行がある場合は候補・差額を表示して確認する。Guestは新しいDatasetで18品を利用できるようにし、標準品で既存の「生涯30商品」枠を消費しない専用の独自品枠を検討する。旧登録とレジ登録が並行する間の枠の定義は実装前に固定する。

移行前に、Dataset別・日付別の旧行数／数量／算出金額、ProductのNULL・同名異価格・画像キー・無効商品・当日旧行、既存の仕込み`product:<id>`を読み取り専用で棚卸しする。旧データには販売当時の単価がなく、現在の`Product.price`で計算した**旧仕様上の推計金額**しか復元できない。過去売上の価格不変性を遡及して保証したと表現しない。月次行の更新経路を止めた後は過去行の価格変更を禁止し、必要なら別の修正履歴を設計する。migrationでは導入前後の件数・識別子・合計とFK整合性を検証し、破壊的なdowngradeを許さない設計とする。

共通の売上読み取り層で、旧行（切替前）と新明細（切替日以降）を**各系統でDataset・営業日を絞って集約してから**足す。日別金額、商品別販売数（返金は負）、商品別金額、年月候補、ランキング、AI入力、ホームの今日の金額・「記録あり」、期間別APIをこの層へ順に切り替える。商品集計は安定商品IDを内部で使い、旧商品と新商品を同じ名前だからと機械的に結合しない。表示名が同じ別商品、改名、同日の価格違いをテストする。数量ランキングと売上金額ランキングは意味を分け、返金で負になることを表示する。

- 本日の差引売上＝対象営業日の旧金額（切替前のみ）＋新会計の`total_yen`（切替後のみ）。商品別数・金額も同じ排他条件。会計件数は新会計ヘッダーの件数（返金を含む）。平均＝**新会計の差引金額合計 ÷ 新会計件数**。表示用の丸め方は実装時に固定する。
- 旧データだけの日は会計件数も平均も「記録なし／算出不可」と表示し、0件・0円と混同しない。旧新が混在する期間では、**全期間売上**と、明示した**レジ導入後のみの会計件数・平均**を別ラベルで示す。全期間売上を新会計件数で割らない。
- 前日比と先週同曜日比は、各営業日の差引売上を同じ排他規則で比較。比較対象に記録がない場合と0円を区別し、0円を分母にした変化率は表示しない。`sales_period_summary.py`の週の起点・進行期間の比較日数・うるう年処理は維持する。
- Guestが会計後に`/dashboard`へ戻れば自分のDatasetのみが見える。AI分析も同じ正本の集計を使い、GuestのAI回数制限を維持する。返金を含む数量・負数をプロンプトへ渡す形式は別途テストする。

仕込みとの最小連携は、販売個数を**参考値**として仕込み画面へDataset・商品キー・営業日付きで渡す読み取り導線。`PrepRecipe`と`PrepPlan`を勝手に更新せず、必要なら店長が予定数に反映する。返品・廃棄・製造ロスを含まないため販売数を実際の材料消費量や在庫変化とは呼ばない。`template:<key>`は新マスタの標準キーへ対応付けられるが、`product:<旧ID>`は移行時に対応を確認し、レシピの参照先を自動で変更しない。材料発注メモへの既存導線は維持する。GuestにAdmin専用の仕込み画面を公開しない。

## 5. Dataset・権限・画像の境界

- レジ閲覧・POSTは`admin_or_guest_required`と`require_current_dataset()`を使い、Dataset IDをフォーム・URLで選ばせない。商品・会計・明細・集計のすべてをDatasetで絞る。複合FKで他Datasetの商品を他会計に挿せないようにする。Guest A/BとAdminの相互参照・更新・削除をテストする。
- Guestの会計件数・明細数・1POST行数・1商品数量・預かり金額／合計、独自商品数、リクエスト容量に上限を置く。具体値はアクセス負荷を見て決定し、並行POSTでも上限を越えないようDatasetロックとDB制約を組み合わせる。期限切れ判定は書込ロック取得後にも再確認し、cleanupは対象Guestの明細→会計→新商品→旧売上→旧商品→Datasetを削除。管理者と有効Guestは対象にしない。cleanupと会計POSTの競合をPostgreSQLでも検証する。
- `CSRFProtect(app)`を新しいフォーム／JSON POSTにも適用し、XSSはテンプレートの自動エスケープを維持。サーバーが価格、数量、商品の有効性、返金区分、合計、お釣りを再検証する。返金承認や直接数量入力はJSだけで成立させない。
- 将来の実店舗販売員は、独立した認証主体と`register:read/write`権限に限定する。レジ専用のendpointをサーバー側の許可リストで検査し、管理URL、API、静的なデータ公開経路を含めて認可する。各リクエストのDatasetも認証主体に固定する。単なる「戻る」非表示や店長／販売員UIモードでは保護できない。今回のAdmin/Guest共通体験にはこの実店舗ログインを混ぜない。
- 画像のある標準品は既存の安全な画像キー解決を利用する。独自品は名前・価格のみで保存でき、画像未設定なら文字タイル。商品一覧に編集ボタンと承認済みテンプレートギャラリーを段階追加する。未実装の「画像を追加」はdisabledにして体験版では未対応と明記し、POST先や疑似アップロードは作らない。Adminの画像カタログ投稿→PR→承認→デプロイの経路は独立機能として維持し、店舗内の商品編集と区別する。

## 6. 未決定事項と複雑さを抑えた推奨案

以下は**提案**であり、ユーザーが確定した仕様として実装しない。Phase 1〜3の着手前に関連項目を確定する。

| 未決定 | 推奨案・確定が必要な理由 |
| --- | --- |
| 1. 月次商品から継続マスタへ | 新`register_products`を並設し、標準18をDatasetごと一度だけ生成。旧独自商品は候補提示後に明示的にコピー。既存行に常設用の年月ダミーを入れない。同名別商品・異なる旧価格をどう選ぶか確認する。 |
| 2. 価格上限 | まず現行の0〜9,990円／10円単位を維持し、0円商品の販売可否と上限緩和は用途・テストを確認して別PR。`±50`は数量制約であって単価制約ではない。預かりと会計合計には独立した安全な上限を設ける。 |
| 3. プラス・マイナス明細の混在 | 1会計では販売か返金のどちらか一方に限定し、混在操作時は分けて会計する案。差引0や現金授受の曖昧さを防ぐ。同じ画面を使い、返金専用ページは設けない。 |
| 4. 合計0円 | 数量0の空会計は保存不可。商品単価0で全体0円になる会計は当面保存不可を推奨（無料提供の履歴要件は別途決定）。販売と返金の相殺0円も3の方針で不可。DB CHECKとAPI両方で確認。 |
| 5. 旧新の統合 | Dataset別の営業日切替を正本にして、旧日と新日を排他的にUNION相当で集計。旧期間の件数・平均を欠損として明示。切替日の選定・既存旧行の有無を承認してから本番移行する。 |
| 6. 直接数量入力 | 選択中の商品数量をタップするとテンキー／数値入力で−50〜50を確定する。空・小数・符号だけ・範囲外を拒否。0→負はタイル−と同じ返金確認。 |
| 7. 実店舗の販売員認証 | 別主体・レジだけのサーバー認可を将来別Phaseで追加。端末共有時のセッション失効・ロックもその設計で扱う。今はAdmin/Guestのみ。 |
| 8. 画像カタログ投稿 | 既存Admin PR投稿経路は維持。新商品の登録は画像なしで即時可能にし、既存承認画像の選択と画像追加申請を分ける。廃止判断は利用実態を確認してから。 |
| 9. 画像なしが増えた場合 | 画像あり／なしで操作は統一し、文字タイルにも同等のサイズと数量表示。商品名検索・カテゴリ／並び順を必要時に追加し、初回から設定画面を増やさない。 |

返金の現金欄は、負の合計時に「返金額」を表示し、お預かり／お釣り欄は適用しない案を推奨する（返金専用ページは不要）。販売の預かりは合計以上の整数、お釣りは預かり−合計。返金の支払済み額を実際に確認する操作と表示文言はUI設計で確定する。

## 7. 変更候補と小さなPRの順序

| Phase / PR | 主な変更候補 | 完了条件と狙うpytest |
| --- | --- | --- |
| **0〜0.1 本PR** | `docs/v3-register-design.md`のみ | 現行コード・migration・既存テストとの照合、4表の作成順とPhase 1Bとの依存解消、`git diff --check`。Python変更なしのためpytestは実行しない。 |
| **1A 4表のDBスキーマ・制約** | `models.py`、4表を正しい順で作る単一の新migration、`tests/test_migrations.py`・PostgreSQL版と新しいDB制約テスト | `register_products`→`register_cutovers`→`register_transactions`→`register_transaction_lines`。既存表・既存行無改変、複合FK・Dataset境界・NOT NULL・CHECK・UNIQUE、空表のupgrade/downgrade、行があるdowngrade拒否をSQLite/PostgreSQLで確認。標準18品・切替行は投入しない。 |
| **1B 商品マスタ運用** | `product_images.py`、Phase 1Aの`register_products`を使うサービス、商品移行・Dataset分離テスト。**追加migrationは原則不要**（新たな列・制約が必要と判明した場合だけ別途根拠を示す） | Datasetごとに標準18品を冪等に用意。名前・価格・画像キーを取得し、Admin/Guestを分離。独自品は画像なし・名前／価格だけで登録可能。旧月次行は同名だけで統合せず確認後コピー。月替わり後も登録を維持。Guest枠、旧Product/仕込みキー不変を確認。旧商品登録UIはこの段階では維持。 |
| **2A 会計保存サービス** | 新`register_service.py`、保存・再送・返金・Guestのテスト | サーバー再計算、±50、二重キー競合、別Dataset商品、価格改定のスナップショット、rollback、期限切れ、件数上限、同時POSTをSQLiteとPostgreSQLで確認。ルート公開前に保存処理を固める。 |
| **2B レジ画面・接続** | 新レジ用template/JS/CSS、`app.py`のGET/POST、`_app_navigation.html` | 18品と独自品、画像なし、＋／−、確認、直接入力、預かり／お釣り、二重タップ・失敗時保持・成功時次会計、戻る確認、Admin/GuestのCSRFと認可。タブレット横向き・スマホ実機確認。旧`/input`はまだ互換保持。 |
| **3A 排他的な集計層** | 新売上集計サービス、`sales_period_summary.py`、集計・境界テスト | Datasetと営業日の排他条件、旧新・返金・0円と未記録の区別、前日／先週同曜日、月年境界、件数・平均の適用範囲、旧Product価格の既知の限界を確認。既存ダッシュボードを変更する前に契約テストを揃える。 |
| **3B ダッシュボード反映** | `app.py`の`dashboard()`、`api_dashboard_data()`、`api_sales_period_summary()`、`api_ai_advice()`、`home()`と補助関数、`dashboard.html`・`store_home.html` | 金額・数量ランキング、月年候補、AI入力、Guest結果、会計件数・平均・比較表示を共通層へ切替。旧日の既存表示と旧集計テストを維持。 |
| **4 商品管理UI・仕込み導線** | `app.py`の`index()`・`product_catalog()`、`index.html`・`products.html`、`production_prep.py`、画像関連テスト | Phase 1Bの独自品登録処理を商品画面から操作可能にする。レジ利用に月次登録不要。後から画像選択、disabledの未実装ボタン、Admin画像PR経路、仕込み参考値の手動反映とAdmin認可を確認。旧`/input`の履歴参照と旧Product保全。 |
| **5 切替・最終UX** | `app.py`の`input_sales()`廃止／POST遮断、ナビ・`input.html`導線整理、`system_overview.html`、README、E2E・migration検証 | Datasetごとの切替日設定と旧行ゼロの事前検査、旧POST遮断、デプロイ境界、二重計上ゼロ、Guest cleanup競合、スマホ／タブレット実機、システム概要の実装との一致。対象テスト後、変更影響が広いため最終Gateで全pytest。 |

順序は「データを安全に保存できる → UIを繋ぐ → 旧新を排他的に読む → 旧経路を止める」。Phase 2Bのルートはテスト環境では検証できても、集計と切替が完成するまで本番のAdmin/Guestには公開しない。切替未設定Datasetでは旧実績だけを読み、レジ保存も拒否する。Phase 3A/3Bと切替は機能フラグまたは明確なDataset切替状態で段階導入し、旧新を同時に正本としない。各PRで`AGENTS.md`に沿って差分確認、対象テスト、必要なPostgreSQLテスト、`git diff --check`を実施する。実店舗用の認証と画像アップロード本体はこのロードマップの対象外で、必要になった時点で別設計・PRとする。

## 8. 主な故障と防止策

| 故障 | 防止策・検証の焦点 |
| --- | --- |
| 旧新二重計上／当日途中切替 | Dataset別の営業日境界、当日旧行ゼロ検査、旧POST遮断、比較日・月跨ぎの契約テスト。 |
| 二重タップ・通信切断・並行保存 | Dataset内一意な会計キー、同内容のみ冪等応答、異内容409、原子的commit、保存状態照会。 |
| 他Datasetの商品指定やGuest期限競合 | 認証主体由来のDataset、複合FK、ロック後の期限再判定、cleanup並行テスト。 |
| 値上げ／商品無効化後の過去改変 | 会計時点の名称・単価・金額スナップショット、レジ商品は論理無効化、旧Productの既知の価格限界を表示。 |
| JS改ざんや返金確認の迂回 | サーバー側の全入力・金額・返金意思の検証とDB CHECK、CSRF。 |
| 保存中の戻る／失敗後の空画面 | 入力中の破棄確認、失敗時状態保持、成功時のみ次会計。 |
| レシピの参照先消失 | `template:`と`product:`の既存キーを残し、旧Productを維持、参考試算は手動反映。 |

### Phase 0の検証範囲

リポジトリのソース、テスト、migrationを読み取りで照合した設計であり、実DBの件数・NULL・価格履歴、Renderのワーカー状況、実機レイアウト、同時保存性能は**未検証**。これらは対応する実装Phaseの事前確認とテストの完了条件に含める。
