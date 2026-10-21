# G580での暗号化バックアップと隔離復元（Phase 3B）

**実データ操作前に読む。** この手順はLenovo G580の `tosane` アカウントで実行する。WorkやCIに実データを渡さない。正式レジはNO-GO。保存と復元の実施状況を別々に記録する。[Phase 3の照合項目](production-db-backup-readiness.md)も参照。

## 1｜G580のGitを安全に同期

まず `~/tosane-works` で、コマンドを**1つずつ**実行する。

```bash
cd ~/tosane-works
git status
git branch --show-current
git fetch origin
```

未コミット変更・未追跡の重要ファイル・進行中の作業があれば**停止して相談する**。勝手にstash、reset、cleanはしない。作業ツリーがクリーンなら次へ。

```bash
git switch main
git pull --ff-only origin main
git status
git log -1 --oneline
```

PR #137の `production_backup_guard.py`、`docs/production-db-backup-readiness.md`、`tests/test_production_backup_guard.py` があることを確認する。Phase 3BのDraft PRを確認するときは、この文書末尾のブランチ取得方法を使う。PRをmainへマージしていない間、mainには今回の新コマンドはない。

## 2｜OSと道具を確認

```bash
cat /etc/os-release
uname -m
df -h /home/tosane
command -v pg_dump
pg_dump --version
command -v pg_restore
pg_restore --version
command -v age
age --version
```

G580が想定どおりLubuntu 24.04でなければ、導入コマンドを決め直す。`age` はUbuntu 22.04以降の標準パッケージから `sudo apt install age`。PostgreSQL 18クライアントがなければ、[PostgreSQL公式のUbuntu向けAPT手順](https://www.postgresql.org/download/linux/ubuntu/)でPGDGの署名付きリポジトリを設定して `postgresql-client-18` のみ導入する。サーバーの自動起動はバックアップ取得には不要。複数版がある場合は `pg_dump --version` と `pg_restore --version` がともに18系になるようPATHを確認する。追加リポジトリ設定・インストール前にOSと候補パッケージを確認する。

## 3｜保管場所と復号鍵

```bash
install -d -m 700 ~/BakeryHub-Backup
install -d -m 700 ~/BakeryHub-Backup/database
```

保存先は `/home/tosane/BakeryHub-Backup/database/` に固定。ツールは所有者・700を検査し、平文dumpを書かず `.dump.age` のみを完成ファイルとする。**バックアップディレクトリはGitや自動クラウド同期に入れない。** 暗号化ファイルは600。`df`で容量を確認し、鍵や別コピーの保存期間を決める。

鍵方式はageの公開鍵暗号を採用する。暗号化には公開recipientだけを使用し、復号用秘密鍵はG580のバックアップディレクトリと**別の安全な場所**（取り外せる暗号化媒体など）に0600で置く。鍵をGit、チャット、PR、バックアップ本体と同じ場所へ保存しない。鍵の別保管と再読込を確認してから実データを保存する。

```bash
umask 077
age-keygen -o /media/tosane/保護媒体/bakeryhub-age-identity.txt
age-keygen -y /media/tosane/保護媒体/bakeryhub-age-identity.txt > /home/tosane/bakeryhub-age-recipient.txt
chmod 600 /home/tosane/bakeryhub-age-recipient.txt
```

`/media/tosane/保護媒体` は例。**実際の暗号化媒体のマウント先・ファイル権限を確認してから**置き換える。秘密鍵の中身を画面共有やチャットに表示しない。媒体を紛失すれば復号できないので、所有者が管理する独立の保護されたコピーも作る。復号検証時だけ鍵を利用し、検証後は媒体を安全に取り外す。秘密鍵を失った場合、バックアップ成功とは判定しない。

## 4｜本番接続先と認証情報の照合

**ここは本番接続の境界。** Web `bakeryhub` の実効 `DATABASE_URL` が指すホスト・DB名と、Render `bakery-db-4` のホスト・DB名をRender管理画面内で照合する。候補は `bakery_db_4` だが、過去の調査値だけで実接続先と決めない。照合日時・一致結果・確認者・サービスIDとDB IDのみを記録し、URL全体やパスワードを転記しない。不一致なら中止する。

G580上の保護された設定に `PGSERVICEFILE`、`PGPASSFILE` を用意する。libpq service `approved_source` にRenderの**外部ホスト名**（IPアドレスに置換しない）、port、dbname、user、`sslmode=require` を設定する。パスワードはPGPASSFILEのみ。両ファイルは所有者の通常ファイル・0600とし、Git外に置く。Renderの外部接続はTLSを必須とする。接続情報をコマンド、履歴、チャットに直接書かない。

```bash
export PGSERVICEFILE=/home/tosane/.config/bakeryhub/pg_service.conf
export PGPASSFILE=/home/tosane/.config/bakeryhub/pgpass
```

値を表示しない照合用のコマンド：

```bash
psql -X -A -t -d 'service=approved_source' -c "SELECT current_database(), current_setting('server_version'), pg_size_pretty(pg_database_size(current_database()))"
```

ツールのバックアップ前検査はDB名、TCP接続先、18系サーバーとクライアント、public表の読取権限を確認する。**Webサービスの実効接続先との一致は管理画面で人が確認する**。ツールの `--web-db-match-confirmed` は、その照合を済ませたことを示す操作で、自動証明ではない。

## 5｜本番から直接暗号化して保存（最終確認後）

**このコマンドの直前で停止し、本番DBの実接続先、保存先、公開鍵、容量、取得時の負荷と通信、鍵の別保管、実データ取得の承認を確認する。** 隔離DBの準備はこの段階の必須条件ではない。

```bash
cd ~/tosane-works/store/bakery-hub
python3 production_backup_guard.py backup-encrypted --source-service approved_source --source-database bakery_db_4 --confirm-source bakery_db_4 --web-db-match-confirmed --recipient-file /home/tosane/bakeryhub-age-recipient.txt
```

内部では `pg_dump --format=custom` の標準出力をageへ直接渡す。途中失敗なら部分ファイルを除去する。完成名は `bakeryhub_YYYYMMDD_HHMMSS_ランダムID.dump.age`。上書きせず600で保存し、暗号文のサイズとSHA-256を表示する。平文dumpファイルは作らない。**この時点は取得済みであっても、復号・復元PASSではない。**

## 6｜暗号化ファイルを検証（鍵を用意した後）

出力された実際のパスとSHA-256を指定する。パスやSHA-256は秘密鍵ではないが、実バックアップをGitやチャットに添付しない。

```bash
python3 production_backup_guard.py verify-encrypted --identity-file /media/tosane/保護媒体/bakeryhub-age-identity.txt --sha256 確認した64桁のSHA256 /home/tosane/BakeryHub-Backup/database/実際のファイル名.dump.age
```

暗号文のサイズ・SHA-256を照合し、ageで全ストリームを復号、`pg_restore` でcustom形式と主要な表を確認する。復号データはプロセス間のパイプを通り、通常のディスクに平文dumpを作らない。成功しても**実際の復元は未検証**と記録する。破損・復号失敗・鍵紛失・認可不足はFAIL。

## 7｜独立PostgreSQL 18へ隔離復元（別途書込承認後）

G580のローカルで、本番と異なるサーバー／TCP接続先に、空の `isolated_restore` DBを用意する。復元先はWebから接続できないことを確認し、`approved_restore_target` のlibpq service設定を作る。G580上のPostgreSQL 18インストール・DB作成は、この段階で復元先への書込を承認してから行う。新しいRenderサービスは作らない。

**復元先名・空状態・鍵・実データ書込承認を確認した直後**に実行する。

```bash
python3 production_backup_guard.py restore-encrypted-approved --source-service approved_source --source-database bakery_db_4 --target-service approved_restore_target --target-database isolated_restore --confirm-target isolated_restore --identity-file /media/tosane/保護媒体/bakeryhub-age-identity.txt --sha256 確認した64桁のSHA256 /home/tosane/BakeryHub-Backup/database/実際のファイル名.dump.age
```

ツールは本番と隔離先の相違、両者18系、空状態、ハッシュと全復号ストリームを確認し、**隔離先だけ**へ1トランザクションで復元する。`--clean`・`--create` は使用しない。失敗時の再試行先は承認済みの別の空DBにする。**この成功表示だけでデータ照合PASSにしない。**

表名と全件数、Admin/Guest Dataset、日付別売上、会計・返金、写真のサイズ・SHA-256、商品とレシピ、スタッフ・シフト、天気履歴、Alembic revision、FK/UNIQUE/CHECK、トリガー、sequenceを[Phase 3の判定基準](production-db-backup-readiness.md#合格判定)に沿って比較する。氏名・パスワードハッシュ・写真の実バイトは記録しない。元DBがdump後も更新される場合、その時点差を調整せず単純な件数比較だけで合格にしない。IndexedDBにある未同期会計やDB外ファイルは対象外。

## 新しい作業ブランチをG580へ取得

PR公開後、G580の作業ツリーがクリーンであることを確認してから、GitHubのブランチを取得する。実PRのブランチ内容を確認し、**PRをmainへマージしない**。

```bash
cd ~/tosane-works
git fetch origin refs/heads/ops/v3-g580-encrypted-db-backup:refs/remotes/origin/ops/v3-g580-encrypted-db-backup
git switch -c ops/v3-g580-encrypted-db-backup --track origin/ops/v3-g580-encrypted-db-backup
```

すでに同名ブランチがある場合、コマンドを機械的に再実行せず状態を確認する。Draft PRで変更内容と限定CIを確認し、G580の実行環境と本番接続先が確定してから次の段階へ進む。

参考：[age公式](https://github.com/FiloSottile/age)、[PostgreSQL公式Ubuntu導入](https://www.postgresql.org/download/linux/ubuntu/)、[Render外部接続](https://render.com/docs/postgresql-creating-connecting)、[pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html)。
