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


def probe_edit_required_fields(token: str, store: dict, item_code: str, fields: dict):
    """
    editItem に最小限のパラメータだけ送って、必須項目のエラーを全部吐かせる。
    必ず400で終わるため商品は変更されない（必須項目の洗い出し専用）。
    """
    res = requests.post(
        f"{BASE}/editItem",
        headers={"Authorization": f"Bearer {token}"},
        data={"seller_id": store["seller_id"], "item_code": item_code},
        timeout=30,
    )
    print(f"  editItem（最小構成）→ ステータス: {res.status_code}")

    try:
        root = ElementTree.fromstring(res.content)
    except ElementTree.ParseError:
        print(res.text[:3000])
        return

    required = []
    for err in root.iter("Error"):
        target = (err.findtext("Target") or "").strip()
        message = (err.findtext("Message") or "").strip()
        code = (err.findtext("Code") or "").strip()
        required.append(target)
        print(f"    必須: {target:<28} {code}  {message}")

    print(f"\n  必須項目の数: {len(required)}")

    # getItem で取れる項目名は CamelCase、editItm の必須項目名は snake_case なので
    # 大文字小文字とアンダースコアを潰して突き合わせる
    def norm(s: str) -> str:
        return s.replace("_", "").replace("-", "").lower()

    available = {norm(k): k for k in fields}
    covered, missing = [], []
    for target in required:
        key = available.get(norm(target))
        (covered if key else missing).append(target if not key else f"{target} → {key}")

    print("\n  --- getItem の値で埋められそうな項目 ---")
    for c in covered:
        print(f"    ✅ {c}")
    print("\n  --- getItem に見当たらない項目（送ると消える恐れ） ---")
    for m in missing:
        print(f"    ❌ {m}")
    if not missing:
        print("    （なし）")


def edit_hide_test(token: str, store: dict, item_code: str, before: dict):
    """
    必須4項目 + display=0 だけを送って、省略した項目が保持されるのか消えるのかを確かめる。

    editItem が「部分更新」なら他の項目は残る。「全置換」なら消える。
    消えた場合に備えて、変更前の全項目を省略せずログに出しておく（復元用のバックアップ）。
    """
    print("\n  --- 変更前の全項目（復元用バックアップ。消えた場合はここから戻す） ---")
    for tag, text in before.items():
        print(f"  [{tag}] {text}")

    payload = {
        "seller_id": store["seller_id"],
        "item_code": item_code,
        "path": before.get("Path", ""),
        "name": before.get("Name", ""),
        "product_category": before.get("ProductCategory", ""),
        "price": before.get("Price", ""),
        "display": "0",
    }
    print("\n  --- 送信内容（必須4項目 + display=0 のみ） ---")
    for k, v in payload.items():
        if k != "seller_id":
            print(f"  {k}: {str(v)[:80]}")

    res = requests.post(
        f"{BASE}/editItem",
        headers={"Authorization": f"Bearer {token}"},
        data=payload,
        timeout=30,
    )
    print(f"\n  → ステータス: {res.status_code}")
    print(f"  {res.text[:800]}")
    if res.status_code >= 400:
        print("  失敗したため商品は変更されていません。")
        return

    time.sleep(2)
    after = get_item(token, store, item_code)

    print("\n  --- 変更前後の差分 ---")
    lost, changed = [], []
    for tag, old in before.items():
        new = after.get(tag)
        if new is None:
            lost.append(tag)
            print(f"    ❌ 消えた: {tag}")
        elif new != old:
            changed.append(tag)
            print(f"    🔄 変化: {tag}: {old[:60]} → {new[:60]}")
    for tag in after:
        if tag not in before:
            print(f"    ➕ 増えた: {tag}: {after[tag][:60]}")

    print(f"\n  消えた項目: {len(lost)}個 / 変化した項目: {len(changed)}個")
    if not lost:
        print("  ✅ 省略した項目は保持されました（部分更新として使えます）")
    else:
        print("  ⚠️ 省略した項目が消えました（全置換型。この方法は危険です）")

    print_state("  変更後", after)


# edit-hide-test（2026/07/28実施）で 13000504-ak の6項目が消えたため、その復元用。
# 値は同テストが変更前に出力したバックアップログから転記したもの。
# ※ 画像や Taxable 等は省略しても消えなかったため、復元対象に含めていない。
RESTORE_PAYLOAD_13000504_AK = {
    "path": "ホーム キッチン",
    "name": "【並行輸入品】The Honest Company ベビーおむつ テープタイプ サイズ1 80枚り 吸水性 ソフト",
    "product_category": "44391",
    "price": "9463",
    "display": "1",
    "delivery": "1",
    "lead_time_in_stock": "1",
    "sp_code": "1000005521",
    "headline": "TheHonestCompany ベビーおむつ アメリカーナ",
    "caption": (
        "The Honest Company ベビーおむつ テープタイプ サイズ1 80枚り 吸水性 ソフト"
        "サイズ　34.5×20.3×19.5cm柔らかく伸縮性のあるサイドパネル、快適な伸縮性ウエストバンド、"
        "ぴったりフィットするレッグカフスが特徴低刺激性、超ソフト、超吸収性、優しく、安全なおむつ"
        "水分を素早く吸い混んでくれるので赤ちゃんの繊細なお肌を快適に保ってくれます。"
        "超吸収性おむつは、赤ちゃんのデリケートな肌に優しいデザイン<br><br><br><br />"
    ),
    "explanation": (
        "The Honest Company ベビーおむつ テープタイプ サイズ1 80枚り 吸水性 ソフト"
        "サイズ　34.5×20.3×19.5cm柔らかく伸縮性のあるサイドパネル、快適な伸縮性ウエストバンド、"
        "ぴったりフィットするレッグカフスが特徴低刺激性、超ソフト、超吸収性、優しく、安全なおむつ"
        "水分を素早く吸い混んでくれるので赤ちゃんの繊細なお肌を快適に保ってくれます。"
        "超吸収性おむつは、赤ちゃんのデリケートな肌に優しいデザイン"
    ),
    "sp_additional": (
        "The Honest Company ベビーおむつ テープタイプ サイズ1 80枚り 吸水性 ソフト"
        "サイズ　34.5×20.3×19.5cm柔らかく伸縮性のあるサイドパネル、快適な伸縮性ウエストバンド、"
        "ぴったりフィットするレッグカフスが特徴低刺激性、超ソフト、超吸収性、優しく、安全なおむつ"
        "水分を素早く吸い混んでくれるので赤ちゃんの繊細なお肌を快適に保ってくれます。"
        "超吸収性おむつは、赤ちゃんのデリケートな肌に優しいデザイン"
    ),
}


def restore_edit(token: str, store: dict, item_code: str, before: dict):
    """edit-hide-test で消えた項目を書き戻す"""
    if item_code != "13000504-ak":
        print(f"  この商品コード用の復元データがありません（対応: 13000504-ak）")
        return

    payload = {"seller_id": store["seller_id"], "item_code": item_code,
               **RESTORE_PAYLOAD_13000504_AK}

    res = requests.post(
        f"{BASE}/editItem",
        headers={"Authorization": f"Bearer {token}"},
        data=payload,
        timeout=30,
    )
    print(f"  → ステータス: {res.status_code}")
    print(f"  {res.text[:800]}")
    if res.status_code >= 400:
        return

    time.sleep(2)
    after = get_item(token, store, item_code)

    print("\n  --- 復元後の状態 ---")
    for tag in ["Headline", "Caption", "Explanation", "SpAdditional", "SpCode",
                "LeadTimeInStock", "Delivery", "Display", "EditingFlag"]:
        value = after.get(tag)
        mark = "✅" if value else "❌"
        print(f"  {mark} {tag}: {str(value)[:80]}")


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


def publish(token: str, store: dict, item_code: str):
    """編集内容を実店舗に反映する（商品個別反映API・submitItem。seller_id+item_codeが必須）"""
    res = requests.post(
        f"{BASE}/submitItem",
        headers={"Authorization": f"Bearer {token}"},
        data={"seller_id": store["seller_id"], "item_code": item_code},
        timeout=60,
    )
    print(f"  反映API(submitItem) → ステータス: {res.status_code}")
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
            publish(token, store, TARGET_ITEM_CODE)
            continue

        if MODE == "probe-edit":
            probe_edit_required_fields(token, store, TARGET_ITEM_CODE, before)
        elif MODE == "edit-hide-test":
            edit_hide_test(token, store, TARGET_ITEM_CODE, before)
        elif MODE == "restore-edit":
            restore_edit(token, store, TARGET_ITEM_CODE, before)
        elif MODE == "hide":
            print(f"  ※ 復元用に現在の在庫数を控えてください: {before.get('Quantity')}")
            change_stock(token, store, TARGET_ITEM_CODE, "0", before)
        elif MODE == "restore":
            if not RESTORE_QUANTITY:
                print("  復元する在庫数（restore_quantity）が指定されていません。中止します。")
                continue
            change_stock(token, store, TARGET_ITEM_CODE, RESTORE_QUANTITY, before)


if __name__ == "__main__":
    main()
