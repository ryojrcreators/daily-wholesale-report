"""
PO番号を指定して、サーバーのPO CSV（Download csv）を取得し、
ショッピングリスト用スプレッドシートに「A1から現在シートを置き換え」で書き込む。

- ログインは po_sheets.py と同じ2段階（Basic認証 + フォームログイン）
- CSV取得元: https://app.jrcreators.com/po-heads/view/{PO#}?download=1
- 書き込み先: ショッピングリストのスプレッドシート（先頭シート）
- PO番号は C1（ラベル "PO#"）/ D1（番号）に記入
"""

import os
import csv
import json
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
from urllib.parse import quote

# ===== 設定（環境変数から読み込み） =====
DOMAIN = "app.jrcreators.com"
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]

# ショッピングリストのスプレッドシート（固定）
SHOPPING_SPREADSHEET_ID = "1L2IKiEjimmXkXfSIt6xT8fbwWjkraDWOM-T62brYVdo"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def download_po_csv(po_number):
    """PO番号のCSVをダウンロードして行リストを返す。"""
    import requests

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        # ===== ログイン（Basic認証 → Loginボタン → フォームログイン）=====
        print("Basic認証付きでトップページを開いています...")
        page.goto(LOGIN_URL, wait_until="networkidle")
        print("Loginボタンをクリックしています...")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")
        print("フォームログインを処理しています...")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")
        print("ログイン完了")

        cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
        browser.close()

    download_url = f"https://{DOMAIN}/po-heads/view/{po_number}?download=1"
    print(f"CSVをダウンロードしています: PO#{po_number}")
    response = requests.get(
        download_url,
        cookies=cookie_dict,
        headers={"User-Agent": USER_AGENT},
        auth=(LOGIN_ID_1, LOGIN_PASS_1),  # Basic認証
    )
    print(f"HTTPステータス: {response.status_code}")
    if response.status_code != 200:
        raise Exception(f"ダウンロード失敗: status={response.status_code}（PO#{po_number}）")

    text = response.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    # 末尾の完全な空行だけ落とす（CSV末尾の余分な改行対策）
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()

    # Order Number 行を確認（取り違え防止のログ）
    order_number = ""
    for r in rows[:6]:
        if r and r[0].strip() == "Order Number" and len(r) > 1:
            order_number = r[1]
            break
    print(f"取得: Order Number = {order_number!r} / データ {len(rows)}行")
    return rows


def write_to_sheet(po_number, rows):
    """先頭シートを rows で置き換え、C1/D1 に PO番号を記入する。"""
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHOPPING_SPREADSHEET_ID).sheet1

    ws.clear()
    # 「A1から現在シートを置き換え」。UPCの先頭ゼロを保つため RAW で書き込む。
    ws.update("A1", rows, value_input_option="RAW")
    # PO番号を C1/D1 に記入
    ws.update("C1", [["PO#", str(po_number)]], value_input_option="RAW")
    print(f"シートに {len(rows)}行を書き込み、C1/D1 に PO#{po_number} を記入しました")


def main():
    po_number = os.environ.get("PO_NUMBER", "").strip()
    if not po_number.isdigit():
        raise SystemExit(f"PO_NUMBER が不正です: {po_number!r}（数字を指定してください）")
    print(f"=== PO Import: PO#{po_number} ===")
    rows = download_po_csv(po_number)
    if not rows:
        raise SystemExit("CSVが空でした。処理を中止します。")
    write_to_sheet(po_number, rows)
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
