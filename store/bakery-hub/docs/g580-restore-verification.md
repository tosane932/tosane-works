# G580の隔離復元を再開して詳細照合する

2026-10-21に利用者が取得・復号・隔離復元と基本検査を実施し、[2026-10-22の再検証記録](g580-encrypted-db-backup.md#2026-10-22の再復元詳細照合記録g580利用者の報告)では同じ暗号文の再復元と詳細SQLの完走を報告した。復元用コンテナは削除済みで、tmpfs上のDBも存在しない。以下は**必要になった場合にG580利用者がローカルで一段階ずつ再実行する手順**であり、Workで実データを試験した結果ではない。今回の記録を理由に追加のG580操作を要求しない。実データ・鍵・接続設定・照合結果の生出力をGit、Work、Driveの公開領域に置かない。

**期限後の移行にこの手順をそのまま転用しない。** `restore-encrypted-approved` は元DBへの2回の `preflight` を要する。Freeの元DBが期限切れで接続不能になった後の復元経路は[移行手順の事前STOP](production-db-cutover-runbook.md#事前に解消すべき技術的なstop)で別途安全な実装・試験を要する。この資料は元DBに接続できる間の独立隔離復元の手順である。

## 判定と証拠の境界

| 対象 | 10月22日現在の状態 | 残る確認 |
|---|---|---|
| 暗号化・復号・独立復元 | 同じ暗号文のSHA-256・サイズ一致、完全復号、空の独立PG18.6への再復元が利用者報告でPASS | 次の新規バックアップは別途検証 |
| 27表とDataset所有関係 | SQLの27表ガードを通過、Dataset分類・全表件数のクエリがERRORなく完走。`orphan_...`はすべて0との利用者報告 | 個々の全表件数・Admin/Guest分類の出力全文はWork未確認 |
| 旧商品・売上、会計・明細・返金・価格履歴 | 集計クエリ完走、対象の孤児件数は0との利用者報告 | 個別件数・日別集約ダイジェストの値と取得時点の独立証拠との照合は未実施 |
| 写真、レシピ・材料・仕込み・商品リンク | 集計クエリ完走、対象の孤児件数は0との利用者報告 | 写真件数・サイズ・集約SHA-256の出力値をWorkは確認していない |
| スタッフ・シフト・店舗メモ、天気履歴 | 件数クエリ完走、シフト孤児0との利用者報告 | 個別件数の出力値をWorkは確認していない |
| revision、制約、トリガー、sequence、インデックス | クエリ完走、`unvalidated`・`invalid`・`not_ready`は0との利用者報告。10月21日の基本検査は制約344、revision行1 | revisionの値、種別別制約・トリガー・sequenceの個別出力をWorkは確認していない |
| 取得時点の本番との完全一致 | **NOT TESTED** | 21:41頃の独立した読取スナップショット証拠があれば同じ時点で比較。リポジトリには全表の当時の独立照合記録を確認できない |

詳細SQLは `psql -X -v ON_ERROR_STOP=1` で読取専用のまま `COMMIT` までERRORなく到達したとの報告。**復元データの内部整合性は、利用者が確認した範囲でPASS、総合判定はPARTIAL。** 後日のRender本番DBは更新されている可能性がある。2026-10-21のdumpに、後日の本番件数を単純に突き合わせて差があってもFAILとしない。一致しても取得時点の完全一致PASSにはしない。新しいバックアップで同時点照合を計画するときも本番は読取専用とし、同じMVCCスナップショットの共有や書込停止の設計を別途承認する。今回のSQLには本番用サービス名を渡さない。

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

期待値は `241d5f915306f207153f2d175cb219587ea8de9986dfc4b5f54599de9b7502d6`。異なる場合は以降を止める。`stat -c '%a %U %s %n'` でファイルが所有者・600・108985 bytes、親ディレクトリが700か確認する。公開鍵は `~/BakeryHub-Backup/keys/bakeryhub-age-recipient.txt`。USB秘密鍵は `/media/tosane/BakeryHub-Key/keys/bakeryhub-age-identity.txt` にあり、中身は表示しない。鍵予備の平文をG580の通常ディスクへ展開しない。利用者は以前Driveに置いた平文秘密鍵をゴミ箱も含めて削除し、クリップボードもクリアしたと報告した。過去の第三者閲覧や外部コピーの不存在までは証明していない。

## 2｜Docker隔離先を新規作成（書込承認とメモリ確認後）

この段階は**新規の隔離DBへの書込**を伴う。利用者が再復元の実行を確認してから進む。既存コンテナに同名のものがあれば停止して調査し、勝手に削除しない。Dockerホストの容量と空きRAMを確認し、tmpfs 512MiBを確保できないときは停止する。コンテナDBはtmpfsのみ。Dockerのログ、ダンプ、ターミナル記録に実データを出さない。

```bash
docker ps -a --filter name=^/bakeryhub-restore-pg18$
docker network ls --filter name=^bakeryhub-restore-net$
```

各結果を確認する。ネットワークが存在する場合、次のコマンドで `bridge` / `true` / 接続コンテナの一覧を確認し、復元専用で接続先がない場合だけ再利用する。別の用途や不明な接続があれば止める。**無条件に削除・再作成しない。** ネットワークが存在しない場合だけ新規作成する。

```bash
docker network inspect -f '{{.Name}} {{.Driver}} {{.Internal}} {{json .Containers}}' bakeryhub-restore-net
```

存在しないことを確認した場合に限る：

```bash
docker network create --driver bridge --internal --label bakeryhub.purpose=restore-only bakeryhub-restore-net
```

新規作成後も `network inspect` で `bridge` / `true` / 接続先なしを確認する。既存ネットワークを使った場合は、この作業で新規作成したものとして扱わない。

ローカルで実際に使用した `/home/tosane/.config/bakeryhub/restore-local-password`（所有者の通常ファイル・0600）が存在することだけ確認する。再生成も内容表示もしない。秘密値をコマンド引数、環境変数、チャットへ書かない。コンテナ作成前に `pg_service.conf` の復元先が削除済みコンテナの古いIPを指していないか確認する。実機ではホスト側 `127.0.0.1:55432` のポート経由が到達せず、G580から内部bridge上のコンテナIPへ直接接続して成功した。この経路を採用し、ポートは公開しない。Dockerの一般的なポート公開の可否を断定するものではない。Docker 29.8.0のG580とActions上のLinuxでの到達性は別々に確認する。

```bash
stat -c '%a %U %n' /home/tosane/.config/bakeryhub/restore-local-password
```

復元時にメモリページが平文でスワップへ書かれるのを避ける。**ネットワーク・鍵・暗号文・RAM・認証設定の確認を先に終えてから**、コンテナ起動の直前に `/swap.img` を停止する。停止しても空きRAMが十分で、他の作業に支障がないことを確認。停止に失敗したら復元しない。途中失敗時もコンテナ停止・削除を確認してからスワップを再有効化する。

```bash
sudo swapoff /swap.img
```

```bash
swapon --show
```

PostgreSQL 18.6コンテナのデータ領域 `/var/lib/postgresql` を、実機で成功した `uid=999,gid=999,mode=0700` のtmpfsに置く。パスワードファイルはreadonly bind mount。Docker内部には平文DBがあるため、Docker管理者とG580のアクセスも保護する。起動に失敗したら**後片付けの節**へ進み、スワップを停止したまま放置しない。

```bash
docker run -d --name bakeryhub-restore-pg18 --network bakeryhub-restore-net --tmpfs /var/lib/postgresql:rw,size=512m,uid=999,gid=999,mode=0700 --mount type=bind,src=/home/tosane/.config/bakeryhub/restore-local-password,dst=/run/secrets/pg_password,readonly --env POSTGRES_USER=restore_operator --env POSTGRES_DB=isolated_restore --env POSTGRES_PASSWORD_FILE=/run/secrets/pg_password postgres:18.6
```

`docker port bakeryhub-restore-pg18` に公開ポートがないこと、コンテナが `bakeryhub-restore-net` **だけ**に接続すること、Dockerのtmpfs設定とデータ領域の所有者が期待どおりであることを確認する。コンテナ起動ログを丸ごと公開しない。`docker inspect` で**その回に作成した**コンテナのIPを確認する（過去のIPを転記しない）。

```bash
docker port bakeryhub-restore-pg18
```

```bash
docker inspect -f '{{json .HostConfig.Tmpfs}} {{json .NetworkSettings.Networks}}' bakeryhub-restore-pg18
```

```bash
docker exec bakeryhub-restore-pg18 stat -c '%a %u %g' /var/lib/postgresql
```

```bash
docker inspect -f '{{ (index .NetworkSettings.Networks "bakeryhub-restore-net").IPAddress }}' bakeryhub-restore-pg18
```

コンテナの準備完了後、G580の保護された `pg_service.conf` の `approved_restore_target` を **host=今回inspectで確認したコンテナIP, port=5432, dbname=isolated_restore, user=restore_operator** に更新し、`pgpass` に対応する認証を保護されたエディタで設定する。既存の本番用 `approved_source` は変更しない。ファイル内容やパスワードをログへ表示しない。IPが空、または接続ができない場合は止める。新しいホスト・DB名・認証・空状態を**実行直前**に再確認する。

```bash
export PGSERVICEFILE=/home/tosane/.config/bakeryhub/pg_service.conf
export PGPASSFILE=/home/tosane/.config/bakeryhub/pgpass
psql -X -A -t -v ON_ERROR_STOP=1 -d 'service=approved_restore_target' -c "SELECT current_database(), current_setting('server_version'), inet_server_addr(), inet_server_port(), (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f'))"
```

`isolated_restore`、18.6、コンテナの今回のIP・port 5432、空（関係0）を確認。ソース `approved_source` のRender本番ホストと同じサーバーではないことを、管理画面と別の接続設定で再確認する。`preflight`も空・接続先・両サーバー18系を確認する。ここで拒否されれば止める。

```bash
cd ~/tosane-works/store/bakery-hub
python3 production_backup_guard.py preflight --source-service approved_source --source-database bakery_db_4 --target-service approved_restore_target --target-database isolated_restore
```

## 3｜復号と復元（復元先の再確認後）

USBの秘密鍵ファイルのパス `/media/tosane/BakeryHub-Key/keys/bakeryhub-age-identity.txt` が存在することだけ手元で確認する。鍵本体は絶対に表示しない。暗号文のハッシュ・全ストリームを再検証する。

```bash
python3 production_backup_guard.py verify-encrypted --identity-file /media/tosane/BakeryHub-Key/keys/bakeryhub-age-identity.txt --sha256 241d5f915306f207153f2d175cb219587ea8de9986dfc4b5f54599de9b7502d6 /home/tosane/BakeryHub-Backup/database/bakeryhub_20261021_214150_271fcbe5bdc0.dump.age
```

この直前でも隔離DBが空であることと書込承認を確認する。以下は隔離DBに実データを書き込む。失敗しても既存バックアップを変更しない。途中失敗した復元先を成功扱いにせず、再実行には新しい空DBを用意する。

```bash
python3 production_backup_guard.py restore-encrypted-approved --source-service approved_source --source-database bakery_db_4 --target-service approved_restore_target --target-database isolated_restore --confirm-target isolated_restore --identity-file /media/tosane/BakeryHub-Key/keys/bakeryhub-age-identity.txt --sha256 241d5f915306f207153f2d175cb219587ea8de9986dfc4b5f54599de9b7502d6 /home/tosane/BakeryHub-Backup/database/bakeryhub_20261021_214150_271fcbe5bdc0.dump.age
```

平文dumpファイルは作らず、age→`pg_restore`の標準ストリームを利用。成功表示は復元コマンドの成功であり、詳細照合のPASSではない。

## 4｜隔離DBの読取だけで詳細照合

付属の[SQL](g580-restore-verification.sql)は接続先DB名と18系を検査し、`REPEATABLE READ READ ONLY`で27表の正確な行数、Datasetの種類と関連、日別旧売上・会計の集約ダイジェスト、写真件数・総サイズ・集約ハッシュ、重要な孤児件数、Alembic revision、制約種別、トリガー状態、sequence値、インデックス状態を表示する。画像バイト・氏名・認証ハッシュ・個別会計は表示しない。

```bash
psql -X -v ON_ERROR_STOP=1 -d 'service=approved_restore_target' -f docs/g580-restore-verification.sql
```

27表すべての行数とDataset分類をG580内の保護された作業記録へ手動記録する。**出力をPRやチャットへ貼らない。** 照合結果は項目ごとのPASS/NOT TESTED/FAILと、個人情報を含まない件数・状態だけ共有する。列の違い・エラー・孤児・無効な制約/インデックスはFAIL候補として調査し、全体PASSを保留する。対象表がないなどSQLが途中で止まったら、その項目はNOT TESTED。完全一致の根拠には独立した21:41時点の本番照合記録が必要であり、現時点では確認できていない。

## 5｜後片付け

**成功・途中失敗・中断のいずれでも**この順序で終了する。まず同名のコンテナが今回作った復元用か調べる。存在するなら停止・停止状態を確認して削除する。`docker run` 前に失敗しコンテナが存在しない場合、停止・削除コマンドは実行しない。tmpfs上の復元DBはコンテナ削除で消失する。停止・削除に失敗した場合は、**コンテナ稼働中にswapを戻さず**、作業を止めて原因を調べる。電源断などの予期しない停止時も、再開後にコンテナとswapを確認する。既存ネットワークは残してよい。

```bash
docker ps -a --filter name=^/bakeryhub-restore-pg18$
```

今回の復元用コンテナが存在する場合に限り、一つずつ：

```bash
docker stop bakeryhub-restore-pg18
docker inspect -f '{{.State.Running}}' bakeryhub-restore-pg18
docker rm bakeryhub-restore-pg18
```

`Running=false` と削除成功を確かめ、次の結果が空と確認してからswapを戻す：

```bash
docker ps -a --filter name=^/bakeryhub-restore-pg18$
sudo swapon /swap.img
swapon --show
```

swapが再有効化されていること、復元コンテナがないことを確認。`pg_service.conf`の旧復元先設定は次回使わず、再構築ごとに再同定する。ネットワークは既存のものだったら削除しない。今回新規作成した場合でも、他の接続先がないことを確認してから、必要な場合だけ別途削除する。スワップ内の過去ページの消去をこの手順は証明しない。DB保存先・OSスワップの暗号化状態も別途管理する。10月21日の暗号化バックアップはその後の更新を含まないため、期限前の**最終バックアップと本番移行は別の承認工程**とする。
