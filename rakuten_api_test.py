"""
楽天RMS API テストスクリプト

MODE:
  get  … 商品情報を取得するだけ（読み取り専用・商品は変更されない）※デフォルト
  hide … 指定した商品を実際に非公開（倉庫入り＝販売停止）にする（本番データを変更する）

⚠️ MODE=hide は実際に商品を非公開にします。誤って別の商品を止めてしまわないよう、
   TARGET_MANAGE_NUMBER で対象の商品管理番号を指定したうえで、CONFIRM に "HIDE" と
   正確に入力した場合のみ実行されます（一致しない場合は何も変更せず中止します）。
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

def try_get(service_secret, license_key, manage_number: str):
    """まずGET（読み取りのみ・商品は変更されない）で正しいURLパターンを探す"""
    headers = {
        **get_auth_header(service_secret, license_key),
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }

    base = "https://api.rms.rakuten.co.jp/es/2.0/items"
    urls = [
        f"{base}/manage-numbers/{manage_number}",
        f"{base}/manage-number/{manage_number}",
        f"{base}/item-numbers/{manage_number}",
        f"{base}/get?manageNumber={manage_number}",
        f"{base}/edit?manageNumber={manage_number}",
    ]

    found = None
    for url in urls:
        res = requests.get(url, headers=headers, timeout=15)
        print(f"  GET {url.split('rms.rakuten.co.jp')[1]}")
        print(f"  → ステータス: {res.status_code}")
        if res.status_code == 200:
            print("  → 成功！このURLが正解です")
            print(res.text[:1500])
            found = url
            break
        print()
    return found


def try_patch(service_secret, license_key, url: str):
    """GETで当たったURLに対して hideItem:true をPATCHする（実際に倉庫に入る）"""
    headers = {
        **get_auth_header(service_secret, license_key),
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {"hideItem": True}
    res = requests.patch(url, headers=headers, json=body, timeout=15)
    print(f"\n  PATCH {url.split('rms.rakuten.co.jp')[1]}")
    print(f"  → ステータス: {res.status_code}")
    print(res.text[:1500])

    # 反映確認
    check = requests.get(url, headers=headers, timeout=15)
    if check.status_code == 200:
        print(f"  → 確認: hideItem = {check.json().get('hideItem')}")


if __name__ == "__main__":
    TARGET = os.environ.get("TARGET_MANAGE_NUMBER", "").strip()
    MODE = os.environ.get("MODE", "get").strip()
    CONFIRM = os.environ.get("CONFIRM", "").strip()

    if not TARGET:
        print("TARGET_MANAGE_NUMBER が指定されていません。対象の商品管理番号を指定してください。")
        raise SystemExit(1)

    URL = f"https://api.rms.rakuten.co.jp/es/2.0/items/manage-numbers/{TARGET}"

    stores = [
        (SHOP_NAME_1, SERVICE_SECRET_1, LICENSE_KEY_1),
        (SHOP_NAME_2, SERVICE_SECRET_2, LICENSE_KEY_2),
    ]

    if MODE == "get":
        for shop_name, secret, key in stores:
            print(f"\n=== 店舗（{shop_name}）: {TARGET} の情報を取得 ===")
            try_get(secret, key, TARGET)

    elif MODE == "hide":
        if CONFIRM != "HIDE":
            print(f"\n⚠️ MODE=hide は {TARGET} を実際に非公開（販売停止）にします。")
            print('   実行するには CONFIRM に "HIDE" と正確に入力してください。')
            print("   確認文字列が一致しなかったため、安全のため中止しました（商品は変更されていません）。")
            raise SystemExit(1)

        for shop_name, secret, key in stores:
            print(f"\n=== 店舗（{shop_name}）: {TARGET} を倉庫に入れる ===")
            try_patch(secret, key, URL)

    else:
        print(f"不明なMODE: {MODE}（get または hide を指定してください）")
        raise SystemExit(1)
