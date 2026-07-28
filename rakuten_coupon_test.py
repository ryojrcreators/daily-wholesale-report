"""
楽天RMS クーポンAPIの調査用スクリプト（読み取りのみ）

目的:
  1. 今のライセンスキーでクーポンAPIが使えるか（利用申請が通っているか）を確かめる
  2. 正しいエンドポイントURLを特定する
  3. 既存クーポンの項目を確認し、「コピーして期限だけ変えて発行」に必要な項目を洗い出す

商品APIのときと同じく、公式ドキュメントがRMSログインの内側にあるため、
候補URLを順に叩いてステータスコードから正解を探る。
  404 = URLが存在しない / 403 = URLはあるが権限なし（＝利用申請が必要）
  400 = URLもメソッドも合っているがパラメータが違う / 200 = 成功

このスクリプトは search と get しか呼ばないので、クーポンは作成・変更されない。
"""

import os
import base64
import json

import requests

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_1"]
SHOP_NAME = os.environ["RAKUTEN_SHOP_NAME_1"]


def auth_headers() -> dict:
    token = base64.b64encode(f"{SERVICE_SECRET}:{LICENSE_KEY}".encode()).decode()
    return {
        "Authorization": f"ESA {token}",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }


BASE = "https://api.rms.rakuten.co.jp"

# クーポン検索の候補URL。第三者ライブラリの命名（coupon.search）から推測したもの。
SEARCH_CANDIDATES = [
    ("GET",  f"{BASE}/es/2.0/coupon/search"),
    ("GET",  f"{BASE}/es/1.0/coupon/search"),
    ("POST", f"{BASE}/es/1.0/coupon/search"),
    ("GET",  f"{BASE}/es/2.0/coupons/search"),
    ("GET",  f"{BASE}/es/1.0/coupon/get"),
    ("GET",  f"{BASE}/es/2.0/coupon"),
    ("GET",  f"{BASE}/es/1.0/coupon"),
]


def probe():
    print(f"=== 店舗（{SHOP_NAME}）: クーポンAPIのエンドポイント調査 ===\n")
    hits = []

    for method, url in SEARCH_CANDIDATES:
        path = url.split("rms.rakuten.co.jp")[1]
        try:
            res = requests.request(method, url, headers=auth_headers(), timeout=20)
        except Exception as e:
            print(f"  {method:<5} {path}\n    → 通信エラー: {e}\n")
            continue

        print(f"  {method:<5} {path}")
        print(f"    → ステータス: {res.status_code}")

        if res.status_code == 404:
            print("       （URLが存在しない）\n")
            continue

        # 404以外はURLとして意味がある。中身を見せる
        print(f"    {res.text[:500]}\n")
        hits.append((method, url, res.status_code))

    print("── 調査結果 ──")
    if not hits:
        print("  すべて404でした。URLの形が違う可能性があります。")
        return

    for method, url, status in hits:
        path = url.split("rms.rakuten.co.jp")[1]
        if status == 403:
            meaning = "URLは存在するが権限なし → クーポンAPIの利用申請が必要"
        elif status == 400:
            meaning = "URLは正しい。パラメータが足りないだけ → 使える見込み"
        elif status == 200:
            meaning = "成功！このエンドポイントが使える"
        else:
            meaning = "要確認"
        print(f"  {method} {path} → {status}: {meaning}")


if __name__ == "__main__":
    probe()
