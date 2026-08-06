"""
一時調査用（読み取りのみ）：以前「本番で実績あり」とされていたso_sheets.py方式
（素のso-headsを開き、start_date/end_dateフォームに入力してSearch）で、
本当に注文データが取れるかを確認する。ship_date/sales_account_idフィルターは使わない。
"""

import os
from urllib.parse import quote
from playwright.sync_api import sync_playwright

APP_DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{APP_DOMAIN}/"
SO_HEADS_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{APP_DOMAIN}/so-heads"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def login(page):
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")


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

        page.goto(SO_HEADS_URL, wait_until="networkidle")
        page.wait_for_timeout(2000)

        start_input = page.locator('input[name="start_date"], input[placeholder*="Start"], input[id*="start"]').first
        start_input.fill("2026-07-01")
        end_input = page.locator('input[name="end_date"], input[placeholder*="End"], input[id*="end"]').first
        end_input.fill("2026-08-07")

        search_button = page.locator('button:has-text("Search"), input[type="submit"][value*="Search"]').first
        search_button.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        info = page.evaluate(
            """() => {
                const bodyText = document.body.innerText;
                const m = bodyText.match(/Showing[^\\n]*/);
                const tables = [...document.querySelectorAll('table')];
                return {
                    showing: m ? m[0] : null,
                    tableRowCounts: tables.map(t => t.querySelectorAll('tbody tr').length),
                    bodyHead: bodyText.slice(0, 800),
                };
            }"""
        )
        print(f"Showing行: {info['showing']}")
        print(f"テーブル各行数: {info['tableRowCounts']}")
        print(f"本文冒頭:\n{info['bodyHead']}")
    finally:
        browser.close()
