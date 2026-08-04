"""
一時調査用（読み取りのみ）：0421ninpart3の価格違和感の原因調査のため、
楽天RMS APIから返るvariants構造をそのまま全部出力する。
"""

from case_orders_auto_close import RAKUTEN_STORES, RMS_BASE, rakuten_auth_headers
import requests
import json

TARGET = "0421ninpart3"

for store in RAKUTEN_STORES:
    headers = rakuten_auth_headers(store)
    url = f"{RMS_BASE}/{TARGET}"
    res = requests.get(url, headers=headers, timeout=30)
    print(f"\n=== 店舗（{store['name']}） status={res.status_code} ===")
    if res.status_code != 200:
        print(res.text[:500])
        continue
    item = res.json()
    print(f"itemNumber: {item.get('itemNumber')}")
    print(f"title: {item.get('title')}")
    variants = item.get("variants") or {}
    print(f"variants件数: {len(variants)}")
    for vid, v in variants.items():
        print(f"  variantId={vid} standardPrice={v.get('standardPrice')} "
              f"salePrice={v.get('salePrice')} taxRate={v.get('taxRate')}")
