"""
一時確認用: 9/30ガイドライン対応の最終確認。
楽天156件（hideItem=true期待）とYahoo257件（削除済み＝存在しない期待）を実際にAPIで
再確認する。書き込みは行わない。
"""

import time

import requests

from case_orders_auto_close import get_rakuten_stores, rakuten_auth_headers, RMS_BASE
from case_orders_auto_close import get_spreadsheet, get_yahoo_access_token, get_yahoo_stores, yahoo_get_item

with open("rakuten_final_verify_items.txt", encoding="utf-8") as f:
    RAKUTEN_ITEMS = [line.strip() for line in f if line.strip()]

with open("yahoo_final_verify_items.txt", encoding="utf-8") as f:
    YAHOO_ITEMS = [line.strip() for line in f if line.strip()]


def check_rakuten():
    print(f"=== 楽天 再確認（{len(RAKUTEN_ITEMS)}件） ===")
    still_open = []
    for item in RAKUTEN_ITEMS:
        for store in get_rakuten_stores():
            headers = rakuten_auth_headers(store)
            url = f"{RMS_BASE}/{item}"
            try:
                res = requests.get(url, headers=headers, timeout=30)
            except Exception as e:
                print(f"  [ERR] {item} / {store['name']}: {e}")
                continue
            finally:
                time.sleep(1.0)
            if res.status_code == 404:
                continue
            if res.status_code >= 400:
                print(f"  [ERR] {item} / {store['name']}: status={res.status_code}")
                continue
            hidden = res.json().get("hideItem")
            if hidden is not True:
                still_open.append((item, store["name"]))
                print(f"  [!!] {item} / {store['name']}: hideItem={hidden}（非公開になっていません）")
    if not still_open:
        print("  → 全件、倉庫（非公開）状態を確認できました。")
    return still_open


def check_yahoo():
    print(f"\n=== Yahoo 再確認（{len(YAHOO_ITEMS)}件） ===")
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)
    still_exists = []
    for item_code in YAHOO_ITEMS:
        for store in get_yahoo_stores():
            try:
                item = yahoo_get_item(token, store, item_code)
            except Exception as e:
                print(f"  [ERR] {item_code} / {store['name']}: {e}")
                continue
            finally:
                time.sleep(1.0)
            if item is not None:
                still_exists.append((item_code, store["name"]))
                print(f"  [!!] {item_code} / {store['name']}: まだ存在しています（削除できていません）")
    if not still_exists:
        print("  → 全件、削除済み（存在しない）ことを確認できました。")
    return still_exists


def main():
    rakuten_issues = check_rakuten()
    yahoo_issues = check_yahoo()
    print(f"\n=== 総合結果: 楽天 未対応{len(rakuten_issues)}件 / Yahoo 未削除{len(yahoo_issues)}件 ===")


if __name__ == "__main__":
    main()
