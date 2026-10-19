# Phase 7 読取再監査・公開ブロッカー（2026-10-20 JST）

**判定：NO-GO。** 対象は正式レジの公開準備。PR #130のUIを再調査・再実装・再検証しない。タブレット全画面UIは今回のユーザー報告で合格と記録する。保存・オフライン復帰の実機合格とは区別する。

## 今回確認した証拠

- 基準main：`97ae495f27f4666482fcd5ca3ffca81dfa73a9a0`。AGENTS.md、切替手順書、設計書、価格確認票、端末試験票、監査・商品登録コードを確認。
- 2026-10-20 05:41〜05:43 JSTにRender連携でサービス詳細・最新deploy・DBメタデータとSELECT結果を取得。SQLは連携が読み取り専用transactionで実行し、`transaction_read_only=on`も結果で確認した。
- 読み取り先は `bakery-db-4`（`dpg-db6q6a0n74is738j0t80-a`）。**Webの実効DATABASE_URLとの一致は証明できていない。本書のDB件数を確定した本番件数と呼ばない。** 同じworkspace・region・期待する件数であることは接続先同定の代わりにならない。
- 2026-10-18の監査値は比較資料のみ。下記の同じ件数は今回再取得した値。過去のCIや合成DB復元の成功も、本番バックアップ成功へ転記しない。

### WebサービスとDBの識別

| 対象 | 今回の読取結果 | なお確認できないこと |
|---|---|---|
| Web `bakeryhub` | `srv-d931ouuh2hms73d4au8g`、My Workspace、Singapore、Free、Docker、main、On Commit自動deploy、設定上1インスタンス。root `store/bakery-hub`、Docker command override空欄 | 実効DATABASE_URL、各機能フラグ、LOCAL_DEVELOPMENT、実効Cookie設定、全稼働ワーカーの版 |
| 最新deploy | `dep-dbb6aql9fdbs73cljktg`、`live`、SHA `97ae495f27f4666482fcd5ca3ffca81dfa73a9a0`、完了2026-10-20 03:47:32 JST | liveはDB同定・正式レジ公開の証拠ではない |
| DB `bakery-db-4` | available、Free、PostgreSQL 18.6、DB名 `bakery_db_4`、約9.79 MB（9,787,071 bytes）、表示期限2026-11-12 12:19:04 JST | Web接続先との同一性、承認済みバックアップの有無・保管場所 |
| 起動ログの限定読取 | 対象deployの18:46:02〜18:49:00 UTCにPostgresqlImplと既存データによるdemo seed skipを確認。Running upgradeの一致なし | この時間窓のログだけで、実効接続先・全履歴・全ワーカーを証明しない |

サービス詳細のAPIには環境変数の読取項目がなく、現在の連携に環境変数GET／Webシェルの手段がない。Free Webは公式仕様上Shellアクセス非対応。設定を変更して調査しない。

### DBスキーマ（候補DBでの実測）

DB時刻2026-10-19 20:41:41.895986 UTC（JST翌05:41:41）にrevision `b6d2e9f4a107`。リポジトリの単一headと一致。20:42:22.088569 UTCには以下を読み取った。

- `bread_kind`・`product_class`・`photo_webp`・`revision`の4列あり。
- 商品・価格履歴2本・会計明細・復旧明細の5価格CHECKが100,000円上限、すべてvalidated。
- 価格履歴・復旧明細・復旧イベントの変更防止トリガー3本がenabled。
- publicの未検証制約0件。これは制約の存在・設定を確認した結果であり、本番へ不正行を書いて拒否を試した結果ではない。

**候補DBについてmigration不足の疑いは今回解消。** 本番へのmigration操作は行わない。Webの実効接続先が一致して初めて、そのサービスのDB Gateへ採用できる。

### 商品・売上・会計・復旧（同一SELECT内の集計）

DB時刻2026-10-19 20:42:06.109821 UTC（JST翌05:42:06）。Admin Datasetだけに限定し、Guest行と混ぜていない。Dataset UUID・会計キー・個人情報・秘密情報を出力していない。

| 項目 | 今回の結果 |
|---|---:|
| Dataset数（Admin／Guest） | 1／1（後続の業務集計はAdminのみ） |
| 旧Product（全件／有効） | 15／14 |
| 旧DailySales | 99行、2026-10-01〜2026-10-15、数量2,590 |
| 旧商品の現在価格で算出した推計 | 633,670円（当時の実売価・現金一致を保証しない） |
| レジ商品／販売可能な0円超商品 | 0／0 |
| アップロード写真／カタログ画像／価格履歴 | 0／0／0 |
| 切替行／切替承認 | 0／0 |
| 会計（販売／返金）／会計明細 | 0（0／0）／0 |
| ヘッダー明細不一致／旧新同日重複 | 0／0（新会計がない状態での結果） |
| 復旧案件／未解決案件／復旧明細／復旧イベント | 0／0／0／0 |
| オフライン許可／仕込み対応 | 0／0 |

サーバー0件は端末IndexedDBの0件を証明しない。全端末・全ブラウザプロファイルの照合は引き続き必要。

## 公開Gateの再判定

PASSは表に明記した範囲の新規観測のみ。FAILは現在の不充足、NOT TESTEDは証拠なし。条件付きPASSを公開許可に読み替えない。

| Gate | 今回 | 証拠・残条件 |
|---|---|---|
| Webの現行リリース | PASS | 上記deployがlive。正式レジの公開フラグの確認とは別 |
| WebとDBの実接続先一致 | NOT TESTED | 実効DATABASE_URLを権限者がDashboardで照合する必要あり |
| 候補DBのrevision・価格制約・監査保護 | PASS | 今回の読取結果。Web接続先確定後に本番Gateへ反映 |
| 必要な販売商品の存在 | FAIL | 候補DBの販売可能商品0件。18候補全部の登録は不要 |
| 店舗の実売価・写真・分類・販売状態の承認 | NOT TESTED | 店舗価格表とID別の承認記録がない |
| 候補DBの未解決サーバー復旧案件・会計不一致 | PASS | 今回0件。ただし端末照合・現金照合は別 |
| DBの継続保全 | FAIL | Free・期限あり、管理バックアップなし。承認済みの継続保全方式未確定 |
| 承認済み本番バックアップ・別DB復元 | NOT TESTED | 今回は禁止範囲。取得・復元を一切実行していない |
| タブレット全画面UI | PASS（ユーザー報告） | PR #130合格との今回の申告。私による再試験は行わない |
| 正式保存・PIN・通信断・再起動・再同期の実機試験 | NOT TESTED | 独立したHTTPS検証環境と端末ごとの証拠が必要 |
| 全端末キューとサーバーのキー照合 | NOT TESTED | サーバーだけでは判定不可 |
| 本番の実効フラグ・認証設定・旧ワーカー排除 | NOT TESTED | コード既定値や1インスタンス設定だけで合格にしない |
| 現金／帳簿照合・切替日・最終管理者承認 | NOT TESTED | 予定日を勝手に設定しない |

## 次に必要な権限・管理者操作

### 1. 実接続先と実効設定の照合（読み取りのみ）

Renderの対象WebサービスのEnvironmentでDATABASE_URLのホスト・DB名をDBのInfoと**画面内で**照合する。環境グループの参照と最新live deployへの反映も確認する。接続文字列全体、認証情報、画面の秘密値をチャット・PR・写真へ貼らない。

記録するのは「確認者・JST・Web ID・一致したDB ID／不一致・対象deploy ID」だけ。Adminとして実アプリの旧売上件数と最終日も読み取り画面で照合し、Dataset対応を確認する。コードはDATABASE_URL未設定ならSQLiteへ退避するため、PostgresqlImplのログだけでDBサービスを同定しない。

同じ保護された画面内でLOCAL_DEVELOPMENTが無効、正式PRODUCTION／CUTOVER／SAVE等が意図せず有効でないこと、必要なREADINESS等の現設定、PUBLIC_DEMOの既存設定を確認する。秘密値以外の有効／無効・確認日時だけ記録し、今回ON/OFFを変更しない。Cookieはコード上LOCAL_DEVELOPMENT無効ならSecure=True、HttpOnly=True、SameSite=Laxだが、稼働設定の確認は未実施。

### 2. バックアップ・別DB復元の実施条件（未承認・未実施）

| 先に確定するもの | 必要な記録・停止条件 |
|---|---|
| 取得元 | Web接続先と同定済みのDB ID、対象revision、管理者、作業窓、取得に必要な接続権限。異なる／未確定なら停止 |
| 保管 | 暗号化・アクセス制限された保管先、鍵管理者、保持期限・廃棄方法、取得実行者の明示承認。Git／一般公開領域へダンプを置かない |
| 継続保全 | Free期限への対応、バックアップ頻度・担当・失敗通知・復元頻度・許容損失。料金やプラン変更は別途承認 |
| クライアント | 現DBはPostgreSQL 18.6。少なくとも18系の対応pg_dumpを用意し、版を記録。秘密値は保護されたlibpq設定で扱う |
| 復元先 | 本番と異なる空の隔離DB、18系等の承認した互換版、ID・費用・保管期間・ネットワーク・権限・廃棄の承認。公開アプリに接続しない |
| 合格証拠 | dump／restoreの終了コード、SHA-256、サイズ、全表件数・revision・日別数量／推計・会計と明細・価格履歴・写真・FK／UNIQUE／CHECK・追記保護・sequence整合。制約拒否の書込試験は隔離復元先だけ |

既存の `tests/test_register_backup_restore_postgresql.py` は合成データだけで実スキーマのdump・別DBrestore・照合・制約拒否を試す手法検証。CIはPostgreSQL 16であり、18.6の実本番ダンプ復元の証拠ではない。本番データを使う試験は上記が承認されるまで開始しない。`pg_restore --list`だけでもPASSにしない。実行例・停止手順は[切替手順書](v3-register-cutover-runbook.md)を参照。

### 3. 商品と実機試験の準備

- [価格確認票](v3-register-price-approval.md)を現行仕様へ修正した。GETで商品を作らず、単価0〜100,000円、18候補は任意。実店舗が扱う商品のID・実売価・販売状態・写真・分類とレシピreview理由を先に承認する。本番登録は別途承認が必要。
- [実機試験票](v3-register-device-test-sheet.md)に開始前の環境同定欄と5時点の照合手順を追加した。専用HTTPSオリジン・独立DB・秘密鍵・Admin・費用・期間・アクセス制限が未確定。Free WebのShellや非保存デモで正式保存試験を代替しない。
- 端末準備→販売3件＋返金1件→通信断→ブラウザ／電源再起動→PIN解除→再接続→元キー同期→再照合を各端末で実施する。端末原本を削除せず、金額・営業日・元キーが一致し、サーバー側が各1件から増えないことを確認する。今回実機試験は未実施。

## 今回減らした未解決事項と限界

候補DBの現行revision・商品統合列・価格CHECK・監査保護と各件数の「現在不明」を実測で減らした。価格確認票の危険な旧手順（GET初期化、500円上限、18品必須）、切替手順の旧head、UI合格と保存試験未完了の混同を修正した。DB同定・承認・実機環境は権限者の操作が必要な未解決条件であり、文書整備だけでPASSにしていない。

変更は監査資料のみ。DB／migration／認証／会計保存／UI／PWA／Render設定を変更しない。監査は既存フローの説明補正で、システム概要へ新機能や主要フローの追加は不要と判断した。NO-GOを維持する。

## 今回実施した検証と反映方針

ローカルの隔離SQLiteで `test_register_cutover_readiness.py`、`test_register_production_readiness.py`、`test_unified_products.py`、`test_register_product_routes.py` の27件が成功。監査の読取専用性、切替の証拠要求／rollback、Dataset境界、商品GETが書き込まないこと、確認付き候補追加、価格上限、18候補任意を確認した。既存のシフトrelationshipに関するSAWarningが3件あり、この監査では変更しない。全pytest・実機試験・本番バックアップ復元はローカル実行していない。

文書6ファイルの通常PRとし、CI結果はPRのChecksで記録する。**mainへのmergeは保留する。** 設定上On Commit自動deployと起動時 `flask db upgrade` がある一方、実効接続先と本番backupが未確定のため、今回禁止されている本番migration適用を誘発しないようにする。新しいmigrationは含まないが、対象DBが確定していない以上、起動コマンドが変更を適用しないと推測で断定しない。DB同定とdeploy時影響を確認してから既存の許可条件で再判断する。

## 参照

2026-10-20に公式資料を再取得：Render [Free](https://render.com/docs/free)（Free Postgres期限・管理backupなし、Free Web Shell非対応）、[Recovery and Backups](https://render.com/docs/postgresql-backups)（有料DBの復旧機能・論理backup）。プラン変更・backup取得・復元を今回実施した記録ではない。
