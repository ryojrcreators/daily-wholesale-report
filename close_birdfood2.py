"""
一時実行用: ry23012300番台の鳥の餌（Lafeber/RoudyBush/Wild Harvest/Wagner's/Lyric Finch等）で
ブランド名キーワードのスキャンから漏れていた10商品を、楽天（hideItem）とYahoo（deleteItem）
の両方で処理する。DRY_RUN=true（既定）では何も変更しない。
"""

import time

from case_orders_auto_close import (
    DRY_RUN,
    get_spreadsheet,
    get_yahoo_access_token,
    get_yahoo_stores,
    rakuten_hide,
)
from guideline_ban_delete import yahoo_delete_item

with open("rakuten_birdfood2_items.txt", encoding="utf-8") as f:
    RAKUTEN_ITEMS = [line.strip() for line in f if line.strip()]
with open("yahoo_birdfood2_codes.txt", encoding="utf-8") as f:
    YAHOO_CODES = [line.strip() for line in f if line.strip()]


def main():
    print(f"=== 鳥の餌 追加対応（DRY_RUN={DRY_RUN}） ===")

    print(f"\n--- 楽天（{len(RAKUTEN_ITEMS)}件） ---")
    for item in RAKUTEN_ITEMS:
        for shop, msg, ok in rakuten_hide(item):
            print(f"  [{'OK' if ok else 'NG'}] {item} / {shop}: {msg}")
        time.sleep(0.5)

    print(f"\n--- Yahoo（{len(YAHOO_CODES)}件） ---")
    spreadsheet = get_spreadsheet()
    token = get_yahoo_access_token(spreadsheet)
    stores = get_yahoo_stores()
    for code in YAHOO_CODES:
        for store in stores:
            ok, note = yahoo_delete_item(token, store, code)
            if note == "すでに存在しません":
                continue
            print(f"  [{'OK' if ok else 'NG'}] {code} / {store['name']}: {note}")
            time.sleep(1.0)

    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
