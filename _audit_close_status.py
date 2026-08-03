"""1回限りの監査用。H列（在庫対応済み）が埋まっている行すべてについて、
実際に楽天RMS側でhideItem=trueになっているか照合する（読み取りのみ、何も変更しない）。
"""
import os
import json
import base64
import requests
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"

creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
creds = Credentials.from_service_account_info(
    json.loads(creds_json),
    scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
)
gc = gspread.authorize(creds)
ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

values = ws.get_all_values()
rows = values[1:]

done_rows = [
    (i + 2, row[0].strip(), row[1].strip() if len(row) > 1 else "")
    for i, row in enumerate(rows)
    if len(row) > 7 and row[7].strip()
]
print(f"H列（在庫対応済み）が埋まっている行: {len(done_rows)}件")

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
RMS_BASE = "https://api.rms.rakuten.co.jp/es/2.0/items/manage-numbers"


def auth_headers(store):
    token = base64.b64encode(f"{store['service_secret']}:{store['license_key']}".encode()).decode()
    return {"Authorization": f"ESA {token}", "Accept": "application/json"}


mismatches = []
for row_num, item_number, name in done_rows:
    hidden_somewhere = False
    exists_somewhere = False
    for store in STORES:
        res = requests.get(f"{RMS_BASE}/{item_number}", headers=auth_headers(store), timeout=30)
        if res.status_code == 404:
            continue
        if res.status_code >= 400:
            print(f"  [{row_num}] {item_number} @ {store['name']}: 取得エラー status={res.status_code}")
            continue
        exists_somewhere = True
        if res.json().get("hideItem") is True:
            hidden_somewhere = True

    status = "OK" if (hidden_somewhere or not exists_somewhere) else "MISMATCH"
    if status == "MISMATCH":
        mismatches.append((row_num, item_number, name))
    print(f"  [{row_num}] {item_number} {name[:30]}: hidden={hidden_somewhere} exists={exists_somewhere} -> {status}")

print(f"\n=== 監査結果: 総{len(done_rows)}件 / 不一致{len(mismatches)}件 ===")
for row_num, item_number, name in mismatches:
    print(f"  不一致: 行{row_num} {item_number} {name}")
