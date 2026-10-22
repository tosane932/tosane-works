# 元DB接続不能時の隔離復元（Phase 3F-B）

`restore-encrypted-offline-approved` は、元DBが期限切れ等で接続できない場合の**別経路**です。既存の `restore-encrypted-approved` は変更しません。G580の実バックアップではまだ実行していません。実行には移行先、停止時間、原本と承認記録、独立DBへの書込について別途承認が必要です。正式レジはNO-GOです。

## 承認記録

暗号文とは別の所有者管理ディレクトリ（0700）にJSON（0600）を保管します。秘密鍵、パスワード、DATABASE_URL、個人情報は書きません。以下の値は**合成例**です。

```json
{
  "backup_id": "bakeryhub_20261021_214150_271fcbe5bdc0.dump.age",
  "captured_at": "2026-10-21T21:41:50+09:00",
  "source_database": "synthetic_source", "source_service": "ci_source",
  "source_host": "source.example.test", "source_address": "192.0.2.10",
  "source_port": 5432, "source_server_id": "synthetic-source-id",
  "postgres_major": 18, "encrypted_size": 123,
  "encrypted_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "target_service": "ci_target", "target_database": "isolated_restore",
  "target_host": "127.0.0.1", "target_address": "127.0.0.1",
  "target_port": 5432, "target_connect_port": 5433,
  "target_server_id": "synthetic-target-id",
  "approved_at": "2026-10-22T09:00:00+09:00",
  "approval_reference": "local-approved-ticket-1"
}
```

`target_connect_port` はserviceファイルから接続するポート、`target_port` と `target_address` はDBサーバーが返す内部アドレスと待受ポートです。Dockerのポート転送では両者が異なり得ます。所有者が取得当時の独立したRender管理画面記録・バックアップ実行記録とハッシュ、サイズ、発表済みの監査記録を照合し、接続先識別子と承認日時を記入します。**過去の値を推測して記録を作らないでください。** 特に2026年10月21日の原本にはこの形式の承認記録がなく、当時のホスト・アドレス等を独立に確認できない場合はSTOPです。ハッシュ一致は暗号文の一致であり、取得元の真正性を単独で証明しません。JSONファイルの権限は改変防止の暗号署名ではありません。承認記録の保護と作成者の確認が必要です。

## 実行前のゲート

G580で `PGSERVICEFILE` と `PGPASSFILE` を既存の保護された設定へ向け、復元先serviceのhost・port・dbnameを承認記録と照合します。元DB用serviceは復元先に指定しません。隔離サーバーが独立し、外部公開なし、PostgreSQL 18、空DB、復元先の所有者権限があり、他の接続セッションがないことを確認します。Dockerを再作成した場合はIPを再同定します。ホスト側で一時DBの内容がスワップへ書かれる可能性があるため、既存の [G580復元手順](g580-restore-verification.md) のスワップ停止と後片付けを適用します。

コマンド形式（実値は端末内だけで設定し、履歴やPRに秘密情報を載せない）:

```bash
python production_backup_guard.py restore-encrypted-offline-approved \
  --approval-record /protected/approved.json \
  --identity-file /mounted-encrypted-usb/keys/identity.txt \
  --confirm-source synthetic_source --confirm-target isolated_restore \
  --confirm-backup-id bakeryhub_20261021_214150_271fcbe5bdc0.dump.age \
  /home/tosane/BakeryHub-Backup/database/bakeryhub_20261021_214150_271fcbe5bdc0.dump.age
```

ガードは承認記録、暗号文のSHA-256とサイズ、age復号とcustom archiveの完全ストリーム、必要なテーブルを確認します。復元先serviceのhost・port・dbname、接続先の実IP/port、DB名、PG18、空schema、他セッション不在、所有権を検査し、書込直前にも再検査します。`pg_restore --single-transaction --exit-on-error --no-owner --no-acl` で標準入力から復元し、平文dumpをディスクに作りません。失敗時は成功扱いせず、**新しい空DBと再承認**を必要とします。`--clean` と `--create` は使用しません。

同時書込は、検査直後に別プロセスが接続する競合が理論上残ります。復元先は他プロセスからアクセス不能の隔離ネットワークで運用し、他の管理者作業を止めてください。接続先の独立性、承認記録の由来、元DBの歴史的な識別を確認できなければSTOPです。元DBへ接続できないため、その時点の元DBとのライブ比較や取得時点の全表完全一致は保証しません。復元後は [詳細照合SQL](g580-restore-verification.md) で内部整合性を検証してください。
