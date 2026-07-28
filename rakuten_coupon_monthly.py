"""
毎月更新している常設クーポン（LINE登録限定・レビュー投稿）を、翌月分として自動発行する。

運用ルール（ヒアリング内容）:
  - 期間は「翌月1日 00:00:00 〜 翌月末日 23:59:59」
  - 作成は毎月20日前後
  - ただし週末は避ける
  - さらに「イベント（セール）期間中」も避ける
      イベント中に取得URLを翌月分へ貼り替えると、その場で使えず利用機会を逃すため。
      実際の貼り替えは人が行うので、このスクリプトは「貼り替えてよい日」にだけ発行する。

イベント判定:
  セール時には【先着◯名】などのクーポンを毎回発行しているため、
  それらの有効期間が今日に重なっていれば「イベント中」とみなす。
  取り漏らしに備えて、スプレッドシートに手動でイベント期間を書けるようにしてあり、
  そちらがあれば優先する。

このスクリプトは発行のみを行う。LINEや商品ページへの取得URLの貼り替えは人の作業。
"""

import os
import sys
import json
import calendar
from datetime import datetime, timezone, timedelta

import requests
import gspread
from google.oauth2.service_account import Credentials

from rakuten_coupon_api import auth_headers, search_all, get_coupon, build_issue_xml, issue_coupon

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
# 手動実行で日付の判定を飛ばしたいとき用（週末・イベント・20日前後の判定を無視する）
FORCE = os.environ.get("FORCE", "false").lower() == "true"

SERVICE_SECRET = os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"]
LICENSE_KEY = os.environ["RAKUTEN_RMS_LICENSE_KEY_1"]

GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

# 発行結果の通知先。ルームidは設定タブから読む（トークンだけSecrets）
CW_TOKEN = os.environ.get("CW_TOKEN", "")

CONFIG_SHEET = "クーポン_月次設定"
LOG_SHEET = "クーポン_発行ログ"
LOG_HEADER = ["実行日時(JST)", "クーポン名", "対象期間", "結果", "新クーポンコード", "取得URL"]

# 設定タブの既定値。タブが無い場合はこの内容で作成する。
# 依頼メッセージは、発行後のChatwork通知の末尾にそのまま入る。
DEFAULT_CONFIG = [
    ["対象クーポン名（この名前の最新のものをコピー元にします）", "更新後の依頼メッセージ"],
    ["アメリカーナLINE登録限定1,000円OFFクーポン", "LINEの方の更新をお願いします。"],
    ["ショップまたは商品レビュー投稿で1,000円クーポン", "メッセージテンプレートの更新をお願いします。"],
    [""],
    ["Chatworkルームid", "60101971"],
    [""],
    ["イベント期間（手動指定。セールクーポンから自動判定もします）"],
    ["開始日(YYYY-MM-DD)", "終了日(YYYY-MM-DD)", "メモ"],
]

# イベント（セール）中かどうかを判定するためのクーポン名の目印。
# セール時に必ず発行している【先着◯名】【先着順】系だけを見る。
# 「限定」は常設クーポン名（アメリカーナLINE登録限定1,000円OFFクーポン）にも
# 含まれてしまい、常にイベント中と誤判定するため使わない。
EVENT_NAME_MARKERS = ["先着"]

CREATE_FROM_DAY = 20  # 何日以降に作成するか


# ══ スプレッドシート ══════════════════════════════
def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def parse_config(rows: list):
    """
    設定タブの内容を読み解く。
    戻り値は (対象クーポン [(名前, 依頼メッセージ)], イベント期間 [(開始, 終了)], Chatworkルームid)
    """
    targets, events, room_id = [], [], ""
    section = None

    for row in rows:
        first = row[0].strip() if row else ""
        second = row[1].strip() if len(row) > 1 else ""

        if not first:
            continue
        if first.startswith("対象クーポン名"):
            section = "targets"
            continue
        if first.startswith("Chatworkルームid"):
            room_id = second
            section = None
            continue
        if first.startswith("イベント期間"):
            section = "events"
            continue
        if first.startswith("開始日"):
            continue

        if section == "targets":
            targets.append((first, second))
        elif section == "events" and second:
            events.append((first, second))

    return targets, events, room_id


def load_config(spreadsheet):
    try:
        ws = spreadsheet.worksheet(CONFIG_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        print(f"「{CONFIG_SHEET}」タブが無いため、既定の内容で作成します。")
        ws = spreadsheet.add_worksheet(title=CONFIG_SHEET, rows=100, cols=3)
        ws.update(range_name="A1", values=DEFAULT_CONFIG)
        return parse_config(DEFAULT_CONFIG)

    return parse_config(ws.get_all_values())


def append_log(spreadsheet, rows: list):
    if not rows:
        return
    try:
        ws = spreadsheet.worksheet(LOG_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_SHEET, rows=1000, cols=len(LOG_HEADER))
        ws.append_row(LOG_HEADER)
    ws.append_rows(rows, value_input_option="USER_ENTERED")


# ══ Chatwork 通知 ═════════════════════════════════
def format_period(start: str, end: str) -> str:
    """2026-08-01T00:00:00+09:00 → 2026/08/01 00:00 の形にする"""
    def fmt(v):
        dt = parse_rms_datetime(v)
        return dt.strftime("%Y/%m/%d %H:%M") if dt else v
    return f"{fmt(start)} 〜 {fmt(end)}"


def post_chatwork(room_id: str, body: str):
    if not room_id:
        print("  Chatworkルームidが未設定のため通知しません。")
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


def build_success_message(name, period_label, start, end, code, url, request_message) -> str:
    lines = [
        f"[info][title]楽天クーポン更新（{period_label}）[/title]",
        name,
        f"期間: {format_period(start, end)}",
        f"クーポンコード: {code}",
        f"取得URL: {url}",
    ]
    if request_message:
        lines += ["", request_message]
    lines.append("[/info]")
    return "\n".join(lines)


# ══ 日付まわり ════════════════════════════════════
def next_month_period(today: datetime):
    """翌月の 1日00:00:00 〜 末日23:59:59 を返す（RMSの日時形式）"""
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    last_day = calendar.monthrange(year, month)[1]
    start = f"{year:04d}-{month:02d}-01T00:00:00+09:00"
    end = f"{year:04d}-{month:02d}-{last_day:02d}T23:59:59+09:00"
    return start, end, f"{year}年{month}月"


def parse_rms_datetime(value: str):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def is_event_day(today: datetime, coupons: list, manual_events: list, target_names: list) -> tuple:
    """今日がイベント期間中かを判定する。(判定, 理由) を返す。"""
    date_str = today.strftime("%Y-%m-%d")
    for start, end in manual_events:
        if start <= date_str <= end:
            return True, f"手動指定のイベント期間（{start}〜{end}）"

    for c in coupons:
        name = c.get("couponName", "")
        # 更新対象の常設クーポン自体をイベント判定に使わない（名前が紛らわしい場合の保険）
        if name in target_names:
            continue
        if not any(marker in name for marker in EVENT_NAME_MARKERS):
            continue
        start = parse_rms_datetime(c.get("couponStartDate", ""))
        end = parse_rms_datetime(c.get("couponEndDate", ""))
        if start and end and start <= today <= end:
            return True, f"セールクーポン「{name}」が期間中（{c.get('couponStartDate')}〜{c.get('couponEndDate')}）"

    return False, ""


def check_today(today: datetime, coupons: list, manual_events: list, target_names: list) -> tuple:
    """今日が発行してよい日かを判定する。(発行してよいか, 理由) を返す。"""
    if today.day < CREATE_FROM_DAY:
        return False, f"まだ{CREATE_FROM_DAY}日前ではありません（今日は{today.day}日）"

    if today.weekday() >= 5:
        return False, f"週末です（{'土曜' if today.weekday() == 5 else '日曜'}）"

    is_event, reason = is_event_day(today, coupons, manual_events, target_names)
    if is_event:
        return False, f"イベント期間中です: {reason}"

    return True, "発行可能な日です"


# ══ メイン ════════════════════════════════════════
def main():
    print("=== 月次クーポン自動発行 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：実際には発行しません")

    today = datetime.now(JST)
    print(f"今日: {today.strftime('%Y-%m-%d (%a) %H:%M')}")

    spreadsheet = get_spreadsheet()
    targets, manual_events, room_id = load_config(spreadsheet)
    target_names = [name for name, _ in targets]
    print(f"対象クーポン: {target_names}")
    print(f"Chatwork通知先ルームid: {room_id or '（未設定）'}")

    headers = auth_headers(SERVICE_SECRET, LICENSE_KEY)
    try:
        coupons = search_all(headers)
    except Exception as e:
        print(f"クーポン一覧の取得に失敗しました: {e}")
        sys.exit(1)
    print(f"既存クーポン: {len(coupons)}件")

    ok, reason = check_today(today, coupons, manual_events, target_names)
    print(f"本日の判定: {reason}")
    if not ok and not FORCE:
        print("今日は発行しません。終了。")
        return
    if not ok and FORCE:
        print("※ FORCE指定のため、判定を無視して発行します。")

    new_start, new_end, period_label = next_month_period(today)
    print(f"作成する期間: {new_start} 〜 {new_end}（{period_label}）")

    log_rows = []
    now_label = today.strftime("%Y/%m/%d %H:%M")

    for name, request_message in targets:
        print(f"\n--- {name} ---")
        matches = [c for c in coupons if c.get("couponName") == name]
        if not matches:
            print("  同名のクーポンが見つかりません。名前が変わっていないか確認してください。")
            log_rows.append([now_label, name, period_label, "コピー元が見つかりません", "", ""])
            continue

        # すでに翌月分を作っていないか確認（二重発行の防止）
        already = [c for c in matches if c.get("couponStartDate", "").startswith(new_start[:7])]
        if already:
            print(f"  すでに{period_label}分が存在します（{already[0].get('couponCode')}）。スキップ。")
            log_rows.append([now_label, name, period_label, "既に作成済みのためスキップ",
                             already[0].get("couponCode", ""), ""])
            continue

        # 最新のものをコピー元にする
        source = max(matches, key=lambda c: c.get("couponStartDate", ""))
        print(f"  コピー元: {source.get('couponCode')}"
              f"（{source.get('couponStartDate')}〜{source.get('couponEndDate')}）")

        src, nested = get_coupon(headers, source["couponCode"])
        if src is None:
            print("  コピー元の詳細取得に失敗しました。")
            log_rows.append([now_label, name, period_label, "コピー元の取得に失敗", "", ""])
            continue

        xml = build_issue_xml(src, new_start, new_end, nested)

        if DRY_RUN:
            print("  【DRY RUN】以下の内容で発行します：")
            print(f"    枚数: {src.get('issueCount')} / 割引: {src.get('discountFactor')}"
                  f" / 条件: {nested['otherConditions']}")
            print("  【DRY RUN】Chatworkに送る予定のメッセージ：")
            print(build_success_message(name, period_label, new_start, new_end,
                                        "（発行後に決まります）", "（発行後に決まります）",
                                        request_message))
            log_rows.append([now_label, name, period_label, "【DRY RUN】発行対象", "", ""])
            continue

        success, message, code, url = issue_coupon(headers, xml)
        print(f"  → {message}")
        if success:
            print(f"    新クーポンコード: {code}")
            print(f"    取得URL: {url}")
            post_chatwork(room_id, build_success_message(
                name, period_label, new_start, new_end, code, url, request_message))
        else:
            # 失敗も知らせる。気づかないまま月をまたぐのを防ぐため
            post_chatwork(room_id, f"[info][title]楽天クーポン更新の失敗（{period_label}）[/title]"
                                   f"{name}\n{message}\n手動での対応をお願いします。[/info]")
        log_rows.append([now_label, name, period_label, message, code or "", url or ""])

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを「{LOG_SHEET}」タブに{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print("=== 月次クーポン自動発行 完了 ===")


if __name__ == "__main__":
    main()
