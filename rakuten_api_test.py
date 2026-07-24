"""
楽天RMS API テストスクリプト
- 商品1件の情報を取得して、どんなデータが返ってくるか確認する
"""

import os
import base64
import requests
import json

SERVICE_SECRET_1 = os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"]
LICENSE_KEY_1    = os.environ["RAKUTEN_RMS_LICENSE_KEY_1"]
SHOP_NAME_1      = os.environ["RAKUTEN_SHOP_NAME_1"]
SERVICE_SECRET_2 = os.environ["RAKUTEN_RMS_SERVICE_SECRET_2"]
LICENSE_KEY_2    = os.environ["RAKUTEN_RMS_LICENSE_KEY_2"]
SHOP_NAME_2      = os.environ["RAKUTEN_SHOP_NAME_2"]

def get_auth_header(service_secret, license_key):
    token = base64.b64encode(f"{service_secret}:{license_key}".encode()).decode()
    return {"Authorization": f"ESA {token}"}

def find_item_by_manage_number(service_secret, license_key, target: str):
    """全件スキャンして manageNumber が一致する商品を探す"""
    url = "https://api.rms.rakuten.co.jp/es/2.0/items/search"
    headers = get_auth_header(service_secret, license_key)
    cursor = "*"
    while True:
        params = {"hits": 100, "cursorMark": cursor}
        res = requests.get(url, headers=headers, params=params, timeout=30)
        data = res.json()
        results = data.get("results", [])
        for r in results:
            item = r.get("item", {})
            mn = item.get("manageNumber", "")
            if target.lower() in mn.lower():
                print(f"  見つかりました！")
                print(f"  manageNumber: {mn}")
                print(f"  itemNumber:   {item.get('itemNumber')}")
                print(f"  hideItem:     {item.get('hideItem')}")
                return
        next_cursor = data.get("nextCursorMark")
        if not next_cursor or next_cursor == cursor:
            print(f"  → 全件スキャン完了。{target} は見つかりませんでした。")
            return
        cursor = next_cursor

def hide_item(item_number: str):
    """商品をhideItem:trueにして倉庫（販売停止）にする"""
    url = f"https://api.rms.rakuten.co.jp/es/2.0/items/{item_number}"
    headers = {
        **get_auth_header(SERVICE_SECRET_1, LICENSE_KEY_1),
        "Content-Type": "application/json",
    }
    body = {"item": {"hideItem": True}}
    res = requests.patch(url, headers=headers, json=body, timeout=15)
    print(f"ステータス: {res.status_code}")
    print(json.dumps(res.json(), ensure_ascii=False, indent=2))

def hide_item(service_secret, license_key, shop_name, manage_number: str):
    """商品をhideItem:trueにして倉庫（販売停止）にする"""
    # パターン1: manageNumber そのまま
    # パターン2: shop_name:manageNumber
    for url_id in [manage_number, f"{shop_name}:{manage_number}"]:
        url = f"https://api.rms.rakuten.co.jp/es/2.0/items/{url_id}"
        headers = {
            **get_auth_header(service_secret, license_key),
            "Content-Type": "application/json",
        }
        body = {"item": {"hideItem": True}}
        res = requests.patch(url, headers=headers, json=body, timeout=15)
        print(f"  URL: {url}")
        print(f"  ステータス: {res.status_code}")
        if res.status_code == 200:
            print("  → 成功！")
            print(json.dumps(res.json(), ensure_ascii=False, indent=2))
            return
        else:
            print(f"  → {res.json()}")

if __name__ == "__main__":
    TARGET = "capt06"
    print(f"\n=== 店舗1（{SHOP_NAME_1}）: {TARGET} を倉庫入れ ===")
    hide_item(SERVICE_SECRET_1, LICENSE_KEY_1, SHOP_NAME_1, TARGET)
