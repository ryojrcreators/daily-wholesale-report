"""
使い捨てデバッグスクリプト。ケース155907のRelated Skusから、100047177行の
Sales Price欄の生の値（repr付き）をそのまま出力する。「¥0」と表示された原因が
本当に0なのか、空欄など別の値なのかを確認する。
"""

from playwright.sync_api import sync_playwright

from case_orders_auto_close import BASE_URL, login
from case_orders_price_adjust import fetch_price_rows

CASE_ID = "155907"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        login(page)

        rows = fetch_price_rows(page, CASE_ID)
        for r in rows:
            print(f"sku={r['sku']!r} salesPrice={r['salesPrice']!r}")

        browser.close()


if __name__ == "__main__":
    main()
