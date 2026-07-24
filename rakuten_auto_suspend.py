"""
楽天 仕入不可商品 自動販売停止スクリプト

- スプレッドシート「ASINあり」の在庫チェック列（E列）が「⚠️ 仕入不可」の商品を抽出
- 楽天RMS APIで hideItem: true にして倉庫（販売停止）に移す
- 結果をH列「販売停止」に記録し、次回以降は再処理しない

RMS API仕様（実機で確認済み）:
  GET   https://api.rms.rakuten.co.jp/es/2.0/items/manage-numbers/{商品管理番号}
  PATCH 同上  body: {"hideItem": true}  → 成功時 204
"""

import os
import time
import json
import base64
import requests
import gspread
from google.oauth2.service_account import Credentials

# ── 設定 ──────────────────────────────────────────
SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"

# DRY_RUN=true の間は実際にAPIで停止せず、対象の一覧を表示するだけ
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# 1回の実行で処理する最大件数（安全弁）
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "200"))

API_INTERVAL = 1.0          # RMS APIのレート制限対策（秒）
SHEET_WRITE_INTERVAL = 1.2  # Sheets書き込みクォータ対策（秒）
SHEET_WRITE_RETRIES = 5

TARGET_STATUS = "⚠️ 仕入不可"

# 列インデックス（0始まり）
COL_ITEM_ID = 0      # A 商品管理番号
COL_NAME = 1         # B 商品名
COL_STOCK_CHECK = 4  # E 在庫チェック
COL_SUSPEND = 7      # H 販売停止（このスクリプトが書き込む）

RMS_BASE = "https://api.rms.rakuten.co.jp/es/2.0/items/manage-numbers"

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


def auth_headers(store: dict) -> dict:
    token = base64.b64encode(
        f"{store['service_secret']}:{store['license_key']}".encode()
    ).decode()
    return {
        "Authorization": f"ESA {token}",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }


# ── Google Sheets ─────────────────────────────────
def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def write_with_retry(sheet, updates: list):
    """Sheets書き込み。429（クォータ超過）時は待機してリトライする。"""
    for attempt in range(SHEET_WRITE_RETRIES):
        try:
            sheet.batch_update(updates)
            return
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < SHEET_WRITE_RETRIES - 1:
                wait = 20 * (attempt + 1)
                print(f"  Sheets書き込みクォータ超過。{wait}秒待機してリトライ...")
                time.sleep(wait)
            else:
                raise


# ── RMS API ───────────────────────────────────────
def fetch_item(store: dict, manage_number: str):
    """商品を取得。存在しなければ None。取得エラーは例外を投げる。"""
    res = requests.get(
        f"{RMS_BASE}/{manage_number}", headers=auth_headers(store), timeout=30
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    return res.json()


def hide_item(store: dict, manage_number: str):
    """hideItem: true にして倉庫（販売停止）へ。成功時は204。"""
    res = requests.patch(
        f"{RMS_BASE}/{manage_number}",
        headers=auth_headers(store),
        json={"hideItem": True},
        timeout=30,
    )
    res.raise_for_status()


def suspend_one(manage_number: str) -> str:
    """
    2店舗を順に探して、見つかった店舗で販売停止にする。
    戻り値はH列に書き込む結果文字列。
    """
    results = []
    for store in STORES:
        try:
            item = fetch_item(store, manage_number)
        except Exception as e:
            results.append(f"{store['name']}:取得エラー")
            print(f"    {store['name']}: 取得エラー {e}")
            continue
        finally:
            time.sleep(API_INTERVAL)

        if item is None:
            continue  # この店舗には存在しない

        if item.get("hideItem") is True:
            results.append(f"{store['name']}:停止済")
            print(f"    {store['name']}: すでに倉庫（変更なし）")
            continue

        if DRY_RUN:
            results.append(f"{store['name']}:停止対象(DRY)")
            print(f"    {store['name']}: 【DRY RUN】停止対象")
            continue

        try:
            hide_item(store, manage_number)
            results.append(f"{store['name']}:停止")
            print(f"    {store['name']}: ✅ 販売停止しました")
        except Exception as e:
            results.append(f"{store['name']}:停止失敗")
            print(f"    {store['name']}: ❌ 停止失敗 {e}")
        finally:
            time.sleep(API_INTERVAL)

    if not results:
        return "対象商品なし"
    return " / ".join(results)


# ── メイン処理 ────────────────────────────────────
def main():
    print("=== 楽天 仕入不可商品の自動販売停止 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：実際の販売停止は行いません")

    sheet = get_sheet()
    rows = sheet.get_all_values()[1:]
    print(f"総行数: {len(rows)}")

    targets = [
        (i + 2, row)  # スプレッドシート上の行番号（ヘッダー1行 + 0始まり補正）
        for i, row in enumerate(rows)
        if len(row) > COL_STOCK_CHECK
        and row[COL_STOCK_CHECK].strip() == TARGET_STATUS
        and (len(row) <= COL_SUSPEND or row[COL_SUSPEND].strip() == "")
        and row[COL_ITEM_ID].strip() != ""
    ]

    print(f"仕入不可・未処理: {len(targets)}件")
    if not targets:
        print("処理対象なし。終了。")
        return

    targets = targets[:MAX_PER_RUN]
    print(f"今回処理: {len(targets)}件\n")

    for sheet_row, row in targets:
        manage_number = row[COL_ITEM_ID].strip()
        name = row[COL_NAME][:40] if len(row) > COL_NAME else ""
        print(f"  [{sheet_row}] {manage_number} {name}")

        result = suspend_one(manage_number)

        cell = gspread.utils.rowcol_to_a1(sheet_row, COL_SUSPEND + 1)
        write_with_retry(sheet, [{"range": cell, "values": [[result]]}])
        time.sleep(SHEET_WRITE_INTERVAL)

    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
