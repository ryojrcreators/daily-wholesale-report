"""
社内システム（app.jrcreators.com）の受注データから、Robot-in（楽天の受注管理ツール）の
「注文ステータス」画面のようなタイル型ダッシュボードをHTMLで生成する。

直近LOOKBACK_DAYS日分（created_time基準）のso-headsデータを取得し、order_status列の
値ごとに件数を集計、モール（shop_name）別の内訳も添えてHTMLファイルに書き出す。

生成したHTMLは自社サイトへの自動デプロイは行わない（ユーザーがWinSCPで手動アップロードする
運用のため）。このスクリプトはファイルを出力するところまでを担当する。
"""

import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from rakuten_ship_notify import login, fetch_recent_orders

LA_TZ = ZoneInfo("America/Los_Angeles")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "21"))
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "order_status_dashboard.html")

COL_ORDER_NUMBER = 0
COL_SHOP_NAME = 4
COL_ORDER_STATUS = 14

# ステータスの色分け（Robot-inの画面を参考に、注意が必要なものを強調）
STATUS_COLORS = {
    "Shipped": "green",
    "Cancelled": "gray",
    "Hold": "amber",
    "Unreceived": "coral",
    "Picking": "blue",
    "Packing": "blue",
    "Entered": "purple",
    "Imported": "purple",
    "Partially Shipped": "amber",
    "Japan Pending": "coral",
}
DEFAULT_COLOR = "gray"

COLOR_HEX = {
    "green":  ("#EAF3DE", "#27500A"),
    "gray":   ("#F1EFE8", "#444441"),
    "amber":  ("#FAEEDA", "#633806"),
    "coral":  ("#FAECE7", "#712B13"),
    "blue":   ("#E6F1FB", "#0C447C"),
    "purple": ("#EEEDFE", "#3C3489"),
}


def fetch_status_counts():
    end = datetime.now(LA_TZ)
    start = end - timedelta(days=LOOKBACK_DAYS)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, device_scale_factor=2)
        page = context.new_page()
        login(page)
        header, rows = fetch_recent_orders(
            page, context,
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        )
        browser.close()

    if not rows:
        return Counter(), defaultdict(Counter), 0, start, end

    # order_number単位で重複除去（1注文に複数商品行があるため、注文単位の状況を数えたい）
    seen_orders = {}
    for row in rows:
        if len(row) <= COL_ORDER_STATUS:
            continue
        order_number = row[COL_ORDER_NUMBER].strip()
        if not order_number:
            continue
        # 同じ注文の行が複数あっても、最後に見た値で上書き（通常は同一のはず）
        seen_orders[order_number] = (
            row[COL_SHOP_NAME].strip() if len(row) > COL_SHOP_NAME else "",
            row[COL_ORDER_STATUS].strip() if len(row) > COL_ORDER_STATUS else "",
        )

    status_counts = Counter()
    status_by_shop = defaultdict(Counter)
    for shop_name, status in seen_orders.values():
        status = status or "(未設定)"
        status_counts[status] += 1
        status_by_shop[status][shop_name or "(不明)"] += 1

    return status_counts, status_by_shop, len(seen_orders), start, end


def build_html(status_counts, status_by_shop, total, start, end, generated_at):
    tiles = []
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        color = STATUS_COLORS.get(status, DEFAULT_COLOR)
        bg, fg = COLOR_HEX[color]
        shop_breakdown = status_by_shop[status]
        breakdown_html = "".join(
            f'<div class="shop-row"><span>{shop}</span><span>{cnt:,}</span></div>'
            for shop, cnt in shop_breakdown.most_common()
        )
        tiles.append(f'''
        <div class="tile" style="background:{bg};">
          <div class="tile-count" style="color:{fg};">{count:,}</div>
          <div class="tile-label" style="color:{fg};">{status}</div>
          <div class="tile-breakdown">{breakdown_html}</div>
        </div>''')

    return f'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>受注ステータス ダッシュボード</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #F7F6F2;
    --text-primary: #2C2C2A;
    --text-secondary: #5F5E5A;
    --border: #E3E1D9;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 2rem;
    background: var(--bg);
    color: var(--text-primary);
    font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic", sans-serif;
  }}
  h1 {{
    font-size: 22px;
    font-weight: 500;
    margin: 0 0 4px;
  }}
  .meta {{
    color: var(--text-secondary);
    font-size: 13px;
    margin: 0 0 1.5rem;
  }}
  .total {{
    font-size: 14px;
    color: var(--text-secondary);
    margin-bottom: 1.5rem;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }}
  .tile {{
    border-radius: 12px;
    padding: 1rem 1.25rem;
    position: relative;
  }}
  .tile-count {{
    font-size: 28px;
    font-weight: 500;
    line-height: 1.2;
  }}
  .tile-label {{
    font-size: 14px;
    font-weight: 500;
    margin-top: 2px;
  }}
  .tile-breakdown {{
    margin-top: 10px;
    padding-top: 8px;
    border-top: 0.5px solid rgba(0,0,0,0.12);
    font-size: 12px;
  }}
  .shop-row {{
    display: flex;
    justify-content: space-between;
    opacity: 0.85;
    padding: 1px 0;
  }}
  .footer {{
    margin-top: 2rem;
    font-size: 12px;
    color: var(--text-secondary);
  }}
</style>
</head>
<body>
  <h1>受注ステータス ダッシュボード</h1>
  <p class="meta">対象期間: {start.strftime('%Y/%m/%d')}〜{end.strftime('%Y/%m/%d')}（created_time基準、直近{LOOKBACK_DAYS}日）</p>
  <p class="total">対象注文数: {total:,}件</p>
  <div class="grid">
    {''.join(tiles)}
  </div>
  <p class="footer">生成日時: {generated_at.strftime('%Y/%m/%d %H:%M')}（PT）</p>
</body>
</html>
'''


def main():
    print("=== 受注ステータス ダッシュボード生成 開始 ===")
    status_counts, status_by_shop, total, start, end = fetch_status_counts()
    print(f"対象注文数: {total}件")
    for status, count in status_counts.most_common():
        print(f"  {status}: {count}件")

    html = build_html(status_counts, status_by_shop, total, start, end, datetime.now(LA_TZ))
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n{OUTPUT_PATH} に出力しました。")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
