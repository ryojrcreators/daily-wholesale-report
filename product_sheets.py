import os
import csv
import json
import tempfile
import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
from urllib.parse import quote
from status_sheet import update_status

# ===== 設定（環境変数から読み込み） =====
DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
DOWNLOAD_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/products/download"
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

# タイプごとの設定
TARGETS = [
    {"type_value": "products", "key_col": "code",  "sheet_name": "Product"},
    {"type_value": "skus",     "key_col": "Code",   "sheet_name": "SKU"},
    {"type_value": "upcs",     "key_col": "Code",   "sheet_name": "UPC"},
]


def login_and_get_cookies():
    """ログインしてcookieを返す"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # ===== Basic認証 =====
        print("Basic認証付きでトップページを開いています...")
        page.goto(LOGIN_URL, wait_until="networkidle")

        # ===== Loginボタンをクリック =====
        print("Loginボタンをクリックしています...")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")

        # ===== フォームログイン =====
        print("フォームログインを処理しています...")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")
        print("ログイン完了")

        # ===== ダウンロードページへ移動してcookieを取得 =====
        page.goto(DOWNLOAD_URL, wait_until="networkidle")
        page.wait_for_timeout(1000)

        cookies = context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        browser.close()

    return cookie_dict


def download_csv_by_type(type_value, cookie_dict):
    """指定タイプのCSVをrequestsでダウンロードする"""
    print(f"  type={type_value}, Purchaser=All でダウンロード中...")

    # フォームをPOSTで送信（ドロップダウンの値を直接送る）
    response = requests.post(
        f"https://{DOMAIN}/products/download",
        data={
            "type": type_value,
            "purchaser": "",
        },
        cookies=cookie_dict,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": f"https://{DOMAIN}/products/download",
        },
        auth=(LOGIN_ID_1, LOGIN_PASS_1),
        allow_redirects=True,
    )
    print(f"  HTTPステータス: {response.status_code}")

    if response.status_code != 200:
        raise Exception(f"ダウンロード失敗: type={type_value}, status={response.status_code}")

    tmp_path = tempfile.mktemp(suffix=".csv")
    with open(tmp_path, "wb") as f:
        f.write(response.content)
    print(f"  CSVを保存しました: {tmp_path}")
    return tmp_path


def update_sheet(csv_path, key_col, sheet_name):
    """シートを新規追加のみで更新する"""
    print(f"  スプレッドシート（{sheet_name}タブ）を更新しています...")

    credentials_info = json.loads(GOOGLE_CREDENTIALS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
    gc = gspread.authorize(credentials)

    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.worksheet(sheet_name)

    # 新しいCSVを読み込む
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        new_rows = list(reader)

    if not new_rows:
        print(f"  CSVが空です。スキップします。")
        return

    new_headers = new_rows[0]
    new_data = new_rows[1:]

    # キー列のインデックスを取得
    try:
        key_idx_new = new_headers.index(key_col)
    except ValueError:
        raise Exception(f"CSVにキー列 '{key_col}' が見つかりません。列名: {new_headers}")

    # 既存データを取得
    existing_data = worksheet.get_all_values()

    # シートが空の場合：全データを分割して書き込む
    if not existing_data or not any(cell for row in existing_data for cell in row):
        print(f"  シートが空のため、全データを書き込みます...")
        all_data = [new_headers] + new_data
        chunk_size = 5000
        for i in range(0, len(all_data), chunk_size):
            chunk = all_data[i:i + chunk_size]
            if i == 0:
                worksheet.update(chunk)
            else:
                worksheet.append_rows(chunk)
            print(f"  {min(i + chunk_size, len(all_data))}/{len(all_data)}行 書き込み済み...")
        print(f"  ヘッダー + {len(new_data)}行 を書き込みました")
        return

    existing_headers = existing_data[0]
    existing_rows = existing_data[1:]

    # キー列のインデックスを取得（既存シート）
    try:
        key_idx_ex = existing_headers.index(key_col)
    except ValueError:
        raise Exception(f"既存シートにキー列 '{key_col}' が見つかりません")

    # 既存のキーをセットに格納
    existing_keys = set()
    for row in existing_rows:
        if len(row) > key_idx_ex and row[key_idx_ex]:
            existing_keys.add(row[key_idx_ex])

    # 新規行だけ抽出
    new_only = []
    for row in new_data:
        if not any(row):
            continue
        if len(row) <= key_idx_new:
            continue
        key = row[key_idx_new]
        if key and key not in existing_keys:
            new_only.append(row)

    print(f"  新規追加: {len(new_only)}行 / スキップ（既存）: {len(new_data) - len(new_only)}行")

    if new_only:
        # 末尾に追記（appendを使って効率よく）
        worksheet.append_rows(new_only)
        print(f"  {len(new_only)}行 を追記しました")
    else:
        print(f"  追加するデータはありませんでした")


if __name__ == "__main__":
    print("=== Product/SKU/UPC CSV → Google Sheets ===")

    # ログインは1回だけ
    cookie_dict = login_and_get_cookies()

    for target in TARGETS:
        print(f"\n--- {target['sheet_name']} タブ処理開始 ---")
        csv_path = download_csv_by_type(target["type_value"], cookie_dict)
        update_sheet(csv_path, target["key_col"], target["sheet_name"])
        update_status(target["sheet_name"])

    print("\n=== 完了 ===")
