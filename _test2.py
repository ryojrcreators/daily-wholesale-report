"""1回限りの確認用。指定URLをそのまま開いて結果が出るか確認する。"""
import os
from playwright.sync_api import sync_playwright
from urllib.parse import quote

DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
TARGET_URL = (
    f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}"
    "/so-heads?start_date=2026-04-30&SoHeads%5Bsales_account_id%5D=3&ship_date=2026-07-31"
)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1800, "height": 900},
        device_scale_factor=2,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    page = context.new_page()

    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")

    page.goto(TARGET_URL, wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.screenshot(path="target_url_result.png", full_page=True)

    order_link_count = page.evaluate("document.querySelectorAll('a[href*=\"/sales/view/\"]').length")
    has_download = page.evaluate("[...document.querySelectorAll('a')].some(a => a.textContent.trim() === 'Download')")
    print(f"order_link_count={order_link_count} has_download={has_download}")

    browser.close()
