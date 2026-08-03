"""
社内システム（app.jrcreators.com）で楽天チャネルの注文が発送済みになったら、
発送日・追跡番号・配送会社を楽天RMSへ自動反映する。

処理の流れ:
  1. Playwrightで app.jrcreators.com にログイン（Basic認証 + フォームログインの2段階）
  2. 直近LOOKBACK_DAYS日分について、/so-heads?start_date=...&SoHeads[sales_account_id]=3&
     ship_date=YYYY-MM-DD というURLを直接開き（フォーム操作は一切不要。この形のURLを
     そのままgotoするのが確実に動くことを検証済み。sales_account_id=3が楽天チャネルの
     絞り込み）、Downloadリンクのhrefを取得→Cookie付きrequestsでCSV取得する
  3. order_number単位に集約し、プレフィックスで店舗（Americana/Founder）を判定、
     ship_methodを配送会社コードに変換する（対象外・変換不能なものはスキップ）
  4. 店舗ごとに getOrder でまとめて取得し、PackageModelList[].ShippingModelList が
     既に値を持つもの（＝登録済み。-Rの再送注文で手動登録済みの場合も含む）はスキップ
  5. 未登録のものだけ updateOrderShipping で発送情報を登録する
     （送付先が複数ある注文は、全basketIdに同じ発送情報を登録する）
  6. エラー・要確認（未知の配送会社名等）があった場合のみ Chatworkに通知する
"""

import os
import sys
import csv
import time
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from playwright.sync_api import sync_playwright

from rakuten_coupon_api import auth_headers

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))  # 当日を含め何日分（ship_date基準）を対象にするか
# テスト用：指定した場合、この注文番号だけを対象にする（カンマ区切りで複数可）
ONLY_ORDER_NUMBERS = {
    s.strip() for s in os.environ.get("ONLY_ORDER_NUMBERS", "").split(",") if s.strip()
}
# テスト用：処理する件数の上限（登録対象になった注文の数。既登録スキップはカウントしない）
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "0")) or None

# ── 社内システム ──────────────────────────────────
APP_DOMAIN = os.environ["APP_DOMAIN"]
LOGIN_ID_1 = os.environ["LOGIN_ID_1"]
LOGIN_PASS_1 = os.environ["LOGIN_PASS_1"]
LOGIN_ID_2 = os.environ["LOGIN_ID_2"]
LOGIN_PASS_2 = os.environ["LOGIN_PASS_2"]
LOGIN_ID_1_ENC = quote(LOGIN_ID_1, safe="")
LOGIN_PASS_1_ENC = quote(LOGIN_PASS_1, safe="")
LOGIN_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{APP_DOMAIN}/"
BASE_URL = f"https://{APP_DOMAIN}"
SO_HEADS_URL = f"https://{LOGIN_ID_1_ENC}:{LOGIN_PASS_1_ENC}@{APP_DOMAIN}/so-heads"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── 楽天RMS 店舗ごとの認証情報 ────────────────────
STORES = {
    "americana": {
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_1"],
    },
    "founder": {
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_2"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_2"],
    },
}

# 注文番号のプレフィックスから店舗を判定する
ORDER_PREFIX_TO_STORE = {
    "296610": "americana",
    "374383": "founder",
}

ORDER_BASE = "https://api.rms.rakuten.co.jp/es/2.0/order"
ORDER_API_VERSION = 10

# ship_method（社内システムの表記）→ 楽天の配送会社コード
CARRIER_CODES = {
    "Sagawa CDS": "1002",
    "ePacket": "1024",
    "Yamato Nekopos": "1001",
}

CW_TOKEN = os.environ.get("CW_TOKEN", "")
CW_ROOM_ID = os.environ.get("CW_ROOM_ID") or "60101971"
CW_ASSIGNEE_ID = "2618849"  # Ryo Higuchiさん（[To:2618849]と同じアカウントID）
CW_MENTION = "[To:2618849]Ryo Higuchiさん"
CW_TITLE = "楽天出荷通知の自動反映でエラー・要確認がありました"


# ══ Chatwork 通知 ═════════════════════════════════
def post_chatwork_task(room_id: str, to_ids: str, body: str):
    if not room_id or not to_ids or not CW_TOKEN:
        print("  Chatworkルームid/担当者id/トークンが未指定のためタスクを作成しません。")
        return
    try:
        res = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/tasks",
            headers={"X-ChatWorkToken": CW_TOKEN},
            data={"body": body, "to_ids": to_ids},
            timeout=30,
        )
        print(f"  Chatworkタスク作成: status={res.status_code}")
        if res.status_code >= 400:
            print(f"    {res.text[:300]}")
    except Exception as e:
        print(f"  Chatworkタスク作成に失敗しました: {e}")


def build_report(unmapped_carriers: list, errors: list) -> str:
    lines = [CW_MENTION, f"[info][title]{CW_TITLE}[/title]", ""]
    if unmapped_carriers:
        lines.append("■ 未知の配送会社名（要マッピング追加）")
        for r in unmapped_carriers:
            lines.append(f"・注文番号 {r['order_number']}: {r['ship_method']!r}")
        lines.append("")
    if errors:
        lines.append("■ エラー")
        for r in errors:
            lines.append(f"・注文番号 {r['order_number']}: {r['message']}")
        lines.append("")
    lines.append("[/info]")
    return "\n".join(lines)


# ══ 社内システム：CSV取得 ═════════════════════════
def login(page):
    print("社内システムにログイン中...")
    if os.environ.get("DEBUG_SO_SEARCH"):
        page.on("console", lambda msg: print(f"    [console.{msg.type}] {msg.text}"))
        page.on("pageerror", lambda exc: print(f"    [pageerror] {exc}"))
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")


# created_time側の下限として使うだけの固定値（過去の日付であれば良く、この値自体に意味は
# ない。この形のURL・パラメータの組み合わせでの直接gotoが確実に動くことを検証済み）
START_DATE_FLOOR = "2026-04-30"


def fetch_shipped_csv(p, ship_date_str: str):
    """指定日にsales_account_id=3（楽天チャネル、両店舗）で発送済みの注文CSVを取得する。
    (ヘッダー行, データ行のリスト) を返す。データが無ければ (None, [])。

    毎回ログインからやり直す新しいブラウザで実行する（同じセッションで検索を
    繰り返すと、2回目以降の検索結果が必ず空になることを確認したため。1回の
    ログインにつき検索は1回だけ、という制約があるとみられる）。
    """
    browser = p.chromium.launch(headless=True)
    try:
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            device_scale_factor=2,
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        login(page)

        url = (
            f"{SO_HEADS_URL}?start_date={START_DATE_FLOOR}"
            f"&SoHeads%5Bsales_account_id%5D=3&ship_date={ship_date_str}"
        )
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Playwrightのlocator().count()ではなく、動作確認済みのJS直接評価でhrefを取得する
        # （「Download」の完全一致のみを対象にし、「Profit Download」等は除外する）
        href = page.evaluate(
            """() => {
                const a = [...document.querySelectorAll('a')].find(el => el.textContent.trim() === 'Download');
                return a ? a.getAttribute('href') : null;
            }"""
        )
        if not href:
            print(f"  {ship_date_str}: Downloadリンクが見つかりません（該当データ無しの可能性）")
            return None, []
        download_url = f"https://{APP_DOMAIN}{href}" if href.startswith("/") else href

        cookie_dict = {c["name"]: c["value"] for c in context.cookies()}
        response = requests.get(
            download_url,
            cookies=cookie_dict,
            headers={"User-Agent": USER_AGENT},
            auth=(LOGIN_ID_1, LOGIN_PASS_1),
        )
        if response.status_code != 200:
            print(f"  {ship_date_str}: CSVダウンロード失敗 status={response.status_code}")
            return None, []

        text = response.content.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(text.splitlines()))
        if not rows:
            return None, []
        return rows[0], rows[1:]
    finally:
        browser.close()


def collect_shipped_orders(p) -> list:
    """直近LOOKBACK_DAYS日分のCSVを取得し、order_number単位に集約したリストを返す。
    各要素: {"order_number": ..., "ship_method": ..., "tracking_num": ..., "ship_time": ...}
    """
    today = datetime.now(JST).date()
    seen = {}
    for i in range(LOOKBACK_DAYS):
        if i > 0:
            wait_sec = int(os.environ.get("INTER_REQUEST_WAIT_SEC", "90"))
            print(f"（同一IPからの連続リクエストを避けるため{wait_sec}秒待機します）")
            time.sleep(wait_sec)
        d = today - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        print(f"取得中: {d_str}")
        header, rows = fetch_shipped_csv(p, d_str)
        if not header:
            continue
        print(f"  取得: {len(rows)}行")
        for row in rows:
            if not any(row):
                continue
            record = dict(zip(header, row))
            order_number = record.get("order_number", "").strip()
            if not order_number or order_number in seen:
                continue
            seen[order_number] = {
                "order_number": order_number,
                "ship_method": record.get("ship_method", "").strip(),
                "tracking_num": record.get("tracking_num", "").strip(),
                "ship_time": record.get("ship_time", "").strip(),
            }
    return list(seen.values())


def parse_ship_datetime(ship_time: str):
    """CSVの ship_time（例 "2/4/26, 11:43 AM"）をdatetimeに変換する。失敗時はNone。"""
    try:
        return datetime.strptime(ship_time.strip(), "%m/%d/%y, %I:%M %p")
    except Exception:
        return None


def parse_ship_date(ship_time: str) -> str:
    """CSVの ship_time から YYYY-MM-DD を返す（updateOrderShippingのshippingDate用）。"""
    dt = parse_ship_datetime(ship_time)
    if dt is not None:
        return dt.strftime("%Y-%m-%d")
    return datetime.now(JST).strftime("%Y-%m-%d")


def resolve_store(order_number: str):
    for prefix, store in ORDER_PREFIX_TO_STORE.items():
        if order_number.startswith(prefix):
            return store
    return None


# ══ 楽天RMS 受注管理API ═══════════════════════════
def get_orders(headers: dict, order_numbers: list) -> dict:
    """getOrderをまとめて呼び、{orderNumber: OrderModel} を返す。"""
    result = {}
    for i in range(0, len(order_numbers), 100):
        batch = order_numbers[i:i + 100]
        res = requests.post(
            f"{ORDER_BASE}/getOrder/",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            json={"orderNumberList": batch, "version": ORDER_API_VERSION},
            timeout=30,
        )
        if res.status_code != 200:
            print(f"  getOrder失敗: status={res.status_code} {res.text[:300]}")
            continue
        data = res.json()
        for order in data.get("OrderModelList", []) or []:
            result[order.get("orderNumber")] = order
    return result


def update_order_shipping(headers: dict, order_number: str, basket_id, delivery_company: str,
                           shipping_number: str, shipping_date: str):
    body = {
        "orderNumber": order_number,
        "BasketidModelList": [{
            "basketId": basket_id,
            "ShippingModelList": [{
                "deliveryCompany": delivery_company,
                "shippingNumber": shipping_number,
                "shippingDate": shipping_date,
            }],
        }],
    }
    res = requests.post(
        f"{ORDER_BASE}/updateOrderShipping/",
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
        json=body,
        timeout=30,
    )
    if res.status_code != 200:
        return False, f"status={res.status_code} {res.text[:300]}"
    data = res.json()
    messages = data.get("MessageModelList", []) or []
    errors = [m for m in messages if m.get("messageType") == "ERROR"]
    if errors:
        return False, "; ".join(m.get("message", "") for m in errors)
    return True, ""


# ══ メイン ════════════════════════════════════════
def main():
    print("=== 楽天 出荷通知 自動反映 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：updateOrderShippingは呼びません")

    with sync_playwright() as p:
        orders = collect_shipped_orders(p)

    print(f"取得した発送済み注文（重複除去後）: {len(orders)}件")

    if ONLY_ORDER_NUMBERS:
        orders = [o for o in orders if o["order_number"] in ONLY_ORDER_NUMBERS]
        print(f"ONLY_ORDER_NUMBERS指定により絞り込み: {len(orders)}件（対象: {sorted(ONLY_ORDER_NUMBERS)}）")

    by_store = {"americana": [], "founder": []}
    ignored_store = 0
    missing_info = 0
    unmapped_carriers = []

    for o in orders:
        if not o["tracking_num"] or not o["ship_method"]:
            missing_info += 1
            continue
        store = resolve_store(o["order_number"])
        if store is None:
            ignored_store += 1
            continue
        code = CARRIER_CODES.get(o["ship_method"])
        if code is None:
            unmapped_carriers.append(o)
            continue
        o["delivery_company"] = code
        o["shipping_date"] = parse_ship_date(o["ship_time"])
        by_store[store].append(o)

    print(f"対象外（店舗判定不能）: {ignored_store}件 / 情報不足: {missing_info}件 / "
          f"配送会社名未マッピング: {len(unmapped_carriers)}件")

    errors = []
    registered = 0
    skipped_already = 0
    not_found = 0

    for store, targets in by_store.items():
        if not targets:
            continue
        print(f"\n--- {store}（{len(targets)}件） ---")
        headers = auth_headers(**STORES[store])
        order_numbers = [t["order_number"] for t in targets]
        order_map = get_orders(headers, order_numbers)

        for t in targets:
            if MAX_PER_RUN is not None and registered >= MAX_PER_RUN:
                print(f"  MAX_PER_RUN={MAX_PER_RUN}に達したため、残りは今回スキップします。")
                break

            order = order_map.get(t["order_number"])
            if order is None:
                not_found += 1
                errors.append({"order_number": t["order_number"], "message": "getOrderで見つかりませんでした"})
                continue

            packages = order.get("PackageModelList", []) or []
            if not packages:
                errors.append({"order_number": t["order_number"], "message": "PackageModelListが空です"})
                continue

            for pkg in packages:
                basket_id = pkg.get("basketId")
                existing = pkg.get("ShippingModelList") or []
                if existing:
                    skipped_already += 1
                    print(f"  {t['order_number']} (basket {basket_id}): 登録済みのためスキップ")
                    continue

                if DRY_RUN:
                    print(f"  【DRY RUN】{t['order_number']} (basket {basket_id}): "
                          f"{t['ship_method']}({t['delivery_company']}) / {t['tracking_num']} / {t['shipping_date']}")
                    registered += 1
                    continue

                ok, message = update_order_shipping(
                    headers, t["order_number"], basket_id,
                    t["delivery_company"], t["tracking_num"], t["shipping_date"],
                )
                if ok:
                    registered += 1
                    print(f"  {t['order_number']} (basket {basket_id}): 登録成功")
                else:
                    errors.append({"order_number": t["order_number"], "message": message})
                    print(f"  {t['order_number']} (basket {basket_id}): 登録失敗 {message}")

    print(f"\n=== 完了: 登録{registered}件 / 既登録スキップ{skipped_already}件 / "
          f"注文見つからず{not_found}件 / エラー{len(errors)}件 / 要確認{len(unmapped_carriers)}件 ===")

    if unmapped_carriers or errors:
        post_chatwork_task(CW_ROOM_ID, CW_ASSIGNEE_ID, build_report(unmapped_carriers, errors))
    else:
        print("エラー・要確認とも無かったため、Chatworkへは通知しません。")

    print("=== 楽天 出荷通知 自動反映 完了 ===")


if __name__ == "__main__":
    main()
