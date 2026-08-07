"""
社内システム（app.jrcreators.com）で楽天チャネルの注文が発送済みになったら、
発送日・追跡番号・配送会社を楽天RMSへ自動反映する。

処理の流れ:
  1. Playwrightで app.jrcreators.com にログイン（Basic認証 + フォームログインの2段階）
  2. /so-heads を開き、start_date/end_date（created_time範囲、既定で当日から
     CREATED_TIME_LOOKBACK_DAYS日前まで）だけをフォームに入力して1回だけ検索する
     （so_sheets.pyと同じ、実績のある方式。ship_date・sales_account_idのようなURL
     フィルターはbotセッションだと検索結果が常に0件になる既知のクセがあるため使わない。
     詳しくは検証時の会話を参照。1回のログインで検索も1回だけ行う）
  3. 取得したCSVをPython側でフィルタする：order_number単位に集約し、プレフィックスで
     店舗（Americana/Founder）を判定、ship_time列が直近LOOKBACK_DAYS日以内のものだけを
     対象にし、ship_methodを配送会社コードに変換する（対象外・変換不能なものはスキップ）
  4. 店舗ごとに getOrder でまとめて取得し、PackageModelList[].ShippingModelList が
     既に値を持つもの（＝登録済み。-Rの再送注文で手動登録済みの場合も含む）はスキップ
  5. 未登録のものだけ updateOrderShipping で発送情報を登録する
     （送付先が複数ある注文は、全basketIdに同じ発送情報を登録する）
  6. エラー・要確認（未知の配送会社名等）があった場合のみ Chatworkに通知する
"""

import os
import re
import csv
import time
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from playwright.sync_api import sync_playwright

from rakuten_coupon_api import auth_headers

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))  # 当日を含め何日分（ship_time基準）を対象にするか
# created_time（注文日）の検索範囲。発送はたいてい注文の数日〜数週間後になるため、
# LOOKBACK_DAYSより広めに取る必要がある
CREATED_TIME_LOOKBACK_DAYS = int(os.environ.get("CREATED_TIME_LOOKBACK_DAYS", "21"))
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
    "Yamato Over Size": "1001",  # 楽天のdeliveryCompanyは運送会社単位のコードのため、ヤマトはサービス種別によらず同じ1001
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
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.click('a:has-text("Login"), button:has-text("Login")')
    page.wait_for_load_state("networkidle")
    page.fill('input[name="username"]', LOGIN_ID_2)
    page.fill('input[type="password"]', LOGIN_PASS_2)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")
    print("ログイン完了")


def fetch_recent_orders(page, context, start_date: str, end_date: str):
    """created_time範囲でso-headsを検索し、(ヘッダー行, データ行のリスト) を返す。
    データが無ければ (None, [])。

    so_sheets.pyの_fetch_so_rangeと同じ、実績のある方式（フォームにstart_date/end_date
    だけを入力してSearch）。ship_date・sales_account_idのようなURLフィルターはbotセッション
    だと検索結果が常に0件になることを確認済みのため使わない。
    """
    for attempt in range(1, 4):  # 最大3回
        try:
            page.goto(SO_HEADS_URL, wait_until="networkidle")
            page.wait_for_timeout(2000)

            page.locator('input[name="start_date"]').first.fill(start_date)
            page.locator('input[name="end_date"]').first.fill(end_date)
            page.click('button:has-text("Search"), input[value="Search"]')
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            # so_sheets.pyと同じLocator方式でhrefを取得する（Playwrightの自動待機が効く。
            # page.evaluate()での即時JS評価は、結果テーブルの描画が完了する前に実行されて
            # href未取得のまま「0件」と誤判定することがあると判明したため使わない）
            download_link = page.locator('a:has-text("Download"), button:has-text("Download")').first
            href = download_link.get_attribute("href")
            if not href:
                print(f"  {start_date}〜{end_date}: Downloadリンクが見つかりません（該当データ無しの可能性）")
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
                raise Exception(f"CSVダウンロード失敗 status={response.status_code}")

            text = response.content.decode("utf-8-sig", errors="replace")
            rows = list(csv.reader(text.splitlines()))
            if not rows:
                return None, []
            return rows[0], rows[1:]
        except Exception as e:
            if attempt == 3:
                raise
            wait = 15 * attempt
            print(f"  通信エラー、{wait}秒後に再試行 ({attempt}/3): {e}")
            time.sleep(wait)


def collect_shipped_orders(page, context) -> list:
    """created_time範囲でCSVを取得し、ship_timeが直近LOOKBACK_DAYS日以内の注文を
    order_number単位に集約したリストを返す。
    各要素: {"order_number": ..., "ship_method": ..., "tracking_num": ..., "ship_time": ...}
    """
    today = datetime.now(JST).date()
    start_date = (today - timedelta(days=CREATED_TIME_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    print(f"取得中: created_time {start_date} 〜 {end_date}")
    header, rows = fetch_recent_orders(page, context, start_date, end_date)
    if not header:
        return []
    print(f"  取得: {len(rows)}行")

    cutoff = datetime.now(JST).replace(tzinfo=None) - timedelta(days=LOOKBACK_DAYS)
    seen = {}
    for row in rows:
        if not any(row):
            continue
        record = dict(zip(header, row))
        order_number = record.get("order_number", "").strip()
        if not order_number or order_number in seen:
            continue
        ship_time = record.get("ship_time", "").strip()
        ship_dt = parse_ship_datetime(ship_time)
        if ship_dt is None or ship_dt < cutoff:
            continue
        seen[order_number] = {
            "order_number": order_number,
            "ship_method": record.get("ship_method", "").strip(),
            "tracking_num": record.get("tracking_num", "").strip(),
            "ship_time": ship_time,
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


def api_order_number(order_number: str) -> str:
    """社内システムの注文番号から、楽天RMS APIに渡せる実際の注文番号を求める。

    再送注文は社内システム側で末尾に -R, -R2 のような接尾辞が付くが、これは楽天RMS上に
    存在しない番号でgetOrder/updateOrderShippingがエラーになる。実際の楽天注文はあくまで
    接尾辞なしの元注文なので、それを取り除いた番号でAPIを呼ぶ（1回の実行で同じ元注文の
    通常発送分・再送分の両方が同時に対象になることはない前提）。
    """
    return re.sub(r"-R\d*$", "", order_number)


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
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1800, "height": 900},
                device_scale_factor=2,
                user_agent=USER_AGENT,
            )
            page = context.new_page()
            login(page)
            orders = collect_shipped_orders(page, context)
        finally:
            browser.close()

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
        order_numbers = [api_order_number(t["order_number"]) for t in targets]
        order_map = get_orders(headers, order_numbers)

        for t in targets:
            if MAX_PER_RUN is not None and registered >= MAX_PER_RUN:
                print(f"  MAX_PER_RUN={MAX_PER_RUN}に達したため、残りは今回スキップします。")
                break

            order = order_map.get(api_order_number(t["order_number"]))
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
                    headers, api_order_number(t["order_number"]), basket_id,
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
