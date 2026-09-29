"""
一時実行用: 米カテゴリの拡張キーワード（ライスプロテイン等）で楽天を再確認した結果
見つかった見落とし商品をClose（hideItem=true）する。
"""

import time

from case_orders_auto_close import DRY_RUN, rakuten_hide

ITEM_NUMBERS = [
    "0403ricepro", "0403ricepro-2", "0403ricepro-3",
    "90478001", "90478001-2", "90478001-3",
    "ma01022401", "aj0000008", "jm0000358",
]


def main():
    print(f"=== 楽天 米カテゴリ見落とし追加Close開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_NUMBERS)}件 ===")
    for item in ITEM_NUMBERS:
        results = rakuten_hide(item)
        for shop, msg, ok in results:
            mark = "OK" if ok else "NG"
            print(f"  [{mark}] {item} / {shop}: {msg}")
        time.sleep(0.5)
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
