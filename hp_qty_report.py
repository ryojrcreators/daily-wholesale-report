import os
import re
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright
from urllib.parse import quote

# ===== 設定（環境変数から読み込み） =====
DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
# 曜日によってURLを切り替え（月=0, 火=1, 水=2, 木=3, 金=4）
_weekday = datetime.now().weekday()
if _weekday == 2:  # 水曜日
    PO_URL = f"https://{DOMAIN}/po-heads?PoHeads%5Bpo_status_id%5D=0&Users%5Bid%5D=231"
else:
    PO_URL = f"https://{DOMAIN}/po-heads?PoHeads%5Bpo_status_id%5D=0&Users%5Bid%5D=21"

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = os.environ["CW_ROOM_ID"]

# 対象POの固定部分
HP_TARGETS = ["Food HP", "Food/Other HP", "Cosme/Other HP"]


def get_hp_qty():
    """PO画面から対象3つのQtyを取得して合計を返す"""
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

        # ===== PO一覧ページへ移動 =====
        print("PO一覧ページへ移動しています...")
        page.goto(PO_URL, wait_until="networkidle")
        page.wait_for_timeout(3000)

        # "Purchase Orders" の見出しが出るまで待つ
        try:
            page.wait_for_selector('text=Purchase Orders', timeout=10000)
            print("Purchase Orders 見出しを検出しました")
        except Exception as e:
            print(f"見出し待機タイムアウト: {e}")

        page.wait_for_timeout(2000)

        # デバッグ：ページ全体のテキストの一部とテーブル構造を確認
        print(f"現在のURL: {page.url}")

        # "Order Number" を含むテーブルを探す
        tables = page.locator("table").all()
        print(f"ページ内のテーブル数: {len(tables)}")
        for ti, table in enumerate(tables):
            header_text = table.locator("thead").inner_text() if table.locator("thead").count() > 0 else "(thead無し)"
            trs = table.locator("tbody tr").all()
            print(f"  テーブル[{ti}]: {len(trs)}行 / ヘッダー: {header_text[:80]}")

        # ===== テーブルからデータを取得 =====
        print("テーブルデータを取得しています...")
        rows = page.locator("table tbody tr").all()
        print(f"取得した行数: {len(rows)}")

        total_qty = 0
        found = {}

        for row in rows:
            cells = row.locator("td").all()
            print(f"  セル数: {len(cells)}")
            # デバッグ用：全セルの内容を出力
            for i, cell in enumerate(cells):
                print(f"    cells[{i}]: {cell.inner_text().strip()}")
            if len(cells) < 1:
                continue

            order_number = cells[1].inner_text().strip() if len(cells) > 1 else ""
            order_date_text = cells[2].inner_text().strip() if len(cells) > 2 else ""
            qty_text = cells[6].inner_text().strip() if len(cells) > 6 else ""

            # デバッグ用：全行の内容を出力
            print(f"  行: order_number={order_number}, order_date={order_date_text}, qty={qty_text}")

            # 当日の日付チェック
            today_str = datetime.now().strftime("%-m/%-d/%y")  # 例: 5/29/26
            if today_str not in order_date_text:
                print(f"  スキップ（当日以外）: {order_date_text}")
                continue

            # 対象POかチェック（Order Numberに固定部分が含まれるか）
            for target in HP_TARGETS:
                if target in order_number:
                    try:
                        qty = int(qty_text.replace(",", ""))
                        total_qty += qty
                        found[target] = qty
                        print(f"  ✓ {order_number} → Qty: {qty}")
                    except ValueError:
                        print(f"  ⚠ Qty取得失敗: {order_number} / qty_text={qty_text}")
                    break

        browser.close()

    print(f"\n対象PO: {found}")
    print(f"合計Qty: {total_qty}")
    return total_qty, found


def send_chatwork(total_qty):
    """Chatworkに結果を送信する"""
    today = datetime.now()
    date_str = today.strftime("%-m/%-d")  # 例: 5/29

    message = (
        f"[To:10892606]Jill S Vinales\n"
        f"{date_str} Today's TG/WM item quantity is {total_qty}"
    )

    print(f"Chatworkに送信しています...\nメッセージ: {message}")

    response = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
        headers={
            "X-ChatWorkToken": CW_TOKEN,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=f"body={requests.utils.quote(message)}",
    )
    print(f"Chatwork送信ステータス: {response.status_code}")

    if response.status_code not in (200, 201):
        raise Exception(f"Chatwork送信失敗: {response.status_code} {response.text}")

    print("送信完了！")


if __name__ == "__main__":
    print("=== HP Qty Report ===")
    total_qty, found = get_hp_qty()

    if len(found) < 3:
        print(f"⚠ 対象のHPが{len(found)}つしか見つかりませんでした（3つ必要）。送信をスキップします。")
        print(f"  見つかったPO: {list(found.keys())}")
    else:
        send_chatwork(total_qty)

    print("=== 完了 ===")
