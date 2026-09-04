"""
社内システム（app.jrcreators.com）でYahoo!ショッピングチャネルの注文が発送済みになったら、
出荷ステータス→注文ステータスをYahoo!ショッピングAPIへ自動反映する。

処理の流れ:
  1. rakuten_ship_notify.pyのlogin/fetch_recent_orders/collect_shipped_ordersをそのまま
     再利用し、直近LOOKBACK_DAYS日以内に発送された全チャネルの注文を取得する
  2. shop_name（社内システム上の店舗名）でYahooの3店舗（American Kitchen/Meta Store/
     Yahoo Rainbow Farms）だけに絞り込み、seller_idを判定する
  3. 社内システムのorder_number（例: "American Kitchen10053228"）からshop_name部分を
     取り除いた数字部分と、seller_idを組み合わせてYahooのOrderId
     （例: "americankitchen-10053228"）を組み立てる
  4. 注文詳細API（orderInfo）で現在のShipStatusを確認し、既に出荷済み/着荷済みなら
     スキップする
  5. 未処理のものだけ、出荷ステータス変更API（orderShipStatusChange、ShipStatus=3）→
     少し待機→注文ステータス変更API（orderStatusChange、OrderStatus=5）の順に呼ぶ
     （ドキュメント上、出荷ステータス変更の直後に注文ステータスを完了にはできないため）
  6. エラー・要確認（未知の配送会社名等）があった場合のみChatworkに通知する

未確定点（実地テストで確認が必要。docstring/コメントに詳細あり）:
  - orderShipStatusChangeとorderStatusChangeの間に必要な待機時間
  - orderInfoのレスポンスから現在のOrderStatusを取得するフィールド名
  - Yahoo側の配送会社コードの完全な一覧（Yamato/Sagawa以外は未確認）
"""

import os
import re
import sys
import time
import requests
from xml.etree import ElementTree
from datetime import timedelta
from playwright.sync_api import sync_playwright

from rakuten_ship_notify import (
    LA_TZ,
    CREATED_TIME_LOOKBACK_DAYS,
    ONLY_ORDER_NUMBERS,
    MAX_PER_RUN,
    login,
    collect_shipped_orders,
    post_chatwork_task,
    CW_ROOM_ID,
    CW_ASSIGNEE_ID,
    CW_MENTION,
)
from case_orders_auto_close import (
    YAHOO_TOKEN_URL,
    YAHOO_BASE,
    API_INTERVAL,
    get_spreadsheet,
)

# 2026-09-03、YahooのIP許可申請が承認されたため、出荷通知専用アプリ
# （YAHOO_ORDER_CLIENT_ID/SECRET）に戻した。px-04102（セッション競合）は
# 共有YAHOO_CLIENT_IDに起因していたため、専用アプリ運用で発生しないはず。
YAHOO_ORDER_CLIENT_ID = os.environ["YAHOO_ORDER_CLIENT_ID"]
YAHOO_ORDER_CLIENT_SECRET = os.environ["YAHOO_ORDER_CLIENT_SECRET"]

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

# 注文系API（orderList/orderInfo/orderChange）は、Close/価格調整と共有しているrefresh_token
# （Yahoo_Configタブ）を使うと「AccessToken has been expired. This API session is shorter
# than another API.」(px-04102)になることが実機で確認できた（2026-08-27）。Close/価格調整が
# 同じトークンを通常APIで使い続けることでセッションの扱いが競合するとみられるため、
# 出荷通知専用の完全に独立したrefresh_tokenを別タブ（Yahoo_Config_Order）で持つ。
ORDER_CONFIG_SHEET_NAME = "Yahoo_Config_Order"


# 2026-08-31、実機で確認：注文系APIのアクセストークンは事実上1回使うとすぐに
# 「session is shorter than another API」(px-04102)になり、5分キャッシュではもたない。
# call_with_session_conflict_retry() が失敗のたびに強制再取得するため、結果的に
# ほぼ毎注文でrefresh_tokenの読み書きが発生する。元の実装は毎回シート全体を
# 読みに行っていたため、注文数が多いと Google Sheets API の読み取りクォータ
# （429 Quota exceeded）に達してしまった。行位置とトークン値をプロセス内に
# キャッシュし、シートへのアクセスを最小限（初回の読み込み1回＋更新のたびの
# 書き込みのみ）に減らす。
_order_token_cache = {"token": None, "row": None}


def load_order_refresh_token(spreadsheet) -> str:
    if _order_token_cache["token"] is not None:
        return _order_token_cache["token"]
    ws = spreadsheet.worksheet(ORDER_CONFIG_SHEET_NAME)
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0] == "refresh_token":
            _order_token_cache["token"] = row[1]
            _order_token_cache["row"] = i
            return row[1]
    raise RuntimeError(f"「{ORDER_CONFIG_SHEET_NAME}」タブに refresh_token が見つかりません。")


def save_order_refresh_token(spreadsheet, new_token: str):
    _order_token_cache["token"] = new_token
    ws = spreadsheet.worksheet(ORDER_CONFIG_SHEET_NAME)
    if _order_token_cache["row"] is not None:
        ws.update(range_name=f"A{_order_token_cache['row']}:B{_order_token_cache['row']}", values=[["refresh_token", new_token]])
        return
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0] == "refresh_token":
            _order_token_cache["row"] = i
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_token]])
            return
    ws.append_row(["refresh_token", new_token])


def get_yahoo_order_access_token(spreadsheet) -> str:
    current = load_order_refresh_token(spreadsheet)
    res = requests.post(
        YAHOO_TOKEN_URL,
        auth=(YAHOO_ORDER_CLIENT_ID, YAHOO_ORDER_CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": current},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Yahoo（出荷通知専用）アクセストークン更新失敗（status={res.status_code}）: {res.text[:300]}")
    data = res.json()
    new_refresh = data.get("refresh_token", current)
    if new_refresh != current:
        save_order_refresh_token(spreadsheet, new_refresh)
    else:
        _order_token_cache["token"] = new_refresh
    return data["access_token"]


# orderShipStatusChangeの直後にorderStatusChangeを呼ぶと前提条件エラーになる可能性があるため待機する。
# ドキュメントに具体的な秒数の記載が無いため、初期値は10秒（実地テストで調整する）
WAIT_BETWEEN_STATUS_SEC = int(os.environ.get("WAIT_BETWEEN_STATUS_SEC", "10"))

# 社内システムのshop_name → Yahoo seller_id
SHOP_NAME_TO_SELLER_ID = {
    "American Kitchen": os.environ.get("YAHOO_SELLER_ID_1", "americankitchen"),
    "Meta Store": os.environ.get("YAHOO_SELLER_ID_2", "drplus"),
    "Yahoo Rainbow Farms": os.environ.get("YAHOO_SELLER_ID_3", "rainbowfarms"),
}

# ship_method（社内システムの表記）→ Yahooの配送会社コード
# 楽天側のCARRIER_CODESと対になる表。Yahoo!デベロッパーネットワークの
# 注文詳細API・配送会社コード一覧より（1000:その他 1001:ヤマト運輸 1002:佐川急便
# 1003:日本郵便 1004:西濃運輸）。ePacketは日本郵便の国際配送サービスのため1003を使う
# （2026-08-27、実際にChatworkで未マッピング報告された7件を受けて追加）。
YAHOO_CARRIER_CODES = {
    "Sagawa CDS": "1002",
    "Yamato Nekopos": "1001",
    "Yamato Over Size": "1001",
    "ePacket": "1003",
}

SHIP_STATUS_SHIPPED = "3"     # 出荷済み
ORDER_STATUS_COMPLETE = "5"   # 完了

CW_TITLE = "Yahoo出荷通知の自動反映でエラー・要確認がありました"


def resolve_yahoo_store(shop_name: str):
    return SHOP_NAME_TO_SELLER_ID.get(shop_name)


def build_order_id(shop_name: str, order_number: str, seller_id: str) -> str:
    """社内システムのorder_number（例: "American Kitchen10053228"）から
    shop_name部分を取り除いた数字部分と、seller_idを組み合わせてYahooのOrderIdを作る。

    末尾の"-R", "-R2"（再送）や"-A", "-B"（分割発送）のような接尾辞（社内システム独自の表記）は
    Yahoo側のOrderIdには存在しないため取り除く。rakuten_ship_notify.pyの
    api_order_number()と同じ正規表現（2026-08-27、americankitchen-10053490-Rが
    "Not Exists"エラーになったのを受けて追加。2026-09-03、分割発送の-Bも同様に対応）。
    """
    numeric_part = order_number[len(shop_name):].strip()
    numeric_part = re.sub(r"-[A-Z]\d*$", "", numeric_part)
    return f"{seller_id}-{numeric_part}"


# ══ Yahoo API（XML形式） ══════════════════════════
def parse_xml_fields(content: bytes) -> dict:
    """レスポンスXMLをタグ名→テキストのフラットな辞書にする（case_orders_auto_close.py
    のyahoo_get_itemと同じ考え方）。"""
    root = ElementTree.fromstring(content)
    fields = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        if text and tag not in fields:
            fields[tag] = text
    return fields


def get_order_info(token: str, seller_id: str, order_id: str):
    """注文詳細APIで現在のShipStatus等を取得する。存在しない場合はNoneを返す。
    orderShipStatusChange/orderStatusChangeと同じくXML POST形式（GET+クエリパラメータではない）。
    """
    body = (
        "<Req>"
        "<Target>"
        f"<OrderId>{order_id}</OrderId>"
        "<Field>ShipStatus,OrderStatus,ShipInvoiceNumber1,ShipCompanyCode</Field>"
        "</Target>"
        f"<SellerId>{seller_id}</SellerId>"
        "</Req>"
    )
    res = requests.post(
        f"{YAHOO_BASE}/orderInfo",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/xml"},
        data=body.encode("utf-8"),
        timeout=30,
    )
    if res.status_code >= 400:
        if "or-01001" in res.text or "not found" in res.text.lower():
            return None
        raise RuntimeError(f"orderInfo エラー({res.status_code}): {res.text[:300]}")
    return parse_xml_fields(res.content)


def change_ship_status(token: str, seller_id: str, order_id: str, carrier_code: str, tracking_num: str):
    """出荷ステータス変更API（ShipStatus=3固定）。戻り値は(成功したか, メッセージ)。"""
    body = (
        "<Req>"
        "<Target>"
        f"<OrderId>{order_id}</OrderId>"
        "<IsPointFix>true</IsPointFix>"
        "</Target>"
        "<Order><Ship>"
        f"<ShipStatus>{SHIP_STATUS_SHIPPED}</ShipStatus>"
        f"<ShipCompanyCode>{carrier_code}</ShipCompanyCode>"
        f"<ShipInvoiceNumber1>{tracking_num}</ShipInvoiceNumber1>"
        "</Ship></Order>"
        f"<SellerId>{seller_id}</SellerId>"
        "</Req>"
    )
    res = requests.post(
        f"{YAHOO_BASE}/orderShipStatusChange",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/xml"},
        data=body.encode("utf-8"),
        timeout=30,
    )
    if res.status_code >= 400:
        return False, f"status={res.status_code} {res.text[:300]}"
    return True, ""


def change_order_status(token: str, seller_id: str, order_id: str):
    """注文ステータス変更API（OrderStatus=5固定＝完了）。戻り値は(成功したか, メッセージ)。"""
    body = (
        "<Req>"
        "<Target>"
        f"<OrderId>{order_id}</OrderId>"
        "<IsPointFix>true</IsPointFix>"
        "</Target>"
        "<Order>"
        f"<OrderStatus>{ORDER_STATUS_COMPLETE}</OrderStatus>"
        "</Order>"
        f"<SellerId>{seller_id}</SellerId>"
        "</Req>"
    )
    res = requests.post(
        f"{YAHOO_BASE}/orderStatusChange",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/xml"},
        data=body.encode("utf-8"),
        timeout=30,
    )
    if res.status_code >= 400:
        return False, f"status={res.status_code} {res.text[:300]}"
    return True, ""


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


# ══ メイン ════════════════════════════════════════
def main():
    print("=== Yahoo 出荷通知 自動反映 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：出荷/注文ステータス変更APIは呼びません")

    spreadsheet = get_spreadsheet()
    # 注文系API（orderList/orderInfo/orderChange）のアクセストークンは、5分キャッシュを
    # 試しても効果が無く、実質1回使うとすぐに「AccessToken has been expired. This API
    # session is shorter than another API.」(px-04102)になることが実機で確認できた
    # （2026-08-27、2026-08-31）。そのため毎回のAPI呼び出し前に必ず取り直す
    # （キャッシュしない）。無駄な失敗呼び出しを減らすため、call_with_session_conflict_retry
    # 経由の呼び出しは常にforce=Trueで取得する。
    yahoo_token_state = {"token": get_yahoo_order_access_token(spreadsheet)}

    def get_fresh_yahoo_token(force: bool = True):
        if force:
            yahoo_token_state["token"] = get_yahoo_order_access_token(spreadsheet)
        return yahoo_token_state["token"]

    def call_with_session_conflict_retry(func, *args, _max_retries: int = 2):
        """
        px-04102（「AccessToken has been expired. This API session is shorter than
        another API.」＝セッション競合）が出た場合、専用トークンを強制的に取り直して
        再試行する。2026-08-31、5分ごとの定期更新だけでは足りず全件が
        px-04102で失敗する事象が発生（GitHub Actions側の延命更新keepaliveの実行タイミング
        が数十分〜1時間ずれることがあり、ローカルPCでの実行中と偶然重なって
        refresh_tokenを取り合ってしまうことが原因と推測）。
        2026-09-04、専用アプリに切り替え後もタイミングの重なりが見当たらないまま
        全件px-04102で失敗する事象を確認。即座の1回リトライだけでは間に合わない
        可能性があるため、少し待ってから最大2回まで再試行するようにする。
        """
        def is_conflict(exc_or_result) -> bool:
            if isinstance(exc_or_result, Exception):
                return "px-04102" in str(exc_or_result)
            return (
                isinstance(exc_or_result, tuple)
                and len(exc_or_result) == 2
                and exc_or_result[0] is False
                and "px-04102" in str(exc_or_result[1])
            )

        for attempt in range(_max_retries + 1):
            try:
                result = func(get_fresh_yahoo_token(force=(attempt > 0)), *args)
            except RuntimeError as e:
                if not is_conflict(e) or attempt == _max_retries:
                    raise
                print(f"  セッション競合(px-04102)を検知。{3 * (attempt + 1)}秒待機してトークンを取り直し、再試行します（{attempt + 1}/{_max_retries}）。")
                time.sleep(3 * (attempt + 1))
                continue

            if is_conflict(result) and attempt < _max_retries:
                print(f"  セッション競合(px-04102)を検知。{3 * (attempt + 1)}秒待機してトークンを取り直し、再試行します（{attempt + 1}/{_max_retries}）。")
                time.sleep(3 * (attempt + 1))
                continue
            return result

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1800, "height": 900},
                device_scale_factor=2,
            )
            page = context.new_page()
            login(page)
            orders = collect_shipped_orders(page, context)
        finally:
            browser.close()

    print(f"取得した発送済み注文（全チャネル・重複除去後）: {len(orders)}件")

    yahoo_orders = []
    for o in orders:
        seller_id = resolve_yahoo_store(o["shop_name"])
        if seller_id is None:
            continue
        o["seller_id"] = seller_id
        o["yahoo_order_id"] = build_order_id(o["shop_name"], o["order_number"], seller_id)
        yahoo_orders.append(o)
    print(f"Yahoo対象（American Kitchen/Meta Store/Yahoo Rainbow Farms）: {len(yahoo_orders)}件")

    if ONLY_ORDER_NUMBERS:
        yahoo_orders = [o for o in yahoo_orders if o["order_number"] in ONLY_ORDER_NUMBERS]
        print(f"ONLY_ORDER_NUMBERS指定により絞り込み: {len(yahoo_orders)}件（対象: {sorted(ONLY_ORDER_NUMBERS)}）")

    missing_info = 0
    unmapped_carriers = []
    errors = []
    registered = 0
    skipped_already = 0
    not_found = 0

    for o in yahoo_orders:
        if MAX_PER_RUN is not None and registered >= MAX_PER_RUN:
            print(f"MAX_PER_RUN={MAX_PER_RUN}に達したため、残りは今回スキップします。")
            break

        if not o["tracking_num"]:
            missing_info += 1
            continue

        if not o["ship_method"] or o["ship_method"].strip().lower() == "none":
            carrier_code = YAHOO_CARRIER_CODES.get("Sagawa CDS")
        else:
            carrier_code = YAHOO_CARRIER_CODES.get(o["ship_method"])
            if carrier_code is None:
                unmapped_carriers.append(o)
                continue

        order_id = o["yahoo_order_id"]
        try:
            info = call_with_session_conflict_retry(get_order_info, o["seller_id"], order_id)
        except Exception as e:
            print(f"  {o['order_number']}（{order_id}）: orderInfo取得エラー: {e}")
            errors.append({"order_number": o["order_number"], "message": f"orderInfo取得エラー: {e}"})
            continue
        finally:
            time.sleep(API_INTERVAL)

        if info is None:
            not_found += 1
            print(f"  {o['order_number']}（{order_id}）: orderInfoで見つかりませんでした")
            errors.append({"order_number": o["order_number"], "message": f"orderInfoで見つかりませんでした（OrderId={order_id}）"})
            continue

        current_ship_status = info.get("ShipStatus")
        if current_ship_status in (SHIP_STATUS_SHIPPED, "4"):  # 3=出荷済み, 4=着荷済み
            skipped_already += 1
            print(f"  {o['order_number']}（{order_id}）: 既に出荷済み/着荷済みのためスキップ")
            continue

        if DRY_RUN:
            print(f"  【DRY RUN】{o['order_number']}（{order_id}）: "
                  f"{o['ship_method'] or 'Sagawa CDS(既定)'}({carrier_code}) / {o['tracking_num']}")
            registered += 1
            continue

        ok, message = call_with_session_conflict_retry(change_ship_status, o["seller_id"], order_id, carrier_code, o["tracking_num"])
        time.sleep(API_INTERVAL)
        if not ok:
            errors.append({"order_number": o["order_number"], "message": f"出荷ステータス変更失敗: {message}"})
            print(f"  {o['order_number']}（{order_id}）: 出荷ステータス変更失敗 {message}")
            continue

        print(f"  {o['order_number']}（{order_id}）: 出荷ステータス変更成功、"
              f"{WAIT_BETWEEN_STATUS_SEC}秒待機してから完了にします")
        time.sleep(WAIT_BETWEEN_STATUS_SEC)

        ok, message = call_with_session_conflict_retry(change_order_status, o["seller_id"], order_id)
        time.sleep(API_INTERVAL)
        if ok:
            registered += 1
            print(f"  {o['order_number']}（{order_id}）: 完了にしました")
        else:
            errors.append({"order_number": o["order_number"], "message": f"注文ステータス変更失敗: {message}"})
            print(f"  {o['order_number']}（{order_id}）: 注文ステータス変更失敗 {message}")

    print(f"\n=== 完了: 登録{registered}件 / 既登録スキップ{skipped_already}件 / "
          f"情報不足{missing_info}件 / 見つからず{not_found}件 / エラー{len(errors)}件 / "
          f"要確認{len(unmapped_carriers)}件 ===")

    if unmapped_carriers or errors:
        post_chatwork_task(CW_ROOM_ID, CW_ASSIGNEE_ID, build_report(unmapped_carriers, errors))
    else:
        print("エラー・要確認とも無かったため、Chatworkへは通知しません。")

    print("=== Yahoo 出荷通知 自動反映 完了 ===")


if __name__ == "__main__":
    main()
