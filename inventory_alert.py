import os
import requests
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
DASHBOARD_URL = f"https://{DOMAIN}/shipping-codes/dashboard"

CW_TOKEN = os.environ["CW_TOKEN"]
CW_ROOM_ID = "325706884"

# 取得する商品数
TOP_N = 4


def get_inventory_alert_items():
    """棚卸未実施テーブルから上位N件を取得する"""
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

        # ===== ダッシュボードページへ移動 =====
        print("ダッシュボードページへ移動しています...")
        page.goto(DASHBOARD_URL, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # ===== 黄色テーブル（class="large-dash bigger yellow"）を狙う =====
        print("対象テーブルを取得しています...")
        target_table = page.locator("table.yellow")

        try:
            target_table.wait_for(state="visible", timeout=10000)
            rows = target_table.locator("tbody tr").all()
            print(f"取得した行数: {len(rows)}")
        except Exception:
            print("黄色テーブルが見つかりませんでした（対象0件とみなします）")
            browser.close()
            return []

        items = []
        for row in rows[:TOP_N]:
            cells = row.locator("td").all()
            if len(cells) < 6:
                continue

            product = cells[0].inner_text().strip()
            maker = cells[1].inner_text().strip()
            description = cells[2].inner_text().strip()
            il = cells[4].inner_text().strip()
            last_count = cells[5].inner_text().strip()

            items.append({
                "product": product,
                "maker": maker,
                "description": description,
                "il": il,
                "last_count": last_count,
            })
            print(f"  ✓ {product} / {maker} / {description} / IL:{il} / {last_count}")

        browser.close()
        return items


def send_chatwork(items):
    """Chatworkにメッセージを送信する"""
    lines = ["[toall]", "Can we have this counted for inventory?"]
    for item in items:
        lines.append(f"{item['product']}\t{item['maker']}\t{item['description']} (IL / {item['il']})")

    message = "\n".join(lines)
    print(f"Chatworkに送信しています...\nメッセージ:\n{message}")

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
    print("=== Inventory Count Alert ===")
    items = get_inventory_alert_items()

    if not items:
        print("⚠ 対象商品が見つかりませんでした。送信をスキップします。")
    else:
        send_chatwork(items)

    print("=== 完了 ===")
