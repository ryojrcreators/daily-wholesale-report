"""
一時調査用（読み取りのみ）：今の時点でLA時間の「今日」Ship Dateが何件あるか再集計する。
"""

from datetime import timedelta
from playwright.sync_api import sync_playwright

from rakuten_ship_notify import (
    LA_TZ,
    CREATED_TIME_LOOKBACK_DAYS,
    login,
    fetch_recent_orders,
    parse_ship_datetime,
    resolve_store,
    USER_AGENT,
)
from datetime import datetime

today_la = datetime.now(LA_TZ).date()
start_date = (today_la - timedelta(days=CREATED_TIME_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
end_date = today_la.strftime("%Y-%m-%d")

print(f"今の時刻（LA）: {datetime.now(LA_TZ)}")
print(f"検索範囲(created_time): {start_date} 〜 {end_date}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            device_scale_factor=2,
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        login(page)
        header, rows = fetch_recent_orders(page, context, start_date, end_date)
    finally:
        browser.close()

if not header:
    print("データが取得できませんでした")
else:
    idx_order = header.index("order_number")
    idx_ship = header.index("ship_time")

    seen = set()
    today_count = 0
    rakuten_count = 0
    by_store_count = {"americana": 0, "founder": 0}
    for row in rows:
        if not any(row):
            continue
        order_number = row[idx_order].strip() if len(row) > idx_order else ""
        ship_time = row[idx_ship].strip() if len(row) > idx_ship else ""
        if not order_number or order_number in seen:
            continue
        seen.add(order_number)
        ship_dt = parse_ship_datetime(ship_time)
        if ship_dt is not None and ship_dt.date() == today_la:
            today_count += 1
            store = resolve_store(order_number)
            if store is not None:
                rakuten_count += 1
                by_store_count[store] += 1

    print(f"今日（LA時間 {today_la}）Ship Dateの件数（order_number単位・重複除去後）: {today_count}件")
    print(f"  うち楽天（americana+founder）: {rakuten_count}件"
          f"（americana {by_store_count['americana']}件 / founder {by_store_count['founder']}件）")
    print(f"（重複除去前の全行数: {len(rows)}行）")
