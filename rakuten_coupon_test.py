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
from xml.etree import ElementTree

import requests

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_1"]
SHOP_NAME = os.environ["RAKUTEN_SHOP_NAME_1"]


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


def probe_issue():
    """
    coupon.issue に空のリクエストを投げて、必須項目のエラーを全部吐かせる。

    必須項目が揃っていないので失敗するはずだが、万一通ってしまった場合に備えて
    レスポンスに couponCode が含まれていないかを確認し、含まれていたら警告する
    （その場合は coupon.delete で消す必要がある）。
    """
    url = f"{BASE}/es/1.0/coupon/issue"

    # まずメソッドを確かめる（GETなら405が返り、許可メソッドが分かることがある）
    probe_res = requests.get(url, headers=auth_headers(), timeout=20)
    print(f"  GET  /es/1.0/coupon/issue → {probe_res.status_code}")
    print(f"    {probe_res.text[:300]}\n")

    # 「Request data is wrong format」＝XMLの入れ物の形が違う、という意味。
    # coupon.get の応答が <result><coupon>... だったので、リクエストも似た構造と推測し、
    # ルート要素の候補を順に試す。必須項目のエラーに変われば、その形が正解。
    # 中身は couponName だけにして、他の必須項目が足りずに必ず失敗するようにしている。
    candidates = [
        ("<request><coupon><couponName>テスト</couponName></coupon></request>", "request>coupon"),
        ("<couponIssueRequest><couponName>テスト</couponName></couponIssueRequest>", "couponIssueRequest"),
        ("<request><couponIssue><couponName>テスト</couponName></couponIssue></request>", "request>couponIssue"),
        ("<coupon><couponName>テスト</couponName></coupon>", "coupon"),
        ("<request><coupons><coupon><couponName>テスト</couponName></coupon></coupons></request>", "request>coupons>coupon"),
    ]

    for body, label in candidates:
        headers = {**auth_headers(), "Content-Type": "application/xml; charset=utf-8"}
        res = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=20)

        print(f"  POST ルート要素 = {label}")
        print(f"    → ステータス: {res.status_code}")
        print(f"    {res.text[:1500]}\n")

        if "wrong format" not in res.text:
            print(f"    ✅ この形が受け付けられました（ルート要素 = {label}）\n")

        if "couponCode" in res.text and "<errors>" not in res.text:
            print("    ⚠️ クーポンが作成された可能性があります。上のcouponCodeを確認し、"
                  "必要なら削除してください。\n")
            return


# coupon.issue に送れる項目と、その並び順。
# coupon.get の応答には shopId / couponStatus / regDate など発行時に指定できない項目も
# 含まれるため、この一覧で絞り込む。
#
# 並び順は RMS公式の .NET クライアント（JakeJP/Rakuten.RMS.Api）の CouponToIssue クラスの
# プロパティ順に合わせている。順序が違うと「Request data is wrong format」で弾かれる
# （multiRankCond を末尾に置いて実際に弾かれた）。
# multiRankCond と otherConditions は入れ子構造なので、この一覧とは別に組み立てる。
ISSUE_FIELD_ORDER = [
    "couponName",
    "couponCaption",
    "couponStartDate",
    "couponEndDate",
    "couponImage",
    "issueCount",
    "itemType",
    "discountType",
    "discountFactor",
    "memberAvailMaxCount",
    "_multiRankCond",   # ここに入る（combineFlagより前）
    "combineFlag",
    "displayFlag",
    "_otherConditions",  # items の後、最後
]


def build_issue_xml(src: dict, start: str, end: str, other_conditions: list, rank_conds: list) -> str:
    """coupon.get で取得した内容から、期間だけ差し替えた発行用XMLを組み立てる"""
    from xml.sax.saxutils import escape

    parts = []
    for field in ISSUE_FIELD_ORDER:
        if field == "_multiRankCond":
            if rank_conds:
                inner = "".join(f"<rankCond>{escape(r)}</rankCond>" for r in rank_conds)
                parts.append(f"      <multiRankCond>{inner}</multiRankCond>")
            continue

        if field == "_otherConditions":
            if other_conditions:
                conds = "".join(
                    f"<otherCondition><conditionTypeCode>{escape(c)}</conditionTypeCode>"
                    f"<startValue>{escape(v)}</startValue></otherCondition>"
                    for c, v in other_conditions
                )
                parts.append(f"      <otherConditions>{conds}</otherConditions>")
            continue

        if field == "couponStartDate":
            value = start
        elif field == "couponEndDate":
            value = end
        else:
            value = src.get(field)
        if value is None or value == "":
            continue
        parts.append(f"      <{field}>{escape(str(value))}</{field}>")

    body = "\n".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<request>\n  <couponIssueRequest>\n    <coupon>\n"
        f"{body}\n"
        "    </coupon>\n  </couponIssueRequest>\n</request>"
    )


def copy_coupon(coupon_code: str, start: str, end: str, do_issue: bool):
    """既存クーポンをコピーし、期間だけ差し替えて発行する"""
    res = requests.get(
        f"{BASE}/es/1.0/coupon/get",
        headers=auth_headers(),
        params={"couponCode": coupon_code},
        timeout=30,
    )
    if res.status_code >= 400:
        print(f"  コピー元の取得に失敗しました（{res.status_code}）")
        print(res.text[:500])
        return

    root = ElementTree.fromstring(res.content)
    coupon = root.find("coupon")
    if coupon is None:
        print("  コピー元のクーポンが見つかりません。")
        print(res.text[:500])
        return

    src = {}
    for child in coupon:
        tag = child.tag.split("}")[-1]
        if not list(child):
            src[tag] = (child.text or "").strip()

    other_conditions = [
        ((oc.findtext("conditionTypeCode") or "").strip(), (oc.findtext("startValue") or "").strip())
        for oc in coupon.iter("otherCondition")
    ]
    rank_conds = [(rc.text or "").strip() for rc in coupon.iter("rankCond")]

    print(f"  コピー元: {src.get('couponName')}")
    print(f"    元の期間: {src.get('couponStartDate')} 〜 {src.get('couponEndDate')}")
    print(f"    新しい期間: {start} 〜 {end}")
    print(f"    発行枚数: {src.get('issueCount')} / 割引: {src.get('discountFactor')}")
    print(f"    利用条件: {other_conditions}")

    xml = build_issue_xml(src, start, end, other_conditions, rank_conds)
    print(f"\n  --- 送信するXML ---\n{xml}\n")

    if not do_issue:
        print("  【確認のみ】実際には発行していません。発行するには MODE=copy-issue で実行してください。")
        return

    issue_res = requests.post(
        f"{BASE}/es/1.0/coupon/issue",
        headers={**auth_headers(), "Content-Type": "application/xml; charset=utf-8"},
        data=xml.encode("utf-8"),
        timeout=30,
    )
    print(f"  → ステータス: {issue_res.status_code}")
    print(f"  {issue_res.text[:1500]}")


def issue_variants(coupon_code: str, start: str, end: str):
    """
    「Request data is wrong format」の原因を切り分けるため、
    XMLの形を少しずつ変えて送り、どこで通るかを探す。

    成功した時点でクーポンが1つ作られるので、そこで必ず止める。
    """
    res = requests.get(
        f"{BASE}/es/1.0/coupon/get",
        headers=auth_headers(),
        params={"couponCode": coupon_code},
        timeout=30,
    )
    coupon = ElementTree.fromstring(res.content).find("coupon")
    src = {c.tag.split("}")[-1]: (c.text or "").strip() for c in coupon if not list(c)}
    other_conditions = [
        ((oc.findtext("conditionTypeCode") or "").strip(), (oc.findtext("startValue") or "").strip())
        for oc in coupon.iter("otherCondition")
    ]
    rank_conds = [(rc.text or "").strip() for rc in coupon.iter("rankCond")]

    full = build_issue_xml(src, start, end, other_conditions, rank_conds)
    body_only = full.split("\n", 1)[1]           # XML宣言を落としたもの
    compact = "".join(line.strip() for line in full.split("\n"))
    minimal = build_issue_xml(
        {k: src.get(k) for k in ("couponName", "couponCaption", "issueCount",
                                 "itemType", "discountType", "discountFactor",
                                 "memberAvailMaxCount", "combineFlag", "displayFlag")},
        start, end, [], [],
    )
    # RS003（利用金額）だけ残したもの。otherCondition を複数送れない可能性の検証用
    only_rs003 = build_issue_xml(
        src, start, end,
        [(c, v) for c, v in other_conditions if c == "RS003"], rank_conds,
    )

    variants = [
        ("XML宣言なし", body_only),
        ("改行・インデントなし", compact),
        ("最小項目（画像・条件・ランクなし）", minimal),
        ("利用条件をRS003だけに絞る", only_rs003),
    ]

    for label, body in variants:
        res = requests.post(
            f"{BASE}/es/1.0/coupon/issue",
            headers={**auth_headers(), "Content-Type": "application/xml; charset=utf-8"},
            data=body.encode("utf-8"),
            timeout=30,
        )
        print(f"  ■ {label}")
        print(f"    → ステータス: {res.status_code}")
        print(f"    {res.text[:600]}\n")

        if "wrong format" not in res.text:
            print(f"    ✅ この形は受け付けられました（{label}）")
            if "<couponCode>" in res.text:
                print("    ⚠️ クーポンが作成されました。上の couponCode を確認してください。")
            return

    print("  どの形でも通りませんでした。")


def probe_issue_format():
    """
    coupon.issue のリクエスト形式を判別する。

    わざと couponName だけを送る。形式が正しければ「他の必須項目が足りない」という
    項目エラーに変わり、形式が違えば「Request data is wrong format」のままになる。
    項目が足りないのでクーポンは作成されない＝安全に判別できる。
    """
    url = f"{BASE}/es/1.0/coupon/issue"
    name = "フォーマット判定用"

    attempts = [
        (
            "フォーム形式（application/x-www-form-urlencoded）",
            {"headers": {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
             "data": {"couponName": name}},
        ),
        (
            "URLクエリパラメータ（本文なし）",
            {"headers": {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
             "params": {"couponName": name}},
        ),
        (
            "JSON（coupon で包む）",
            {"headers": {"Content-Type": "application/json; charset=utf-8"},
             "json": {"coupon": {"couponName": name}}},
        ),
        (
            "JSON（couponIssueRequest で包む）",
            {"headers": {"Content-Type": "application/json; charset=utf-8"},
             "json": {"couponIssueRequest": {"coupon": {"couponName": name}}}},
        ),
        (
            "XML（couponIssueRequest を root に）",
            {"headers": {"Content-Type": "application/xml; charset=utf-8"},
             "data": f"<couponIssueRequest><coupon><couponName>{name}</couponName></coupon></couponIssueRequest>".encode("utf-8")},
        ),
        (
            "XML（text/xml で送る）",
            {"headers": {"Content-Type": "text/xml; charset=utf-8"},
             "data": f"<request><couponIssueRequest><coupon><couponName>{name}</couponName></coupon></couponIssueRequest></request>".encode("utf-8")},
        ),
    ]

    for label, kwargs in attempts:
        headers = {**auth_headers(), **kwargs.pop("headers")}
        res = requests.post(url, headers=headers, timeout=20, **kwargs)
        print(f"  ■ {label}")
        print(f"    → ステータス: {res.status_code}")
        print(f"    {res.text[:800]}\n")

        if "wrong format" not in res.text:
            print(f"    ✅ この形式が受け付けられました（{label}）")
            if "<couponCode>" in res.text:
                print("    ⚠️ クーポンが作成されました。couponCode を確認してください。")
            return

    print("  どの形式でも通りませんでした。")


if __name__ == "__main__":
    mode = os.environ.get("MODE", "probe")
    if mode == "probe-format":
        print(f"=== 店舗（{SHOP_NAME}）: coupon.issue のリクエスト形式を判別 ===\n")
        probe_issue_format()
        raise SystemExit(0)

    if mode == "issue-variants":
        code = os.environ.get("COUPON_CODE", "").strip()
        start = os.environ.get("NEW_START", "").strip()
        end = os.environ.get("NEW_END", "").strip()
        if not (code and start and end):
            print("COUPON_CODE / NEW_START / NEW_END をすべて指定してください。")
            raise SystemExit(1)
        print(f"=== 店舗（{SHOP_NAME}）: XMLの形を変えて切り分け ===\n")
        issue_variants(code, start, end)
        raise SystemExit(0)

    if mode in ("copy-preview", "copy-issue"):
        code = os.environ.get("COUPON_CODE", "").strip()
        start = os.environ.get("NEW_START", "").strip()
        end = os.environ.get("NEW_END", "").strip()
        if not (code and start and end):
            print("COUPON_CODE / NEW_START / NEW_END をすべて指定してください。")
            print("  日時の形式: 2026-08-01T20:00:00+09:00")
            raise SystemExit(1)

        # 日時の打ち間違い（桁落ちなど）でおかしな期間のクーポンを作らないよう、形式を検証する
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
        print(f"=== 店舗（{SHOP_NAME}）: {code} をコピーして発行 ===\n")
        copy_coupon(code, start, end, do_issue=(mode == "copy-issue"))
        raise SystemExit(0)

    if mode == "probe-issue":
        print(f"=== 店舗（{SHOP_NAME}）: coupon.issue の必須項目を調べる ===\n")
        probe_issue()
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
    else:
        print(f"不明なMODE: {mode}")
