# 商品設定のCSV保存・復元（形式v1）

これは商品・写真・レシピの再入力を減らす独立CLIです。**会計のバックアップ、DB全体の復元、本番接続先の切替ではありません。正式レジはNO-GOのままです。** メニュー・HTTP route・Docker起動処理には組み込みません。

## 保存するもの／しないもの

| ファイル | 保存内容 |
|---|---|
| products.csv | RegisterProduct全件（終売含む）の参照ID、名前、単価、パンの種類、商品分類、状態、表示順、候補キー、写真ファイル |
| recipes.csv | PrepRecipeの参照ID、元キー、表示名、基準製造数、メモ、承認画像キー・ファイル |
| ingredients.csv | PrepIngredientの参照ID、レシピ参照、名前、単位、Decimalの必要量 |
| product_recipe_links.csv | RegisterPrepLinkの明示的な商品・レシピ対応 |
| images/ | 写真の元バイト列。SHA-256をファイル名に使い、同じ内容は1ファイルにまとめる |
| backup-manifest.json | 保存UTC日時、形式版、backup UUID、元Admin Dataset UUID、対象4表、CSV件数、全CSV・写真のサイズとSHA-256 |

旧Product・DailySales、実売上・販売／返金会計、価格変更監査履歴、復旧案件、スタッフ個人情報、認証情報、PIN、セッション、未同期会計、IndexedDB、日々の製造予定・シフト・予定・タスクは含めません。**確定会計・日次売上は別途承認したDB全量バックアップで保全してください。** CSVを保存しただけでは無料DB失効への備えは完了しません。

## 最初の準備

`store/bakery-hub/` で実行し、既存requirementsを入れたPython環境を使います。接続先はアプリのDATABASE_URLを暗黙に読まず、権限0600の通常ファイルに保存したURLだけを明示指定します。接続文字列をコマンド引数・履歴・Git・画面写真へ貼り付けないでください。接続設定の準備は保護されたエディタで行います。CLIはapp.pyやconfig.pyをimportせず、migrationもAdmin Datasetの作成も行いません。

元／先それぞれのDB識別子・Admin Dataset UUIDを管理者が確認してください。復元先はスキーマ準備済みの**空のAdmin Dataset**。旧商品・会計・履歴・予定・スタッフ等、対象Adminの業務行が1件でもあればdry-runから拒否します。他Datasetへの紐づけや同名商品との自動統合はしません。Dataset UUIDは接続先ごとに異なって構いません。

**新DBの通常起動は、別途の移行承認まで止めます。** 現行DockerのCMDは `flask db upgrade && python seed_demo.py && gunicorn ...`。`seed_demo.py` は旧Product／DailySalesの有無で投入を判断し、新RegisterProduct／PrepRecipeだけの復元を検知しません。したがって本CLIで設定だけを戻したDBを、そのまま現行Docker起動へ接続すると旧形式のデモ商品・売上が投入され得ます。復元試験は独立CLIと隔離DBだけで実施し、接続先切替前に旧売上の別途移行とseed方針を管理者が承認・検証する必要があります。今回Docker／seed処理を変更せず、既存データを削除して空にする手段も用意しません。

以下は**承認済み隔離環境用の例**です。今回、本番の書き出し・取得・移行を実行する許可ではありません。`SOURCE_ADMIN_UUID`／`TARGET_ADMIN_UUID`は確認したUUIDに置き換えます。保存先は既存でないフォルダを、公開リポジトリの外に指定します。既存バックアップは上書きしません。

## 保存 → 内容検証

```bash
python data_vault.py export --database-url-file /protected/source-url --dataset-id SOURCE_ADMIN_UUID --folder /protected/backup_YYYYMMDD_HHMM
```

```bash
python data_vault.py verify --folder /protected/backup_YYYYMMDD_HHMM
```

exportはAdmin Datasetだけを読み取ります。PostgreSQLではREPEATABLE READ・READ ONLYの同一transactionから4表を読み、時点を揃えます。出力先は0700・ファイル0600、Manifestは最後に書き、完成後に読み戻して整合性を検査します。失敗時はその呼出しが新規作成した出力先だけを取り除きます。

UTF-8・全セル引用符のCSVです。**数式対策として全データセルの先頭にアポストロフィを1個追加**し、読込時に1個だけ除去します。元からあるアポストロフィ、改行、引用符、日本語はそのまま戻ります。Excel等で上書きすると保護やチェックサムが変わるため、このバックアップCSVを表計算ソフトで編集して復元しないでください。変更は通常の商品・レシピ画面で行い、再exportします。

この方式と再保存時の注意点は[OWASPのCSV Injection解説](https://owasp.org/www-community/attacks/CSV_Injection)を参照。あらゆる表計算ソフトで安全に編集できるという保証ではなく、このCLIで検証・復元するための保存形式です。

## 復元先の検査 → 明示した復元

importは何も指定しなければdry-runです。CSV・Manifest・画像を先に全検査し、復元先がAdminで空かを確認して件数と元／先Datasetを表示します。書込はしません。

```bash
python data_vault.py import --database-url-file /protected/target-url --dataset-id TARGET_ADMIN_UUID --folder /protected/backup_YYYYMMDD_HHMM
```

表示内容・DB識別子・件数を確認した管理者だけが、同じ復元先UUIDをもう一度指定して実行します。

```bash
python data_vault.py import --database-url-file /protected/target-url --dataset-id TARGET_ADMIN_UUID --folder /protected/backup_YYYYMMDD_HHMM --apply --confirm-dataset TARGET_ADMIN_UUID
```

書込直前に空状態を再検査します。PostgreSQLは業務表への書込を一時的にロックし、SQLiteはBEGIN IMMEDIATEで競合を防ぎます。復元は対象DBが未使用の作業窓で行い、他利用者の操作を並行させないでください。商品→レシピ→材料→対応を一つのtransactionで追加し、DBから件数・値・写真・関連を読み戻して一致した場合だけcommitします。エラーなら全体rollback。非空バックアップの再実行は既存データ検出で拒否します。0件バックアップの再実行は追加0件です。

## ID・写真・レシピの扱い

- `p101`・`r201`等の参照はバックアップ内だけで使い、新しいDB内部IDへ対応づけます。同名商品は別物のまま保持します。価格履歴を過去分として捏造・コピーしません。復元された現在価格の以後の変更は既存の編集処理で履歴化されます。
- 標準レシピと商品の暗黙対応は`template:<key>`と商品候補キーで保持。独自商品の明示対応はRegisterPrepLinkとして再作成します。
- **旧月次Product由来のレシピ**は、元のProductをこの機能で復元しないため、`product:<旧ID>`を新DBの同番号に結びません。`vault:<backup UUID>:<レシピ参照>`へ置換し、レシピ・材料・明示対応を保持します。旧日次売上との対応は復元対象外。必要なら通常画面で新レジ商品との明示対応を管理者が確認します。
- 商品のDB写真と承認済みWebPを元のバイト列で保存し、復元先ではDBのphoto_webpに保持します（承認画像キー由来でも再圧縮しない）。写真なしも保持します。
- レシピ写真は既存モデルの承認済みstatic画像参照です。元PNG／JPEG／WebPも保存しますが、復元先コードに同じ承認画像キー・同一バイト列が必要です。異なる／欠ける場合は停止し、public staticへ任意ファイルを書き込みません。画像・分類・保存形式の変更が必要なら別途設計します。

## 検査と制限

列順・件数・重複キー・参照・Admin種別、価格0〜100,000円、基準数1〜1,000、材料1〜30行・単位g/ml/個・必要量0超〜1,000,000（小数3桁）を検査します。各CSVは10,000行まで、各ファイル16MiB、全体256MiB、商品写真500KiB・WebP単一フレーム・画像1,600万画素以内。上限超過は一部を省略せず全体を停止します。

画像欠損・破損・パス逸脱・シンボリックリンク・未知ファイル・数式保護欠落・チェックサム不一致を拒否します。Manifestは暗号署名ではなく、改変した攻撃者はハッシュも作り直せます。出所を信頼できるバックアップだけを使い、Private保管庫と端末の権限を管理してください。

## Private GitHub保管庫と無料DB更新

保管先は `tosane932/bakeryhub-data-vault` の**Private確認済みリポジトリ**に限定します。公開リポジトリでは代用しません。初期構成の[テンプレート](data-vault-template/README.md)とexamplesは架空データのみ。本番のexportや自動pushは今回実行・実装しません。実データ保存の前に、所有者・Private設定・共同作業者・保存対象・保持期間を確認します。DB全量dumpや認証情報はこの保管庫に置きません。

期限前に「担当者・最終export日時・対象DB／Dataset・更新停止時点・保管先commit・検証結果」を記録します。export後に商品・写真・レシピを編集した場合は、その時点のバックアップに含まれないため、接続先切替前の承認した作業窓で更新を止め、最終exportを取り直します。DB全量バックアップ・隔離復元・実売上と端末キューの照合・接続先切替は別の承認作業です。無料DBの期限切れ後の自動復旧・切替はありません。

Private GitHubも障害・アカウント喪失・誤削除への万能な対策ではありません。承認した別保管先にもコピーし、復元試験で確かめます。会計のバックアップをこの機能で置き換えず、Phase 7のNO-GOを維持します。
