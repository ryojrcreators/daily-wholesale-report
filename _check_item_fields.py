"""1回限りの確認用。既存のGET APIで1商品の全フィールドを取得し、価格関連フィールド名を確認する（読み取りのみ）。"""
import os
import json
import base64
import requests

MANAGE_NUMBER = os.environ.get("MANAGE_NUMBER", "10000018")

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
    print(f"=== {store['name']} status={res.status_code} ===")
    if res.status_code == 200:
        data = res.json()
        print("トップレベルキー一覧:", list(data.keys()))

        def find_price_keys(obj, path=""):
            found = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    p = f"{path}.{k}" if path else k
                    if "price" in k.lower():
                        found.append((p, v if not isinstance(v, (dict, list)) else type(v).__name__))
                    found.extend(find_price_keys(v, p))
            elif isinstance(obj, list):
                for i, v in enumerate(obj[:2]):  # サンプルとして先頭2件だけ
                    found.extend(find_price_keys(v, f"{path}[{i}]"))
            return found

        for path, val in find_price_keys(data):
            print(f"  {path} = {val}")

        # variantsの中身を丸ごと見る（価格情報が入っている想定）
        if "variants" in data:
            print("\nvariants抜粋（先頭1件）:")
            variants = data["variants"]
            first_key = next(iter(variants)) if isinstance(variants, dict) else None
            if first_key is not None:
                print(f"  variantId={first_key}")
                print(json.dumps(variants[first_key], ensure_ascii=False, indent=2)[:3000])
        break  # 1店舗分見れれば十分
    elif res.status_code != 404:
        print(res.text[:500])
