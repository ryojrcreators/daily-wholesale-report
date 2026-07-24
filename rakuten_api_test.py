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

def test_search_items():
    """商品検索APIで上位3件取得"""
    url = "https://api.rms.rakuten.co.jp/es/2.0/items/search"
    headers = get_auth_header(SERVICE_SECRET_1, LICENSE_KEY_1)
    params = {"hits": 3, "offset": 1}
    res = requests.get(url, headers=headers, params=params, timeout=15)
    print(f"ステータス: {res.status_code}")
    print(json.dumps(res.json(), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    print(f"店舗名: {SHOP_NAME_1}")
    print("\n=== 商品検索テスト（上位3件） ===")
    test_search_items()
