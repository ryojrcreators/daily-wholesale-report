"""
一時調査用（読み取りのみ）：rakuten_ship_notify.pyのso-heads検索が7日連続で0件だった件、
ship_dateパラメータ自体が原因か切り分けるため、ship_dateを外した状態で同じURLパターンに
アクセスし、「Showing X records out of Y total」を確認する。
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


def check(label, url):
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
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(2000)
            info = page.evaluate(
                """() => {
                    const bodyText = document.body.innerText;
                    const m = bodyText.match(/Showing[^\\n]*/);
                    return { showing: m ? m[0] : null, bodyHead: bodyText.slice(0, 300) };
                }"""
            )
            print(f"=== {label} ===")
            print(f"  URL: {url}")
            print(f"  Showing行: {info['showing']}")
            print(f"  本文冒頭: {info['bodyHead']!r}")
        finally:
            browser.close()


# 1. start_date + sales_account_id=3 のみ（ship_dateなし）→ デフォルトの並び順で何件あるか
check("ship_dateなし（start_date + sales_account_id=3のみ）",
      f"{SO_HEADS_URL}?start_date=2026-04-30&SoHeads%5Bsales_account_id%5D=3")

# 2. start_dateもsales_account_idも外し、本当に何もフィルタしない状態
check("フィルタなし（so-headsをそのまま開く）", SO_HEADS_URL)

# 3. so_sheets.pyのフォーム項目名（start_date/end_date、SoHeadsのbracket無し）をそのままURLに
check("start_date/end_date（so_sheets.pyのフォーム項目名そのまま、直近7日）",
      f"{SO_HEADS_URL}?start_date=2026-07-30&end_date=2026-08-07")
