"""
ケース（Close(Temporary/Permanent)・Change Price）の概要欄（Description）に、AI自動更新の
対象であることを示す次の2行が入っているかを確認する共通部品（2026-10-16、ユーザー依頼）。

    === REMOVE this 2 lines if you want to prevent from AI (Jura) Auto Mode
    AI による自動更新から除外したいとき「のみ」この2行を削除してください ===

この2行が無いケースは、担当者が自動処理から除外したい意思表示とみなし、スクリプトでは
一切処理せず（ケースもモールも変更しない）、Chatworkに知らせる。
case_orders_auto_close.py と case_orders_price_adjust.py の両方から使う。
"""

import os
import re

import requests

AUTO_MODE_MARKER_LINES = (
    "REMOVE this 2 lines if you want to prevent from AI (Jura) Auto Mode",
    "AI による自動更新から除外したいとき「のみ」この2行を削除してください",
)

CW_TOKEN = os.environ.get("CW_TOKEN", "")
CW_ROOM_ID = "83994351"
CW_MENTION_RYO = "[To:2618849]Ryo Higuchiさん"

SKIP_LOG_SHEET_NAME = "自動処理_除外ケース通知済み"
SKIP_LOG_HEADER = ["初回通知日時(JST)", "ケースID", "スクリプト", "ケース種別", "商品", "理由"]

# 2026-10-16、ユーザー指示により、2行が無いままでも今回だけ処理するケース（処理後に外す）
ONE_TIME_OVERRIDE_CASE_IDS = {"159704", "159701", "159264", "150260"}

REASON_NO_MARKER = "概要欄に除外用の2行が無い"
REASON_UNREADABLE = "概要欄を読み取れなかった"


def _normalize(text: str) -> str:
    # 改行・半角/全角スペースの違いや大文字小文字の違いで判定がぶれないよう、空白を全て除く
    return re.sub(r"\s+", "", text or "").lower()


def get_case_description(page):
    """ケースのview画面（開いている前提）から、Description見出し以降〜次の見出しまでの
    テキストを返す。Description見出しが見つからなければ None。"""
    return page.evaluate(
        """() => {
            const h4 = [...document.querySelectorAll('h4')].find(h => h.textContent.trim() === 'Description');
            if (!h4) return null;
            const parts = [];
            let el = h4.nextElementSibling;
            while (el && !/^H[1-6]$/.test(el.tagName)) {
                parts.push(el.innerText || el.textContent || '');
                el = el.nextElementSibling;
            }
            return parts.join('\\n');
        }"""
    )


def has_auto_mode_marker(description) -> bool:
    """2行の両方が概要欄にあるときだけ True（片方だけ消された場合も除外の意思とみなす）。"""
    if not description:
        return False
    body = _normalize(description)
    return all(_normalize(line) in body for line in AUTO_MODE_MARKER_LINES)


def check_case(page, case_id: str):
    """戻り値は None（自動処理してよい）か、スキップ理由の文字列。"""
    if case_id in ONE_TIME_OVERRIDE_CASE_IDS:
        print("  ※ 今回だけ例外として処理します（概要欄の2行の確認を省略）")
        return None
    description = get_case_description(page)
    if description is None:
        return REASON_UNREADABLE
    if not has_auto_mode_marker(description):
        return REASON_NO_MARKER
    return None


# ── Chatwork通知（同じケースは1回だけ） ─────────────────────
def load_reported_skips(spreadsheet) -> set:
    try:
        ws = spreadsheet.worksheet(SKIP_LOG_SHEET_NAME)
    except Exception:
        return set()
    return {row[1] for row in ws.get_all_values()[1:] if len(row) >= 2}


def mark_skips_reported(spreadsheet, skipped: list, now: str, script_label: str):
    if not skipped:
        return
    try:
        ws = spreadsheet.worksheet(SKIP_LOG_SHEET_NAME)
    except Exception:
        ws = spreadsheet.add_worksheet(title=SKIP_LOG_SHEET_NAME, rows=1000, cols=len(SKIP_LOG_HEADER))
        ws.append_row(SKIP_LOG_HEADER)
    rows = [[now, s["case_id"], script_label, s["case_type"], s["product"], s["reason"]] for s in skipped]
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def notify_skipped_cases(spreadsheet, skipped: list, now: str, script_label: str, base_url: str, dry_run: bool):
    """スキップしたケースのうち、まだ通知していないものだけをChatworkへ1通にまとめて送る。
    スキップしたケースはNewのまま残り毎時再検出されるため、ケースID単位で通知済みを記録して
    繰り返し通知を防ぐ。"""
    if not skipped:
        return
    if dry_run:
        print("  【DRY RUN】除外ケースのChatwork通知は送りません。")
        return

    reported = load_reported_skips(spreadsheet)
    new_items = [s for s in skipped if s["case_id"] not in reported]
    if not new_items:
        print(f"  除外ケース{len(skipped)}件は通知済みのため、Chatworkへは再送しません。")
        return
    if not CW_TOKEN:
        print("  CW_TOKENが未設定のため除外ケースを通知できません（通知済みにも記録しません）。")
        return

    lines = [
        f"・ケース{s['case_id']}（{s['case_type']} / {s['product']}）: {s['reason']}\n"
        f"  {base_url}/case-orders/view/{s['case_id']}"
        for s in new_items
    ]
    body = (
        f"{CW_MENTION_RYO}\n"
        f"[info][title]AI自動処理の対象外ケースをスキップしました（{len(new_items)}件）[/title]"
        + "\n".join(lines)
        + "\n\n概要欄に「=== REMOVE this 2 lines … ===」の2行が無いケースは自動処理しません"
          "（ケースもモールも変更していません）。自動処理してよい場合は、この2行を概要欄に入れてください。"
          f"（{script_label}）[/info]"
    )
    try:
        res = requests.post(
            f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/messages",
            headers={"X-ChatWorkToken": CW_TOKEN},
            data={"body": body},
            timeout=30,
        )
        print(f"  Chatwork通知（除外ケース{len(new_items)}件）: status={res.status_code}")
        if res.status_code >= 400:
            print(f"    {res.text[:300]}")
            return
    except Exception as e:
        print(f"  Chatwork通知に失敗しました: {e}")
        return

    try:
        mark_skips_reported(spreadsheet, new_items, now, script_label)
    except Exception as e:
        print(f"  通知済み記録の保存に失敗しました: {e}")
