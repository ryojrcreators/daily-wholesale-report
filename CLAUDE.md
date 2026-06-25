# 楽天赤字・仕入不可チェックツール 引き継ぎドキュメント

## 開発方針：Claude従量課金対象の利用禁止

このプロジェクトでは、以下の利用は**一切行わない**こと。

- `claude -p` などのヘッドレス/非対話実行（スクリプトやCIからのCLI呼び出し）
- `anthropic` ライブラリ（Claude Agent SDK）のimportおよびAPI呼び出し
- GitHub ActionsワークフローからのClaude CLI実行（`claude-code-action` 含む）
- `claude -p` をラップする常駐スクリプト・botの登録

自動化処理には **Keepa API / DeepL API / Google Sheets API / Playwright** を使う。
翻訳・要約・判定などをAIで処理したい場合も、上記の代替手段で対応するか、事前に確認すること。

---

## プロジェクト概要

楽天市場で出品中の商品について、Amazon.com（US）の現在価格・在庫状況をKeepa APIで自動チェックし、**赤字商品・仕入れ不可商品を事前に発見する予防型ツール**。

---

## ビジネス背景

- **仕入れ元：Amazon.com（US）、USD建て**
- **販売先：楽天市場（JPY建て）**
- **問題：長期間売れていない商品は価格改定・廃盤に気づかず、売れた時に初めて赤字・仕入れ不可が発覚する**
- **目的：注文が入る前に問題商品を発見して事前対処する**

---

## リポジトリ

`ryojrcreators/daily-wholesale-report`（GitHub Private）

### ファイル構成

```
daily-wholesale-report/
├── .github/workflows/
│   └── rakuten_check.yml       ← 定期実行ワークフロー（追加済み）
├── rakuten_price_check.py      ← チェックスクリプト本体（追加済み・動作確認済み）
├── requirements.txt            ← gspread==6.1.2 / google-auth==2.29.0 追記済み
├── main.py
├── report_delay.py
└── ...
```

---

## 技術スタック

| 項目 | 内容 |
|---|---|
| 言語 | Python 3.11 |
| 定期実行 | GitHub Actions（JST 9:00 / 13:00 / 17:00） |
| データ読み書き | Google Sheets API（gspread） |
| 価格・在庫取得 | Keepa API（domain=1 = Amazon.com US） |
| 為替レート | frankfurter.app API（リアルタイム） |

---

## 環境変数（GitHub Secrets）

| Secret名 | 内容 |
|---|---|
| `KEEPA_API_KEY` | Keepa Pro APIキー |
| `GOOGLE_CREDENTIALS_JSON` | GCPサービスアカウントのJSONキー（中身をそのままペースト） |

### GCPプロジェクト
- プロジェクトID：`daily-wholesale-report`
- 有効化済みAPI：Google Sheets API / Google Drive API
- サービスアカウント：（実際のメアドは GCP コンソール参照。公開リポジトリには記載しない）

---

## 対象スプレッドシート

- **スプレッドシートID：** （Secret `RAKUTEN_SPREADSHEET_ID` を参照。公開リポジトリには記載しない）
- **URL：** （同上）

### シート構成

| シート名 | 内容 | 件数 |
|---|---|---|
| `ASINあり` | チェック対象（本スクリプトの処理対象） | （非公開） |
| `ASINなし（要調査）` | 今後のPhase 2対象 | （非公開） |

### 列構成（`ASINあり`シート）

| 列 | インデックス（0始まり） | 内容 |
|---|---|---|
| A | 0 | 商品管理番号（商品URL） |
| B | 1 | 商品名 |
| C | 2 | ASIN |
| D | 3 | 通常購入販売価格（JPY） |
| E | 4 | **在庫チェック**（スクリプトが書き込む） |
| F | 5 | **価格チェック**（スクリプトが書き込む） |

---

## 判定ロジック

### 在庫チェック（E列）の判定

```python
# Keepa stats.current を使用（インデックスに注意）
current[0]   # AMAZON    : Amazon本体価格（セント、-1=なし）
current[1]   # NEW       : 新品最安値（3rdパーティ含む、セント、-1=なし）
current[11]  # COUNT_NEW : 新品出品数（-1=なし）

Amazon本体価格あり              → ✅ 正常（本体価格で価格チェック）
本体なし・3rdパーティ新品あり    → 🟡 3rdパーティ（新品最安値で価格チェック）
新品出品が全くない              → ⚠️ 仕入不可
Keepaでデータ取得できず          → ⚠️ 仕入不可

# ※ 旧バージョンは current[7]（実際はFBM価格）を「出品数」と誤認していたため、
#   3rdパーティから普通に仕入れ可能な商品まで「仕入不可」と誤判定していた。
```

### 価格チェック（F列）の判定

```python
# 計算式
# 仕入れ元価格 = 本体あれば current[0]、なければ新品最安値 current[1]
source_price_usd = source_price_cents / 100  # セント→ドル変換
weight_lbs = packageWeight(g) / 453.592  # グラム→ポンド変換（なければ1.0lbs）
shipping_usd = 重量別送料テーブルで算出
cost_jpy = (source_price_usd + shipping_usd) * 為替レート

# 損益分岐点
breakeven = 楽天販売価格JPY × (1 - 利益率 - 手数料率)
           = 楽天販売価格JPY × (1 - ○○ - ○○)   # 実際の率は非公開（コード内 PROFIT_RATE / COMMISSION_RATE 参照）

cost_jpy <= breakeven → ✅ 正常
cost_jpy >  breakeven → 🔴 赤字
仕入れ元価格なし      → -（ハイフン）
```

### 出力パターンまとめ

| 在庫チェック（E列） | 価格チェック（F列） | 意味 |
|---|---|---|
| ✅ 正常 | ✅ 正常 | Amazon本体から仕入れ可能・問題なし |
| ✅ 正常 | 🔴 赤字 | Amazon本体から仕入れ可能だが赤字 |
| 🟡 3rdパーティ | ✅ 正常 | 本体なし・3rdパーティ新品から仕入れ可能 |
| 🟡 3rdパーティ | 🔴 赤字 | 3rdパーティから仕入れ可能だが赤字 |
| ⚠️ 仕入不可 | - | 新品出品ゼロ or データ取得不可 |

---

## 実行設定

### 1回あたりの処理件数
- バッチサイズ：100件（Keepa最大）
- バッチ数：2回/実行
- **合計：200件/実行**

### 定期実行スケジュール
- JST 9:00 / 13:00 / 17:00（1日3回）
- **600件/日 → 全件チェック完了まで約2週間（件数は非公開）**

### Keepaトークン消費の目安
- Keepa Pro：1分あたり1トークン補充 = 1日1,440トークン
- **Product API（価格チェック）は ASIN 1件につき約1トークン**（`stats=1` は追加コストなし）
  - 200件/実行 → 約200トークン消費
  - 1日3回実行 → 約600トークン/日
- **Search API（ASIN補完）は1検索につき約10トークン**（上位約20件返却のため）
  - 30件/実行 → 約300トークン消費、1日3回 → 約900トークン/日
- 価格チェック + ASIN補完を併用すると 1日約1,500トークン → 補充量とほぼ同等なので注意

---

## スクリプト概要（rakuten_price_check.py）

```python
# 主要な関数

get_sheet()           # Google Sheets認証・シート取得
get_exchange_rate()   # frankfurter.app APIでUSD→JPYリアルタイムレート取得
get_shipping_cost()   # 重量（lbs）→ 送料（USD）変換（テーブル参照）
fetch_keepa_batch()   # KeepaバッチAPI（最大100件）で価格・在庫取得
judge()               # 在庫・価格判定して結果文字列を返す
main()                # 全体制御（未チェック行抽出→バッチ処理→書き込み）
```

### 処理フロー

```
Google Sheetsから全行読み込み
        ↓
在庫チェック列（E列）が空の行を抽出
        ↓
上から200件を今回の処理対象に
        ↓
100件ずつKeepaバッチAPIで取得（バッチ間30秒待機）
        ↓
各商品を判定
        ↓
Google Sheetsにまとめて書き込み（batch_update）
```

---

## 動作確認済み状況

- ✅ GitHub Actions手動実行で正常動作確認済み
- ✅ スプレッドシートへの書き込み確認済み
- ✅ `⚠️ 仕入不可` / `✅ 正常` / `🔴 赤字` の判定結果が正しく出力されることを確認済み

---

## 今後の課題（Phase 2）

**ASINなし（件数は非公開）の対応**

- DeepL API無料枠（月50万文字）で商品名を英訳
- Keepaキーワード検索で上位1件マッチ
- 信頼度低い場合は「🟡 要確認」フラグ

---

## 未確定事項・検討中

- 手数料率（`COMMISSION_RATE`）は固定値 → 実際の楽天手数料率に合わせて要調整（具体値は非公開）
- チェック完了後、赤字・仕入不可商品への対処フロー（価格改定・取り下げ等）は未設計
- Phase 2（ASINなし）をどのリポジトリ・スクリプトに組み込むか未決定
