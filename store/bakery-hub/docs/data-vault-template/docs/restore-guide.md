# 保存・復元の短い手順

1. 所有者がtosane932、名前がbakeryhub-data-vault、公開範囲がPrivateであることをGitHubで確認します。接続から存在・非公開設定を確認できない場合は保存を止めます。
2. tosane-worksの `store/bakery-hub/data_vault.py export` で、承認した元Admin Datasetの商品設定をローカルの新規フォルダへ書き出します。接続URLは権限0600のファイルで指定し、コマンド引数に直接書きません。本番exportは別途承認が必要です。
3. `python data_vault.py verify --folder <保存先>` で検査し、実売上・秘密情報など対象外の情報が混ざっていないか確認します。
4. 確認済みフォルダをこのPrivate保管庫の `backups/<日時>/` へコピーしてcommitします。前回分を上書きせず、保管日時・担当者・対象を残します。GitHubへの自動push機能はありません。
5. 復元先は承認した別DBの空Admin Dataset。`data_vault.py import` の既定dry-runで確認し、許可後だけ `--apply --confirm-dataset <復元先UUID>` を付けます。内部IDは採番し直され、途中エラーではrollbackします。
6. 元との件数・商品値・写真SHA-256・レシピ・材料・対応を確認します。DB接続先の本番切替は別の管理者承認です。

CLIの全引数・制限・旧月次Product由来レシピの扱いは、tosane-worksの `store/bakery-hub/docs/data-vault-guide.md` を参照してください。レシピ画像は復元先コードに同一の承認素材が必要です。examplesは全て架空データです。

**CSVは商品設定のみです。** 過去会計・価格履歴・日次売上・未同期会計は復元しません。確定会計等はDB全量バックアップで別に保全します。無料DBの期限前に取得し、取得後の編集を反映する最終export時点と更新停止窓を記録します。正式レジはNO-GOのままです。
