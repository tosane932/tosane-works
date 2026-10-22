# 無料PostgreSQL期限前の保全と移行判断（Phase 3C／3E）

この資料は**計画と判断条件**であり、本番DB・Render設定を変更する実行承認ではない。正式レジ公開はNO-GO。暗号化取得とG580での隔離復元の実績・再実行手順は [G580の記録](g580-encrypted-db-backup.md) と [詳細照合](g580-restore-verification.md) を参照する。

## 現状と期限

2026-10-21にWorkがRenderの読み取り情報で `bakery-db-4` / `bakery_db_4` を確認した。Free、PostgreSQL major 18、状態available、expiry `2026-11-12T03:19:04Z`（**2026-11-12 12:19:04 JST**）。Webの実効接続先との一致は利用者のRender管理画面での確認に基づく。バージョンminor 18.6は利用者のG580での実測値。実行前に期限・状態・接続先を再確認する。10月21日21:41 JSTのバックアップはその時点までのデータだけである。

**2026-10-23 JSTの読取再確認：** 一意に識別できたRender Workspace内で対象DB `dpg-db6q6a0n74is738j0t80-a` は引き続きFree・`available`・major 18、期限表示も同じ。Web `bakeryhub`（`srv-d931ouuh2hms73d4au8g`）はmainのcommitで`autoDeploy=yes` / `autoDeployTrigger=commit`。この調査では秘密の接続URL・環境変数・本番SQLを取得していない。実接続先の現在の一致、minor版、実使用容量と最小有料プランの管理画面見積りは**NOT TESTED**（移行直前に再確認）。2026-10-22の再復元・詳細SQL完走は[利用者報告](g580-encrypted-db-backup.md)であり、バックアップ総合判定はPARTIAL。

[Render Free](https://render.com/docs/free)によると、Free PostgreSQLはWorkspaceに同時にactiveで1つ、容量1 GB、作成後30日で期限切れ、期限後は有料化しない限りアクセス不可、14日間の有料化猶予後に削除される。**「active」枠が空いた時点で必ず新Freeを作れる／期限切れ旧DBを残して新Freeを作れるとは確認していない。** 管理画面での作成可否が確定するまで旧DB削除を計画の前提にしない。期限後の無料dumpを前提にしない。Freeには[Render管理バックアップがない](https://render.com/docs/postgresql-backups)。[Renderの接続・作成案内](https://render.com/docs/postgresql-creating-connecting)には新規作成可能なPostgreSQL major version 13〜18の記載があるが、移行直前の表示と互換性を確認する。

## 継続保全（G580を起動する手動運用）

- 期限まで、**2〜3日ごと**および重要な売上・商品・スタッフ変更の後に新しい一意名の暗号化バックアップを取得する。10月21日分の次の目安は10月23〜24日（取得済みではない）。G580を起動できない日は未保護の更新が増えるため、次回起動時に優先実行する。業務側が許容する最大の損失期間を決め、必要なら間隔を短くする。自動バックアップは稼働していない。常駐サービスや有料ツールは導入しない。[毎回のチェックと空の記録様式](g580-encrypted-db-backup.md)を使用する。
- 毎回Webの実効接続先と対象DB、クライアント18系、空き容量、公開鍵を確認する。`backup-encrypted` の正常終了、サイズ、SHA-256、600/700の権限、`verify-encrypted` の全ストリーム・custom形式を記録する。失敗した取得は正常バックアップに数えず、元ファイルを上書きしない。
- G580の暗号文と独立した暗号文コピーのSHA-256を照合する。USBの秘密鍵と暗号化予備鍵を別管理し、鍵の再生成・内容表示はしない。少なくとも期限前の最終候補と、その後の定期的なサンプルについて隔離復元・件数・制約・重要リンクまで確認する。取得と実復元を別の合格欄に記録する。
- 保存先の空き容量、各ファイルの増加量、何世代を保持できるかを各取得前に点検する。削除や世代整理は別途バックアップ管理者が復元済みコピーと保持期間を決めて承認する。10月21日の実績ファイルも機械的に削除しない。
- 端末のIndexedDB内の未同期会計、DB外ファイル、Render環境変数は全量dumpの対象外。端末ごとに別途確認する。

## 移行選択肢とゲート

| 選択肢 | 期限・費用・停止時間 | 切戻し上の条件 |
|---|---|---|
| 現Freeを継続 | 現期限を超える継続方法ではない。期限まで利用し、定期・最終dumpを取得 | 期限内に旧DBが接続可能な間だけ旧DBへ戻せる可能性がある。切替後の新DBへの書込は別途再反映が必要 |
| 現DBの有料化 | [Renderの公開価格](https://render.com/pricing)では最小の有料Postgres computeは**$6/月**、ストレージ等は別料金になり得る。対象DBへの実際の料金と請求条件は管理画面で見積り・承認。計画停止を見込み、[プラン変更の停止](https://render.com/docs/postgresql-creating-connecting#changing-your-compute-plan)も確認 | 同じDBを保持できる案。無料へ戻せる保証はない。Paidでは[PITRとlogical export](https://render.com/docs/postgresql-backups)が利用可能だが、既存のageバックアップは継続する。課金操作は別承認 |
| 新しいFree DBへ引継ぎ | 同一WorkspaceでFree DBを二つactiveにできない。新Free作成可能条件が未確定のため、旧DB削除を伴うなら停止時間と切戻し不能リスクが大きい。Free 1 GB・30日ごとに期限。PG18は現在新規作成可能との公式記載 | 旧DBを先に削除すれば**旧DBへの切戻しは不可能**。期限失効後も旧DBには接続できず、猶予期間をdump窓にしない。作成可否と一時停止の条件を別工程で確認 |
| 他社PostgreSQLへ引継ぎ（比較のみ） | 例：[Neon Free](https://neon.com/blog/neon-free-plan-1-gb-per-project)は公表上1 GB/プロジェクト、[PG18対応](https://neon.com/blog/postgres-18)の案内がある。[Supabase Free](https://supabase.com/docs/guides/platform/billing-on-supabase)はDB 500 MB/プロジェクトで[低活動時の自動pause](https://supabase.com/docs/guides/platform/free-project-pausing)がある。いずれもRender Webとの外部TLS通信、容量、権限・拡張機能、接続数、無料枠継続条件を合成DBで検証していない | Render Free Webからの外部DB通信は[高トラフィック時の停止条件](https://render.com/docs/free#service-initiated-traffic-threshold)もある。接続遅延、停止・障害時の復旧、費用条件はNOT TESTED。登録・契約・新DB作成は行わない |

推奨は**現在のFree期限より十分早くバックアップを継続し、最終の暗号化dump・復号・隔離復元の成功を確保してから、停止窓と新DB条件を別途承認する**こと。新無料DBへの切替をこのPRで確定実行しない。期限当日の初回検証は避ける。期限直前には更新停止・端末未同期の確認、最終dumpと二重コピーの照合、隔離復元、移行先の空状態と独立性、復元・集計・認証・権限の照合、Web接続先切替とロールバック判定を一つの作業窓として計画する。旧DBと新DBのスナップショット時点をそろえられない場合、後日の件数差だけで不整合と判定しない。

**現時点の判断：** 費用を受け入れられるなら現DBの有料化は削除を挟まない候補。無料継続を優先する場合でも、削除が必須かを管理画面・サポートで確定するまで手を付けず、期限前に最終dumpを検証する。他社案は比較対象であって移行先確定ではない。具体的な作業とSTOP／切戻し条件は[移行当日のGate 0〜6](production-db-cutover-runbook.md)で分ける。

## 本番起動を止める条件

現在の `Dockerfile` は `flask db upgrade && python seed_demo.py && gunicorn ...` を起動時に実行する。`seed_demo.py` はAdminの**旧** `Product` または `DailySales` の存在で投入を止め、両方なければ旧デモ商品・日次売上を投入する。**新レジ商品だけの存在では抑止されない。** 10月21日の復元DBには `daily_sales` が104行あったが、新DBを通常起動して安全だと一般化できない。接続先を変更する前に、migrationの適用方法とseedの停止・制御を別PRで検証し、通常起動で意図しない書込が起きないことを審査する。今回はDockerfile、seed、Renderを変更しない。

| 合成DBの状態（Admin Dataset） | 現行 `seed_demo_data()` の分岐 | 移行時の判定 |
|---|---|---|
| 完全に空 | Admin Dataset自体がなければ例外。既存Admin Datasetのみで旧商品・売上が空なら7旧商品・7日×7の旧売上を投入 | 新DBへデモを自動投入すること自体を移行手順として許可しない |
| 旧Productあり、またはProductに紐づく旧DailySalesあり | いずれかを検出すればskip | 復元内容・Dataset境界の独立照合が必要 |
| RegisterProductのみ、または新商品とPrepRecipe／PrepIngredientのみ | 旧Product/旧DailySalesはゼロなのでseedを実行し得る | **誤投入のブロッカー**。旧商品7件と旧売上49件が混入し得る |
| 全量復元済み（10月21日の報告値） | 旧 `daily_sales` 104件の報告だけではAdmin所有と対応するProductの存在までは直接証明しない | 起動前にAdmin Datasetの旧Productの有無と件数を照合。通常起動の安全を推定しない |
| ロック・制約・commitの失敗 | seedの書込はcommit前の同じsessionで行い、SQLAlchemyErrorでrollback。事前の旧売上書込禁止検査が失敗してもrollback | ただし先行の`flask db upgrade`の適用はseed失敗で自動的に巻き戻らない。途中migration失敗や再起動を別検証 |

`get_admin_dataset()` は存在しないAdminをseed内で作らず、見つからなければ例外。`DailySales` はProductへjoinしAdminのProductで絞る。新RegisterProduct、レシピ、材料、写真、Guestの存在は停止判定に入らない。既存seedテストは空、旧商品あり、Guestのみ、再実行の基本動作を確認している。**Phase 3Eでは現行seedの本番挙動を変更しない。** 次Phaseの独立PRで、本番起動からseedを明示的に分離し、migrationを承認済みの一回の手順に移す設計を優先する。合成の「新商品だけ」「新商品＋レシピ」と復元済み相当の両方、seed失敗のrollback／migration失敗後の起動停止を試験し、デモ投入がないことを確認してからDB切替を審査する。mainのmergeだけでも`autoDeploy=yes`により起動CMDが走り得ることに注意する。

2026-10-23 Workの**合成SQLite**限定試験では、既存の `tests/test_seed_demo.py` と一時的な架空データの3ケースが計12件PASS。Adminの新RegisterProductのみ、または新商品＋レシピ＋材料のみのケースで、それぞれ旧商品7件・旧売上49件の投入を再現し、旧Productがあるケースでは抑止された。別の一時試験1件ではcommit時の合成例外後にProduct/DailySalesがいずれも0のまま、rollbackを確認した。計13件PASS。一時試験ファイルは検証後に除去。これは現行挙動の再現であり、望ましい移行動作の合格ではない。PostgreSQL上の新DB通常起動、migration失敗時、実復元済みDBでの起動はこの試験では**NOT TESTED**。

もう一つのブロッカーとして、既存の `restore-encrypted-approved` は暗号文を復号して復元する前と直前に `preflight` で**生きている元DB**へ接続する。無料期限切れで元DBに接続できなくなる場合、暗号文を持っていても現行CLIのままでは新DBへ復元できない。元DBが利用可能な間のG580隔離復元には成功済みだが、期限後の移行には元DBなしでも元DB識別記録・暗号文の真正性・空の復元先・誤接続拒否を検証する専用復元経路を別PRで追加し、合成PG18で試す。**既存ガードを省略して本番へ即席restoreしない。** 詳細は[当日のSTOPとGate](production-db-cutover-runbook.md)。

## 端末と三つの判定

スマホ・タブレット別にPWAの未同期件数・端末元キー・金額を、既存の未同期保護画面と運用記録で確認する。同期やIndexedDB削除を勝手に行わない。切替前に更新を止め、既存の二重送信防止で元キーが保持されることを試験し、承認した同期後にDBの件数・金額と突合する。再起動・オフライン・途中失敗時には端末データを保持し、相違があれば接続先切替を停止する。旧DBが使えるか、切替後の書込がどこにあるかを確認して切戻しを決める。

1. **DB全量バックアップの安全性**：暗号化、SHA-256、復号、隔離復元、同時点証拠または範囲を明示した詳細照合を判定。10月21日分は10月22日に詳細SQLが利用者報告で完走したが、取得時点の独立した全表照合証拠はなく総合PARTIAL。
2. **DB移行の準備完了**：上記に加え、最終dumpの取得窓、新DB作成条件、起動時migration/seed対策、停止・切戻し手順、端末未同期の扱い、費用承認を判定。現在は未完了。
3. **正式レジ公開の準備完了**：独立の運用・監査・未同期会計検証で判断。バックアップや移行の合格から自動的にGOへ変更しない。現在は**NO-GO**。

本計画は作成・削除・料金変更・接続先切替・本番migrationを実施しない。最終移行の直前にRender公式条件、期限、費用、ダウンタイムと戻し先を再評価する。
