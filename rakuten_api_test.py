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

def get_auth_header(service_secret, license_key):
    token = base64.b64encode(f"{service_secret}:{license_key}".encode()).decode()
    return {"Authorization": f"ESA {token}"}

def test_get_item(item_id: str):
    """商品管理番号で1件取得"""
    url = f"https://api.rms.rakuten.co.jp/es/2.0/items/{item_id}"
    headers = get_auth_header(SERVICE_SECRET_1, LICENSE_KEY_1)
    res = requests.get(url, headers=headers, timeout=15)
    print(f"ステータス: {res.status_code}")
    print(json.dumps(res.json(), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    # テスト用に商品管理番号を1つ入れてください
    TEST_ITEM_ID = "kraft3030-02"
    print(f"=== 商品取得テスト: {TEST_ITEM_ID} ===")
    test_get_item(TEST_ITEM_ID)
