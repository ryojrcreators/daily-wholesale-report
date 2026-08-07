"""
一時調査用（読み取りのみ）：先ほど9,010行取得できた実績あるso_sheets.py方式が、
今この瞬間にも同じように動くか再確認する（rakuten_ship_notify.py側の実装差分が原因か、
それとも環境要因（レート制限等）かを切り分けるため）。
"""

import os
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

from so_sheets import LOGIN_URL, LOGIN_ID_2, LOGIN_PASS_2, _fetch_so_range

JST = timezone(timedelta(hours=9))


def login(page):
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")


today = datetime.now(JST).date()
start = (today - timedelta(days=21)).strftime("%Y-%m-%d")
end = today.strftime("%Y-%m-%d")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            device_scale_factor=2,
        )
        page = context.new_page()
        login(page)
        print(f"so_sheets.py._fetch_so_range を呼び出します: {start} 〜 {end}")
        status, rows = _fetch_so_range(page, context, start, end)
        print(f"status={status}")
        print(f"取得行数（ヘッダー含む）: {len(rows) if rows else 0}")
    finally:
        browser.close()
