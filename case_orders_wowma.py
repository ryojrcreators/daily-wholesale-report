"""
Wowma!（現au PAYマーケット）Wow!manager APIのラッパー。

参照した仕様書（"C:\\Users\\custo\\OneDrive\\Desktop\\wowma_api_specifications\\"）：
  - Wow!manager_API利用説明書.pdf（ベースURL・認証方式）
  - 商品/【API設計書】_商品管理_価格更新API.xlsx（updateItemPrice）
  - 商品/【API設計書】_商品管理_商品情報取得API（個別）.xlsx（searchItemInfo）
  - 商品/【API設計書】_商品管理_在庫情報更新API.xlsx（updateStock、未実機検証）
  - 受注・決済/【API設計書】_受注管理_受注情報取得API.xlsx（searchTradeInfoProc、未実機検証）
  - 受注・決済/【API設計書】_受注管理_受注情報更新API.xlsx（updateTradeInfoProc、未実機検証）

【重要】XMLのタグ名は仕様書の記載から推測して実装している。
実際のAPIレスポンスと完全に一致するかは未検証のため、本番の価格変更（DRY_RUN=false）に
使う前に、まず DRY RUN で1商品だけ searchItemInfo を叩いてレスポンスを確認し、
下記の各関数のタグ名（itemPrice / status 等）が実物と合っているか必ず確認すること。

APIキーは発行から90日で失効する。切れたらWow!manager管理画面で再発行し、
GitHub Secrets の WOWMA_API_KEY を手動で更新する（自動更新は行わない）。
"""

import os
import time
from xml.etree import ElementTree

import requests

WOWMA_BASE = "https://api.manager.wowma.jp/wmshopapi"
WOWMA_API_KEY = os.environ["WOWMA_API_KEY"]
WOWMA_SHOP_ID = os.environ["WOWMA_SHOP_ID"]

SHOP_WOWMA = "Wowma"

API_INTERVAL = 1.0


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WOWMA_API_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _xml_headers() -> dict:
    return {
        "Authorization": f"Bearer {WOWMA_API_KEY}",
        "Content-Type": "application/xml; charset=UTF-8",
    }


def _parse_xml_flat(content: bytes) -> dict:
    """
    yahoo_get_item と同じ考え方：タグ名の名前空間を落として
    {タグ名: テキスト} のフラット辞書にする（ネスト構造は問わない簡易パース）。
    """
    root = ElementTree.fromstring(content)
    fields = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        if text and tag not in fields:
            fields[tag] = text
    return fields


def _strip_ns(root):
    """名前空間プレフィックスを落として、通常のfind/findall/iter(タグ名)を使えるようにする。"""
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
    return root


def wowma_search_items(start_count: int, total_count: int = 500) -> tuple:
    """
    商品情報取得API（複数）（searchItemInfos）。ページング用。
    仕様書（【API設計書】_商品管理_商品情報取得API（複数）.xlsx「IF仕様」シート）確認済み
    （2026-08-28）：REST API = /searchItemInfos（PDF利用説明書P27のサンプルURLは
    "/serchItemInfos"という綴りだったが、これはPDF側の誤植。Excel設計書のREST API欄が
    正なので、こちらに合わせる）。GET, Content-Type: application/x-www-form-urlencoded。
    それでも401（code=0002 認証に失敗しました）が出る場合はエンドポイント名の問題では
    なく、APIキーにこのAPI区分の利用権限が付与されていない可能性が高い（要:
    Wow!manager管理画面でのAPIキー権限確認）。
      - startCount: 何件目から取得するか（1始まり）、totalCount: 1回の取得件数（最大500）
      - レスポンスの maxCount が全体のヒット件数（＝これに達するまでstartCountを進めてループする）

    戻り値: (items: list[dict（itemCode/itemName/itemPriceなど）], max_count: int)
    """
    res = requests.get(
        f"{WOWMA_BASE}/searchItemInfos",
        headers=_headers(),
        params={"shopId": WOWMA_SHOP_ID, "startCount": str(start_count), "totalCount": str(total_count)},
        timeout=30,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"searchItemInfos エラー({res.status_code}): {res.text[:300]}")

    root = _strip_ns(ElementTree.fromstring(res.content))
    status = root.findtext(".//status")
    if status == "1":
        err = root.find(".//error")
        code = err.findtext("code") if err is not None else ""
        message = err.findtext("message") if err is not None else ""
        raise RuntimeError(f"searchItemInfos エラー: {code} {message}")

    max_count = int(root.findtext(".//maxCount") or "0")
    items = []
    for item_el in root.iter("resultItems"):
        item = {child.tag: (child.text or "").strip() for child in item_el if child.text}
        items.append(item)
    return items, max_count


def wowma_end_sale(item_code: str, dry_run: bool) -> tuple:
    """
    商品削除の前段階として、対象商品の販売ステータスを「販売終了」(saleStatus=2)に
    更新する。仕様書P59「商品削除API」に「※削除できる商品は販売ステータスが
    「販売終了」の場合に限ります」と明記されているため、販売中の商品はこれを経ないと
    削除できない。エンドポイント名は他の個別更新系（updateItemPrice/updateStock）の
    命名パターンからの推測であり未実機検証（2026-08-28）。

    戻り値: (成功したか, メッセージ)
    """
    if dry_run:
        return True, "【DRY RUN】販売終了への変更対象"

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<request><shopId>{WOWMA_SHOP_ID}</shopId>"
        f"<updateItem><itemCode>{item_code}</itemCode><saleStatus>2</saleStatus></updateItem>"
        "</request>"
    )
    try:
        res = requests.post(
            f"{WOWMA_BASE}/updateItemInfo", headers=_xml_headers(), data=body.encode("utf-8"), timeout=30
        )
    except Exception as e:
        return False, f"販売終了への変更エラー: {e}"

    if res.status_code >= 400:
        return False, f"販売終了への変更失敗({res.status_code}) {res.text[:150]}"

    root = _strip_ns(ElementTree.fromstring(res.content))
    status = root.findtext(".//status")
    if status == "1":
        err = root.find(".//error")
        code_ = err.findtext("code") if err is not None else ""
        message = err.findtext("message") if err is not None else ""
        return False, f"販売終了への変更失敗: {code_} {message}"
    return True, "販売終了に変更しました"


def wowma_delete_items(item_codes: list, dry_run: bool) -> list:
    """
    商品削除API（複数）（deleteItemInfos）。1回のリクエストで最大1000件まとめて削除できる。
    仕様書（【API設計書】_商品管理_商品削除API.xlsx）確認済み（2026-08-28、未実機検証）：
      - POST /deleteItemInfos、Content-Type: application/xml; charset=utf-8
      - <request><shopId>…</shopId><deleteItemInfo><itemCode>…</itemCode></deleteItemInfo>…</request>
      - 削除できる商品は販売ステータスが「販売終了」の場合に限る（P59）。事前に
        wowma_end_sale() で販売終了にしていない商品を渡すと失敗する可能性が高い。

    戻り値: [(item_code, 成功したか, メッセージ)]
    """
    if not item_codes:
        return []
    if dry_run:
        return [(code, True, "【DRY RUN】削除対象") for code in item_codes]

    from xml.sax.saxutils import escape

    delete_blocks = "".join(
        f"<deleteItemInfo><itemCode>{escape(code)}</itemCode></deleteItemInfo>" for code in item_codes
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<request><shopId>{WOWMA_SHOP_ID}</shopId>{delete_blocks}</request>"
    )
    try:
        res = requests.post(
            f"{WOWMA_BASE}/deleteItemInfos", headers=_xml_headers(), data=body.encode("utf-8"), timeout=30
        )
    except Exception as e:
        return [(code, False, f"削除エラー: {e}") for code in item_codes]

    if res.status_code >= 400:
        return [(code, False, f"削除失敗({res.status_code}) {res.text[:150]}") for code in item_codes]

    root = _strip_ns(ElementTree.fromstring(res.content))
    status = root.findtext(".//status")
    if status == "1":
        err = root.find(".//error")
        code_ = err.findtext("code") if err is not None else ""
        message = err.findtext("message") if err is not None else ""
        return [(code, False, f"削除失敗: {code_} {message}") for code in item_codes]

    deleted_codes = {el.findtext("itemCode") for el in root.iter("deleteResult")}
    return [
        (code, code in deleted_codes, "削除しました" if code in deleted_codes else "削除結果に含まれず（要確認）")
        for code in item_codes
    ]


def wowma_get_item(item_code: str):
    """
    商品情報取得API（個別）。存在しない場合は None を返す。
    現在価格の確認（更新前チェック・検証）に使う。読み取りのみ。

    実機確認済み（2026-08-13）：
      - Content-Type は application/x-www-form-urlencoded
      - 存在する商品（ry23010062）: <result><status>0</status></result>
        <searchResult><itemInfo><itemPrice>5000</itemPrice>...
      - 存在しない商品（ry99999999）: status は 0 のまま（エラー扱いではない）で
        <searchResult><itemInfo/></searchResult>（中身が空）で返る。
        → itemPrice の有無で「存在するか」を判定する必要がある。
      - shopId誤りなどの本当のAPIエラー時は <result><status>1</status>
        <error><code>CME0022</code><message>...</message></error></result>
    """
    res = requests.get(
        f"{WOWMA_BASE}/searchItemInfo",
        headers=_headers(),
        params={"shopId": WOWMA_SHOP_ID, "itemCode": item_code},
        timeout=30,
    )
    fields = _parse_xml_flat(res.content)
    status = fields.get("status")

    if res.status_code >= 400 or status == "1":
        raise RuntimeError(f"searchItemInfo エラー({res.status_code}): {res.text[:300]}")

    if not fields.get("itemPrice"):
        return None
    return fields


def wowma_update_price(item_code: str, price: int, dry_run: bool) -> tuple:
    """
    価格更新API。戻り値は (成功したか, メッセージ)。
    dry_run=True の場合は実際には呼ばずメッセージのみ返す。
    """
    if dry_run:
        return True, f"{item_code}: 【DRY RUN】¥{price:,} に更新予定"

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<request>"
        f"<shopId>{WOWMA_SHOP_ID}</shopId>"
        "<updateItemInfo>"
        f"<itemCode>{item_code}</itemCode>"
        f"<itemPrice>{price}</itemPrice>"
        "</updateItemInfo>"
        "</request>"
    )
    try:
        res = requests.post(
            f"{WOWMA_BASE}/updateItemPrice",
            headers=_xml_headers(),
            data=body.encode("utf-8"),
            timeout=30,
        )
    except Exception as e:
        return False, f"{item_code}: 更新エラー: {e}"
    finally:
        time.sleep(API_INTERVAL)

    if res.status_code >= 400:
        return False, f"{item_code}: 更新失敗({res.status_code}): {res.text[:200]}"

    fields = _parse_xml_flat(res.content)
    status = fields.get("status")
    if status == "0":
        return True, f"{item_code}: ¥{price:,} に更新しました"
    error_msg = fields.get("message", res.text[:200])
    return False, f"{item_code}: 更新失敗（status={status}）: {error_msg}"


def wowma_update_stock(item_code: str, stock_count: int, dry_run: bool) -> tuple:
    """
    在庫情報更新API。戻り値は (成功したか, メッセージ)。
    dry_run=True の場合は実際には呼ばずメッセージのみ返す。

    仕様書「在庫情報更新API」より：stockCount に 0 を送ると、Wowma側が自動的に
    販売ステータスを「販売終了」に更新してくれる（Closeケース用にはこれで十分で、
    別途ステータス変更APIを呼ぶ必要はない）。バリエーション商品（選択肢別在庫）は
    別の項目群が必要になり未対応（単品商品のみを想定）。
    """
    if dry_run:
        return True, f"{item_code}: 【DRY RUN】在庫{stock_count}に更新予定"

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<request>"
        f"<shopId>{WOWMA_SHOP_ID}</shopId>"
        "<stockUpdateItem>"
        f"<itemCode>{item_code}</itemCode>"
        "<stockSegment>1</stockSegment>"
        f"<stockCount>{stock_count}</stockCount>"
        "</stockUpdateItem>"
        "</request>"
    )
    try:
        res = requests.post(
            f"{WOWMA_BASE}/updateStock",
            headers=_xml_headers(),
            data=body.encode("utf-8"),
            timeout=30,
        )
    except Exception as e:
        return False, f"{item_code}: 更新エラー: {e}"
    finally:
        time.sleep(API_INTERVAL)

    if res.status_code >= 400:
        return False, f"{item_code}: 更新失敗({res.status_code}): {res.text[:200]}"

    fields = _parse_xml_flat(res.content)
    status = fields.get("status")
    if status == "0":
        return True, f"{item_code}: 在庫{stock_count}に更新しました"
    error_msg = fields.get("message", res.text[:200])
    return False, f"{item_code}: 更新失敗（status={status}）: {error_msg}"


# 配送業者コード（仕様書「受注情報更新API」「受注情報取得API」共通）
WOWMA_CARRIER_CODES = {
    "Yamato Nekopos": "1",   # クロネコヤマト
    "Yamato Over Size": "1",
    "Sagawa CDS": "2",       # 佐川急便
    "ePacket": "6",          # 日本郵便
}


def wowma_get_order_info(order_id: str):
    """
    受注情報取得API（searchTradeInfoProc）。存在しない場合・エラー時は None を返す。
    shippingNumber の有無で「既に発送情報が登録済みか」を判定するために使う
    （orderStatus は貴店様カスタムステータスも含む文字列のため、シンプルに追跡番号の
    有無で判定する）。未実機検証。
    """
    res = requests.get(
        f"{WOWMA_BASE}/searchTradeInfoProc",
        headers=_headers(),
        params={"shopId": WOWMA_SHOP_ID, "orderId": order_id},
        timeout=30,
    )
    fields = _parse_xml_flat(res.content)
    status = fields.get("status")

    if res.status_code >= 400 or status == "1":
        return None

    return fields


def wowma_update_trade_info(order_id: str, shipping_date: str, carrier_code: str,
                             tracking_num: str, dry_run: bool) -> tuple:
    """
    受注情報更新API（updateTradeInfoProc）。発送日・配送業者・追跡番号を登録する。
    戻り値は (成功したか, メッセージ)。dry_run=True の場合は実際には呼ばない。

    仕様書の「request」はラッパータグではなく、shopId/orderId/shippingDate等が
    直接requestの子要素になるフラットな構造（updateItemInfo/stockUpdateItemのような
    中間タグは無い）。2026-08-27、実機で「<updateTradeInfo>」でラップした版が
    「[orderId]必須入力項目です」エラーになったのを受けて修正。
    """
    if dry_run:
        return True, f"{order_id}: 【DRY RUN】{shipping_date} / carrier={carrier_code} / {tracking_num} で登録予定"

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<request>"
        f"<shopId>{WOWMA_SHOP_ID}</shopId>"
        f"<orderId>{order_id}</orderId>"
        f"<shippingDate>{shipping_date}</shippingDate>"
        f"<shippingCarrier>{carrier_code}</shippingCarrier>"
        f"<shippingNumber>{tracking_num}</shippingNumber>"
        "</request>"
    )
    try:
        res = requests.post(
            f"{WOWMA_BASE}/updateTradeInfoProc",
            headers=_xml_headers(),
            data=body.encode("utf-8"),
            timeout=30,
        )
    except Exception as e:
        return False, f"{order_id}: 更新エラー: {e}"
    finally:
        time.sleep(API_INTERVAL)

    if res.status_code >= 400:
        return False, f"{order_id}: 更新失敗({res.status_code}): {res.text[:200]}"

    fields = _parse_xml_flat(res.content)
    status = fields.get("status")
    if status == "0":
        return True, f"{order_id}: 発送情報を登録しました"
    error_msg = fields.get("message", res.text[:200])
    return False, f"{order_id}: 更新失敗（status={status}）: {error_msg}"


TRADE_STATUS_COMPLETE = "完了"


def wowma_update_trade_status(order_id: str, order_status: str, dry_run: bool) -> tuple:
    """
    受注ステータス更新API（updateTradeStsProc）。orderStatusは文字列指定
    （新規受付/出荷待ち/完了/保留 等、貴店様カスタムステータスも含む）。
    戻り値は (成功したか, メッセージ)。dry_run=True の場合は実際には呼ばない。
    updateTradeInfoProcと同じくrequestはフラット構造（未実機検証）。
    """
    if dry_run:
        return True, f"{order_id}: 【DRY RUN】ステータスを「{order_status}」に更新予定"

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<request>"
        f"<shopId>{WOWMA_SHOP_ID}</shopId>"
        f"<orderId>{order_id}</orderId>"
        f"<orderStatus>{order_status}</orderStatus>"
        "</request>"
    )
    try:
        res = requests.post(
            f"{WOWMA_BASE}/updateTradeStsProc",
            headers=_xml_headers(),
            data=body.encode("utf-8"),
            timeout=30,
        )
    except Exception as e:
        return False, f"{order_id}: ステータス更新エラー: {e}"
    finally:
        time.sleep(API_INTERVAL)

    if res.status_code >= 400:
        return False, f"{order_id}: ステータス更新失敗({res.status_code}): {res.text[:200]}"

    fields = _parse_xml_flat(res.content)
    status = fields.get("status")
    if status == "0":
        return True, f"{order_id}: ステータスを「{order_status}」に更新しました"
    error_msg = fields.get("message", res.text[:200])
    return False, f"{order_id}: ステータス更新失敗（status={status}）: {error_msg}"
