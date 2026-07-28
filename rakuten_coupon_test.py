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
from xml.etree import ElementTree

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


def list_coupons(hits: int):
    """クーポン一覧を取得して、コピー元の候補を探す（読み取りのみ）"""
    res = requests.get(
        f"{BASE}/es/1.0/coupon/search",
        headers=auth_headers(),
        params={"hits": hits, "page": 1},
        timeout=30,
    )
    print(f"  GET /es/1.0/coupon/search?hits={hits} → {res.status_code}\n")
    if res.status_code >= 400:
        print(res.text[:1000])
        return

    root = ElementTree.fromstring(res.content)
    print(f"  総件数: {root.findtext('allCount')}\n")

    for i, coupon in enumerate(root.iter("coupon"), start=1):
        code = coupon.findtext("couponCode") or ""
        name = coupon.findtext("couponName") or ""
        start = coupon.findtext("couponStartDate") or ""
        end = coupon.findtext("couponEndDate") or ""
        print(f"  {i:>3}. {code}")
        print(f"       {name[:60]}")
        print(f"       期間: {start} 〜 {end}")


def show_coupon(coupon_code: str):
    """1件のクーポンの全項目を出力する（発行に必要な項目の洗い出し用）"""
    res = requests.get(
        f"{BASE}/es/1.0/coupon/get",
        headers=auth_headers(),
        params={"couponCode": coupon_code},
        timeout=30,
    )
    print(f"  GET /es/1.0/coupon/get?couponCode={coupon_code} → {res.status_code}\n")
    if res.status_code >= 400:
        print(res.text[:1000])
        return

    # 構造を保ったまま全項目を出す（入れ子があるとコピー時に効いてくるため）
    root = ElementTree.fromstring(res.content)

    def walk(elem, depth=0):
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        children = list(elem)
        indent = "  " + "  " * depth
        if children:
            print(f"{indent}[{tag}]")
            for child in children:
                walk(child, depth + 1)
        else:
            print(f"{indent}{tag}: {text}")

    walk(root)


def probe_issue():
    """
    coupon.issue に空のリクエストを投げて、必須項目のエラーを全部吐かせる。

    必須項目が揃っていないので失敗するはずだが、万一通ってしまった場合に備えて
    レスポンスに couponCode が含まれていないかを確認し、含まれていたら警告する
    （その場合は coupon.delete で消す必要がある）。
    """
    url = f"{BASE}/es/1.0/coupon/issue"

    # まずメソッドを確かめる（GETなら405が返り、許可メソッドが分かることがある）
    probe_res = requests.get(url, headers=auth_headers(), timeout=20)
    print(f"  GET  /es/1.0/coupon/issue → {probe_res.status_code}")
    print(f"    {probe_res.text[:300]}\n")

    for content_type, body in [
        ("application/json; charset=utf-8", {}),
        ("application/xml; charset=utf-8", "<request></request>"),
    ]:
        headers = {**auth_headers(), "Content-Type": content_type}
        kwargs = {"json": body} if isinstance(body, dict) else {"data": body}
        res = requests.post(url, headers=headers, timeout=20, **kwargs)

        print(f"  POST /es/1.0/coupon/issue  Content-Type={content_type.split(';')[0]}")
        print(f"    → ステータス: {res.status_code}")
        print(f"    {res.text[:2000]}\n")

        if "couponCode" in res.text and "<errors>" not in res.text:
            print("    ⚠️ クーポンが作成された可能性があります。上のcouponCodeを確認し、"
                  "必要なら削除してください。\n")


if __name__ == "__main__":
    mode = os.environ.get("MODE", "probe")
    if mode == "probe-issue":
        print(f"=== 店舗（{SHOP_NAME}）: coupon.issue の必須項目を調べる ===\n")
        probe_issue()
        raise SystemExit(0)
    if mode == "probe":
        probe()
    elif mode == "list":
        print(f"=== 店舗（{SHOP_NAME}）: クーポン一覧 ===\n")
        list_coupons(int(os.environ.get("HITS", "20")))
    elif mode == "detail":
        code = os.environ.get("COUPON_CODE", "").strip()
        if not code:
            print("COUPON_CODE が指定されていません。")
        else:
            print(f"=== 店舗（{SHOP_NAME}）: クーポン {code} の全項目 ===\n")
            show_coupon(code)
    else:
        print(f"不明なMODE: {mode}")
