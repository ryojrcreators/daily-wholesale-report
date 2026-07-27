"""
Yahoo!ショッピング 出品停止APIの調査用テストスクリプト（読み取りのみ）

目的:
  楽天の hideItem に相当する「出品停止」のフィールドがYahoo側に何という名前で
  存在するのかを、getItem のレスポンスを全部出力して確認する。

このスクリプトは GET しかしないので、商品は一切変更されない。
対象の商品コードは環境変数 TARGET_ITEM_CODE で指定する。
"""

import os
import json
from xml.etree import ElementTree

import requests
import gspread
from google.oauth2.service_account import Credentials

CLIENT_ID = os.environ["YAHOO_CLIENT_ID"]
CLIENT_SECRET = os.environ["YAHOO_CLIENT_SECRET"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

TARGET_ITEM_CODE = os.environ["TARGET_ITEM_CODE"]

STORES = [
    {"name": os.environ["YAHOO_SHOP_NAME_1"], "seller_id": os.environ["YAHOO_SELLER_ID_1"]},
    {"name": os.environ["YAHOO_SHOP_NAME_2"], "seller_id": os.environ["YAHOO_SELLER_ID_2"]},
]

CONFIG_SHEET_NAME = "Yahoo_Config"
TOKEN_URL = "https://auth.login.yahoo.co.jp/yconnect/v2/token"
BASE = "https://circus.shopping.yahooapis.jp/ShoppingWebService/V1"


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
    # ローテーションされるので新しい値をすぐ保存
    save_refresh_token(spreadsheet, data.get("refresh_token", current))
    return data["access_token"]


# ── 調査本体 ──────────────────────────────────────
def dump_item(token: str, store: dict, item_code: str):
    """getItem で商品の全フィールドを出力する（読み取りのみ）"""
    res = requests.get(
        f"{BASE}/getItem",
        headers={"Authorization": f"Bearer {token}"},
        params={"seller_id": store["seller_id"], "item_code": item_code},
        timeout=30,
    )
    print(f"  ステータス: {res.status_code}")
    if res.status_code >= 400:
        print(f"  {res.text[:1000]}")
        return

    root = ElementTree.fromstring(res.content)
    print("  --- 取得できたフィールド一覧 ---")
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        if not text:
            continue
        # 商品説明などは長いので先頭だけ
        print(f"  {tag}: {text[:120]}")


if __name__ == "__main__":
    spreadsheet = get_spreadsheet()
    token = get_access_token(spreadsheet)

    for store in STORES:
        print(f"\n=== 店舗（{store['name']}）: {TARGET_ITEM_CODE} を取得（変更なし） ===")
        try:
            dump_item(token, store, TARGET_ITEM_CODE)
        except Exception as e:
            print(f"  エラー: {e}")
