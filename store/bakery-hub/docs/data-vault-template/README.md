# Bakery Hub Data Vault（Private専用・初期構成）

このテンプレートを `tosane932/bakeryhub-data-vault` の**Privateを確認したリポジトリ**へコピーします。ここに含まれるexamplesは架空の合成データのみ。GitHub保管庫が作成・検証済みであると示すファイルではありません。

商品名・価格・分類・写真・レシピ・材料・対応表のCSVを保存し、Git履歴から以前の設定を参照するための保管庫です。実際の保存物は `backups/<保存日時>/` に完成したexportフォルダごと置きます。自動pushはありません。アップロード前に内容・元Dataset・公開範囲を管理者が確認してください。

**禁止する保管物：** 実売上・会計・返金・価格監査履歴・日次売上、スタッフ個人情報、認証情報、PIN、セッション、未同期会計、DB全量dump、接続設定、秘密鍵。Privateでもこれらの保管先にはしません。

examplesにはproducts.csv・recipes.csv・ingredients.csv・product_recipe_links.csvとbackup-manifest.jsonがあります。UTF-8、セル先頭のアポストロフィは数式保護であり、独自CLIが読込時に取り除きます。写真を含む本来のexportはimages/も含み、元内容のSHA-256で照合します。架空例は写真なしです。

利用手順は[復元ガイド](docs/restore-guide.md)。CSVは会計の完全バックアップではなく、別途承認したDB全量バックアップも必要です。保管庫とは別の承認先にもコピーし、時々復元して確かめてください。
