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

def get_auth_header(service_secret, license_key):
    token = base64.b64encode(f"{service_secret}:{license_key}".encode()).decode()
    return {"Authorization": f"ESA {token}"}

def search_item(keyword: str):
    """キーワードで商品検索して manageNumber / itemNumber を確認"""
    url = "https://api.rms.rakuten.co.jp/es/2.0/items/search"
    headers = get_auth_header(SERVICE_SECRET_1, LICENSE_KEY_1)
    params = {"hits": 5, "offset": 1, "keyword": keyword}
    res = requests.get(url, headers=headers, params=params, timeout=15)
    print(f"ステータス: {res.status_code}")
    data = res.json()
    for r in data.get("results", []):
        item = r.get("item", {})
        print(f"  manageNumber: {item.get('manageNumber')}")
        print(f"  itemNumber:   {item.get('itemNumber')}")
        print(f"  hideItem:     {item.get('hideItem')}")
        print()

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

if __name__ == "__main__":
    print(f"店舗名: {SHOP_NAME_1}")
    print("\n=== capt06 検索 ===")
    search_item("capt06")
