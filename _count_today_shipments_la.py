"""
一時調査用（読み取りのみ）：Ship Dateが「今日」（LA時間基準）のものが何件あるかを数える。
CSVのship_time自体はおそらくLA時間（現地倉庫の時刻）で入っているとみられるため、
JSTではなくLA時間の「今日」で判定する。
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

from rakuten_ship_notify import (
    JST,
    CREATED_TIME_LOOKBACK_DAYS,
    login,
    fetch_recent_orders,
    parse_ship_datetime,
    USER_AGENT,
)

LA = ZoneInfo("America/Los_Angeles")

today_jst = datetime.now(JST).date()
today_la = datetime.now(LA).date()
print(f"JSTでの今日: {today_jst} / LAでの今日: {today_la}")

start_date = (today_jst - timedelta(days=CREATED_TIME_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
end_date = today_jst.strftime("%Y-%m-%d")

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
    today_rows = []
    for row in rows:
        if not any(row):
            continue
        order_number = row[idx_order].strip() if len(row) > idx_order else ""
        ship_time = row[idx_ship].strip() if len(row) > idx_ship else ""
        if not order_number or order_number in seen:
            continue
        seen.add(order_number)
        ship_dt = parse_ship_datetime(ship_time)
        # ship_timeはタイムゾーン情報を持たない文字列。LA時間の値だという前提でLA日付と比較する
        if ship_dt is not None and ship_dt.date() == today_la:
            today_count += 1
            today_rows.append((order_number, ship_time))

    print(f"今日（LA時間 {today_la}）Ship Dateの件数（order_number単位・重複除去後）: {today_count}件")
    for order_number, ship_time in today_rows[:30]:
        print(f"  {order_number}: {ship_time}")
