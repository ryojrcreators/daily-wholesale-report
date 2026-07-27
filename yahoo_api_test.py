"""
Yahoo!ショッピング 出品停止APIの調査用テストスクリプト

MODE:
  dump    … getItem で全フィールドを出力するだけ（読み取りのみ・変更なし）
  hide    … 出品停止を試す（editItem のパラメータ名を順に試し、実際に変わったものを特定する）
  restore … hide で変更したものを元に戻す
  publish … 「反映」APIを叩いて、編集内容を実店舗に反映する

Yahooは「編集 → 反映」の2段階のため、editItem が成功しても EditingFlag が 1（編集中）の
ままだと実店舗にはまだ反映されていない。hide の後に必ず EditingFlag を確認すること。
"""

import os
import json
import time
from xml.etree import ElementTree

import requests
import gspread
from google.oauth2.service_account import Credentials

CLIENT_ID = os.environ["YAHOO_CLIENT_ID"]
CLIENT_SECRET = os.environ["YAHOO_CLIENT_SECRET"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

TARGET_ITEM_CODE = os.environ["TARGET_ITEM_CODE"]
MODE = os.environ.get("MODE", "dump")

STORES = [
    {"name": os.environ["YAHOO_SHOP_NAME_1"], "seller_id": os.environ["YAHOO_SELLER_ID_1"]},
    {"name": os.environ["YAHOO_SHOP_NAME_2"], "seller_id": os.environ["YAHOO_SELLER_ID_2"]},
]

CONFIG_SHEET_NAME = "Yahoo_Config"
TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"

# editItem は部分更新ができず、全項目の送信が必須（path/name/product_category...）。
# 送り漏れで商品ページを壊すリスクがあるため使わない。
# 代わりに在庫数だけを更新する setStock を使い、在庫0にして注文を止める。
# 復元用の在庫数は RESTORE_QUANTITY で指定する。
RESTORE_QUANTITY = os.environ.get("RESTORE_QUANTITY", "")


# ── リフレッシュトークン（yahoo_listing_sync.py と同じ仕組み） ──
def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def load_refresh_token(spreadsheet) -> str:
    ws = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    for row in ws.get_all_values():
        if row and row[0] == "refresh_token":
            return row[1]
    raise RuntimeError("Yahoo_Config タブに refresh_token が見つかりません。")


def save_refresh_token(spreadsheet, new_token: str):
    ws = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0] == "refresh_token":
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_token]])
            return
    ws.append_row(["refresh_token", new_token])


def get_access_token(spreadsheet) -> str:
    current = load_refresh_token(spreadsheet)
    res = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": current},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"アクセストークン更新失敗（status={res.status_code}）: {res.text[:500]}")
    data = res.json()
    save_refresh_token(spreadsheet, data.get("refresh_token", current))
    return data["access_token"]


# ── API 呼び出し ──────────────────────────────────
def get_item(token: str, store: dict, item_code: str):
    """getItem。存在しない場合は None。取得できたら {タグ名: 値} を返す。"""
    res = requests.get(
        f"{BASE}/getItem",
        headers={"Authorization": f"Bearer {token}"},
        params={"seller_id": store["seller_id"], "item_code": item_code},
        timeout=30,
    )
    if res.status_code >= 400:
        # 他店舗に無い商品は 400 + it-05002 が返る
        if "it-05002" in res.text:
            return None
        raise RuntimeError(f"getItem エラー（status={res.status_code}）: {res.text[:500]}")

    root = ElementTree.fromstring(res.content)
    fields = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        if text and tag not in fields:
            fields[tag] = text
    return fields


def print_state(label: str, fields: dict):
    print(f"  {label}: Display={fields.get('Display')} / "
          f"HiddenFlag={fields.get('HiddenFlag')} / "
          f"EditingFlag={fields.get('EditingFlag')} / "
          f"Quantity={fields.get('Quantity')}")


def set_stock(token: str, store: dict, item_code: str, quantity: str) -> bool:
    """在庫数だけを更新する。他の項目は一切変更しないので商品ページを壊さない。"""
    res = requests.post(
        f"{BASE}/setStock",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "seller_id": store["seller_id"],
            "item_code": item_code,
            "quantity": quantity,
        },
        timeout=30,
    )
    print(f"    → ステータス: {res.status_code}")
    print(f"    {res.text[:600]}")
    return res.status_code < 400


def publish(token: str, store: dict):
    """編集内容を実店舗に反映する"""
    res = requests.post(
        f"{BASE}/publish",
        headers={"Authorization": f"Bearer {token}"},
        data={"seller_id": store["seller_id"]},
        timeout=60,
    )
    print(f"  反映API → ステータス: {res.status_code}")
    print(f"  {res.text[:600]}")


def change_stock(token: str, store: dict, item_code: str, quantity: str, before: dict):
    print(f"\n  setStock quantity={quantity}")
    if not set_stock(token, store, item_code, quantity):
        return

    time.sleep(2)
    after = get_item(token, store, item_code)
    print_state("  変更後", after)

    if after.get("Quantity") == quantity:
        print(f"  ✅ 在庫数を {before.get('Quantity')} → {quantity} に変更できました")
    else:
        print(f"  ⚠️ 在庫数が反映されていません（現在: {after.get('Quantity')}）")

    if after.get("EditingFlag") == "1":
        print("  ※ EditingFlag=1（編集中・未反映）。MODE=publish で反映APIを試してください。")


# ── メイン ────────────────────────────────────────
def main():
    spreadsheet = get_spreadsheet()
    token = get_access_token(spreadsheet)

    for store in STORES:
        print(f"\n=== 店舗（{store['name']}）: {TARGET_ITEM_CODE} / MODE={MODE} ===")

        try:
            before = get_item(token, store, TARGET_ITEM_CODE)
        except Exception as e:
            print(f"  エラー: {e}")
            continue

        if before is None:
            print("  この店舗にはこの商品コードは存在しません（スキップ）")
            continue

        if MODE == "dump":
            print("  --- 取得できたフィールド一覧 ---")
            for tag, text in before.items():
                print(f"  {tag}: {text[:120]}")
            continue

        print_state("現在", before)

        if MODE == "publish":
            publish(token, store)
            continue

        if MODE == "hide":
            print(f"  ※ 復元用に現在の在庫数を控えてください: {before.get('Quantity')}")
            change_stock(token, store, TARGET_ITEM_CODE, "0", before)
        elif MODE == "restore":
            if not RESTORE_QUANTITY:
                print("  復元する在庫数（restore_quantity）が指定されていません。中止します。")
                continue
            change_stock(token, store, TARGET_ITEM_CODE, RESTORE_QUANTITY, before)


if __name__ == "__main__":
    main()
