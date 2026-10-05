# AGENTS.md

このファイルは、Codexの作業を必要以上に制限するためのものではありません。
データ、Git履歴、本番環境を守りながら、安全かつ確実に作業を完了するための共通ルールです。

## 基本思想

このプロジェクトでは「安全第一」を最優先します。

人の注意力だけに頼らず、調査、テスト、差分確認、Git確認、明示的な許可によって、事故を起こしにくい作業手順を維持してください。

## 絶対遵守事項

### 絶対禁止

- `git push --force`など、Git履歴を書き換える操作
- secret、API key、passwordなどの秘密情報の値を、コード、commit、ターミナル出力、ログ、報告文へ表示・記録すること
- 未確認の既存変更や未追跡ファイルを勝手に削除、上書き、破棄すること
- 既存pytestを削除、skip、xfail化したり、assertionやfixtureを不当に弱めてGREENにすること
- 実行していない確認を実行済みとして報告すること

### 明示的な許可が必要

以下は、ユーザーから対象と操作について明示的な許可がない限り行わないでください。

- 本番DBの変更、削除、migration適用
- Render本番環境の変更、deploy操作
- 実Gemini APIの呼び出し
- `main`への直接commit
- merge
- destructiveなDB操作やその他の本番操作

不明な既存差分や未追跡ファイルを発見した場合は、まずRead-Onlyで内容と影響範囲を確認してください。勝手に削除、上書き、復元せず、ユーザーへ報告してください。

## 標準作業手順

機能追加、不具合修正、安全性改善では、原則として次の順序で進めてください。

1. 現在のbranch、HEAD、working treeを確認する
2. 関連コード、テスト、migrationをRead-Onlyで調査する
3. 既存保証と影響範囲を整理する
4. 必要な場合は、production codeより先にREDテストを作成して想定した理由で失敗することを確認する
5. production codeを目的達成に必要な最小範囲で変更する
6. 対象pytestを実行する
7. 関連pytestを実行する
8. 必要に応じて全pytestを実行する
9. `git diff --check`を実行する
10. `git diff`と`git status`で想定外の変更がないことを確認する
11. 実施結果、未検証範囲、残る懸念を報告する

調査だけで十分な場合や、テスト追加が不適切な作業では、機械的にREDテストを追加しないでください。

## Bakery Hubのシステム概要

- Bakery Hub（`store/bakery-hub/`）に新機能を追加した場合、または既存機能の主要な仕様・処理フローを変更した場合は、作業完了前に `store/bakery-hub/templates/system_overview.html` への反映要否を必ず確認する
- 反映が必要な場合は、実際のコード・テストを確認したうえで、機能説明、使用技術（Python / Flask / SQLAlchemy / JavaScript / pytest）、処理フロー、基礎概念など、変更に関係する箇所を現在の実装に合わせて更新する
- 軽微な文言修正や見た目だけの変更など、学習内容やシステム理解に影響しない変更では、無理に追記しない

## Git運用

- 原則としてfeatureまたはchore branchで作業する
- `main`へ直接commitしない
- commit、push、PR作成、mergeは、現在の依頼で許可された範囲を確認してから行う
- 明示的に許可されていない場合は、作業完了後に勝手に次工程へ進まない
- commit前に変更ファイルとworking treeを確認する
- push前にbranchとHEADを確認する
- PR作成時はbase branchとhead branchを確認する
- merge前にGitHub Actionsの結果を確認する

## Database / migration

DB schema変更が必要な場合は、作業前に次を確認してください。

- 既存migration head
- `down_revision`
- 対象テーブル
- upgrade内容
- downgrade内容
- 既存データへの影響

Dataset、Product、DailySalesなどの業務データを扱うmigrationでは、件数、識別子、関連データ、NULL、外部キー、DB制約を確認し、既存データの保全を特に重視してください。

明示的な許可なしに、本番DBへmigrationを適用しないでください。

## Guest Demo

Guest Demoに関する変更では、特に次を守ってください。

- AdminとGuestのDataset境界を越えない
- Guest AとGuest Bのデータを混在させない
- Product、DailySales、集計、API、AI入力のqueryを、認証済み利用者に許可されたDatasetへ限定する
- URL、form、JSON、query parameter、sessionなどの外部入力をDataset認可の根拠にしない
- Guest cleanupでAdmin Dataset、有効なGuest、別Guestのデータを削除しない
- Guest期限、AI利用制限、Guest作成制限を迂回できる設計にしない
- 認証、認可、CSRF、XSSに関する既存保証を弱めない
- Guest関連処理の不正入力、欠損、DB障害、競合では、可能な限りfail-closedにする

## テスト

pytestは件数だけでなく、「何を守るGREENなのか」を重視してください。

特に次の観点を確認してください。

- 境界値
- 不正入力
- 認証、認可の迂回
- Dataset越境
- rollbackとatomic性
- migrationのupgrade、downgrade、データ保全
- DB制約
- 競合と同時request
- failure path

## 回答・説明スタイル

- 日本語で回答する
- 硬すぎる作業報告調ではなく、柔らかく自然で読みやすい文章で説明する
- 結論や重要な点を先に伝え、その後に理由や詳細を説明する
- 変更内容だけでなく、なぜその変更が必要なのかも説明する
- 専門用語は使用してよいが、文脈だけでは分かりにくい場合は簡潔に補足する
- コード変更では、原因、変更内容、影響範囲を区別して説明する
- 問題がない場合は問題がないことを明確に伝え、問題がある場合は曖昧にせず具体的に指摘する
- 箇条書きを過剰に増やさず、内容に応じて自然な文章と使い分ける
- 必要以上に堅い定型文や機械的な作業報告だけで回答しない

## 作業終了時の確認

終了報告では、最低限次を確認して報告してください。

- branch
- HEAD
- working tree
- 実行したpytestと結果
- `git diff --check`の結果
- 変更ファイル
- commit、push、PR作成、mergeを行ったか
- 本番DB、Render、実Gemini APIへ触れたか
- 未検証範囲
- 残る懸念

過去に記録された結果と、現在の作業で新たに確認した結果を区別してください。
