"""
使い捨てスクリプト。指定した楽天SKU（商品管理番号）について、楽天・Yahoo両方の
出品状態を確認し、まだCloseされていなければCloseする（楽天=hideItem、Yahoo=在庫0）。
Case Ordersのケースには紐づかない、直接指定での実行。
"""

import os

from case_orders_auto_close import (
    YAHOO_SUFFIXES,
    rakuten_hide,
    yahoo_close,
    get_spreadsheet,
    get_yahoo_access_token,
)

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

SKUS = ["jm0000269", "jm0000270", "jm0000749", "je0000500"]


def main():
    print("=== 指定SKUのClose 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：実際には変更しません")

    spreadsheet = get_spreadsheet()
    yahoo_token = get_yahoo_access_token(spreadsheet)

    for sku in SKUS:
        print(f"\n--- {sku} ---")

        for store_name, message, ok in rakuten_hide(sku):
            print(f"  [楽天] {store_name}: {message}")

        candidates = [sku + suffix for suffix in YAHOO_SUFFIXES]
        for store_name, message, ok in yahoo_close(yahoo_token, candidates):
            print(f"  [Yahoo] {store_name}: {message}")

    print("\n=== 指定SKUのClose 完了 ===")


if __name__ == "__main__":
    main()
