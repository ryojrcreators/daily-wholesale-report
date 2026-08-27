"""一時デバッグ: 社内システムの発送済み注文一覧から、shop_name の種類と
Wowma（au PAYマーケット）らしき表記・注文番号パターンを確認する。"""
import os
from collections import Counter
from playwright.sync_api import sync_playwright

from rakuten_ship_notify import login, collect_shipped_orders


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, device_scale_factor=2)
        page = context.new_page()
        login(page)
        orders = collect_shipped_orders(page, context)
        browser.close()

    print(f"取得件数: {len(orders)}")
    counter = Counter(o["shop_name"] for o in orders)
    print("\n--- shop_name の内訳 ---")
    for name, count in counter.most_common():
        print(f"  {name!r}: {count}件")

    print("\n--- 各shop_nameのサンプル注文番号3件ずつ ---")
    seen = {}
    for o in orders:
        seen.setdefault(o["shop_name"], []).append(o["order_number"])
    for name, nums in seen.items():
        print(f"  {name!r}: {nums[:3]}")


if __name__ == "__main__":
    main()
