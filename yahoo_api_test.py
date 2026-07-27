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

# 出品停止に効きそうなフィールドの候補。実機で順に試して、実際に値が変わったものを採用する。
HIDE_ATTEMPTS = [
    ("display=0",     {"display": "0"}),
    ("hidden-flag=1", {"hidden-flag": "1"}),
    ("hiddenFlag=1",  {"hiddenFlag": "1"}),
]
RESTORE_ATTEMPTS = [
    ("display=1",     {"display": "1"}),
    ("hidden-flag=0", {"hidden-flag": "0"}),
    ("hiddenFlag=0",  {"hiddenFlag": "0"}),
]


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


def edit_item(token: str, store: dict, item_code: str, extra: dict):
    res = requests.post(
        f"{BASE}/editItem",
        headers={"Authorization": f"Bearer {token}"},
        data={"seller_id": store["seller_id"], "item_code": item_code, **extra},
        timeout=30,
    )
    print(f"    → ステータス: {res.status_code}")
    if res.status_code >= 400:
        print(f"    {res.text[:400]}")
        return False
    return True


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


def try_attempts(token: str, store: dict, item_code: str, attempts: list, before: dict):
    """パラメータ名の候補を順に試し、実際に値が変わったものを特定する"""
    for label, params in attempts:
        print(f"\n  試行: editItem {label}")
        if not edit_item(token, store, item_code, params):
            continue

        time.sleep(2)
        after = get_item(token, store, item_code)
        print_state("    変更後", after)

        if (after.get("Display") != before.get("Display")
                or after.get("HiddenFlag") != before.get("HiddenFlag")):
            print(f"    ✅ このパラメータが効きました: {label}")
            return after, label

        print("    → 値は変わりませんでした。次の候補を試します。")

    print("\n  ⚠️ どの候補も効きませんでした。")
    return None, None


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

        attempts = HIDE_ATTEMPTS if MODE == "hide" else RESTORE_ATTEMPTS
        after, label = try_attempts(token, store, TARGET_ITEM_CODE, attempts, before)

        if after and after.get("EditingFlag") == "1":
            print("\n  ※ EditingFlag=1（編集中・未反映）です。"
                  "実店舗に反映するには MODE=publish で反映APIを実行してください。")


if __name__ == "__main__":
    main()
