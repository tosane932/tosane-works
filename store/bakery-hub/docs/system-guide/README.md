# Bakery Hub システムガイドの原稿・生成・検証

更新：2026-11-04 JST。基準main：`6f61e12c5927d31a0be31a5a1e2f89a15b36b859`。
対象：v3.2実装を含むコード。本番稼働状態の再監査・設定変更・公開操作は行っていません。

## 成果物と編集場所

- Web：`templates/system_overview.html`。初めて見る方向けの5章。現在地・目次・スクラバーは維持。
- 主原稿：[`guide.md`](guide.md)。全体像、機能、保存の往復、データと安全、UI/UX、テスト、運用復旧、改善と課題の8章。
- 付録：[`legacy-reference.md`](legacy-reference.md)。旧ページの処理ウォークスルー、機能別解説、実装でのPython使用箇所、共通技術、テスト、障害逆引きを移設。クイズは含めない。
- 機械検証用コード住所：[`code-addresses.json`](code-addresses.json)。旧マップのファイル・定義名・用途を保全。pytestで全定義の存在を照合。
- PDF（57ページ）：[`../../static/docs/bakery-hub-system-guide.pdf`](../../static/docs/bakery-hub-system-guide.pdf)。Webから `/static/docs/bakery-hub-system-guide.pdf` へ匿名でアクセス可能。

クイズ8問・Python基礎15問とその解答は撤廃しました。折りたたみの説明は原稿・PDFで展開済みです。旧9章の目次とクイズ専用テストは新仕様へ置き換えました。匿名閲覧・セキュリティヘッダー・DB/AI非呼出し・業務データ非表示・POST拒否・業務画面ナビゲーションの既存検証は維持しています。

## PDF再生成（本番DB・Flask・外部API不要）

アプリのルート `store/bakery-hub/` で実行します。資料用ツールは本番requirementsやDockerイメージへ追加しません。

```bash
python3 -m venv /tmp/bakery-guide-build
```

```bash
/tmp/bakery-guide-build/bin/pip install -r docs/system-guide/requirements-build.txt
```

Noto Sans JPの元フォントを一度取得します。以後は同じファイルを使いオフライン生成できます。SIL OFL 1.1、配布ライセンスは `FONT-LICENSE.txt`。生成PDFには使用文字のフォントを埋め込みます。

```bash
curl --fail --location 'https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf' --output /tmp/NotoSansJP.ttf
```

確認するSHA-256：`c2f3b4d463500a2ddcd3849cded1fceeb9fd6d1c32e6cbecd568453ba50fc68f`。
URLのmainが更新されても異なるフォントを無断採用せず、生成スクリプトが拒否します。ハッシュ不一致なら元フォントの版を確認し、必要な変更をレビューしてください。

```bash
sha256sum /tmp/NotoSansJP.ttf
```

```bash
/tmp/bakery-guide-build/bin/python docs/system-guide/build_pdf.py --font /tmp/NotoSansJP.ttf
```

`guide.md` → `legacy-reference.md` の順でA4縦PDFを生成します。可変フォントの400ウェイトを使用し、本文10.5pt・行送り18pt、表9pt・行送り15pt、目次・PDFしおり・ページ番号付き。図表の行は分断せず、表の見出しは繰り返します。コード抜粋は改行・字下げを保ちます。PDFメタデータの日付は再現性のため固定されており、資料更新日は表紙・フッターを参照してください。

```bash
pdftoppm -scale-to 1200 -png static/docs/bakery-hub-system-guide.pdf /tmp/bakery-guide-page
```

```bash
pdffonts static/docs/bakery-hub-system-guide.pdf
```

全ページのPNG、表・コード・目次・章の開始終了を確認してください。印刷はA4・実際のサイズを基本とします。実プリンター出力は今回未実施です。

## 調査・照合

開始時にmainのHEAD・clean tree・AGENTS.md・ルートとアプリのREADMEを確認。`/system-overview` はapp.pyのGETルートがテンプレートを返すだけで、ルート変更は不要でした。外部から旧章へ向けたハッシュリンクはリポジトリ内で見つかりませんでした。

| 内容 | 照合先 |
| --- | --- |
| 日次入力→保存→分析 | app.py / input_sales、dashboard、templates/input.html、dashboard.html |
| 会計と返金・再送・価格差・営業日 | register_routes.py / create_transaction、register_service.py / save_register_transaction |
| 商品と会計明細・Dataset制約 | models.py、register_products.py、register_images.py |
| Guest公開条件と制限 | guest_register.py、README、v3.2-guest-launch-readiness、phase5b記録 |
| スタッフ・仕込み・廃棄 | shop_staff_shifts.py、production_prep.py、waste_records.pyと関連pytest |
| セッション失効 | admin_sessions.py、AdminAuthSession、認証テスト |
| オフライン・再同期 | register_offline_service.py、register_offline_store.js、関連pytest・Node・E2Eコード |
| 起動と復旧 | Dockerfile、startup_guard.py、data_vault.py、production_backup_guard.py、既存runbook |
| 天気とニュース | current_weather.py、home_forecast.py、bakery_news.py、README |

公開状態は本番へ接続して確定したものではありません。Guest v3.2本番未公開、Admin正式レジNO-GOを維持します。READMEの廃棄ロス節には古い「Draft PR・本番未適用」が残り、後続revisionの復元報告と時点が混在しています。廃棄ロスは実装済みとし、Adminの本番利用可否は未確認としてWeb・PDFに明示しました。元READMEの古い運用記録を今回推測で書き換えていません。付録のGuest出勤予定と旧月次入力の説明はv3.2の条件に合わせて補正しました。

## ナビゲーションの保護と検証

既存コードの動作を調査し、通常スクロール、rAFによる現在章更新、現在章ボタンからの目次、Escape・閉じる・背景クリック、移動後の見出しフォーカス、実測スクロール余白、ResizeObserver、reduced-motion対応を確認しました。これは変更前のコード調査であり、変更前ブラウザ実行済みという意味ではありません。

scrubberは従来どおり900px以下・coarse pointer・高さ500px以上で表示し、PCでは現在地と目次を使います。ドラッグ・12px閾値・pointercancel・キー操作・1600ms表示時間は変更していません。唯一のロジック修正は固定9章条件をDOMの章数と目次数の一致へ変え、aria-valuemaxも章数に合わせるものです。最終章も見出し位置まで移動できるよう最小高さを設けました。

### 今回実行した結果

- pytest：システム概要・auth・authorization・Dataset認可の関連77件PASS。モデルの既存SQLAlchemy relationship警告3件あり。モデル変更は対象外。
- Node：`node --test tests_js/*.test.cjs`、既存63件PASS。
- 新E2E：`tests_e2e/system_overview.spec.cjs` に390/800/1280px、現在地・目次・フォーカス・背景閉じ・文字拡大・幅変更・reduced-motion・スクラバードラッグとキー・JS無効・Admin/Guest/PDFを追加。
- E2E実行は7件ともChrome起動時に失敗。Chromeが `socket() failed: Operation not permitted` で停止し、ページ操作に到達せず。Playwright標準ブラウザ取得も不完全なアーカイブで失敗し、別途取得したChromeでも環境の起動制限を確認。操作検証のPASSとはしない。
- PDF：pypdfで全ページの本文抽出、必要語・A4サイズを検証。Popplerで全ページをPNG化して一覧を目視し、日本語、目次、ページ番号、章・表のレイアウトを確認。日本語フォントの埋め込みとUnicode対応をpdffontsで確認。
- 本番・Render・migration・実Gemini・実データ変更：なし。

実行コマンド（アプリの通常テスト依存に加え、PDF検査にはpypdfが必要。CIではtestジョブだけに追加）：

```bash
pytest tests/test_system_overview.py tests/test_auth.py tests/test_authorization.py tests/test_dataset_authorization.py -q
```

```bash
node --test tests_js/*.test.cjs
```

```bash
npx playwright test --config=tests_e2e/playwright.config.cjs system_overview
```

## 判定と残課題

原稿・PDF・Web実装はレビュー可能ですが、スクロール操作のブラウザ検証が未完了のため完成・merge判断はNO-GOです。Draft PRでCIのChromium E2Eを確認し、スマホ・タブレット・PCの実表示と実機を確認してください。今回は隔離PostgreSQL・全pytest・実プリンター検証を実行していません。業務処理・モデル・migration・認証・Dataset・PWA保存領域・公開フラグは変更していません。

### PR #162での追加確認

GitHub Actions初回（66c88e2）では管理者セッションとPostgreSQL統合ジョブが成功。全pytestは1514 passed / 68 skipped / 1 failedで、失敗は旧 `#feature-pwa` の詳細解説をWebから読む別ファイルのテストでした。PWAの短い説明をWebに戻し、Manifest・Service Worker・Cache Storage・未同期保存の区別を移設先原稿でも同じ期待値で確認するよう修正しました。修正後の関連PWA＋概要pytestは46件PASSです。

追加のナビゲーションpytestでは、`test_app_action_controls_disable_selection_without_covering_content` の旧商品登録 `/` ケースがローカルで失敗しました。同じテスト選択を基準mainの独立worktreeでも実行し、同じ1 failed / 2 passedを再現しています。今回の概要変更による回帰とは区別し、業務画面・既存テストは変更していません。CI初回の全pytestではこのケースは成功していました。ローカルでの単独選択時の既存課題として記録します。
