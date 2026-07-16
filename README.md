このリポジトリは、社内システム（ドメインは Secret `APP_DOMAIN` で管理）とGoogle Sheets / Chatworkを連携させる複数の自動化スクリプトをまとめたものです。

### 楽天 赤字・仕入不可チェック関連

1. **赤字・仕入不可チェック**（`rakuten_price_check.py` / ワークフロー: `rakuten_check.yml`）
   - 楽天出品商品（ASIN登録済み）について、Keepa APIでAmazon.comの価格・在庫を取得
   - 仕入れコスト（商品代＋送料＋為替）と楽天販売価格を比較し、赤字かどうかを判定
   - 赤字の場合は損益分岐点となる「適正価格」も算出
   - Amazon本体に出品がなくても3rdパーティ新品があれば仕入れ可能と判定

2. **ASIN補完**（`rakuten_asin_finder.py` / ワークフロー: `rakuten_asin_finder.yml`）
   - ASINが未登録の商品について、商品名をDeepL APIで英訳
   - Keepa Search APIでキーワード検索し、ASIN候補と信頼度（HIGH/LOW）を提示
   - 信頼度HIGHのものは手動確認後に「ASINあり」シートへ移動する運用

これら2つはGitHub Actionsで定期実行され、結果はGoogle Sheetsに書き込まれます（共通の進捗管理は `status_sheet.py` がGoogle SheetsのStatusタブに記録）。

3. **出品データ同期**（`rakuten_listing_sync.py` / ワークフロー: `rakuten_listing_sync.yml`）
   - 楽天RMS API（items.search）で出品中の全商品（商品管理番号・商品名・価格・在庫状況）を、**楽天2店舗分**まとめて取得
   - 専用スプレッドシート（`RAKUTEN_LISTING_SPREADSHEET_ID`）の「楽天_出品データ」タブへ、毎日の最新状態としてまるごと上書き（履歴は持たない）。先頭列に「店舗名」を持ち、2店舗分を1つのタブに集約
   - 取得失敗・0件時はシートを上書きしない（前日データを保護）。いずれか1店舗でも取得失敗した場合は他店舗分も含めて書き込み中止
   - 在庫は数量ではなく「在庫あり/在庫切れ」の二値（items.searchは数量を返さないため）
   - Yahoo!ショッピング（ストアクリエイターPro API）連携は次フェーズで追加予定
   - 将来的には、Purchaser側の「購入不可」報告（`shopping_report_process.py`のNot Bought/Close処理）とこの出品データを突き合わせ、対応する楽天・Yahoo出品を自動的に洗い出す仕組みへ拡張予定

### 社内システム連携レポート（Playwright）

社内システム（ドメインは Secret `APP_DOMAIN` で管理）にPlaywrightで自動ログインし、CSVダウンロードやデータ抽出を行ってGoogle Sheets / Chatworkに連携するスクリプト群です。

| スクリプト | ワークフロー | 内容 |
|---|---|---|
| `main.py` | `daily_wholesale.yml` | Wholesale CSVを毎日ダウンロードしGoogle Sheetsへ反映 |
| `po_sheets.py` | `daily_po_report.yml` | PO（発注）データをGoogle SheetsのPOタブへ反映（JST 20:00） |
| `product_sheets.py` | `daily_product_report.yml` | Product/SKU/UPCデータをGoogle Sheetsへ反映 |
| `so_sheets.py` | `daily_so_report.yml` | SO（受注）データをGoogle SheetsのSales Orderタブへ反映 |
| `hp_qty_report.py` | `daily_hp_qty_report.yml` | HP（Hold/Pending）在庫数を集計しChatworkへ通知 |
| `report_delay.py` | `report_delay.yml` | 配送遅延中の案件を検出し、店舗ごとのテンプレートで遅延連絡を自動送信（1日2回） |
| `japan_custom.py` | `japan_custom.yml` | Japan Custom（関税関連）案件を検出し、店舗ごとのテンプレートで自動対応（1日2回） |

進捗・実行ステータスは `status_sheet.py` を通じてGoogle SheetsのStatusタブにまとめて記録されます。

## 技術スタック

| 項目 | 内容 |
|---|---|
| 言語 | Python 3.11 |
| 定期実行 | GitHub Actions（cron） |
| データ読み書き | Google Sheets API（gspread） |
| 価格・在庫取得 | Keepa API（domain=1 = Amazon.com US） |
| 翻訳 | DeepL API |
| 為替レート | frankfurter.app API（リアルタイム） |

## セットアップ

### 必要なGitHub Secrets

| Secret名 | 内容 |
|---|---|
| `KEEPA_API_KEY` | Keepa Pro APIキー |
| `DEEPL_API_KEY` | DeepL APIキー |
| `GOOGLE_CREDENTIALS_JSON` | GCPサービスアカウントのJSONキー |
| `RAKUTEN_SHOP_NAME_1` | 楽天出品データ同期・店舗1の表示名 |
| `RAKUTEN_RMS_SERVICE_SECRET_1` | 楽天出品データ同期・店舗1のRMS serviceSecret |
| `RAKUTEN_RMS_LICENSE_KEY_1` | 楽天出品データ同期・店舗1のRMSライセンスキー（3か月ごとに要更新） |
| `RAKUTEN_SHOP_NAME_2` | 楽天出品データ同期・店舗2の表示名 |
| `RAKUTEN_RMS_SERVICE_SECRET_2` | 楽天出品データ同期・店舗2のRMS serviceSecret |
| `RAKUTEN_RMS_LICENSE_KEY_2` | 楽天出品データ同期・店舗2のRMSライセンスキー（3か月ごとに要更新） |
| `RAKUTEN_LISTING_SPREADSHEET_ID` | 出品データ同期専用スプレッドシートのID |

### ローカル実行

```bash
pip install -r requirements.txt

# 環境変数を設定してから実行
python rakuten_price_check.py
python rakuten_asin_finder.py
```

## 定期実行スケジュール（JST）

| ワークフロー | 実行時刻 |
|---|---|
| 赤字・仕入不可チェック | 9:00 / 13:00 / 17:00 |
| ASIN補完 | 10:00 / 14:00 / 18:00 |
| 出品データ同期 | 7:00 |

それぞれ`workflow_dispatch`で手動実行も可能です。

## 詳細ドキュメント

判定ロジック・列構成・トークン消費の目安など詳細は [CLAUDE.md](./CLAUDE.md) を参照してください。
