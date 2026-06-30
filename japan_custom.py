import os
import re
from datetime import date
from playwright.sync_api import sync_playwright
from urllib.parse import quote

# ============================================================
# 設定（環境変数から読み込み）
# ============================================================
DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1   = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2   = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]

LOGIN_ID_1_ENC   = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{DOMAIN}/"
BASE_URL  = f"https://{DOMAIN}"

STATUS_NEW                       = "1"
STATUS_AWAITING_CUSTOMER         = "5"
CASE_TYPE_JAPAN_CUSTOM           = "29"

SHOP_CONFIG = {
    "楽天": {
        "enabled": True,
        "template_labels": ["楽天", "Wowma"],
        "templates": {
            "home_address":   "106",
            "full_name":      "182",
            "incomplete":     "220",
            "split_delivery": "179",
        },
    },
    "California Mart": {
        "enabled": True,
        "template_labels": ["California Mart", "カリマ"],
        "templates": {
            "home_address":   "90",
            "full_name":      "91",
            "incomplete":     "92",
            "split_delivery": "183",
        },
    },
    "Yahoo (new)": {
        "enabled": True,
        "template_labels": ["Yahoo"],
        "templates": {
            "home_address":   "176",
            "full_name":      "190",
            "incomplete":     "186",
            "split_delivery": "229",
        },
    },
    "Shop LA!": {
        "enabled": True,
        "template_labels": ["Shop LA!"],
        "templates": {
            "home_address":   "24",
            "full_name":      "26",
            "incomplete":     "27",
            "split_delivery": "76",
        },
    },
}


# ============================================================
# 判定ユーティリティ
# ============================================================

def mask_order(s: str) -> str:
    """実行ログ用に注文番号をぼかす。先頭6桁だけ残す。
    Public リポジトリでは Actions ログも公開されるため。"""
    s = (s or "").strip()
    return s[:6] + "-****" if len(s) > 6 else "******"


def get_shop_config(shop_name: str):
    for shop_key, config in SHOP_CONFIG.items():
        if shop_key in shop_name and config.get("enabled", False):
            return config
    return None


def select_template(description: str, config: dict):
    """DescriptionからテンプレートIDを返す。対応不可の場合はNoneを返す。"""
    if "-Several Addresses" in description or "複数居住所" in description:
        return config["templates"]["split_delivery"]
    if "-Home Address" in description or "居住所" in description:
        return config["templates"]["home_address"]
    if "-Full Name" in description or "フルネーム" in description:
        return config["templates"]["full_name"]
    if "-Incomplete Address" in description or "住所不備" in description:
        return config["templates"]["incomplete"]
    return None


def parse_split_count(description: str):
    """Descriptionから分割数を取得。例: '5分割' → '5'"""
    m = re.search(r'(\d+)分割', description)
    return m.group(1) if m else None


def parse_latest_release_after(page) -> str:
    """sales/viewページのRelease After日付の中で最遅のものをM/D形式で返す"""
    latest = None
    for row in page.query_selector_all('table tr'):
        text = row.inner_text()
        m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', text)
        if not m:
            continue
        # Release Afterっぽい行（Sagawa/ePacket等の配送行）のみ対象
        if 'Sagawa' not in text and 'ePacket' not in text and 'CDS' not in text:
            continue
        year  = int(m.group(3))
        if year < 100:
            year += 2000
        try:
            d = date(year, int(m.group(1)), int(m.group(2)))
            if latest is None or d > latest:
                latest = d
        except ValueError:
            continue
    if latest:
        return f"{latest.month}/{latest.day}"
    return None


def replace_split_placeholders(body: str, split_count: str, latest_date: str) -> str:
    """★を順番に分割数→最終便日付で置換"""
    body = body.replace("★", split_count, 1)
    body = body.replace("★", latest_date, 1)
    return body


def verify_template_shop(selected_label: str, config: dict) -> bool:
    return any(label in selected_label for label in config["template_labels"])


# ============================================================
# メイン処理
# ============================================================

def process_japan_custom():
    processed = 0
    skipped   = 0
    errors    = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

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

        print("Japan Custom, Adds, Business (New) を検索中...")
        page.goto(
            f"{BASE_URL}/case-orders/cs-index?case_status_id={STATUS_NEW}&case_type_id={CASE_TYPE_JAPAN_CUSTOM}",
            wait_until="networkidle"
        )

        while True:
            page.wait_for_timeout(1500)
            processed_this_page = 0

            all_edit_links = page.query_selector_all('a[href*="cs-edit"]')
            print(f"  cs-editリンク数: {len(all_edit_links)}")
            for i, link in enumerate(all_edit_links[:6]):
                print(f"    [{i}] text={repr(link.inner_text().strip())} href={link.get_attribute('href')}")

            seen_hrefs = set()
            case_list  = []
            for link in all_edit_links:
                href = link.get_attribute('href') or ''
                if not re.search(r'/cs-edit/\d+$', href):
                    continue
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                row = link.evaluate_handle('el => el.closest("tr")').as_element()
                if not row:
                    continue
                cells = row.query_selector_all('td')
                if len(cells) < 6:
                    continue
                case_id      = cells[0].inner_text().strip()
                shop_name    = cells[4].inner_text().strip()
                order_number = cells[5].inner_text().strip()
                case_list.append({
                    "id":           case_id,
                    "shop":         shop_name,
                    "order_number": order_number,
                    "edit_href":    href,
                })

            if not case_list:
                print("案件なし、終了")
                break

            print(f"このページの案件数: {len(case_list)}")

            for case in case_list:
                case_id      = case["id"]
                shop_name    = case["shop"]
                order_number = case["order_number"]
                edit_href    = case["edit_href"]

                print(f"\n--- Case {case_id} / {shop_name} / {mask_order(order_number)} ---")

                shop_config = get_shop_config(shop_name)
                if shop_config is None:
                    print(f"  → 対象外店舗: {shop_name} → スキップ")
                    skipped += 1
                    continue

                try:
                    # Step 1: Descriptionを取得してテンプレート判定
                    page.goto(f"{BASE_URL}{edit_href}", wait_until="networkidle")
                    description = page.eval_on_selector('#description', 'el => el.value')
                    print(f"  Description: {description!r}")

                    is_split = "-Several Addresses" in description or "複数居住所" in description
                    template_id = select_template(description, shop_config)

                    if template_id is None:
                        print(f"  → テンプレート判定不可 → スキップ")
                        errors.append(f"Case {case_id}: テンプレート判定不可 ({description!r})")
                        continue

                    print(f"  テンプレート: {template_id}  分割配送: {is_split}")

                    # Step 2: sales/viewでSend Emailリンク・Release After取得
                    # Order Numberに一致するsales/viewリンクを選ぶ
                    # （通知ベル等に他注文のsales/viewリンクが混ざるため、先頭を拾わない）
                    sales_href = None
                    for a in page.query_selector_all('a[href*="sales/view"]'):
                        if order_number in (a.inner_text() or ''):
                            sales_href = a.get_attribute('href')
                            break
                    if sales_href is None:
                        first_sales = page.query_selector('a[href*="sales/view"]')
                        sales_href = first_sales.get_attribute('href') if first_sales else None

                    send_email_href = None
                    latest_release_date = None

                    if sales_href:
                        page.goto(f"{BASE_URL}{sales_href}", wait_until="networkidle")

                        # たどり着いたsales/viewが本当に対象注文か検証
                        page_text = page.inner_text('body')
                        if order_number not in page_text:
                            print(f"  → ❌ sales/viewの注文番号不一致（{mask_order(order_number)}が見つからない）→ スキップ")
                            errors.append(f"Case {case_id}: sales/view注文番号不一致")
                            continue

                        send_link = page.query_selector('a[href*="emails-unsent/add"]')
                        if send_link:
                            send_email_href = send_link.get_attribute('href')
                        if is_split:
                            latest_release_date = parse_latest_release_after(page)
                            print(f"  最終便Release After: {latest_release_date}")

                    if not send_email_href:
                        print(f"  → Send Emailリンクが見つからない → スキップ")
                        errors.append(f"Case {case_id}: Send Emailリンクなし")
                        continue

                    if is_split and not latest_release_date:
                        print(f"  → Release After日付が取得できない → スキップ")
                        errors.append(f"Case {case_id}: Release After取得失敗")
                        continue

                    # Step 3: メール送信
                    page.goto(f"{BASE_URL}{send_email_href}", wait_until="networkidle")
                    # page.url はドメイン・注文番号を含むためログには出さない
                    print("  メール送信ページを開きました")

                    # 利用可能なテンプレート一覧をログ出力（デバッグ用）
                    template_select = page.query_selector('#template')
                    print(f"  #templateセレクト存在: {template_select is not None}")
                    if template_select:
                        available_options = page.eval_on_selector_all(
                            '#template option',
                            'els => els.map(e => e.value + ": " + e.textContent.trim())'
                        )
                        print(f"  利用可能テンプレート: {available_options}")

                    page.select_option('#template', template_id, timeout=5000)
                    page.wait_for_timeout(500)

                    # テンプレートが正しい店舗のものか確認
                    selected_label = page.eval_on_selector(
                        '#template option:checked', 'el => el.textContent'
                    ) or ""
                    if not verify_template_shop(selected_label, shop_config):
                        msg = f"テンプレート不一致: ID={template_id} ラベル={selected_label!r} 店舗={shop_name}"
                        print(f"  → ❌ {msg}")
                        errors.append(f"Case {case_id}: {msg}")
                        continue
                    print(f"  テンプレート確認OK: {selected_label!r}")

                    # 分割配送の場合は★を置換
                    if is_split:
                        split_count = parse_split_count(description)
                        if not split_count:
                            print(f"  → 分割数が取得できない → スキップ")
                            errors.append(f"Case {case_id}: 分割数取得失敗")
                            continue
                        print(f"  分割数: {split_count}  最終便: {latest_release_date}")
                        body = page.eval_on_selector('#body', 'el => el.value')
                        new_body = replace_split_placeholders(body, split_count, latest_release_date)
                        page.eval_on_selector('#body', '(el, val) => el.value = val', new_body)

                    page.click('button[type="submit"]')
                    page.wait_for_load_state("networkidle")

                    if "New Emails Added to Queue" in page.content():
                        print(f"  → メール送信成功 ✓")
                    else:
                        print(f"  → メール送信結果不明（要確認）")
                        errors.append(f"Case {case_id}: 送信結果不明")

                    # Step 4: Status を In-Progress に変更
                    page.goto(f"{BASE_URL}{edit_href}", wait_until="networkidle")
                    page.select_option('#case-status-id', STATUS_AWAITING_CUSTOMER)
                    page.click('button[type="submit"]')
                    page.wait_for_load_state("networkidle")
                    print(f"  → Status: Awaiting Customer Response 完了 ✓")

                    processed += 1
                    processed_this_page += 1

                except Exception as e:
                    print(f"  → エラー: {e}")
                    errors.append(f"Case {case_id}: {e}")
                    continue

            # 処理済みケースはNewから消えるため、常に同じURLを再読み込み
            # 今回のループで1件も処理できなければ終了（無限ループ防止）
            if processed_this_page == 0:
                break
            page.goto(
                f"{BASE_URL}/case-orders/cs-index?case_status_id={STATUS_NEW}&case_type_id={CASE_TYPE_JAPAN_CUSTOM}",
                wait_until="networkidle"
            )

        browser.close()

    print(f"\n{'='*40}")
    print(f"処理完了: {processed}件")
    print(f"スキップ（対象外店舗）: {skipped}件")
    if errors:
        print(f"エラー・未対応: {len(errors)}件")
        for e in errors:
            print(f"  - {e}")
    print("=== 完了 ===")


if __name__ == "__main__":
    print("=== Japan Custom, Adds, Business 自動処理 ===")
    process_japan_custom()
