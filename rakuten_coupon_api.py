"""
楽天RMS クーポンAPIの共通処理。

エンドポイントとリクエスト形式は実機とRMS公式ドキュメントで確認済み。
  GET  /es/1.0/coupon/search   一覧（hits/page でページング）
  GET  /es/1.0/coupon/get      1件取得（couponCode）
  POST /es/1.0/coupon/issue    発行（XML、Content-Type: application/xml; charset=utf-8）

発行XMLの構造:
  <request><couponIssueRequest><coupon> … </coupon></couponIssueRequest></request>

注意点:
  - purchaseHistoryCond / genderCond / ageRangeCond / birthmonthCond /
    multiPrefectureCond は「指定なし」でも要素自体が必須。省略すると
    「Request data is wrong format」で弾かれる。
  - 要素の順序は公式ドキュメントの XML:coupon の定義順に合わせる必要がある。
  - couponStartDate は「現在の60分後〜30日後」の範囲でなければならない。
"""

import base64
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import requests

BASE = "https://api.rms.rakuten.co.jp/es/1.0/coupon"

# 発行XMLの要素と並び順。アンダースコア付きは入れ子構造なので個別に組み立てる。
ISSUE_FIELD_ORDER = [
    "couponName",
    "couponCaption",
    "couponStartDate",
    "couponEndDate",
    "couponImage",
    "issueCount",
    "itemType",
    "_items",
    "discountType",
    "discountFactor",
    "memberAvailMaxCount",
    "_purchaseHistoryCond",
    "_multiRankCond",
    "genderCond",
    "_ageRangeCond",
    "birthmonthCond",
    "_multiPrefectureCond",
    "combineFlag",
    "displayFlag",
    "_otherConditions",
]


def auth_headers(service_secret: str, license_key: str) -> dict:
    token = base64.b64encode(f"{service_secret}:{license_key}".encode()).decode()
    return {"Authorization": f"ESA {token}"}


def search_all(headers: dict, hits: int = 100, max_pages: int = 20) -> list:
    """全クーポンを取得する。各要素は {タグ名: 値} の辞書（入れ子は除く）。"""
    coupons = []
    for page in range(1, max_pages + 1):
        res = requests.get(
            f"{BASE}/search", headers=headers,
            params={"hits": hits, "page": page}, timeout=30,
        )
        res.raise_for_status()
        root = ElementTree.fromstring(res.content)

        page_coupons = list(root.iter("coupon"))
        if not page_coupons:
            break

        for c in page_coupons:
            coupons.append({
                ch.tag.split("}")[-1]: (ch.text or "").strip()
                for ch in c if not list(ch)
            })

        all_count = int(root.findtext("allCount") or 0)
        if len(coupons) >= all_count:
            break

    return coupons


def get_coupon(headers: dict, coupon_code: str):
    """1件取得。単純な項目は src、入れ子の条件は nested に分けて返す。"""
    res = requests.get(
        f"{BASE}/get", headers=headers,
        params={"couponCode": coupon_code}, timeout=30,
    )
    res.raise_for_status()
    coupon = ElementTree.fromstring(res.content).find("coupon")
    if coupon is None:
        return None, None

    src = {
        ch.tag.split("}")[-1]: (ch.text or "").strip()
        for ch in coupon if not list(ch)
    }
    age = coupon.find("ageRangeCond")
    nested = {
        "otherConditions": [
            ((oc.findtext("conditionTypeCode") or "").strip(),
             (oc.findtext("startValue") or "").strip())
            for oc in coupon.iter("otherCondition")
        ],
        "rankConds": [(rc.text or "").strip() for rc in coupon.iter("rankCond")],
        "prefectures": [(p.text or "").strip() for p in coupon.iter("prefectureCond")],
        "purchaseHistoryType": (coupon.findtext("purchaseHistoryCond/type") or "0").strip(),
        "ageLower": (age.findtext("lowerBound") if age is not None else "0") or "0",
        "ageUpper": (age.findtext("upperBound") if age is not None else "0") or "0",
        # itemType=1（対象商品限定）のクーポンは対象商品の一覧が必須。
        # coupon.get の itemUrl は商品管理番号なのでそのまま使う。
        "itemUrls": [
            (it.findtext("itemUrl") or "").strip()
            for it in coupon.iter("item")
            if (it.findtext("itemUrl") or "").strip()
        ],
    }
    return src, nested


def build_issue_xml(src: dict, start: str, end: str, nested: dict) -> str:
    """既存クーポンの内容から、期間だけ差し替えた発行用XMLを組み立てる。"""
    def esc(v):
        return escape(str(v))

    parts = []
    for field in ISSUE_FIELD_ORDER:
        if field == "_items":
            # itemType=1（対象商品限定）のときだけ必要。対象商品が拾えていれば送る
            item_urls = nested.get("itemUrls") or []
            if item_urls:
                inner = "".join(f"<item><itemUrl>{esc(u)}</itemUrl></item>" for u in item_urls)
                parts.append(f"      <items>{inner}</items>")
        elif field == "_purchaseHistoryCond":
            parts.append(
                f"      <purchaseHistoryCond><type>"
                f"{esc(nested.get('purchaseHistoryType', '0'))}</type></purchaseHistoryCond>"
            )
        elif field == "_multiRankCond":
            inner = "".join(f"<rankCond>{esc(r)}</rankCond>"
                            for r in (nested.get("rankConds") or ["0"]))
            parts.append(f"      <multiRankCond>{inner}</multiRankCond>")
        elif field == "_ageRangeCond":
            parts.append(
                f"      <ageRangeCond><lowerBound>{esc(nested.get('ageLower', '0'))}</lowerBound>"
                f"<upperBound>{esc(nested.get('ageUpper', '0'))}</upperBound></ageRangeCond>"
            )
        elif field == "_multiPrefectureCond":
            inner = "".join(f"<prefectureCond>{esc(p)}</prefectureCond>"
                            for p in (nested.get("prefectures") or ["NONE"]))
            parts.append(f"      <multiPrefectureCond>{inner}</multiPrefectureCond>")
        elif field == "_otherConditions":
            conds = nested.get("otherConditions") or []
            if conds:
                inner = "".join(
                    f"<otherCondition><conditionTypeCode>{esc(c)}</conditionTypeCode>"
                    f"<startValue>{esc(v)}</startValue></otherCondition>"
                    for c, v in conds
                )
                parts.append(f"      <otherConditions>{inner}</otherConditions>")
        else:
            if field == "couponStartDate":
                value = start
            elif field == "couponEndDate":
                value = end
            else:
                value = src.get(field)

            # 必須項目は元に無ければ「指定なし」で補う
            if not value and field == "genderCond":
                value = "NONE"
            if not value and field == "birthmonthCond":
                value = "0"

            if value:
                parts.append(f"      <{field}>{esc(value)}</{field}>")

    body = "\n".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<request>\n  <couponIssueRequest>\n    <coupon>\n"
        f"{body}\n"
        "    </coupon>\n  </couponIssueRequest>\n</request>"
    )


def issue_coupon(headers: dict, xml: str):
    """
    クーポンを発行する。戻り値は (成功したか, メッセージ, クーポンコード, 取得URL)。

    RMSは項目エラーでも systemStatus=OK を返し <errors> に内容を入れてくるため、
    HTTPステータスだけでなく <errors> の有無も確認する。
    """
    res = requests.post(
        f"{BASE}/issue",
        headers={**headers, "Content-Type": "application/xml; charset=utf-8"},
        data=xml.encode("utf-8"),
        timeout=30,
    )
    if res.status_code >= 400:
        return False, f"HTTP {res.status_code}: {res.text[:300]}", None, None

    root = ElementTree.fromstring(res.content)
    errors = [
        f"{e.findtext('code')}: {e.findtext('message')}"
        for e in root.iter("error")
    ]
    if errors:
        return False, " / ".join(errors), None, None

    coupon = root.find("coupon")
    if coupon is None:
        return False, f"応答にクーポン情報がありません: {res.text[:300]}", None, None

    return True, "発行成功", coupon.findtext("couponCode"), coupon.findtext("pcGetUrl")
