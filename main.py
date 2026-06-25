import os
import csv
import json
import tempfile
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
CSV_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/po-temps/wholesale?flag=daily&purchaser=&_supplier_ids=&suppliers_select_all=0"

GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]


def download_csv():
    """CSVをダウンロードする"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # ===== ステップ1：Basic認証 =====
        print("Basic認証付きでページを開いています...")
        page.goto(LOGIN_URL, wait_until="networkidle")

        # ===== ステップ2：Loginボタンをクリック =====
        print("Loginボタンをクリックしています...")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")

        # ===== ステップ3：フォームログイン =====
        print("フォームログインを処理しています...")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")
        print("ログイン完了")

        # ===== CSVページに移動 =====
        print("CSVページに移動しています...")
        page.goto(CSV_URL, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # ===== CSVダウンロード =====
        print("CSVをダウンロードしています...")
        with page.expect_download() as download_info:
            page.click('a:has-text("Download Full CSV"), button:has-text("Download Full CSV")')
        download = download_info.value

        # 一時ファイルに保存
        tmp_path = tempfile.mktemp(suffix=".csv")
        download.save_as(tmp_path)
        print(f"CSVを保存しました: {tmp_path}")

        browser.close()
        return tmp_path


def upload_to_sheets(csv_path):
    """CSVをGoogleスプレッドシートに書き込む"""
    print("Googleスプレッドシートに書き込んでいます...")

    # 認証
    credentials_info = json.loads(GOOGLE_CREDENTIALS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
    gc = gspread.authorize(credentials)

    # スプレッドシートを開く
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.sheet1

    # CSVを読み込む
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        data = list(reader)

    # シートをクリアして書き込む
    worksheet.clear()
    worksheet.update(data)
    print(f"{len(data)}行のデータを書き込みました")
    update_status("Purchase")


if __name__ == "__main__":
    print("=== Daily Wholesale CSV → Google Sheets ===")
    csv_path = download_csv()
    upload_to_sheets(csv_path)
    print("=== 完了 ===")
