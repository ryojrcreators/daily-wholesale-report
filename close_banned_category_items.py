"""
一時実行用: 2026-09-30から楽天が新たに出品禁止にするカテゴリ
（外国産米／ペットフード・おやつ・サプリ・ヘアケア用品）に該当すると人が確認した
商品管理番号を、楽天の2店舗（Americana/Founder）からClose（hideItem=true）する。

対象リストは debug_banned_category_scan.py によるキーワードスキャン結果を
人が目視で確認して確定させたもの（誤検出だったペット用おもちゃ・給餌器・食器・
米加工品は対象外にできる場合は除外している）。

DRY_RUN=true（既定）では実際には変更しない。
"""

import os
import time

from case_orders_auto_close import DRY_RUN, rakuten_hide

ITEM_NUMBERS = [
    # 米（生米。Augason Farms 非常食バケツ）
    "ry23013233", "0126re023", "ry23013235", "ry23013235-1",
    # 米加工品（ライスパスタ・玄米茶・ライスシロップ・ライスクリスプ等。含める指示済み）
    "ma66593", "my031904270", "23007669", "mins-001", "my20230867", "my20230867-3",
    "my20230438", "160515-055", "150602-057", "150602-056",
    # ペットフード／おやつ（Greenies各種・Milk-Bone・Sunseed・Carna4）
    "11007133", "11007134", "11007135", "11007136", "9497141", "9497142",
    "je0000982", "je0001098",
    "my094301202", "my094301203", "my094301204",
    "my202211033", "my202211033-2", "my202211034", "my202211034-2",
    "my202211035", "my202211035-2", "my202211036", "my202211036-2",
    "my202211037", "my202211037-2", "my202211038", "my202211038-2",
    "my20221254", "my20221255", "my20221256", "my20221257",
    "my20221258", "my20221259", "my20221260", "my20221261",
    "wa-9497141", "wa-yi250218-04", "wa-yi250414-01",
    # ペット用ヘアケア（シャンプー）
    "14007001",
]


def main():
    print(f"=== 出品禁止カテゴリ対応Close開始（DRY_RUN={DRY_RUN}） 対象{len(ITEM_NUMBERS)}件 ===")
    for item in ITEM_NUMBERS:
        results = rakuten_hide(item)
        for shop, msg, ok in results:
            mark = "OK" if ok else "NG"
            print(f"  [{mark}] {item} / {shop}: {msg}")
        time.sleep(0.5)
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
