"""
社内システム（app.jrcreators.com）でShopifyチャネル（100percentpure.jp/Rainbow Farms
Japan/Active Wow Japanの3店舗）の注文が発送済みになったら、Shopify Admin APIへ
発送情報（追跡番号・配送会社）を登録し、Shopify標準の出荷通知メールを顧客に自動送信する。
rakuten_ship_notify.py/yahoo_ship_notify.pyと同じ構成。

処理の流れ:
  1. rakuten_ship_notify.pyのlogin/fetch_recent_ordersを再利用し、created_time
     直近CREATED_TIME_LOOKBACK_DAYS日以内の全チャネルの注文CSVを取得する
  2. Shopify系の注文はCSVの`shop_name`列が空欄になる（楽天/Yahoo/Wowmaと違う
     フィールドが使われている）。店舗の判定には代わりに`sku_shop`列を使う
     （2026-09-21、実機調査で判明。値は"100％ Pure"のように全角記号混じりの
     こともあるので、STORESのキーは実データをそのまま使う）。ship_timeが
     直近LOOKBACK_DAYS日以内の行だけ、order_number単位に集約する
  3. 社内システムのorder_numberは、ユーザー確認済みでShopify側の注文番号
     （Name欄）と完全一致するため、そのままorders.json?name=で検索する
     （Shopifyは通常Nameの先頭に"#"を付けるため、付き/付き無し両方を試す）
  4. 注文が見つかったら fulfillment_orders.json で未発送（status=open）の
     fulfillment orderを取得。無ければ登録済みとみなしてスキップ
  5. 未登録のものだけ fulfillments.json に追跡番号・配送会社・
     notify_customer=true を登録する（登録と同時に顧客へ標準の出荷通知
     メールが送られる。文面はカスタマイズせず標準のまま使う方針）
  6. エラー・要確認（注文が見つからない等）があった場合のみChatworkに通知する

Rakuten RMSと違い、配送会社名を数値コードへ変換する必要はない
（Shopifyのtracking_info.companyは自由記述のテキストのため、社内システムの
ship_method欄をそのまま渡す）。
"""

import os
import requests
from datetime import datetime, timedelta

from rakuten_ship_notify import (
    LA_TZ,
    CREATED_TIME_LOOKBACK_DAYS,
    ONLY_ORDER_NUMBERS,
    MAX_PER_RUN,
    login,
    fetch_recent_orders,
    parse_ship_datetime,
    parse_ship_date,
    post_chatwork_task,
    CW_ROOM_ID,
    CW_ASSIGNEE_ID,
    CW_MENTION,
)

from playwright.sync_api import sync_playwright

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))
SHOPIFY_API_VERSION = "2025-10"

CW_TITLE = "Shopify出荷通知の自動反映でエラー・要確認がありました"

# ── 店舗定義 ──────────────────────────────────────
# キーは社内システムCSVの`sku_shop`列の実際の値（2026-09-21実機確認。
# "100％ Pure"は全角%）。domainは.myshopify.comドメイン、token_envは
# GitHub Secrets/環境変数名（値はAdmin API access token、
# shopify-ship-notify-setup.md参照の手動OAuthで取得済み）。
# ※ Active Wow Japanは調査時点で該当注文が無く、sku_shop表記が「ActiveWow」で
#   合っているか未確認（sales_account_idのプルダウン表記から類推した値）。
#   最初の実注文で要検証。
STORES = {
    "100％ Pure": {
        "domain": "100percentpure-jp.myshopify.com",
        "token_env": "SHOPIFY_ACCESS_TOKEN_PUREJP",
    },
    "RainbowFarms": {
        "domain": "rainbowfarmsjapan.myshopify.com",
        "token_env": "SHOPIFY_ACCESS_TOKEN_RAINBOW",
    },
    "ActiveWow": {
        "domain": "active-wow-japan.myshopify.com",
        "token_env": "SHOPIFY_ACCESS_TOKEN_ACTIVEWOW",
    },
}


def collect_shopify_orders(page, context) -> list:
    """created_time範囲でCSVを取得し、shop_nameが空欄（＝Shopify系を含む
    非マーケットプレイス扱いの注文）かつsku_shopがSTORESに該当し、ship_timeが
    直近LOOKBACK_DAYS日以内の注文をorder_number単位に集約して返す。
    rakuten_ship_notify.pyのcollect_shipped_ordersと同じ考え方だが、
    店舗判定列がshop_nameではなくsku_shopである点が異なる。"""
    today = datetime.now(LA_TZ).date()
    start_date = (today - timedelta(days=CREATED_TIME_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    print(f"取得中: created_time {start_date} 〜 {end_date}")
    header, rows = fetch_recent_orders(page, context, start_date, end_date)
    if not header:
        return []
    print(f"  取得: {len(rows)}行")

    cutoff = datetime.now(LA_TZ).replace(tzinfo=None) - timedelta(days=LOOKBACK_DAYS)
    seen = {}
    for row in rows:
        if not any(row):
            continue
        record = dict(zip(header, row))
        if record.get("shop_name", "").strip():
            continue  # 楽天/Yahoo/Wowma等、別列で判定する既存チャネルは対象外
        sku_shop = record.get("sku_shop", "").strip()
        if sku_shop not in STORES:
            continue
        order_number = record.get("order_number", "").strip()
        if not order_number or order_number in seen:
            continue
        ship_time = record.get("ship_time", "").strip()
        ship_dt = parse_ship_datetime(ship_time)
        if ship_dt is None or ship_dt < cutoff:
            continue
        seen[order_number] = {
            "order_number": order_number,
            "sku_shop": sku_shop,
            "ship_method": record.get("ship_method", "").strip(),
            "tracking_num": record.get("tracking_num", "").strip(),
            "ship_time": ship_time,
        }
    return list(seen.values())


class ShopifyAuthError(RuntimeError):
    """アクセストークンが無効（401）な場合に送出する。店舗単位でスキップし、
    その店舗のためだけに全件エラーにしない（rakuten_ship_notify.pyのRMSAuthErrorと同じ考え方）。"""


def shopify_headers(token: str) -> dict:
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json; charset=utf-8"}


def find_order(domain: str, headers: dict, order_number: str):
    """order_numberでShopify注文を検索する。Shopifyは通常Nameの先頭に"#"を
    付けるため、付き/付き無し両方を試す。見つからなければNone。"""
    base = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    for name in (order_number, f"#{order_number}"):
        res = requests.get(
            base, headers=headers,
            params={"name": name, "status": "any"},
            timeout=30,
        )
        if res.status_code == 401:
            raise ShopifyAuthError(f"status=401 {res.text[:300]}")
        if res.status_code != 200:
            continue
        orders = res.json().get("orders", []) or []
        if orders:
            return orders[0]
    return None


def get_open_fulfillment_orders(domain: str, headers: dict, order_id) -> list:
    res = requests.get(
        f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/fulfillment_orders.json",
        headers=headers, timeout=30,
    )
    if res.status_code == 401:
        raise ShopifyAuthError(f"status=401 {res.text[:300]}")
    if res.status_code != 200:
        return []
    all_fo = res.json().get("fulfillment_orders", []) or []
    return [fo for fo in all_fo if fo.get("status") == "open"]


def create_fulfillment(domain: str, headers: dict, fulfillment_order_id, carrier: str, tracking_number: str):
    body = {
        "fulfillment": {
            "line_items_by_fulfillment_order": [
                {"fulfillment_order_id": fulfillment_order_id}
            ],
            "tracking_info": {
                "number": tracking_number,
                "company": carrier,
            },
            "notify_customer": True,
        }
    }
    res = requests.post(
        f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/fulfillments.json",
        headers=headers, json=body, timeout=30,
    )
    if res.status_code == 401:
        raise ShopifyAuthError(f"status=401 {res.text[:300]}")
    if res.status_code not in (200, 201):
        return False, f"status={res.status_code} {res.text[:300]}"
    return True, ""


def build_report(errors: list, auth_error_stores: list) -> str:
    lines = [CW_MENTION, f"[info][title]{CW_TITLE}[/title]", ""]
    if auth_error_stores:
        stores_label = "、".join(auth_error_stores)
        lines.append(f"■ APIキー認証エラー: {stores_label}店舗")
        lines.append("Shopify Admin API access tokenが失効（アプリのアンインストール等）している可能性があります。再取得してください。")
        lines.append("")
    if errors:
        lines.append(f"■ エラー: {len(errors)}件")
        lines.append("詳細はGitHub Actionsの実行ログを確認してください。")
        lines.append("")
    lines.append("[/info]")
    return "\n".join(lines)


def main():
    print("=== Shopify 出荷通知 自動反映 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：fulfillments.jsonは呼びません")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1800, "height": 900},
                device_scale_factor=2,
            )
            page = context.new_page()
            login(page)
            orders = collect_shopify_orders(page, context)
        finally:
            browser.close()

    print(f"取得した発送済み注文（重複除去後）: {len(orders)}件")

    if ONLY_ORDER_NUMBERS:
        orders = [o for o in orders if o["order_number"] in ONLY_ORDER_NUMBERS]
        print(f"ONLY_ORDER_NUMBERS指定により絞り込み: {len(orders)}件（対象: {sorted(ONLY_ORDER_NUMBERS)}）")

    by_store = {name: [] for name in STORES}
    ignored_store = 0
    missing_info = 0

    for o in orders:
        if not o["tracking_num"]:
            missing_info += 1
            continue
        store = o["sku_shop"]
        if store not in STORES:
            ignored_store += 1
            continue
        o["shipping_date"] = parse_ship_date(o["ship_time"])
        by_store[store].append(o)

    print(f"対象外（未知のsku_shop）: {ignored_store}件 / 情報不足: {missing_info}件")

    errors = []
    auth_error_stores = []
    registered = 0
    skipped_already = 0
    not_found = 0

    for store, targets in by_store.items():
        if not targets:
            continue
        print(f"\n--- {store}（{len(targets)}件） ---")

        token = os.environ.get(STORES[store]["token_env"])
        if not token:
            print(f"  {STORES[store]['token_env']} が未設定のため、{store}店舗はスキップします。")
            auth_error_stores.append(store)
            continue
        domain = STORES[store]["domain"]
        headers = shopify_headers(token)

        try:
            for t in targets:
                if MAX_PER_RUN is not None and registered >= MAX_PER_RUN:
                    print(f"  MAX_PER_RUN={MAX_PER_RUN}に達したため、残りは今回スキップします。")
                    break

                order = find_order(domain, headers, t["order_number"])
                if order is None:
                    not_found += 1
                    errors.append({"order_number": t["order_number"], "message": "Shopify側で注文が見つかりませんでした"})
                    continue

                open_fos = get_open_fulfillment_orders(domain, headers, order["id"])
                if not open_fos:
                    skipped_already += 1
                    print(f"  {t['order_number']}: 登録済み（未発送のfulfillment orderなし）のためスキップ")
                    continue

                if DRY_RUN:
                    print(f"  【DRY RUN】{t['order_number']}: {t['ship_method']} / {t['tracking_num']} "
                          f"/ fulfillment_order_id={[fo['id'] for fo in open_fos]}")
                    registered += 1
                    continue

                ok_all = True
                for fo in open_fos:
                    ok, message = create_fulfillment(domain, headers, fo["id"], t["ship_method"], t["tracking_num"])
                    if not ok:
                        ok_all = False
                        errors.append({"order_number": t["order_number"], "message": message})
                        print(f"  {t['order_number']} (fulfillment_order {fo['id']}): 登録失敗 {message}")
                if ok_all:
                    registered += 1
                    print(f"  {t['order_number']}: 登録成功")
        except ShopifyAuthError as e:
            print(f"  APIキー認証エラー、{store}店舗の残りはスキップします: {e}")
            auth_error_stores.append(store)
            continue

    print(f"\n=== 完了: 登録{registered}件 / 既登録スキップ{skipped_already}件 / "
          f"注文見つからず{not_found}件 / エラー{len(errors)}件 ===")

    if errors or auth_error_stores:
        post_chatwork_task(CW_ROOM_ID, CW_ASSIGNEE_ID, build_report(errors, auth_error_stores))
    else:
        print("エラー・要確認とも無かったため、Chatworkへは通知しません。")

    print("=== Shopify 出荷通知 自動反映 完了 ===")


if __name__ == "__main__":
    main()
