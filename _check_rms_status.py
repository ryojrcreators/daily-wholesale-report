"""1回限りの確認用。指定商品管理番号の楽天RMS上のhideItem状態を確認する（読み取りのみ）。"""
import os
import base64
import requests

MANAGE_NUMBER = os.environ.get("MANAGE_NUMBER", "10000408")

STORES = [
    {
        "name": os.environ["RAKUTEN_SHOP_NAME_1"],
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_1"],
    },
    {
        "name": os.environ["RAKUTEN_SHOP_NAME_2"],
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_2"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_2"],
    },
]

RMS_BASE = "https://api.rms.rakuten.co.jp/es/2.0/items/manage-numbers"

for store in STORES:
    token = base64.b64encode(f"{store['service_secret']}:{store['license_key']}".encode()).decode()
    headers = {"Authorization": f"ESA {token}", "Accept": "application/json"}
    res = requests.get(f"{RMS_BASE}/{MANAGE_NUMBER}", headers=headers, timeout=30)
    if res.status_code == 404:
        print(f"{store['name']}: この店舗には存在しません")
        continue
    if res.status_code >= 400:
        print(f"{store['name']}: 取得エラー status={res.status_code}")
        continue
    data = res.json()
    print(f"{store['name']}: hideItem={data.get('hideItem')}")
