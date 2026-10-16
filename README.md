# Tosane Works

![Tosane Works](img/tosane-works-banner.png)

**現場で感じた課題や不便を、Webアプリとして形にしていくポートフォリオです。**

物流・店舗業務・情報収集など、異なる分野で開発したWebアプリケーションをまとめています。

[🌐 Web Portfolio](https://bakery-salesdata.onrender.com/)

---

## 🏪 Store

### 🥐 Bakery Hub

![Bakery Hub](img/bakery-hub-banner.png)

標準18商品の月次管理・履歴Catalogと日次売上・分析に加え、店舗ホーム、スケジュール、スタッフ／シフト、3ステップの仕込み管理、天気記録、材料発注・メモ・タスク、必要時のAIアドバイスを一元化したWebアプリケーションです。

**Bakery Hub v3.0 の写真タッチ式レジは開発・切替前監査まで実装済みですが、正式な本番レジは未公開です。** 現在の通常の売上入力は従来の日次入力方式です。

**Python / Flask / PostgreSQL / Docker / pytest / GitHub Actions / Gemini API**

#### PC

<p align="center">
  <img src="demo/pc_dashboard.jpg" alt="Bakery Hub Dashboard" width="900">
</p>

#### Smartphone

<p align="center">
  <img src="demo/mobile_order.jpg" alt="材料発注" width="32%">
  <img src="demo/mobile_memo.jpg" alt="店舗メモ" width="32%">
  <img src="demo/mobile_task.jpg" alt="タスク管理" width="32%">
</p>

[▶ Live Demo](https://bakery-salesdata.onrender.com/) ・ [📖 詳細README](store/bakery-hub/README.md) ・ [📘 公開システム概要](https://bakery-salesdata.onrender.com/system-overview)

---

## 📰 Information

### 🕊 Puoppo

![Puoppo](img/puoppo-banner.png)

公式RSSから関連記事を収集し、Gemini APIで要約・分析する情報収集Webアプリケーションです。

**Python / Flask / SQLite / Gemini API / Docker**

#### Screenshots

<p align="center">
  <img src="demo/puoppo_search.jpg" alt="Puoppo検索画面" width="48%">
  <img src="demo/puoppo_analysis.jpg" alt="Puoppo AI分析結果" width="48%">
</p>

[▶ Live Demo](https://puoppo.onrender.com/) ・ [📖 詳細README](information/puoppo/README.md)

---

## 🚛 Logistics

### Driver Personality Test

![Driver Personality Test](img/dpt-banner.png)

物流現場で起こり得る判断場面を全50問の診断形式へ落とし込み、運転傾向を可視化するブラウザ完結型Webアプリケーションです。

**JavaScript / HTML / CSS / sql.js / GitHub Pages**

#### Screenshot

<p align="center">
  <img src="demo/dpt.jpg" alt="Driver Personality Test" width="48%">
</p>

[▶ Live Demo](https://tosane932.github.io/tosane-works/logistics/driver-personality-test/) ・ [📖 詳細README](logistics/driver-personality-test/README.md)

---

## Repository Structure

```text
Tosane Works
├── Store
│   └── Bakery Hub
├── Information
│   └── Puoppo
└── Logistics
    └── Driver Personality Test
```

各プロダクトは、依存関係・テスト・Docker・デプロイ設定などを可能な限り独立して管理しています。

---

## 🆕 Bakery Hubの開発状況（2026年10月17日時点）

- **店舗運営**：スケジュール・スタッフ／シフト・仕込み3ステップ・ホームと売上の天気記録を追加。
- **写真タッチ式レジ v3.0（PR #107〜#118）**：継続商品マスタ、会計保存・返金、IndexedDBの未同期キュー、旧新売上の排他集計、商品管理、画像選択、仕込み販売数の参考表示、Admin専用非保存デモ、切替前監査と保存状況照会まで段階実装。
- **公開上の区別**：本番レジ・切替日・Renderの本番用フラグは未切替。非保存デモと監査画面も別途明示的な有効化が必要。Guest Demoは引き続き従来の日次入力方式を体験できます。
- **安全な切替の残条件**：過去日付の未同期会計の復旧根拠、価格履歴・承認と監査、実店舗向け認可や本番切替の確認。

設計上の詳細は[レジv3.0設計書](store/bakery-hub/docs/v3-register-design.md)と[Bakery Hub詳細README](store/bakery-hub/README.md)を参照してください。

---

## 📝 Bakery Hubの最近の開発・検証記録

2026年10月には商品管理・Catalogの改善に加え、CodexとCosmic Rayでpytestの検知力を再監査し、正式なテストをmainへ反映しました。

| 記事 | タイトル・リンク |
|---|---|
| 第1記事 | [🥐Bakery Hubの商品管理を作り直した｜18商品テンプレート・履歴Catalog・Admin登録まで](https://qiita.com/tosane932/items/e62462c105efdc8a5d5e) |
| 第2記事 | [🧪pytestが通るだけでは安心できない｜CodexとCosmic Rayで2度目のMutation Testing](https://qiita.com/tosane932/items/070c26c8cab19a226a30) |
| 第3記事 | [🧪Codexで挑んだ1,286件のMutation Testing｜pytest再監査からmain反映までの5日間](https://qiita.com/tosane932/items/52dbc54dc2de3748bb72) |
