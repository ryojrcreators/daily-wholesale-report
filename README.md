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
| `po_sheets.py` | `daily_po_report.yml` | PO（発注）データをGoogle SheetsのPOタブへ反映。常に「半年前〜当日」の6か月分を保持（初回のみバックフィル、以降は差分更新） |
| `so_sheets.py` | `daily_so_report.yml` | SO（受注）データをGoogle SheetsのSOタブへ反映。POと同じく6か月分を差分更新 |
| `product_sheets.py` | `daily_product_report.yml` | Product/SKU/UPCデータをGoogle Sheetsへ反映 |
| `hp_qty_report.py` | `daily_hp_qty_report.yml` | HP（Hold/Pending）在庫数を集計しChatworkへ通知 |
| `inventory_alert.py` | `daily_inventory_alert.yml` | 棚卸未実施の商品を上位から抽出しChatworkへ通知 |
| `report_delay.py` | `report_delay.yml` | 配送遅延中の案件を検出し、店舗ごとのテンプレートで遅延連絡を自動送信（1日2回）。入荷見込みが「今週末までに発送」等の曖昧表現の場合は自動送信せず手動確認へ回す |
| `japan_custom.py` | `japan_custom.yml` | Japan Custom（関税関連）案件を検出し、店舗ごとのテンプレートで自動対応（1日2回） |
| `po_import.py` | `po_import.yml` | 指定したPO番号のCSVをサーバーから取得し、ショッピングリスト用スプレッドシートへ取り込む（Googleスプレッドシートのボタン＝GASから起動） |
| `shopping_report_process.py` | `shopping_report.yml` | Chatworkの「End Shopping Report」を読み取り、対応するPOの買えなかった商品をClose・数量変更（LA 18:00に自動実行） |

進捗・実行ステータスは `status_sheet.py` を通じてGoogle SheetsのStatusタブにまとめて記録されます。

### ショッピングレポート自動処理（`shopping_report_process.py`）

Chatworkに投稿される「[End Shopping Report]」を自動で読み取り、対応する社内システムのPO（発注）を更新します。

- **買えなかった商品（Not Bought）**
  - `(0/x)`（1個も買えなかった）→ その行を **Close**
  - `(n/x)`（一部だけ買えた）→ その行の **Qty を n に変更**
- **追加で買った商品（Extra / Got Extra）** `+k` → その行の **Qty を 現在+k に変更**
- **複数店舗対応**：レポート内に `(PO# nnnnn)` があれば、その店舗ブロックを該当PO番号へ適用（複数店舗を1レポートで処理可能）。PO#の記載が無い旧形式レポートは、指定タブのD1に入っているPO番号を使う
- **完了通知**：処理後、対象PO URL・Closed件数・Reduced（`code before->after`）・Extra（`code before->after`）・見つからなかったコードを、英語のブロック形式でChatworkへ投稿
- **安全策**
  - Force Closeは一切使わない（Closeは各行のリンク経由で、確認ダイアログにOKを返す形）
  - `_state` タブに処理済みメッセージIDを記録し、同じレポートは二度処理しない
  - `DRY_RUN=true`（手動実行の既定）では変更せず「何をするか」だけ表示。`DRY_RUN=false` で実際に反映
- **自動実行**：毎日 **LA 18:00** に本番実行（レポートは16:30〜17:00頃に届く想定）。GitHub CronはUTC固定のため、夏時間・冬時間の両方（UTC 01:00 / 02:00）を登録し、上記の二重実行防止で実処理は1日1回

### PO取り込み（`po_import.py`）

新しいPOを作成したあと、ショッピングリスト用スプレッドシートへCSVを取り込む作業を自動化します。

- スプレッドシートのカスタムメニュー（GAS）からPO番号を入力すると、GitHub Actions（`po_import.yml`）が起動
- サーバーからPO CSVをダウンロードし、対象タブを「A1から置き換え」で書き込み（UPCの先頭ゼロを保つためRAWで書き込み）
- PO番号は C1／D1 に記入
- shopping-listページの「!」（緊急）フラグを読み取り、K列に「Urgent」列として自動付与

## 技術スタック

| 項目 | 内容 |
|---|---|
| 言語 | Python 3.11 |
| 定期実行 | GitHub Actions（cron） / cron-job.org（外部スケジューラ） |
| ブラウザ自動操作 | Playwright（社内システムへの2段階ログイン・CSV取得） |
| データ読み書き | Google Sheets API（gspread） |
| 通知 | Chatwork API |
| 価格・在庫取得 | Keepa API（domain=1 = Amazon.com US） |
| 翻訳 | DeepL API |
| 為替レート | frankfurter.app API（リアルタイム） |

## セットアップ

### 必要なGitHub Secrets

| Secret名 | 内容 |
|---|---|
| `APP_DOMAIN` | 社内システムのドメイン |
| `LOGIN_ID_1` / `LOGIN_PASS_1` | 社内システムのBasic認証 ID／パスワード |
| `LOGIN_ID_2` / `LOGIN_PASS_2` | 社内システムのフォームログイン ID／パスワード |
| `GOOGLE_CREDENTIALS` / `GOOGLE_CREDENTIALS_JSON` | GCPサービスアカウントのJSONキー（Playwright系 / 楽天系で参照名が異なる） |
| `CW_TOKEN` | Chatwork APIトークン |
| `CW_ROOM_ID` | Chatwork通知先ルームID |
| `SPREADSHEET_ID` | 業務データ用スプレッドシートのID |
| `PO_SO_SPREADSHEET_ID` | PO/SO（6か月分）専用スプレッドシートのID |
| `KEEPA_API_KEY` | Keepa Pro APIキー |
| `DEEPL_API_KEY` | DeepL APIキー |
| `PROFIT_RATE` / `COMMISSION_RATE` | 赤字判定の利益率・手数料率 |
| `RAKUTEN_SPREADSHEET_ID` | 赤字チェック用スプレッドシートのID |
| `RAKUTEN_LISTING_SPREADSHEET_ID` | 出品データ同期専用スプレッドシートのID |
| `RAKUTEN_SHOP_NAME_1` / `RAKUTEN_SHOP_NAME_2` | 楽天出品データ同期・各店舗の表示名 |
| `RAKUTEN_RMS_SERVICE_SECRET_1` / `_2` | 楽天RMS serviceSecret（店舗ごと） |
| `RAKUTEN_RMS_LICENSE_KEY_1` / `_2` | 楽天RMSライセンスキー（店舗ごと・3か月ごとに要更新） |

> GAS（スプレッドシート側）からワークフローを起動する `po_import` では、GAS内のスクリプトプロパティに GitHub PAT（fine-grained / Actions: Read and write）を保存して使用します（リポジトリのSecretsとは別管理）。

### ローカル実行

```bash
pip install -r requirements.txt

# 環境変数を設定してから実行
python rakuten_price_check.py
python rakuten_asin_finder.py
```

## 定期実行スケジュール

| ワークフロー | 実行タイミング | 起動方法 |
|---|---|---|
| 赤字・仕入不可チェック | JST 9:00 / 13:00 / 17:00 | GitHub cron |
| ASIN補完 | JST 10:00 / 14:00 / 18:00 | GitHub cron |
| 出品データ同期 | JST 7:00 | GitHub cron |
| POデータ反映 | JST 20:00 | GitHub cron |
| SOデータ反映 | サーバー準備完了時 | `repository_dispatch`（server-is-ready） |
| Report Delay 自動処理 | LA 9:00 / 17:00（1日2回） | GitHub cron |
| Japan Custom 自動処理 | LA 9:00 / 17:00（1日2回） | GitHub cron |
| ショッピングレポート処理 | LA 18:00 | GitHub cron（夏冬2本＋二重実行防止） |
| PO取り込み | 手動（GASボタンから起動） | `workflow_dispatch` |
| Wholesale / Product / HP Qty / 棚卸アラート | 外部スケジューラ等から起動 | `workflow_dispatch` |

いずれのワークフローも`workflow_dispatch`で手動実行が可能です。

## 詳細ドキュメント

判定ロジック・列構成・トークン消費の目安など詳細は [CLAUDE.md](./CLAUDE.md) を参照してください。
