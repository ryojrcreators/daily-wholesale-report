"""1回限りの切り分け用。so_sheets.pyの_fetch_so_rangeをそのまま呼び出すだけ。"""
import os
import csv
import requests
from datetime import date
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
SO_SEARCH_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/so-heads"


def _fetch_so_range(page, context, start_date, end_date):
    page.goto(SO_SEARCH_URL, wait_until="networkidle")
    page.wait_for_timeout(2000)

    start_input = page.locator('input[name="start_date"], input[placeholder*="Start"], input[id*="start"]').first
    start_input.fill(start_date)
    end_input = page.locator('input[name="end_date"], input[placeholder*="End"], input[id*="end"]').first
    end_input.fill(end_date)

    page.click('button:has-text("Search"), input[value="Search"]')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    download_link = page.locator('a:has-text("Download"), button:has-text("Download")').first
    href = download_link.get_attribute("href")
    print(f"href={href!r}")
    if not href:
        raise Exception("Download リンクが見つかりませんでした")
    download_url = f"https://{DOMAIN}{href}" if href.startswith("/") else href

    cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
    response = requests.get(
        download_url,
        cookies=cookie_dict,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        },
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
    )
    print(f"status={response.status_code} content_length={len(response.content)}")
    if response.status_code != 200:
        return response.status_code, None

    text = response.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    return 200, rows


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1800, "height": 900},
        device_scale_factor=2,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    print("Basic認証付きでトップページを開いています...")
    page.goto(LOGIN_URL, wait_until="networkidle")
    print("Loginボタンをクリックしています...")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    print("フォームログインを処理しています...")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")

    # ログイン後のナビゲーション部分にアカウント名が表示されるはずなので、
    # 実際の値は出力せずスクリーンショットとしてのみ保存する
    page.goto(f"https://{DOMAIN}/so-heads", wait_until="networkidle")
    page.wait_for_timeout(1000)
    page.screenshot(path="logged_in_account.png", full_page=False)
    print("ログイン後画面のスクリーンショットを保存しました: logged_in_account.png")

    # 別ルートのテスト：/shipping-codes/edit/{id} は以前の調査でbotセッションでも
    # 安定して動くことを確認済み（shipping_code_id=3450973は先に確認済みの実データ）
    test_url = f"https://{DOMAIN}/shipping-codes/edit/3450973"
    print(f"別ルートをテスト: {test_url}")
    page.goto(test_url, wait_until="networkidle")
    page.wait_for_timeout(1000)
    body_text = page.evaluate("document.body.innerText.slice(0, 800)")
    print(f"本文冒頭800文字:\n{body_text}")
    has_view_link = page.evaluate("!!document.querySelector('a[href*=\"/sales/view/\"]')")
    print(f"/sales/view/ へのリンクが存在するか: {has_view_link}")

    today = date.today().strftime("%Y-%m-%d")
    print(f"検索: {today} 〜 {today}")
    status, rows = _fetch_so_range(page, context, today, today)
    print(f"status={status} rows={len(rows) if rows else 0}")

    browser.close()
