# G580の隔離復元を再開して詳細照合する

2026-10-21の[利用者報告](g580-encrypted-db-backup.md#2026-10-21の実施記録g580利用者の報告)では取得・復号・独立復元と基本検査まで成功した。復元用コンテナは削除済みで、tmpfs上のDBも存在しない。以下は**G580利用者がローカルで一段階ずつ実行する手順**であり、Workで実データを試験した結果ではない。実データ・鍵・接続設定・照合結果の生出力をGit、Work、Driveの公開領域に置かない。

## 判定と証拠の境界

| 対象 | 現在の状態 | 次の合格証拠 |
|---|---|---|
| 暗号化・復号・独立復元 | 利用者報告で成功 | 保存済みSHA-256を再照合し、同じ暗号文から再復元できること |
| 27表と全件数、Dataset所有関係 | 表27、`daily_sales` 104、Alembic 1のみ実測 | SQLの全表件数、Admin/Guest分類、孤児0をG580内の保護された記録に残す |
| 旧商品・日次売上、会計・明細・返金・価格履歴 | 未照合 | 全表件数、日別旧売上と会計の日別・kind別の集計ダイジェスト、重要リンクの孤児0 |
| 写真、レシピ・材料・仕込み・商品リンク | 未照合 | 写真件数・総バイト・集約SHA-256、関連表件数と孤児0 |
| スタッフ・シフト・店舗メモ、天気履歴 | 未照合 | 対応表の件数、Dataset分類、シフトの孤児0 |
| revision、制約、トリガー、sequence、インデックス | 制約344、未検証0、無効インデックス0、revision行1のみ実測 | revisionの値、FK/UNIQUE/CHECK/PK別件数、トリガー有効状態、sequence値、インデックス状態を確認 |
| 取得時点の本番との完全一致 | **NOT TESTED** | 21:41頃の独立した読取スナップショット証拠があれば同じ時点で比較。リポジトリには全表の当時の独立照合記録を確認できない |

後日のRender本番DBは更新されている可能性がある。2026-10-21のdumpに、後日の本番件数を単純に突き合わせて差があってもFAILとしない。一致しても取得時点の完全一致PASSにはしない。独立した同時点証拠がなければ「復元内容の内部整合性は検査済み／取得時点の元DBとの完全一致は未証明」と記録する。新しいバックアップで同時点照合を計画するときも本番は読取専用とし、同じMVCCスナップショットの共有や書込停止の設計を別途承認する。今回のSQLには本番用サービス名を渡さない。

## 1｜変更とローカル状態の確認（ここで停止して判断）

コマンドは**一つずつ**実行する。作業ツリーに変更があれば勝手にstash/reset/cleanしない。

```bash
cd ~/tosane-works
git status
git branch --show-current
git fetch origin
```

追加PRの対象コミットを確認してから、作業ツリーがクリーンな場合だけ、そのブランチまたはマージ済みmainの`docs/g580-restore-verification.sql`を使う。PR #138は既にmainにマージ済み。`pg_dump`、`pg_restore`、`psql`が18系、`age`、Dockerが使えることを確認。`PGSERVICEFILE`・`PGPASSFILE`はGit外で0600、`PGPASSWORD`を使わない。USB秘密鍵の中身は表示しない。

```bash
pg_dump --version
pg_restore --version
psql --version
age --version
docker --version
df -h /home/tosane
free -h
swapon --show
```

既存の暗号文を上書きしない。鍵媒体のマウント先は利用者が確認する。次のハッシュは**暗号化済みファイル**だけを読む。

```bash
sha256sum /home/tosane/BakeryHub-Backup/database/bakeryhub_20261021_214150_271fcbe5bdc0.dump.age
```

期待値は `241d5f915306f207153f2d175cb219587ea8de9986dfc4b5f54599de9b7502d6`。異なる場合は以降を止める。`stat -c '%a %U %s %n'` でファイルが所有者・600・108985 bytes、親ディレクトリが700か確認する。公開鍵は `~/BakeryHub-Backup/keys/bakeryhub-age-recipient.txt` で、古い `~/bakeryhub-age-recipient.txt` を使わない。秘密鍵は暗号化媒体 `BakeryHub-Key` のみから読み、鍵予備の平文をG580の通常ディスクへ展開しない。以前Driveに置いた平文鍵の履歴・ゴミ箱の完全削除は未確認として別に調べる。

## 2｜Docker隔離先を新規作成（書込承認とメモリ確認後）

この段階は**新規の隔離DBへの書込**を伴う。利用者が再復元の実行を確認してから進む。既存コンテナ・ネットワークに同名のものがあれば停止して調査し、削除や再利用を機械的に行わない。Dockerホストの容量と空きRAMを確認し、tmpfs 512MiBを確保できないときは停止する。コンテナDBはtmpfsのみ。Dockerのログ、ダンプ、ターミナル記録に実データを出さない。

```bash
docker ps -a --filter name=^/bakeryhub-restore-pg18$
docker network ls --filter name=^bakeryhub-restore-net$
```

復元時にメモリページが平文でスワップへ書かれるのを避ける。`/swap.img`を一時停止しても空きRAMが十分なこと、他の作業に支障がないことを確認。スワップ停止に失敗したら復元しない。終了後は再有効化を必ず確認する。

```bash
sudo swapoff /swap.img
swapon --show
```

ローカルの保護された `/home/tosane/.config/bakeryhub/restore_password`（通常ファイル、所有者・0600）を、**エディタでオフライン作成済みであること**を確認する。秘密値をコマンド引数、環境変数、チャットへ書かない。これが用意できない場合は停止。コンテナ作成前に既存の `pg_service.conf` の復元先が削除済みコンテナの古いIPを指していないか確認する。以下ではホストloopback `127.0.0.1:15432` にだけ公開し、Docker内部ネットワークにWebを接続しない。ポートの競合があれば停止。

```bash
stat -c '%a %U %n' /home/tosane/.config/bakeryhub/restore_password
docker network create --internal bakeryhub-restore-net
docker network inspect -f '{{.Internal}}' bakeryhub-restore-net
```

`true`を確認してから一つずつ実行。Docker 18.6イメージのデータ領域 `/var/lib/postgresql` をtmpfsに置く。パスワードファイルはreadonly bind mount。Docker内部には平文DBがあるため、Docker管理者とG580のアクセスも保護する。

```bash
docker run -d --name bakeryhub-restore-pg18 --network bakeryhub-restore-net --publish 127.0.0.1:15432:5432 --tmpfs /var/lib/postgresql:rw,nosuid,nodev,size=512m --mount type=bind,src=/home/tosane/.config/bakeryhub/restore_password,dst=/run/secrets/pg_password,readonly --env POSTGRES_USER=restore_operator --env POSTGRES_DB=isolated_restore --env POSTGRES_PASSWORD_FILE=/run/secrets/pg_password postgres:18.6
docker port bakeryhub-restore-pg18
```

`docker port` は `127.0.0.1:15432` だけであることを確認。コンテナの準備完了後、G580の保護された `pg_service.conf` の `approved_restore_target` を **host=127.0.0.1, port=15432, dbname=isolated_restore, user=restore_operator** に更新し、`pgpass` に対応する認証を保護されたエディタで設定する。旧コンテナのIPは使わない。ファイル内容やパスワードをログへ表示しない。新しいホスト・DB名・認証・空状態を**実行直前**に再確認する。

```bash
export PGSERVICEFILE=/home/tosane/.config/bakeryhub/pg_service.conf
export PGPASSFILE=/home/tosane/.config/bakeryhub/pgpass
psql -X -A -t -v ON_ERROR_STOP=1 -d 'service=approved_restore_target' -c "SELECT current_database(), current_setting('server_version'), inet_server_addr(), inet_server_port(), (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f'))"
```

`isolated_restore`、18.6、空（関係0）を確認。ソース `approved_source` のRender本番ホストと同じサーバーではないことを、管理画面と別の接続設定で再確認する。`preflight`も空・接続先・両サーバー18系を確認する。ここで拒否されれば止める。

```bash
cd ~/tosane-works/store/bakery-hub
python3 production_backup_guard.py preflight --source-service approved_source --source-database bakery_db_4 --target-service approved_restore_target --target-database isolated_restore
```

## 3｜復号と復元（復元先の再確認後）

USBの秘密鍵ファイルの**実際のパスだけ**を手元で確認し、以下の例の `/media/tosane/BakeryHub-Key/bakeryhub-age-identity.txt` が異なれば手元で置き換える。鍵本体は絶対に表示しない。暗号文のハッシュ・全ストリームを再検証する。

```bash
python3 production_backup_guard.py verify-encrypted --identity-file /media/tosane/BakeryHub-Key/bakeryhub-age-identity.txt --sha256 241d5f915306f207153f2d175cb219587ea8de9986dfc4b5f54599de9b7502d6 /home/tosane/BakeryHub-Backup/database/bakeryhub_20261021_214150_271fcbe5bdc0.dump.age
```

この直前でも隔離DBが空であることと書込承認を確認する。以下は隔離DBに実データを書き込む。失敗しても既存バックアップを変更しない。途中失敗した復元先を成功扱いにせず、再実行には新しい空DBを用意する。

```bash
python3 production_backup_guard.py restore-encrypted-approved --source-service approved_source --source-database bakery_db_4 --target-service approved_restore_target --target-database isolated_restore --confirm-target isolated_restore --identity-file /media/tosane/BakeryHub-Key/bakeryhub-age-identity.txt --sha256 241d5f915306f207153f2d175cb219587ea8de9986dfc4b5f54599de9b7502d6 /home/tosane/BakeryHub-Backup/database/bakeryhub_20261021_214150_271fcbe5bdc0.dump.age
```

平文dumpファイルは作らず、age→`pg_restore`の標準ストリームを利用。成功表示は復元コマンドの成功であり、詳細照合のPASSではない。

## 4｜隔離DBの読取だけで詳細照合

付属の[SQL](g580-restore-verification.sql)は接続先DB名と18系を検査し、`REPEATABLE READ READ ONLY`で27表の正確な行数、Datasetの種類と関連、日別旧売上・会計の集約ダイジェスト、写真件数・総サイズ・集約ハッシュ、重要な孤児件数、Alembic revision、制約種別、トリガー状態、sequence値、インデックス状態を表示する。画像バイト・氏名・認証ハッシュ・個別会計は表示しない。

```bash
psql -X -v ON_ERROR_STOP=1 -d 'service=approved_restore_target' -f docs/g580-restore-verification.sql
```

27表すべての行数とDataset分類をG580内の保護された作業記録へ手動記録する。**出力をPRやチャットへ貼らない。** 照合結果は項目ごとのPASS/NOT TESTED/FAILと、個人情報を含まない件数・状態だけ共有する。列の違い・エラー・孤児・無効な制約/インデックスはFAIL候補として調査し、全体PASSを保留する。対象表がないなどSQLが途中で止まったら、その項目はNOT TESTED。完全一致の根拠には独立した21:41時点の本番照合記録が必要であり、現時点では確認できていない。

## 5｜後片付け

保護したローカル記録が不要なら作成しない。隔離DBの確認終了後、コンテナを停止し、停止済みと確認してから削除する。tmpfsの復元DBは消失する。ネットワークがこのコンテナ専用と確認できた場合だけ削除する。停止・削除に失敗した場合は原因を調べ、スワップ復帰を忘れない。

```bash
docker stop bakeryhub-restore-pg18
docker rm bakeryhub-restore-pg18
docker network rm bakeryhub-restore-net
sudo swapon /swap.img
swapon --show
docker ps -a --filter name=^/bakeryhub-restore-pg18$
```

swapが再有効化されていること、復元コンテナがないことを確認。`pg_service.conf`の旧復元先設定は次回使わず、再構築ごとに再同定する。スワップ内の過去ページの消去をこの手順は証明しない。DB保存先・OSスワップの暗号化状態も別途管理する。10月21日の暗号化バックアップはその後の更新を含まないため、期限前の**最終バックアップと本番移行は別の承認工程**とする。
