"""
社内システム（app.jrcreators.com）の Wholesale（Purchase）画面を、
指定フィルタ（purchaser=9 等）付きで開き、「No Records」以外の表示なら
（＝対応が必要な商品がある場合）Chatworkへ通知する。

「Server is Ready」通知（Chatworkルーム80285259）をきっかけに、
毎日の他のレポート更新と同時に起動する想定。
"""

import os
import requests
from urllib.parse import quote
from playwright.sync_api import sync_playwright

DOMAIN = "app.jrcreators.com"
BASE_URL = f"https://{DOMAIN}"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_URL = f"https://{quote(LOGIN_ID_1, safe='')}:{quote(LOGIN_PASS_1, safe='')}@{DOMAIN}/"

TARGET_URL = (
    f"{BASE_URL}/po-temps/wholesale"
    "?flag=daily&flag_filter=&purchaser=9&_supplier_ids=&suppliers_select_all=0"
    "&neg_live=0&neg_live=1&has_suggested=0&has_rush=0"
)

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "444852330"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def login(page):
    print("ログイン中...")
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")


def check_page():
    """対象ページを開き、本文テキストを返す。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, user_agent=USER_AGENT)
        page = context.new_page()
        login(page)
        print(f"対象ページを開いています: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="networkidle")
        page.wait_for_timeout(1500)
        body_text = page.inner_text("body")
        browser.close()
    return body_text


def post_chatwork():
    message = f"本日対応が必要な商品があります\n{TARGET_URL}"
    resp = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
        headers={"X-ChatWorkToken": CW_TOKEN},
        data={"body": message},
        timeout=30,
    )
    print(f"Chatwork通知送信: status={resp.status_code}")


def main():
    print("=== Wholesale 対応要否チェック 開始 ===")
    body_text = check_page()

    if "No Records" in body_text:
        print("No Records のため通知は行いません")
    else:
        print("対応が必要な商品が見つかりました。Chatworkへ通知します")
        post_chatwork()

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
