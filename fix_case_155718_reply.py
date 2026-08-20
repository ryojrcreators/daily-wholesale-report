"""
使い捨て一回限りのスクリプト。ケース155718に、デバッグ調査時に誤って残った
テスト用Replyを補足する、正しい内容のReplyを追加投稿する。
Case Groupsは既にRakuten/Yahooが外れているため、ここでは触らずReplyの追加のみ行う。
"""

from case_orders_auto_close import BASE_URL, login
from playwright.sync_api import sync_playwright

CASE_ID = "155718"
REPLY_TEXT = (
    "No Rakuten/Yahoo listing for this case. "
    "(Correction: the previous reply above was a debug test message, please disregard it.)"
)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        login(page)

        page.goto(f"{BASE_URL}/case-orders/edit/{CASE_ID}", wait_until="networkidle")
        page.wait_for_timeout(300)

        page.fill('textarea[id^="case-order-replies-"][id$="-message"]', REPLY_TEXT)

        with page.expect_navigation(timeout=15000):
            page.evaluate(
                """() => document.querySelector('form[action*="/case-orders/edit/"]').requestSubmit()"""
            )
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(300)

        body = page.evaluate("() => document.body.innerText")
        if REPLY_TEXT in body:
            print("Reply投稿に成功しました。")
        else:
            print("Replyが見当たりません。失敗した可能性があります。")

        browser.close()


if __name__ == "__main__":
    main()
