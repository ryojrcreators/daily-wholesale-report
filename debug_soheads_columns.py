"""一時デバッグ: so-headsのCSV列名と、ステータス系フィールドのサンプル値を確認する。
受注管理ダッシュボード（Robot-in風のステータス別集計）を作る前の設計用調査。"""
from collections import Counter
from playwright.sync_api import sync_playwright

from rakuten_ship_notify import login, fetch_recent_orders


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1800, "height": 900}, device_scale_factor=2)
        page = context.new_page()
        login(page)
        header, rows = fetch_recent_orders(page, context, "2026-08-20", "2026-08-27")
        browser.close()

    print(f"件数: {len(rows)}")
    print(f"\n列数: {len(header)}")
    print("列名一覧:")
    for i, col in enumerate(header):
        print(f"  [{i}] {col}")

    print("\nサンプル行（先頭3件）:")
    for row in rows[:3]:
        print("---")
        for col, val in zip(header, row):
            if val:
                print(f"  {col}: {val!r}")

    # status/statusっぽい列の値分布
    for i, col in enumerate(header):
        lowered = col.lower()
        if any(k in lowered for k in ["status", "state", "flag", "cancel", "hold", "payment", "ship"]):
            counter = Counter(row[i] for row in rows if len(row) > i)
            print(f"\n--- 列 [{i}] {col} の値分布（上位10） ---")
            for val, count in counter.most_common(10):
                print(f"  {val!r}: {count}件")


if __name__ == "__main__":
    main()
