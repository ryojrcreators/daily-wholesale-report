import os
import re
from datetime import date, timedelta
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

STATUS_NEW             = "1"
STATUS_IN_PROGRESS     = "2"
CASE_TYPE_REPORT_DELAY = "25"

# 環境変数 SUBJECT_PREFIX があれば件名の先頭に付ける（誤送信の訂正再送などの一時利用）。
# 通常の定期実行では設定しないので、件名は今まで通り変化しない。
SUBJECT_PREFIX = os.environ.get("SUBJECT_PREFIX", "")

# 環境変数 BODY_PREFIX があれば本文の先頭に付ける（訂正のお詫び文など）。
# プレフィックスの後に空行を1つ入れて既存本文につなげる。通常運用では設定しない。
BODY_PREFIX = os.environ.get("BODY_PREFIX", "")

TEMPLATE_170 = "170"
TEMPLATE_108 = "108"
TEMPLATE_107 = "107"
TEMPLATE_161 = "161"

SHOP_CONFIG = {
    "楽天": {
        "enabled": True,
        "type": "rakuten",
        "template_labels": ["楽天", "Wowma"],
    },
    "California Mart": {
        "enabled": True,
        "type": "simple",
        "template_normal": "97",   # 遅延メール（ETA≦EDD またはETAなし）
        "template_long":   "88",   # 入荷がかなり先（ETA＞EDD）
        "date_placeholder": r"x{3,}",
        "template_labels": ["California Mart", "カリマ"],
    },
    "Shop LA!": {
        "enabled": True,
        "type": "simple",
        "template_normal": "163",  # 遅延メール
        "template_long":   "7",    # 入荷がかなり先
        "date_placeholder": r"★",
        "template_labels": ["Shop LA!"],
    },
    "US International Service": {
        "enabled": True,
        "type": "simple",
        "template_normal": "19",   # 遅延メール
        "template_long":   "122",  # 入荷がかなり先
        "date_placeholder": r"★/★",
        "template_labels": ["UK"],
    },
    "Yahoo (new)": {
        "enabled": True,
        "type": "yahoo",
        "template_normal": "172",  # 遅延連絡（ETA≦EDD またはETAなし）
        "template_long":   "187",  # かなり先（ETA＞EDD）
        "template_labels": ["Yahoo"],
    },
    # "Wowma": {"enabled": True, "type": ...},
}

# ============================================================
# 日付ユーティリティ
# ============================================================

def mask_order(s: str) -> str:
    """実行ログ用に注文番号をぼかす。先頭6桁だけ残す。
    Public リポジトリでは Actions ログも公開されるため。"""
    s = (s or "").strip()
    return s[:6] + "-****" if len(s) > 6 else "******"


# 月名（英語・省略形）→月番号
_MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}

# 配送予定を示す文脈（日付の文脈判定・英語/日本語）
_DELIVERY_CONTEXT = re.compile(
    r'estimated delivery|delivery by|deliver by|arriv|ship|dispatch|'
    r'入荷|発送|お届け|到着|出荷',
    re.IGNORECASE,
)


def _mk_date(month, day, year, today):
    """年補完・年末ロールオーバー付きで date を作る。失敗時 None。
    年なしで30日以上過去の日付は翌年扱い（年末年始の取り違え防止）。"""
    try:
        if year is not None:
            if year < 100:
                year += 2000
            return date(year, month, day)
        d = date(today.year, month, day)
        # 半年以上過去なら翌年扱い（年末年始の取り違え防止）。
        # 少し過去のETA（例：7/24時点の6/22）は当年のままにする。
        if (today - d).days > 180:
            d = date(today.year + 1, month, day)
        return d
    except ValueError:
        return None


def _find_dates(text, today):
    """text 内の日付候補をできる限り拾って date のリストで返す。
    対応：M/D・M-D・M.D（年あり/なし）、月名(July 30 / 30 Jul)、日本語(7月30日)。"""
    out = []
    # 数字形式 M/D, M/D/Y, M-D, M-D-Y, M.D, M.D.Y
    for m in re.finditer(r'(?<!\d)(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?(?!\d)', text):
        year = int(m.group(3)) if m.group(3) else None
        d = _mk_date(int(m.group(1)), int(m.group(2)), year, today)
        if d:
            out.append(d)
    # 月名 + 日（例：July 30, Jul 30 2026）
    for m in re.finditer(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?', text):
        mon = _MONTH_NAMES.get(m.group(1).lower())
        if mon:
            d = _mk_date(mon, int(m.group(2)), int(m.group(3)) if m.group(3) else None, today)
            if d:
                out.append(d)
    # 日 + 月名（例：30 July, 30th of July）
    for m in re.finditer(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})(?:,?\s+(\d{4}))?', text):
        mon = _MONTH_NAMES.get(m.group(2).lower())
        if mon:
            d = _mk_date(mon, int(m.group(1)), int(m.group(3)) if m.group(3) else None, today)
            if d:
                out.append(d)
    # 日本語 7月30日
    for m in re.finditer(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日', text):
        d = _mk_date(int(m.group(1)), int(m.group(2)), None, today)
        if d:
            out.append(d)
    return out


def parse_eta(description: str):
    """Descriptionから配送予定日(ETA)を推定。範囲がある場合は遅い方を採用。
    読み取れなければ None（呼び出し側が手動対応/未定判定に回す）。"""
    if not description:
        return None
    today = date.today()

    # 1. 「ETA」直後のテキストから日付を拾う（ETA表記があれば最優先）
    m = re.search(r'ETA\b', description, re.IGNORECASE)
    if m:
        after = description[m.end():m.end() + 60]
        ds = _find_dates(after, today)
        if ds:
            return max(ds)

    # 2. 配送予定を示す文脈がある場合、本文全体から日付を拾う
    if _DELIVERY_CONTEXT.search(description):
        ds = _find_dates(description, today)
        if ds:
            return max(ds)

    # 3. 年つきの完全な日付はどこにあっても採用（M/D/Y・M-D-Y・M.D.Y）
    m = re.search(r'(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?!\d)', description)
    if m:
        return _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)

    return None


def looks_like_has_date(description: str) -> bool:
    """ETA表記や日付らしき記述が含まれるか（parse_etaがNoneでも手動に回す判断用）。"""
    if not description:
        return False
    if re.search(r'ETA\b', description, re.IGNORECASE):
        return True
    if re.search(r'(?<!\d)\d{1,2}[/\-.]\d{1,2}(?!\d)', description):
        return True
    if re.search(r'\d{1,2}\s*月\s*\d{1,2}\s*日', description):
        return True
    if re.search(r'\b[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?\b', description) and \
       any(mn in description.lower() for mn in _MONTH_NAMES):
        return True
    return False


# ETAの具体的な日付は無いが「入荷予定はある」ことを示す表現。
# 例：「no specific ETA, but scheduled to be shipped by the end of this week」
# これらが含まれ、かつ日付が取れない場合は、誤って"未定"を送らず手動対応に回す。
# 新しい言い回しが出てきたらここに追記すればOK（小文字で書く）。
INCOMING_SIGNALS = [
    # 時期（具体的な日付ではない）
    "this week", "next week", "end of the week", "end of this week",
    "end of next week", "beginning of next week", "coming week",
    "few days", "couple of days", "coming days", "within a week", "within days",
    "by the end of",
    # 発送・入荷の意思／状態
    "will ship", "will be ship", "to be ship", "scheduled to ship",
    "scheduled to be ship", "expected to ship", "ship by", "shipped by",
    "in transit", "on the way", "en route", "on order",
    "reserved", "allocated", "restock", "pallet",
]


def has_incoming_signal(description: str) -> bool:
    """Descriptionに『入荷予定はある』ことを示す表現が含まれるか判定。"""
    if not description:
        return False
    text = description.lower()
    return any(kw in text for kw in INCOMING_SIGNALS)


def parse_edd(edd_text: str):
    if not edd_text:
        return None
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', edd_text)
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def eta_to_japanese(eta: date, today: date = None) -> str:
    # ETAが今日（アメリカ時間）なら特別表現、それ以外はETA+1日をM/D頃で返す
    if today is None:
        today = date.today()
    if eta == today:
        return "本日〜明日頃"
    delivery_date = eta + timedelta(days=1)
    return f"{delivery_date.month}/{delivery_date.day}頃"


def select_template_rakuten(eta, edd) -> tuple:
    """楽天用テンプレート選択。(template_id, needs_date_replace) を返す"""
    if eta is None:
        return TEMPLATE_161, False
    if edd is None or eta <= edd:
        return TEMPLATE_170, True
    delay_days = (eta - edd).days
    template = TEMPLATE_108 if delay_days <= 7 else TEMPLATE_107
    return template, True


def select_template_simple(eta, edd, config) -> tuple:
    """California Mart / Shop LA! 用。(template_id, needs_date_replace) を返す"""
    if eta is not None and edd is not None and eta > edd:
        return config["template_long"], True
    return config["template_normal"], False


def replace_date_rakuten(body: str, eta: date, template_id: str) -> str:
    japanese_expr = eta_to_japanese(eta)
    if template_id == TEMPLATE_170:
        body = re.sub(r'今週半ばから今週末頃', japanese_expr, body)
    elif template_id in (TEMPLATE_108, TEMPLATE_107):
        # 108（遅延7日以内）：「来週始めから半ば頃」
        # 107（大幅遅延）：「〇月中旬から下旬頃」
        # どちらの言い回しでも具体的な日付（ETA+1）に置換する。
        body = re.sub(r'来週[初始]めから半ば頃', japanese_expr, body)
        body = re.sub(r'[〇○0]月中旬から下旬頃', japanese_expr, body)
    return body


def replace_date_yahoo(body: str, eta: date, template_id: str) -> str:
    if template_id == "172":
        return re.sub(r'今週半ば頃', eta_to_japanese(eta), body)
    else:  # 187: かなり先
        delivery_date = eta + timedelta(days=1)
        return re.sub(r'xxxx', f"{delivery_date.month}/{delivery_date.day}", body)


def replace_date_simple(body: str, eta: date, config: dict) -> str:
    delivery_date = eta + timedelta(days=1)
    date_str = f"{delivery_date.month}/{delivery_date.day}"
    return re.sub(config["date_placeholder"], date_str, body)


def should_skip_email(order_number: str) -> bool:
    order_number = order_number.strip()
    if order_number.endswith('-R'):
        return True
    if re.match(r'^\d{9}$', order_number):
        return True
    return False


def verify_template_shop(selected_label: str, config: dict) -> bool:
    """選択されたテンプレートのラベルが正しい店舗のものか確認"""
    return any(label in selected_label for label in config["template_labels"])


def get_shop_config(shop_name: str):
    for shop_key, config in SHOP_CONFIG.items():
        if shop_key in shop_name and config.get("enabled", False):
            return config
    return None


# ============================================================
# メイン処理
# ============================================================

def process_report_delays():
    processed = 0
    skipped   = 0
    errors    = []
    manual_review = []   # ETA日付を確定できない → 自動送信せず手動対応に回したケース

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

        print("Report Delay (New) を検索中...")
        page.goto(
            f"{BASE_URL}/case-orders/cs-index?case_status_id={STATUS_NEW}&case_type_id={CASE_TYPE_REPORT_DELAY}",
            wait_until="networkidle"
        )

        while True:
            page.wait_for_timeout(1500)
            processed_this_page = 0

            # cs-editリンクを全取得してテキストをデバッグ出力
            all_edit_links = page.query_selector_all('a[href*="cs-edit"]')
            print(f"  cs-editリンク数: {len(all_edit_links)}")
            for i, link in enumerate(all_edit_links[:6]):
                print(f"    [{i}] text={repr(link.inner_text().strip())} href={link.get_attribute('href')}")

            # case_idをhrefから直接取得（テキストフィルター不要）
            # /case-orders/cs-edit/XXXXXX の形式から重複なくcase_idを収集
            seen_hrefs = set()
            case_list  = []
            for link in all_edit_links:
                href = link.get_attribute('href') or ''
                # cs-edit/数字 のパターンのみ対象
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
                    # Step 1: Case詳細でETA取得
                    page.goto(f"{BASE_URL}{edit_href}", wait_until="networkidle")
                    description = page.eval_on_selector('#description', 'el => el.value')
                    eta = parse_eta(description)
                    print(f"  ETA: {eta}  (description: {description!r})")

                    # ETAの日付は取れないが「入荷予定はある」文面の場合、
                    # 誤って"未定"メールを送らず、自動送信せず手動対応に回す（ステータスはNEWのまま）
                    # ETA日付を確定できない場合は、店舗を問わず自動送信せず手動対応に回す。
                    # （161「未定」テンプレートやプレースホルダー未置換メールの誤送信を防ぐ）
                    if eta is None:
                        print(f"  → ETA日付を確定できず → 自動送信せず手動対応（161は使わない）")
                        manual_review.append(f"Case {case_id}（{shop_name}）: ETA日付なし → 要手動確認")
                        continue

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

                    # Step 2: sales/viewでEDD・Send Emailリンク取得
                    edd             = None
                    send_email_href = None

                    if sales_href:
                        page.goto(f"{BASE_URL}{sales_href}", wait_until="networkidle")

                        # たどり着いたsales/viewが本当に対象注文か検証
                        page_text = page.inner_text('body')
                        if order_number not in page_text:
                            print(f"  → ❌ sales/viewの注文番号不一致（{mask_order(order_number)}が見つからない）→ スキップ")
                            errors.append(f"Case {case_id}: sales/view注文番号不一致")
                            continue

                        for row in page.query_selector_all('table tr'):
                            if 'End Delivery' in row.inner_text():
                                tds = row.query_selector_all('td')
                                if tds:
                                    edd = parse_edd(tds[0].inner_text().strip())
                                break
                        print(f"  EDD: {edd}")
                        send_link = page.query_selector('a[href*="emails-unsent/add"]')
                        if send_link:
                            send_email_href = send_link.get_attribute('href')

                    # Step 3: メールスキップ判定
                    if should_skip_email(order_number):
                        print(f"  → メールスキップ対象（{mask_order(order_number)}）→ Status変更のみ")
                        page.goto(f"{BASE_URL}{edit_href}", wait_until="networkidle")
                        page.select_option('#case-status-id', STATUS_IN_PROGRESS)
                        page.click('button[type="submit"]')
                        page.wait_for_load_state("networkidle")
                        print(f"  → Status: In-Progress 完了")
                        processed += 1
                        continue

                    # Step 4: テンプレート選択
                    if shop_config["type"] == "rakuten":
                        template_id, needs_date = select_template_rakuten(eta, edd)
                    else:
                        template_id, needs_date = select_template_simple(eta, edd, shop_config)
                        if shop_config["type"] == "yahoo" and eta:
                            needs_date = True
                    print(f"  テンプレート: {template_id}  日付置換: {needs_date}")

                    if not send_email_href:
                        print(f"  → Send Emailリンクが見つからない → スキップ")
                        errors.append(f"Case {case_id}: Send Emailリンクなし")
                        continue

                    # Step 5: メール送信
                    page.goto(f"{BASE_URL}{send_email_href}", wait_until="networkidle")
                    # page.url はドメイン・注文番号を含むためログには出さない
                    print("  メール送信ページを開きました")
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

                    if eta and needs_date:
                        body = page.eval_on_selector('#body', 'el => el.value')
                        if shop_config["type"] == "rakuten":
                            new_body = replace_date_rakuten(body, eta, template_id)
                        elif shop_config["type"] == "yahoo":
                            new_body = replace_date_yahoo(body, eta, template_id)
                        else:
                            new_body = replace_date_simple(body, eta, shop_config)
                        page.eval_on_selector('#body', '(el, val) => el.value = val', new_body)

                    # 本文プレフィックス（BODY_PREFIX 指定時のみ）：先頭に付けて空行で本文につなげる
                    if BODY_PREFIX:
                        cur_body = page.eval_on_selector('#body', 'el => el.value')
                        if not cur_body.startswith(BODY_PREFIX):
                            page.eval_on_selector(
                                '#body', '(el, val) => el.value = val',
                                BODY_PREFIX + "\n\n" + cur_body
                            )
                            print("  本文プレフィックス付与")

                    # 件名プレフィックス（SUBJECT_PREFIX 指定時のみ）
                    if SUBJECT_PREFIX:
                        subj_el = page.query_selector('#subject') or page.query_selector('input[name="subject"]')
                        if subj_el:
                            cur_subject = subj_el.input_value()
                            if not cur_subject.startswith(SUBJECT_PREFIX):
                                subj_el.fill(SUBJECT_PREFIX + cur_subject)
                                print(f"  件名プレフィックス付与: {SUBJECT_PREFIX!r}")
                        else:
                            print("  → ⚠️ 件名欄が見つからずプレフィックス未付与")

                    page.click('button[type="submit"]')
                    page.wait_for_load_state("networkidle")

                    if "New Emails Added to Queue" in page.content():
                        print(f"  → メール送信成功 ✓")
                    else:
                        print(f"  → メール送信結果不明（要確認）")
                        errors.append(f"Case {case_id}: 送信結果不明")

                    # Step 6: Status を In-Progress に変更
                    page.goto(f"{BASE_URL}{edit_href}", wait_until="networkidle")
                    page.select_option('#case-status-id', STATUS_IN_PROGRESS)
                    page.click('button[type="submit"]')
                    page.wait_for_load_state("networkidle")
                    print(f"  → Status: In-Progress 完了 ✓")

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
                f"{BASE_URL}/case-orders/cs-index?case_status_id={STATUS_NEW}&case_type_id={CASE_TYPE_REPORT_DELAY}",
                wait_until="networkidle"
            )

        browser.close()

    print(f"\n{'='*40}")
    print(f"処理完了: {processed}件")
    print(f"スキップ（対象外店舗）: {skipped}件")
    if manual_review:
        print(f"★要手動確認（入荷予定ありだがETA日付なし）: {len(manual_review)}件")
        for m in manual_review:
            print(f"  - {m}")
    if errors:
        print(f"エラー: {len(errors)}件")
        for e in errors:
            print(f"  - {e}")
    print("=== 完了 ===")


if __name__ == "__main__":
    print("=== Report Delay 自動処理 ===")
    process_report_delays()
