"""
自社製品レビュークーポンを発行したあと、「レビュークーポン送付テンプレート集」
（Googleドキュメント）の該当クーポンのコード・クーポンURL・有効期間を新しい内容に書き換える
（2026-10-22、ユーザー依頼）。rakuten_coupon_review_batch.py から呼ばれる。

ドキュメントの構成（12クーポン分、同じ並びの繰り返し）:
    クーポンURL / （URL） / コード / （コード） / 有効期限 / （YYYY/MM/DD HH:MM〜YYYY/MM/DD HH:MM）
    ほかに、冒頭の早見表（表）にも同じコードが入っている。
古いコードをキーに該当の節を見つけ、早見表のコードも含めて書き換える。見つからない・並びが
想定と違うクーポンは、1箇所も書き換えずに「失敗」として返す（部分的に壊さないため）。
書き換えは文字の差し替えのあと、元の文字の見た目（フォント・色・リンク）を引き継ぐ。
URLはリンクの宛先も新しいURLにする。
"""

import copy
import json
import os
import re
from datetime import datetime

import requests
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

DEFAULT_DOC_ID = "1FNBdFH6iZait9tZUOjpze1x-EUcx72OV3N9lpPpSUtQ"
DOCS_API = "https://docs.googleapis.com/v1/documents"

LABEL_URL = "クーポンURL"
LABEL_CODE = "コード"
LABEL_PERIOD = "有効期限"
URL_PREFIX = "https://coupon.rakuten.co.jp/getCoupon"
PERIOD_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}〜\d{4}/\d{2}/\d{2} \d{2}:\d{2}$")


def doc_id() -> str:
    # テスト用にコピーしたドキュメントを指定できる（通常は設定しない）
    return os.environ.get("REVIEW_COUPON_DOC_ID") or DEFAULT_DOC_ID


def _token() -> str:
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/documents"]
    )
    creds.refresh(Request())
    return creds.token


def format_period(start: str, end: str) -> str:
    """RMSの日時（2026-12-01T00:00:00+09:00）→ ドキュメントの表記（2026/12/01 00:00〜2027/01/31 23:59）"""
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    return f"{s.strftime('%Y/%m/%d %H:%M')}〜{e.strftime('%Y/%m/%d %H:%M')}"


def flatten_paragraphs(doc: dict) -> list:
    """ドキュメント本文の段落を、出現順に [{start, end, text, style, in_table}] で返す。"""
    out = []

    def walk(elements, in_table):
        for el in elements:
            if "paragraph" in el:
                runs = el["paragraph"].get("elements", [])
                text = "".join(r.get("textRun", {}).get("content", "") for r in runs)
                style = {}
                for r in runs:
                    if "textRun" in r and r["textRun"].get("content", "").strip("\n"):
                        style = copy.deepcopy(r["textRun"].get("textStyle", {}))
                        break
                out.append({
                    "start": el["startIndex"],
                    "end": el["endIndex"],
                    "text": text.rstrip("\n"),
                    "style": style,
                    "in_table": in_table,
                })
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []), True)

    walk(doc.get("body", {}).get("content", []), False)
    return out


def _prev_nonempty(paras: list, i: int):
    for j in range(i - 1, -1, -1):
        if paras[j]["text"].strip():
            return j
    return None


def _next_nonempty(paras: list, i: int):
    for j in range(i + 1, len(paras)):
        if paras[j]["text"].strip():
            return j
    return None


def plan_edits(paras: list, updates: list):
    """戻り値は (edits, problems)。editsは [{para, new_text, style}]、problemsは失敗理由の文字列リスト。
    1つのクーポンでも構成が想定と違えば、そのクーポンの編集は1つも出さない。"""
    edits = []
    problems = []
    used = set()

    for u in updates:
        name, old_code = u["name"], u["old_code"]
        occurrences = [i for i, p in enumerate(paras) if p["text"].strip() == old_code]
        sections = []
        for i in occurrences:
            if paras[i]["in_table"]:
                continue
            prev = _prev_nonempty(paras, i)
            if prev is not None and paras[prev]["text"].strip() == LABEL_CODE:
                sections.append((i, prev))
        if len(sections) != 1:
            problems.append(f"{name}: ドキュメントに現在のコードの節が{len(sections)}か所あります（1か所のはず）")
            continue
        code_i, code_label_i = sections[0]

        url_i = _prev_nonempty(paras, code_label_i)
        url_label_i = _prev_nonempty(paras, url_i) if url_i is not None else None
        if (url_i is None or url_label_i is None
                or not paras[url_i]["text"].strip().startswith(URL_PREFIX)
                or paras[url_label_i]["text"].strip() != LABEL_URL):
            problems.append(f"{name}: クーポンURLの並びが想定と違います")
            continue

        period_label_i = _next_nonempty(paras, code_i)
        period_i = _next_nonempty(paras, period_label_i) if period_label_i is not None else None
        if (period_label_i is None or period_i is None
                or paras[period_label_i]["text"].strip() != LABEL_PERIOD
                or not PERIOD_RE.match(paras[period_i]["text"].strip())):
            problems.append(f"{name}: 有効期限の並びが想定と違います")
            continue

        new_period = format_period(u["start"], u["end"])
        url_style = copy.deepcopy(paras[url_i]["style"])
        if "link" in url_style:
            url_style["link"] = {"url": u["new_url"]}

        targets = [(i, u["new_code"], None) for i in occurrences]
        targets.append((url_i, u["new_url"], url_style))
        targets.append((period_i, new_period, None))

        if any(i in used for i, _, _ in targets):
            problems.append(f"{name}: 同じ箇所が別のクーポンの更新対象と重なっています")
            continue
        for i, new_text, style in targets:
            used.add(i)
            edits.append({"para": paras[i], "new_text": new_text,
                          "style": style if style is not None else paras[i]["style"]})
    return edits, problems


def build_requests(edits: list) -> list:
    """後ろの位置から順に編集すると、前の位置がずれないため、開始位置の降順で並べる。"""
    requests_list = []
    for e in sorted(edits, key=lambda x: x["para"]["start"], reverse=True):
        start = e["para"]["start"]
        end = e["para"]["end"] - 1  # 段落末尾の改行は残す
        new_text = e["new_text"]
        requests_list.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}})
        requests_list.append({"insertText": {"location": {"index": start}, "text": new_text}})
        requests_list.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": start + len(new_text)},
                "textStyle": e["style"],
                "fields": "*",
            }
        })
    return requests_list


def update_review_coupon_doc(updates: list, dry_run: bool = False) -> dict:
    """updates: [{name, old_code, new_code, new_url, start, end}]。
    戻り値: {"updated": 件数, "problems": [失敗理由...], "edits": 編集箇所数}"""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.get(f"{DOCS_API}/{doc_id()}", headers=headers, timeout=30)
    res.raise_for_status()
    doc = res.json()

    paras = flatten_paragraphs(doc)
    edits, problems = plan_edits(paras, updates)
    ok_names = len(updates) - len(problems)

    if edits and not dry_run:
        body = {
            "requests": build_requests(edits),
            # 読み取ったあとに誰かが編集していたら、位置がずれるので書き換えずに失敗させる
            "writeControl": {"requiredRevisionId": doc["revisionId"]},
        }
        res = requests.post(f"{DOCS_API}/{doc_id()}:batchUpdate", headers=headers, json=body, timeout=60)
        if res.status_code != 200:
            raise RuntimeError(f"ドキュメントの書き換えに失敗（status={res.status_code}）: {res.text[:300]}")
    return {"updated": ok_names if edits else 0, "problems": problems, "edits": len(edits)}
