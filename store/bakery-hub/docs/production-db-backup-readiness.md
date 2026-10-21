# 本番PostgreSQL全量保全と隔離復元（Phase 3、実行前手順）

G580へ平文dumpを作らずに直接暗号化する実行手順と**2026-10-21の利用者報告による取得・隔離復元実績**は[Phase 3B](g580-encrypted-db-backup.md)を参照。詳細照合は[再開手順](g580-restore-verification.md)を参照。以下の旧コマンド例はPhase 3時点の方式で、G580で実データを保存する際には使用しない。

この文書は実データ操作の許可ではありません。正式レジはNO-GO。商品設定の[CSV保管](data-vault-guide.md)とは別の全DB保存です。下の「現時点」は**Phase 3作成時の過去の調査記録**。その後のG580実行結果とは区別する。

## Phase 3時点の確認と停止条件（履歴）

Render Workspace `tea-d8ejjg9o3t8c73fhqjt0` の Web `bakeryhub` (`srv-d931ouuh2hms73d4au8g`) は main の `42a982d` を live と表示。候補 `bakery-db-4` (`dpg-db6q6a0n74is738j0t80-a`) は Free・available、DB名 `bakery_db_4`、PostgreSQL 18.6、サイズ9,787,071 bytes、期限 **2026-11-12 03:19:04 UTC（12:19:04 JST）**。このDBの読取ではAdmin Dataset 1、旧商品15、旧日次売上99、新レジ会計0、Alembic `b6d2e9f4a107`、public表27。これらは**候補DBの値**であり、WebがそのDBへ接続している証拠ではありません。

Render連携の読取情報にWebの実効 `DATABASE_URL` のホスト・DB名が現れない。権限を持つ管理者がRender管理画面内でWebの実効接続先（変数参照も解決する）と候補DBの接続先ホスト・DB名を並べ、秘密部分を記録せず、一致／不一致・確認者・日時・サービスID・DB IDを署名付き作業記録へ残す。異なる場合は対象を決め直す。画面上のAdmin売上件数とDB側の件数も照合するが、画面の読取が副作用を起こさないことを事前確認する。**接続先未確定の間は実dumpを開始しない。**

Phase 3文書作成時は本番dumpと隔離復元が未実施だった。**2026-10-21にはG580利用者の報告による実取得・実復元が成功**。この段落を現在の未実施判定には使わない。Work環境やGitHub（Private保管庫・Actions artifactを含む）へ実dumpを置かない。端末/P10のIndexedDBに残る未同期会計はDBに含まれず、端末側で別途確認する。未同期データの削除・再同期をこの手順では行わない。

## 対象と実行環境

`pg_dump -Fc` は対象DBの全スキーマ・全表・データ・インデックス・制約・トリガー・sequence・Alembic版を保存する。ロール、tablespace等の**クラスタ全体のグローバルオブジェクト**、外部ファイル、Render設定、端末のIndexedDBは含まない。復元先に必要なロールと拡張は予め確認する。実表はdump直前に `pg_catalog` から列挙し、記録する。2026-10-21の候補DBの表は以下の27個：

`alembic_version`, `daily_forecasts`, `daily_sales`, `daily_weather`, `datasets`, `guest_creation_rate_limits`, `material_order_items`, `prep_ingredients`, `prep_plans`, `prep_recipes`, `products`, `register_cutover_approvals`, `register_cutovers`, `register_offline_permits`, `register_prep_links`, `register_price_history`, `register_products`, `register_recovery_cases`, `register_recovery_events`, `register_recovery_lines`, `register_transaction_lines`, `register_transactions`, `shop_memos`, `shop_schedules`, `shop_shifts`, `shop_staff`, `shop_tasks`。

実行場所は所有者のローカルPC。PostgreSQL 18 `psql`・`pg_dump`・`pg_restore` と、Webから隔離した別の**空のPostgreSQL 18サーバー**を準備する。新しいRender有料サービスは作成しない。復元先を本番と同じサーバー上の別DBにしない。隔離先は他ユーザーに接続させず、アプリのDATABASE_URLには設定しない。保存先は暗号化ボリューム（例：OSのフルディスク暗号化とアクセス制御）で、鍵をバックアップと同じ場所に平文保存せず、利用できる空き容量と保持期限を確認する。Git管理・自動同期・共有領域の外を選ぶ。

権限0600の `PGSERVICEFILE` と `PGPASSFILE` を保護されたエディタで用意し、`approved_source` と `approved_restore_target` を別々に定義する。接続URLやパスワードをコマンド引数、履歴、PR、CI、チャットに貼らない。管理者がWebの実効接続先と `approved_source` のホスト・DB名を**別経路で照合**する。読み取るロールに全業務表、sequence、large object（存在する場合）への必要な権限があることも確認する。接続失敗・権限不足・版違いは中止する。

## 承認後の実行例（ローカルPCのみ）

実行前に作業窓・本番負荷と短い読取ロックの可能性・通信量・暗号化先・費用・鍵の管理者・隔離先への書込・保持期間について、**本番取得と隔離書込のそれぞれ**明示承認を得る。Webの運用状況と接続先一致を再確認する。以下の `/encrypted/backup-root` は実際に暗号化が確認できた専用領域に置き換える。接続設定ファイルは必ず保護領域に置く。

```bash
umask 077
export PGSERVICEFILE=/protected/pg_service.conf
export PGPASSFILE=/protected/pgpass
python production_backup_guard.py preflight --source-service approved_source --source-database bakery_db_4 --target-service approved_restore_target --target-database isolated_restore
backup_dir=$(mktemp -d /encrypted/backup-root/bakeryhub-YYYYMMDD-XXXXXXXX)
pg_dump --dbname='service=approved_source' --format=custom --no-owner --no-acl --file="$backup_dir/full.dump"
python production_backup_guard.py verify-archive "$backup_dir/full.dump"
```

`mktemp` は新規ディレクトリを作り、既存バックアップを上書きしない。`pg_dump` の終了コードを確認し、失敗・中断時にはそのファイルを成功物として扱わない。別の安全な作業記録へUTC/JST時刻、サービスID、DB ID、DB名、PostgreSQL版、サイズ、SHA-256、`pg_restore --list` の結果、保存場所・鍵の管理者を記録する。**ファイル本体や接続文字列を記録へ添付しない。** `verify-archive` は全ストリームを `/dev/null` に展開して破損を調べるが、復元PASSではない。テーブル定義とTABLE DATA項目の存在を検査する。`--no-owner --no-acl` により元の所有者・GRANTは復元しないため、復元先の権限と本番切替時の認可を別途検証する。原本を変更せず、承認した独立の暗号化コピーを作ることも検討する。

隔離先の空状態と独立性を再確認し、先ほど出力されたSHA-256を照合して、承認済みの**隔離先**に限り実行する。以下の値はプレースホルダーであり、実際のダイジェストに置き換える。

```bash
python production_backup_guard.py restore-approved --source-service approved_source --source-database bakery_db_4 --target-service approved_restore_target --target-database isolated_restore --confirm-target isolated_restore --sha256 ACTUAL_64_LOWERCASE_HEX "$backup_dir/full.dump"
```

ツールは直前に双方のTCP接続先、DB名、18系クライアント／サーバー、隔離先の空状態を検査し、`pg_restore --single-transaction --exit-on-error --no-owner --no-acl` を実行する。`--clean` と `--create` は使用しない。**名前とアドレスの確認だけで本番環境の所有者まで証明できない**ため、管理画面との手動照合と隔離ネットワークの確認を省略しない。復元を途中で失敗した隔離DBは成功扱いにせず、再試験には承認済みの新しい空DBを使用する。

## 合格判定

両DBの読取専用スナップショットを同じ作業窓で取得する。本番が書き込まれ続ける場合、dumpのスナップショット時点と後の直接照合値がずれる。書込停止を合意するか、dump時点と整合する記録を別に残し、無理な件数一致で合格にしない。**2026-10-21 21:41 JSTのdumpについて、取得時点の独立した全表照合記録はこのリポジトリ内では確認できない。後日の本番値との差は不整合の証拠ではなく、完全一致の証拠にもならない。** 照合結果には個人名、メール、認証ハッシュ、写真の実バイトを出さない。

- 全public表名と各表件数、Dataset数と所有関係、旧商品／日次売上、レジ会計／明細／返金、価格履歴、スタッフ／シフト、レシピ／材料／商品リンクを比較する。
- 日付別の旧売上集計と会計の金額・状態別集計、レシピと商品リンク、画像のサイズとSHA-256集計を比較する。価格履歴など主要参照の孤児件数を確認する。
- `alembic_version`、全FK/UNIQUE/CHECK/PK、トリガーの有効状態、sequenceの現在値を照合する。制約の違反試験は**隔離DBのみ**トランザクションをrollbackして実施する。復元したアプリの安全な読み取り確認は認可境界を維持し、公開接続をしない。
- `pg_dump` と `pg_restore` の終了コード、アーカイブサイズ・SHA-256・全ストリーム、一覧、復元後の上記照合がそろった時だけPASS。10月21日の**取得・復号・復元コマンド成功と基本検査**は利用者の報告により確認済みだが、詳細照合が残るため全体はPARTIAL。未実施はNOT TESTED、同一時点と証明できる相違や失敗はFAILとして原因を調べる。

## 無料DB期限と切替の別工程

継続バックアップ間隔、費用・停止時間を含む選択肢、起動時のmigration/seed、端末未同期の扱い、三つの独立した判定ゲートは[期限前の保全と移行判断](production-db-free-expiry-plan.md)に整理した。以下はPhase 3時点の要点として残す。

Renderの公開資料によるとFree PostgreSQLは同一Workspaceで同時に原則1つ、30日で期限切れとなり、管理バックアップはない。期限後は有料プランへ変更しない限りアクセスできず、有料へ変更できる猶予14日を過ぎるとDBが削除される。猶予を無料のdump取得期間とみなさない。期限後を待たず**2026-11-12 12:19 JSTより十分前に**本番バックアップと隔離復元を終える。現在のDBがある間に新無料DBを当然作れると仮定しない。実際の作成条件、作業停止時間、ロールバック可能な旧DBの生存期間をRender画面と公式文書で移行直前に再確認する。

新DBへの本番移行は今回行わない。別承認の作業窓で更新停止・端末未同期会計の確認、最終dump、独立復元検証、新しいDBの作成条件確認、復元、最終差分・認証と権限の検査を行い、その後だけWebの接続先変更を検討する。切替中はサービスをメンテナンス状態にし、ダウンタイムを見込む。旧DBを期限内に使える場合だけ切戻し候補となるが、切替後の書込は旧DBに戻すと失われ得るので再反映手順を承認する。期限失効後のロールバックを保証しない。

現行Docker CMDは `flask db upgrade && python seed_demo.py && gunicorn ...`。`seed_demo.py` はAdminの旧Product/DailySalesの有無で投入を決めるため、復元状態次第でデモ商品・売上を混入させ得る。**新DBへの通常起動前に**起動コマンドとseedの条件を別変更として審査・検証し、自動migrationやseedが走らない安全な手順を確定する。Render環境変数・Docker起動処理・DB作成／削除・本番migration・正式レジ切替は本PRでは変更しない。

参考：[Render Free](https://render.com/docs/free)、[Render PostgreSQL backups](https://render.com/docs/postgresql-backups)、[PostgreSQL pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html)。
