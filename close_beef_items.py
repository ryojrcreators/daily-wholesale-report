"""
一時実行用: 2026-09-30から楽天が新たに出品禁止にする「牛エキス入り食品」に該当すると
人が確認した商品管理番号を、楽天の2店舗（Americana/Founder）からClose（hideItem=true）する。

対象リストは debug_beef_scan.py によるキーワードスキャン結果を人が目視で確認して確定
させたもの（ビーフンニ・ロビーフロア等の無関係な偶然一致や、ビーフジャーキー"用"の
調理家電は対象外にしている）。

DRY_RUN=true（既定）では実際には変更しない。
"""

import time

from case_orders_auto_close import DRY_RUN, rakuten_hide

ITEM_NUMBERS = [
    "my20231547", "my20231547-2", "my20231547-4", "my20231547-8",
    "now1960", "now1960-2", "now1960-3",
    "nissin2", "nissin4", "nissin8",
    "ry23014184", "ry23014187", "ry23014176",
    "my083102214", "jo0000060", "aj0001016", "ma01082402",
]


def main():
    print(f"=== 牛エキス系Close開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_NUMBERS)}件 ===")
    for item in ITEM_NUMBERS:
        results = rakuten_hide(item)
        for shop, msg, ok in results:
            mark = "OK" if ok else "NG"
            print(f"  [{mark}] {item} / {shop}: {msg}")
        time.sleep(0.5)
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
