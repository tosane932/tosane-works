# Web起動とDB保守の分離（Phase 3F-A）

通常のDocker起動は `python startup_guard.py && exec gunicorn ...` です。ガードはAlembicの単一head、DBの `alembic_version`、モデルの全テーブルと列の存在を読み取りで照合し、不一致・接続失敗時にはWebを起動しません。migrationもデモ投入も自動では実行しません。既存DBのrevisionが現行コードと異なるままRenderが自動デプロイすると、サービスが停止する可能性があります。**マージ前に本番DBのrevision、事前バックアップ、停止時間、保守手順を確認し、別途本番適用を承認してください。** このPR自体は本番操作をしません。

## 新しいローカル開発DBだけを準備する

`docker compose up -d db` で開発用PostgreSQL 16を起動し、接続先が**開発DB**であることを確認します。`docker compose run --rm --no-deps web flask db current` で現revisionを確認し、開発DBへの書込を明示的に決めてから `docker compose run --rm --no-deps web flask db upgrade` を一度だけ実行します。`flask db current` と `flask db heads` が一致したら `docker compose up -d web` を実行します。デモが必要な隔離開発DBでだけ、Admin Datasetと既存データを確認した上で `docker compose run --rm --no-deps web python seed_demo.py` を明示的に実行します。新RegisterProductだけがあるDBにこのseedを実行すると旧デモ商品・売上が追加され得るため、復元済みDBには実行しません。

## 保守時の停止条件

本番migrationは通常起動と独立した承認操作です。接続先DB、現revision、コードの期待head、変更内容、バックアップの復元可能性と停止時間を別々に確認してください。意図しない接続先、想定外revision、migration失敗があれば停止します。失敗後は自動再実行やWeb公開へ進めず、適用済みのDDLとrevisionを調査します。seedは本番では実行しません。Renderの本番環境変数やデプロイ設定はこのPRで変更しません。

ガードはrevisionとモデルのテーブル・列の存在を確認します。制約や全データの完全な一致を証明する検査ではありません。DB内の起動後リクエストによる通常業務書込やGuestの利用は既存の認証・認可に従います。
