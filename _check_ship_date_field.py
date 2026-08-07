"""
一時調査用（読み取りのみ）：so-heads検索フォームの「Ship Date」欄（input[name="ship_date"]）
を実際にフォーム経由で入力してSearchを押した場合に、本当にデータが取れるかを確認する。
これまでのテストはURLに直接ship_dateを埋め込む方式（フォーム送信を経由しない）だったため、
「Ship Date欄自体が悪いのか」「URL直埋め込みという経路が悪いのか」を切り分ける。
1リクエストのみ実行し、同一IPレート制限の影響を避ける。
"""

import os
import requests
from datetime import datetime, timedelta, timezone
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
JST = timezone(timedelta(hours=9))

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

today = datetime.now(JST).date()
# Ship Date欄は「発送日」なので、実際に出荷があったはずの直近日を1点で狙う
ship_date_str = (today - timedelta(days=2)).strftime("%Y-%m-%d")
start_str = (today - timedelta(days=30)).strftime("%Y-%m-%d")
end_str = today.strftime("%Y-%m-%d")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            device_scale_factor=2,
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="networkidle")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(SO_HEADS_URL, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # created_time範囲は広めに（start_date/end_date）、Ship Date欄にピンポイントで発送日を指定
        page.locator('input[name="start_date"]').first.fill(start_str)
        page.locator('input[name="end_date"]').first.fill(end_str)
        page.locator('input[name="ship_date"]').first.fill(ship_date_str)
        print(f"検索条件: start_date={start_str} end_date={end_str} ship_date={ship_date_str}")

        page.click('button:has-text("Search"), input[value="Search"]')
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        href = page.evaluate(
            """() => {
                const a = [...document.querySelectorAll('a')].find(el => el.textContent.trim() === 'Download');
                return a ? a.getAttribute('href') : null;
            }"""
        )
        if not href:
            print("Downloadリンクなし（hrefが取得できず）→ Ship Date欄をフォーム経由で使っても0件扱いの可能性")
        else:
            download_url = f"https://{APP_DOMAIN}{href}" if href.startswith("/") else href
            cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
            response = requests.get(
                download_url,
                cookies=cookie_dict,
                headers={"User-Agent": USER_AGENT},
                auth=(LOGIN_ID_1, LOGIN_PASS_1),
            )
            if response.status_code != 200:
                print(f"CSVダウンロード失敗 status={response.status_code}")
            else:
                text = response.content.decode("utf-8-sig", errors="replace")
                lines = text.splitlines()
                row_count = max(len(lines) - 1, 0)
                print(f"href={href}")
                print(f"CSV行数（ヘッダー除く）={row_count}")
                if row_count:
                    print(f"ヘッダー: {lines[0]}")
                    print(f"先頭2件: {lines[1:3]}")
    finally:
        browser.close()
