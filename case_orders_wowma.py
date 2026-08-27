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
