# Tosane Works

![Tosane Works](img/tosane-works-banner.png)

**現場で感じた課題や不便を、Webアプリとして形にしていくポートフォリオです。**

物流・店舗業務・情報収集など、異なる分野で開発したWebアプリケーションをまとめています。

[🌐 Web Portfolio](https://bakery-salesdata.onrender.com/)

---

## 🏪 Store

### 🥐 Bakery Hub

![Bakery Hub](img/bakery-hub-banner.png)

18種の商品テンプレートによる月次商品管理・履歴Catalog、日次売上管理・分析から、AIによる経営アドバイス、材料発注、店舗メモ、タスク管理までを一元化したWebアプリケーションです。

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

## 📝 Bakery Hubの最近の開発・検証記録

2026年10月には商品管理・Catalogの改善に加え、CodexとCosmic Rayでpytestの検知力を再監査し、正式なテストをmainへ反映しました。

| 記事 | タイトル・リンク |
|---|---|
| 第1記事 | [🥐Bakery Hubの商品管理を作り直した｜18商品テンプレート・履歴Catalog・Admin登録まで](https://qiita.com/tosane932/items/e62462c105efdc8a5d5e) |
| 第2記事 | [🧪pytestが通るだけでは安心できない｜CodexとCosmic Rayで2度目のMutation Testing](https://qiita.com/tosane932/items/070c26c8cab19a226a30) |
| 第3記事 | [🧪Codexで挑んだ1,286件のMutation Testing｜pytest再監査からmain反映までの5日間](https://qiita.com/tosane932/items/52dbc54dc2de3748bb72) |
