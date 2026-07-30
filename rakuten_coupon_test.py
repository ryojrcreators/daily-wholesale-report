"""
楽天RMS クーポンAPIの調査用スクリプト（読み取りのみ）

目的:
  1. 今のライセンスキーでクーポンAPIが使えるか（利用申請が通っているか）を確かめる
  2. 正しいエンドポイントURLを特定する
  3. 既存クーポンの項目を確認し、「コピーして期限だけ変えて発行」に必要な項目を洗い出す

商品APIのときと同じく、公式ドキュメントがRMSログインの内側にあるため、
候補URLを順に叩いてステータスコードから正解を探る。
  404 = URLが存在しない / 403 = URLはあるが権限なし（＝利用申請が必要）
  400 = URLもメソッドも合っているがパラメータが違う / 200 = 成功

このスクリプトは search と get しか呼ばないので、クーポンは作成・変更されない。
"""

import os
import base64
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree

import requests

from rakuten_coupon_api import auth_headers as _shared_auth_headers, search_all

JST = timezone(timedelta(hours=9))

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_1"]
SHOP_NAME = os.environ["RAKUTEN_SHOP_NAME_1"]

CW_TOKEN = os.environ.get("CW_TOKEN", "")


def post_chatwork(room_id: str, body: str):
    if not room_id:
        print("  Chatworkルームidが未指定のため通知しません。")
        return
    if not CW_TOKEN:
        print("  CW_TOKENが未設定のため通知しません。")
        return
    try:
        res = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/messages",
            headers={"X-ChatWorkToken": CW_TOKEN},
            data={"body": body},
            timeout=30,
        )
        print(f"  Chatwork通知: status={res.status_code}")
        if res.status_code >= 400:
            print(f"    {res.text[:300]}")
    except Exception as e:
        print(f"  Chatwork通知に失敗しました: {e}")


def build_batch_report(mention: str, title: str, intro: str, results: list) -> str:
    """バッチ発行結果からChatwork報告メッセージを組み立てる（成功分のみ列挙）。"""
    lines = [mention, f"[info][title]{title}[/title]", intro, ""]
    for r in results:
        if not r["success"]:
            continue
        lines.append(f"■ {r['name']}")
        lines.append(f"クーポンコード: {r['code']}")
        lines.append(f"取得URL: {r['url']}")
        lines.append("")

    failed = [r for r in results if not r["success"]]
    if failed:
        lines.append("---")
        lines.append("以下は発行に失敗しました。手動で確認してください：")
        for r in failed:
            lines.append(f"・{r['name']}: {r['message']}")
        lines.append("")

    lines.append("[/info]")
    return "\n".join(lines)


def auth_headers() -> dict:
    token = base64.b64encode(f"{SERVICE_SECRET}:{LICENSE_KEY}".encode()).decode()
    return {
        "Authorization": f"ESA {token}",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }


BASE = "https://api.rms.rakuten.co.jp"

# クーポン検索の候補URL。第三者ライブラリの命名（coupon.search）から推測したもの。
SEARCH_CANDIDATES = [
    ("GET",  f"{BASE}/es/2.0/coupon/search"),
    ("GET",  f"{BASE}/es/1.0/coupon/search"),
    ("POST", f"{BASE}/es/1.0/coupon/search"),
    ("GET",  f"{BASE}/es/2.0/coupons/search"),
    ("GET",  f"{BASE}/es/1.0/coupon/get"),
    ("GET",  f"{BASE}/es/2.0/coupon"),
    ("GET",  f"{BASE}/es/1.0/coupon"),
]


def probe():
    print(f"=== 店舗（{SHOP_NAME}）: クーポンAPIのエンドポイント調査 ===\n")
    hits = []

    for method, url in SEARCH_CANDIDATES:
        path = url.split("rms.rakuten.co.jp")[1]
        try:
            res = requests.request(method, url, headers=auth_headers(), timeout=20)
        except Exception as e:
            print(f"  {method:<5} {path}\n    → 通信エラー: {e}\n")
            continue

        print(f"  {method:<5} {path}")
        print(f"    → ステータス: {res.status_code}")

        if res.status_code == 404:
            print("       （URLが存在しない）\n")
            continue

        # 404以外はURLとして意味がある。中身を見せる
        print(f"    {res.text[:500]}\n")
        hits.append((method, url, res.status_code))

    print("── 調査結果 ──")
    if not hits:
        print("  すべて404でした。URLの形が違う可能性があります。")
        return

    for method, url, status in hits:
        path = url.split("rms.rakuten.co.jp")[1]
        if status == 403:
            meaning = "URLは存在するが権限なし → クーポンAPIの利用申請が必要"
        elif status == 400:
            meaning = "URLは正しい。パラメータが足りないだけ → 使える見込み"
        elif status == 200:
            meaning = "成功！このエンドポイントが使える"
        else:
            meaning = "要確認"
        print(f"  {method} {path} → {status}: {meaning}")


def list_coupons(hits: int):
    """クーポン一覧を取得して、コピー元の候補を探す（読み取りのみ）"""
    res = requests.get(
        f"{BASE}/es/1.0/coupon/search",
        headers=auth_headers(),
        params={"hits": hits, "page": 1},
        timeout=30,
    )
    print(f"  GET /es/1.0/coupon/search?hits={hits} → {res.status_code}\n")
    if res.status_code >= 400:
        print(res.text[:1000])
        return

    root = ElementTree.fromstring(res.content)
    print(f"  総件数: {root.findtext('allCount')}\n")

    for i, coupon in enumerate(root.iter("coupon"), start=1):
        code = coupon.findtext("couponCode") or ""
        name = coupon.findtext("couponName") or ""
        start = coupon.findtext("couponStartDate") or ""
        end = coupon.findtext("couponEndDate") or ""
        print(f"  {i:>3}. {code}")
        print(f"       {name[:60]}")
        print(f"       期間: {start} 〜 {end}")


def _is_active_now(coupon: dict) -> bool:
    def parse(v):
        try:
            return datetime.fromisoformat(v)
        except (ValueError, TypeError):
            return None

    start = parse(coupon.get("couponStartDate", ""))
    end = parse(coupon.get("couponEndDate", ""))
    if not start or not end:
        return False
    now = datetime.now(JST)
    return start <= now <= end


def search_coupons_by_keyword(keyword_groups: list, active_only: bool = False):
    """
    全クーポンをページングしながら取得し、クーポン名にキーワードを含むものだけ表示する（読み取りのみ）。
    keyword_groups は [["リポソーム型ビタミンC", "レビュー投稿で"], ["アルロース"]] のような形で、
    グループ内は全部含む（AND）、グループ同士は別々に集計する。
    active_only=True の場合、現在時刻が有効期間内のものだけに絞る。
    """
    coupons = search_all(_shared_auth_headers(SERVICE_SECRET, LICENSE_KEY))
    print(f"  既存クーポン総数: {len(coupons)}件")
    if active_only:
        print(f"  （現在時刻 {datetime.now(JST).strftime('%Y-%m-%d %H:%M')} 時点で有効期間中のものだけに絞ります）")
    print()

    for terms in keyword_groups:
        matches = [
            c for c in coupons
            if all(term in c.get("couponName", "") for term in terms)
        ]
        if active_only:
            matches = [c for c in matches if _is_active_now(c)]
        label = "」かつ「".join(terms)
        suffix = "（有効期間中のみ）" if active_only else ""
        print(f"「{label}」を含むクーポン{suffix}: {len(matches)}件")
        for c in matches:
            print(f"    {c.get('couponCode')}  {c.get('couponName')}")
            print(f"      期間: {c.get('couponStartDate')} 〜 {c.get('couponEndDate')}")
        print()


def show_coupon(coupon_code: str):
    """1件のクーポンの全項目を出力する（発行に必要な項目の洗い出し用）"""
    res = requests.get(
        f"{BASE}/es/1.0/coupon/get",
        headers=auth_headers(),
        params={"couponCode": coupon_code},
        timeout=30,
    )
    print(f"  GET /es/1.0/coupon/get?couponCode={coupon_code} → {res.status_code}\n")
    if res.status_code >= 400:
        print(res.text[:1000])
        return

    # 構造を保ったまま全項目を出す（入れ子があるとコピー時に効いてくるため）
    root = ElementTree.fromstring(res.content)

    def walk(elem, depth=0):
        tag = elem.tag.split("}")[-1]
        text = (elem.text or "").strip()
        children = list(elem)
        indent = "  " + "  " * depth
        if children:
            print(f"{indent}[{tag}]")
            for child in children:
                walk(child, depth + 1)
        else:
            print(f"{indent}{tag}: {text}")

    walk(root)


# coupon.issue に送る要素と、その並び順（RMS公式ドキュメントの XML:coupon の定義順）。
#
# 重要: purchaseHistoryCond / genderCond / ageRangeCond / birthmonthCond /
# multiPrefectureCond は「値が指定なし」でも要素自体が必須。1つでも欠けると
# 「Request data is wrong format」で弾かれる。
#
# coupon.get の応答には shopId / couponStatus / regDate / pcGetUrl など
# 発行時に指定できない項目も含まれるため、この一覧で絞り込む。
# アンダースコアで始まるものは入れ子構造なので個別に組み立てる。
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


def build_issue_xml(src: dict, start: str, end: str, nested: dict) -> str:
    """coupon.get で取得した内容から、期間だけ差し替えた発行用XMLを組み立てる"""
    from xml.sax.saxutils import escape

    def esc(v):
        return escape(str(v))

    parts = []
    for field in ISSUE_FIELD_ORDER:
        if field == "_purchaseHistoryCond":
            # 必須。値は「0=指定なし」でも要素は必ず送る
            parts.append(
                f"      <purchaseHistoryCond><type>"
                f"{esc(nested.get('purchaseHistoryType', '0'))}</type></purchaseHistoryCond>"
            )
            continue

        if field == "_items":
            # itemType=1（対象商品限定）のときだけ必要。念のためitemType=0でも
            # 対象商品が拾えていれば送る（未検証のため挙動を見て調整する）
            item_urls = nested.get("itemUrls") or []
            if item_urls:
                inner = "".join(f"<item><itemUrl>{esc(u)}</itemUrl></item>" for u in item_urls)
                parts.append(f"      <items>{inner}</items>")
            continue

        if field == "_multiRankCond":
            ranks = nested.get("rankConds") or ["0"]
            inner = "".join(f"<rankCond>{esc(r)}</rankCond>" for r in ranks)
            parts.append(f"      <multiRankCond>{inner}</multiRankCond>")
            continue

        if field == "_ageRangeCond":
            parts.append(
                f"      <ageRangeCond><lowerBound>{esc(nested.get('ageLower', '0'))}</lowerBound>"
                f"<upperBound>{esc(nested.get('ageUpper', '0'))}</upperBound></ageRangeCond>"
            )
            continue

        if field == "_multiPrefectureCond":
            prefs = nested.get("prefectures") or ["NONE"]
            inner = "".join(f"<prefectureCond>{esc(p)}</prefectureCond>" for p in prefs)
            parts.append(f"      <multiPrefectureCond>{inner}</multiPrefectureCond>")
            continue

        if field == "_otherConditions":
            conds = nested.get("otherConditions") or []
            if conds:
                inner = "".join(
                    f"<otherCondition><conditionTypeCode>{esc(c)}</conditionTypeCode>"
                    f"<startValue>{esc(v)}</startValue></otherCondition>"
                    for c, v in conds
                )
                parts.append(f"      <otherConditions>{inner}</otherConditions>")
            continue

        if field == "couponStartDate":
            value = start
        elif field == "couponEndDate":
            value = end
        else:
            value = src.get(field)

        # genderCond / birthmonthCond は必須なので、元に無ければ「指定なし」を補う
        if (value is None or value == "") and field == "genderCond":
            value = "NONE"
        if (value is None or value == "") and field == "birthmonthCond":
            value = "0"

        if value is None or value == "":
            continue
        parts.append(f"      <{field}>{esc(value)}</{field}>")

    body = "\n".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<request>\n  <couponIssueRequest>\n    <coupon>\n"
        f"{body}\n"
        "    </coupon>\n  </couponIssueRequest>\n</request>"
    )


def copy_coupon(coupon_code: str, start: str, end: str, do_issue: bool, no_image: bool = False, image_url: str = ""):
    """
    既存クーポンをコピーし、期間だけ差し替えて発行する。
    戻り値は {"name", "success", "code", "url", "message"} の辞書
    （Chatwork報告など、呼び出し側でまとめて使えるように）。
    """
    result = {"name": coupon_code, "success": False, "code": None, "url": None, "message": ""}

    res = requests.get(
        f"{BASE}/es/1.0/coupon/get",
        headers=auth_headers(),
        params={"couponCode": coupon_code},
        timeout=30,
    )
    if res.status_code >= 400:
        print(f"  コピー元の取得に失敗しました（{res.status_code}）")
        print(res.text[:500])
        result["message"] = f"コピー元の取得に失敗（{res.status_code}）"
        return result

    root = ElementTree.fromstring(res.content)
    coupon = root.find("coupon")
    if coupon is None:
        print("  コピー元のクーポンが見つかりません。")
        print(res.text[:500])
        result["message"] = "コピー元のクーポンが見つかりません"
        return result

    src = {}
    for child in coupon:
        tag = child.tag.split("}")[-1]
        if not list(child):
            src[tag] = (child.text or "").strip()

    # 入れ子の条件は、要素自体が必須なので取りこぼさないよう個別に拾う
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
    if nested["itemUrls"]:
        print(f"    対象商品: {nested['itemUrls']}")

    if image_url:
        print(f"    クーポン画像を差し替えます（元の値: {src.get('couponImage')} → {image_url}）")
        src["couponImage"] = image_url
    elif no_image and src.get("couponImage"):
        print(f"    クーポン画像を除外します（元の値: {src['couponImage']}）")
        src["couponImage"] = ""

    result["name"] = src.get("couponName") or coupon_code

    print(f"  コピー元: {src.get('couponName')}")
    print(f"    元の期間: {src.get('couponStartDate')} 〜 {src.get('couponEndDate')}")
    print(f"    新しい期間: {start} 〜 {end}")
    print(f"    発行枚数: {src.get('issueCount')} / 割引: {src.get('discountFactor')}")
    print(f"    利用条件: {nested['otherConditions']}")

    xml = build_issue_xml(src, start, end, nested)
    print(f"\n  --- 送信するXML ---\n{xml}\n")

    if not do_issue:
        print("  【確認のみ】実際には発行していません。発行するには MODE=copy-issue で実行してください。")
        result["message"] = "確認のみ（未発行）"
        return result

    issue_res = requests.post(
        f"{BASE}/es/1.0/coupon/issue",
        headers={**auth_headers(), "Content-Type": "application/xml; charset=utf-8"},
        data=xml.encode("utf-8"),
        timeout=30,
    )
    print(f"  → ステータス: {issue_res.status_code}")
    print(f"  {issue_res.text[:1500]}")

    if issue_res.status_code >= 400:
        result["message"] = f"HTTP {issue_res.status_code}"
        return result

    issue_root = ElementTree.fromstring(issue_res.content)
    errors = [
        f"{e.findtext('code')}: {e.findtext('message')}"
        for e in issue_root.iter("error")
    ]
    if errors:
        result["message"] = " / ".join(errors)
        return result

    issued = issue_root.find("coupon")
    if issued is None:
        result["message"] = "応答にクーポン情報がありません"
        return result

    result["success"] = True
    result["code"] = issued.findtext("couponCode")
    result["url"] = issued.findtext("pcGetUrl")
    result["message"] = "発行成功"
    return result


def validate_period(start: str, end: str):
    """日時の打ち間違い（桁落ちなど）でおかしな期間のクーポンを作らないよう、形式を検証する。"""
    import re
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$")
    for label, value in (("NEW_START", start), ("NEW_END", end)):
        if not date_pattern.match(value):
            print(f"{label} の形式が正しくありません: {value}")
            print("  正しい形式: 2026-08-01T20:00:00+09:00")
            raise SystemExit(1)
    if start >= end:
        print(f"開始日時が終了日時以降になっています: {start} 〜 {end}")
        raise SystemExit(1)


if __name__ == "__main__":
    mode = os.environ.get("MODE", "probe")
    if mode in ("copy-preview", "copy-issue"):
        code = os.environ.get("COUPON_CODE", "").strip()
        start = os.environ.get("NEW_START", "").strip()
        end = os.environ.get("NEW_END", "").strip()
        if not (code and start and end):
            print("COUPON_CODE / NEW_START / NEW_END をすべて指定してください。")
            print("  日時の形式: 2026-08-01T20:00:00+09:00")
            raise SystemExit(1)
        validate_period(start, end)

        no_image = os.environ.get("NO_IMAGE", "false").lower() == "true"
        image_url = os.environ.get("IMAGE_URL", "").strip()
        print(f"=== 店舗（{SHOP_NAME}）: {code} をコピーして発行 ===\n")
        copy_coupon(code, start, end, do_issue=(mode == "copy-issue"), no_image=no_image, image_url=image_url)
        raise SystemExit(0)

    if mode in ("batch-preview", "batch-issue"):
        raw_codes = os.environ.get("COUPON_CODES", "").strip()
        codes = [c.strip() for c in raw_codes.split(",") if c.strip()]
        start = os.environ.get("NEW_START", "").strip()
        end = os.environ.get("NEW_END", "").strip()
        if not (codes and start and end):
            print("COUPON_CODES（カンマ区切り）/ NEW_START / NEW_END をすべて指定してください。")
            print("  日時の形式: 2026-08-01T20:00:00+09:00")
            raise SystemExit(1)
        validate_period(start, end)

        no_image = os.environ.get("NO_IMAGE", "false").lower() == "true"
        image_url = os.environ.get("IMAGE_URL", "").strip()
        print(f"=== 店舗（{SHOP_NAME}）: {len(codes)}件を一括コピー発行 ===")
        print(f"    新しい期間: {start} 〜 {end}\n")

        results = []
        for i, code in enumerate(codes, start=1):
            print(f"--- [{i}/{len(codes)}] {code} ---")
            results.append(copy_coupon(code, start, end, do_issue=(mode == "batch-issue"),
                                        no_image=no_image, image_url=image_url))
            print()

        ok_count = sum(1 for r in results if r["success"])
        print(f"=== 完了: {ok_count}/{len(results)}件 発行成功 ===")

        if mode == "batch-issue":
            room_id = os.environ.get("CW_ROOM_ID", "").strip()
            mention = os.environ.get("CW_MENTION", "").strip()
            title = os.environ.get("CW_TITLE", "楽天クーポン更新").strip()
            intro = os.environ.get(
                "CW_INTRO", "下記自社製品レビュークーポンを更新しました。\nテンプレートの更新をお願いします。"
            ).strip().replace("\\n", "\n")
            if room_id and mention:
                body = build_batch_report(mention, title, intro, results)
                post_chatwork(room_id, body)
            else:
                print("  CW_ROOM_ID / CW_MENTION が未指定のため、Chatworkへは通知しません。")
        raise SystemExit(0)

    if mode == "probe":
        probe()
    elif mode == "list":
        print(f"=== 店舗（{SHOP_NAME}）: クーポン一覧 ===\n")
        list_coupons(int(os.environ.get("HITS", "20")))
    elif mode == "detail":
        code = os.environ.get("COUPON_CODE", "").strip()
        if not code:
            print("COUPON_CODE が指定されていません。")
        else:
            print(f"=== 店舗（{SHOP_NAME}）: クーポン {code} の全項目 ===\n")
            show_coupon(code)
    elif mode == "search":
        raw = os.environ.get("KEYWORDS", "").strip()
        # カンマ区切りでグループ（OR）、グループ内は + 区切りで全部含む（AND）
        keyword_groups = [
            [term.strip() for term in group.split("+") if term.strip()]
            for group in raw.split(",")
        ]
        keyword_groups = [g for g in keyword_groups if g]
        if not keyword_groups:
            print("KEYWORDS が指定されていません（例: リポソーム型ビタミンC+レビュー投稿で,アルロース+レビュー投稿で）。")
        else:
            active_only = os.environ.get("ACTIVE_ONLY", "false").lower() == "true"
            print(f"=== 店舗（{SHOP_NAME}）: クーポン名キーワード検索 ===\n")
            search_coupons_by_keyword(keyword_groups, active_only=active_only)
    else:
        print(f"不明なMODE: {mode}")
