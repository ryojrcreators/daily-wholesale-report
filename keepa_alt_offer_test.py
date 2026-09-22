"""
Alt Offer自動化の検証用: 指定ASINについてKeepaから商品名・画像・特徴（feature bullets）が
取得できるか確認する（読み取りのみ、書き込みは一切行わない）。

背景（2026-09-22、ユーザー確認済み）:
  楽天/WowmaのAlt Offerケース対応メールを半自動化するにあたり、代替商品（Amazon.comの
  US商品）の商品名・画像・特徴を取得する必要がある。SP-API（Shop LA!=JP、
  US International Service=EU）はUS本体のカタログ情報を取得する権限を持たない可能性が
  高いため、既存契約のKeepa APIで代替できるか確認する。
"""
import os

import requests

KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]
ASIN = os.environ.get("TEST_ASIN", "B0DT4ZZXD2")


def main():
    print(f"=== Keepa商品情報確認: {ASIN} ===\n")
    url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain=1&asin={ASIN}&stats=1"
    res = requests.get(url, timeout=60)
    res.raise_for_status()
    data = res.json()
    products = data.get("products", [])
    print(f"tokensLeft: {data.get('tokensLeft')}")
    if not products:
        print("商品が見つかりませんでした")
        return

    p = products[0]
    print(f"title: {p.get('title')}")
    print(f"brand: {p.get('brand')}")
    print(f"imagesCSV: {p.get('imagesCSV')}")
    if p.get("imagesCSV"):
        first_image = p["imagesCSV"].split(",")[0]
        print(f"画像URL(推定): https://images-na.ssl-images-amazon.com/images/I/{first_image}")
    print(f"features: {p.get('features')}")


if __name__ == "__main__":
    main()
