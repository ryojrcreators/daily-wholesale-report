"""
Wowma API接続確認用の使い捨てスクリプト。
searchItemInfo を1商品だけ叩いて、生のレスポンス（XML）をそのまま表示する。
価格は変更しない（読み取りのみ）。

タグ名の実物確認ができたら、case_orders_wowma.py の推測部分を実物に合わせて修正し、
このスクリプトと専用ワークフローは削除する。
"""

import os
import requests

WOWMA_BASE = "https://api.manager.wowma.jp/wmshopapi"


def main():
    item_code = os.environ.get("TEST_ITEM_CODE", "ry23010062")
    headers = {
        "Authorization": f"Bearer {os.environ['WOWMA_API_KEY']}",
        "Content-Type": "application/xml; charset=UTF-8",
    }
    res = requests.get(
        f"{WOWMA_BASE}/searchItemInfo",
        headers=headers,
        params={"shopId": os.environ["WOWMA_SHOP_ID"], "itemCode": item_code},
        timeout=30,
    )
    print(f"item_code: {item_code}")
    print(f"status_code: {res.status_code}")
    print("---- raw response ----")
    print(res.text)


if __name__ == "__main__":
    main()
