"""
毎月更新している常設クーポン（LINE登録限定・レビュー投稿）を、翌月分として自動発行する。
楽天の複数店舗（STORES）に対応しており、店舗ごとに別のRMS認証情報・別のテンプレートIDを持てる。

運用ルール（ヒアリング内容）:
  - 期間は「翌月1日 00:00:00 〜 翌月末日 23:59:59」
  - 作成は毎月20日前後
  - ただし週末は避ける
  - さらに「イベント（セール）期間中」も避ける（店舗ごとに、その店舗のクーポン一覧で判定）
      イベント中に取得URLを翌月分へ貼り替えると、その場で使えず利用機会を逃すため。
      実際の貼り替えは人が行うので、このスクリプトは「貼り替えてよい日」にだけ発行する。

イベント判定:
  セール時には【先着◯名】などのクーポンを毎回発行しているため、
  それらの有効期間が今日に重なっていれば「イベント中」とみなす。
  取り漏らしに備えて、スプレッドシートに手動でイベント期間を書けるようにしてあり、
  そちらがあれば優先する。

このスクリプトは発行を行う。「ショップまたは商品レビュー投稿で1,000円クーポン」については、
発行成功後に社内システム（app.jrcreators.com）のメールテンプレート
（Americana: ID 259、Founder: ID 260、TEMPLATE_ID_BY_TARGETで対応付け）も自動更新する
（クーポンコード・期限・取得URLの3箇所）。LINE側の貼り替えは引き続き人の作業。
"""

import os
import re
import sys
import json
import calendar
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

from rakuten_coupon_api import auth_headers, search_all, get_coupon, build_issue_xml, issue_coupon

JST = timezone(timedelta(hours=9))

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
# 手動実行で日付の判定を飛ばしたいとき用（週末・イベント・20日前後の判定を無視する）
FORCE = os.environ.get("FORCE", "false").lower() == "true"

# 店舗キー → RMS認証情報。店舗を増やす場合はここに追加する。
STORES = {
    "americana": {
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_1"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_1"],
    },
    "founder": {
        "service_secret": os.environ["RAKUTEN_RMS_SERVICE_SECRET_2"],
        "license_key": os.environ["RAKUTEN_RMS_LICENSE_KEY_2"],
    },
}
DEFAULT_STORE = "americana"  # 設定タブに店舗列が無い/空の既存行はこの店舗扱いにする

# コピー元クーポンのcouponImageをそのまま引き継ぐと、画像が古くなっている場合に
# 「couponImage.not_available_url」エラーで発行が失敗することがある
# （2026-08-19、Founder店の「ショップまたは商品レビュー投稿で1,000円OFFクーポン」で実際に発生）。
# rakuten_coupon_review_batch.py と同じ対処として、店舗ごとの有効な画像URLに強制的に差し替える。
IMAGE_URL_BY_STORE = {
    "americana": "https://image.rakuten.co.jp/americana/cabinet/logo1.jpg",
    "founder": "https://image.rakuten.co.jp/founder/cabinet/logo.jpg",
}

GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

# 発行結果の通知先。ルームidは設定タブから読む（トークンだけSecrets）
CW_TOKEN = os.environ.get("CW_TOKEN", "")
# 通知の宛先。Chatworkの記法なのでそのまま送る
CW_MENTION = "[To:2158846]Yoko Matsusakaさん"
CW_ASSIGNEE_ID = "2158846"  # Yoko Matsusakaさん（[To:2158846]と同じアカウントID）
CW_TASK_DUE_DAYS = 7  # タスクの期限：発行日から何日後か

# (店舗キー, クーポン名) → 発行後に自動更新する社内システムのメールテンプレートID
# クーポン名は店舗をまたいで同じ場合があるため、店舗キーと組み合わせて一意にする。
TEMPLATE_ID_BY_TARGET = {
    ("americana", "ショップまたは商品レビュー投稿で1,000円クーポン"): "259",
    ("founder", "ショップまたは商品レビュー投稿で1,000円OFFクーポン"): "260",
}

APP_DOMAIN = os.environ.get("APP_DOMAIN", "")
LOGIN_ID_1 = os.environ.get("LOGIN_ID_1", "")
LOGIN_PASS_1 = os.environ.get("LOGIN_PASS_1", "")
LOGIN_ID_2 = os.environ.get("LOGIN_ID_2", "")
LOGIN_PASS_2 = os.environ.get("LOGIN_PASS_2", "")

CONFIG_SHEET = "クーポン_月次設定"
LOG_SHEET = "クーポン_発行ログ"
LOG_HEADER = ["実行日時(JST)", "クーポン名", "対象期間", "結果", "新クーポンコード", "取得URL"]

# 設定タブの既定値。タブが無い場合はこの内容で作成する。
# 依頼メッセージは、発行後のChatwork通知の末尾にそのまま入る。
# 3列目（店舗）が空の行は DEFAULT_STORE（americana）扱いにする。
DEFAULT_CONFIG = [
    ["対象クーポン名（この名前の最新のものをコピー元にします）", "更新後の依頼メッセージ", "店舗"],
    ["アメリカーナLINE登録限定1,000円OFFクーポン", "LINEとロボチンのテンプレ更新をお願いします。", "americana"],
    ["ショップまたは商品レビュー投稿で1,000円クーポン", "サーバーテンプレート自動更新済み。\nLINEの更新をお願いします。", "americana"],
    ["ショップまたは商品レビュー投稿で1,000円クーポン", "サーバーテンプレート自動更新済みです。", "founder"],
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
    戻り値は (対象クーポン [(店舗, 名前, 依頼メッセージ)], イベント期間 [(開始, 終了)], Chatworkルームid)

    店舗列（3列目）が無い/空の行は DEFAULT_STORE 扱いにする（後方互換）。
    """
    targets, events, room_id = [], [], ""
    section = None

    for row in rows:
        first = row[0].strip() if row else ""
        second = row[1].strip() if len(row) > 1 else ""
        third = row[2].strip() if len(row) > 2 else ""

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
            store = third or DEFAULT_STORE
            targets.append((store, first, second))
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


def post_chatwork_task(room_id: str, to_ids: str, body: str, due_days: int = None):
    """
    普通のメッセージではなく、Chatworkの「タスク」として作成する
    （担当者に割り当てられ、Chatwork上のタスク一覧にも表示される）。
    """
    if not room_id or not to_ids:
        print("  Chatworkルームid/担当者idが未指定のためタスクを作成しません。")
        return
    if not CW_TOKEN:
        print("  CW_TOKENが未設定のためタスクを作成しません。")
        return

    data = {"body": body, "to_ids": to_ids}
    if due_days is not None:
        # 時刻を含めたまま計算すると、limit_type=date側でのタイムゾーン解釈次第で
        # 日付が前後にずれることがあるため、対象日のJST 0時ちょうどに正規化する
        due = (datetime.now(JST) + timedelta(days=due_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        data["limit"] = int(due.timestamp())
        data["limit_type"] = "date"
        print(f"    タスク期限: {due.strftime('%Y-%m-%d')}（limit={data['limit']}）")

    try:
        res = requests.post(
            f"https://api.chatwork.com/v2/rooms/{room_id}/tasks",
            headers={"X-ChatWorkToken": CW_TOKEN},
            data=data,
            timeout=30,
        )
        print(f"  Chatworkタスク作成: status={res.status_code}")
        if res.status_code >= 400:
            print(f"    {res.text[:300]}")
    except Exception as e:
        print(f"  Chatworkタスク作成に失敗しました: {e}")


def build_success_message(name, period_label, start, end, code, url, request_message) -> str:
    lines = [
        CW_MENTION,
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


# ══ 社内システムのメールテンプレート更新 ═══════════
def format_template_date(value: str) -> str:
    """
    2026-08-01T00:00:00+09:00 → 2026/8/01 00:00 の形にする
    （テンプレート本文の既存表記に合わせる：月はゼロ埋めなし、日・時・分はゼロ埋めあり）
    """
    dt = parse_rms_datetime(value)
    if dt is None:
        return value
    return f"{dt.year}/{dt.month}/{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"


def update_email_template(template_id: str, old_code: str, new_code: str,
                           old_url: str, new_url: str, new_start: str, new_end: str):
    """
    社内システム（app.jrcreators.com）のメールテンプレートを開き、本文中の
    クーポンコード・期限・取得URLを新しい値に書き換えて保存する。
    本文の該当箇所が1つも見つからなかった場合は、テンプレートの文言が
    変わっている可能性があるため、何も保存せず例外を投げる。
    """
    if not old_code or not old_url:
        # 空文字列でreplace()すると全文字の間に挿入されて本文が壊れるため、先に弾く
        raise RuntimeError(f"置き換え元の値が空です（old_code={old_code!r}, old_url={old_url!r}）")

    login_id_1_enc = quote(LOGIN_ID_1, safe="")
    login_pass_1_enc = quote(LOGIN_PASS_1, safe="")
    login_url = f"https://{login_id_1_enc}:{login_pass_1_enc}@{APP_DOMAIN}/"
    edit_url = f"https://{login_id_1_enc}:{login_pass_1_enc}@{APP_DOMAIN}/email-templates/edit/{template_id}"

    new_period_line = f"期限：{format_template_date(new_start)}　～　{format_template_date(new_end)}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1800, "height": 900},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        page.goto(login_url, wait_until="networkidle")
        page.click('a:has-text("Login"), button:has-text("Login")')
        page.wait_for_load_state("networkidle")
        page.fill('input[name="username"]', LOGIN_ID_2)
        page.fill('input[type="password"]', LOGIN_PASS_2)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(edit_url, wait_until="networkidle")
        body = page.input_value('textarea[name="body"]')

        updated = body.replace(old_code, new_code).replace(old_url, new_url)
        updated, n_period = re.subn(r"期限：.*", new_period_line, updated)

        if updated == body or n_period == 0:
            browser.close()
            raise RuntimeError(
                "本文中に置き換え対象（クーポンコード／期限／URL）が見つかりませんでした。"
                "テンプレートの文言が変わっている可能性があります。"
            )

        page.fill('textarea[name="body"]', updated)
        page.click('button:has-text("SUBMIT"), input[value="SUBMIT"]')
        page.wait_for_load_state("networkidle")
        browser.close()


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
    print(f"対象クーポン: {[(store, name) for store, name, _ in targets]}")
    print(f"Chatwork通知先ルームid: {room_id or '（未設定）'}")

    new_start, new_end, period_label = next_month_period(today)
    print(f"作成する期間: {new_start} 〜 {new_end}（{period_label}）")

    # 同じ店舗のクーポン一覧取得を使い回すため、店舗ごとにグループ化する
    targets_by_store = {}
    for store, name, request_message in targets:
        targets_by_store.setdefault(store, []).append((name, request_message))

    log_rows = []
    now_label = today.strftime("%Y/%m/%d %H:%M")

    for store, store_targets in targets_by_store.items():
        print(f"\n=== 店舗: {store} ===")
        store_creds = STORES.get(store)
        if not store_creds:
            print(f"  未知の店舗キーです: {store}。スキップします。")
            for name, _ in store_targets:
                log_rows.append([now_label, name, period_label, f"未知の店舗キー（{store}）", "", ""])
            continue

        headers = auth_headers(store_creds["service_secret"], store_creds["license_key"])
        try:
            coupons = search_all(headers)
        except Exception as e:
            print(f"  クーポン一覧の取得に失敗しました: {e}")
            for name, _ in store_targets:
                log_rows.append([now_label, name, period_label, "クーポン一覧の取得に失敗", "", ""])
            continue
        print(f"  既存クーポン: {len(coupons)}件")

        store_target_names = [name for name, _ in store_targets]
        ok, reason = check_today(today, coupons, manual_events, store_target_names)
        print(f"  本日の判定: {reason}")
        if not ok and not FORCE:
            print("  今日は発行しません。この店舗をスキップします。")
            continue
        if not ok and FORCE:
            print("  ※ FORCE指定のため、判定を無視して発行します。")

        for name, request_message in store_targets:
            print(f"\n--- [{store}] {name} ---")
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

            image_url = IMAGE_URL_BY_STORE.get(store)
            if image_url and src.get("couponImage") != image_url:
                print(f"    クーポン画像を差し替えます（元の値: {src.get('couponImage')} → {image_url}）")
                src["couponImage"] = image_url

            xml = build_issue_xml(src, new_start, new_end, nested)

            if DRY_RUN:
                print("  【DRY RUN】以下の内容で発行します：")
                print(f"    枚数: {src.get('issueCount')} / 割引: {src.get('discountFactor')}"
                      f" / 条件: {nested['otherConditions']}")
                template_id = TEMPLATE_ID_BY_TARGET.get((store, name))
                if template_id:
                    print(f"  【DRY RUN】テンプレートID {template_id} も更新予定（実際には更新しません）")
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

                template_note = ""
                template_id = TEMPLATE_ID_BY_TARGET.get((store, name))
                if template_id:
                    print(f"    テンプレートID {template_id} を更新します...")
                    try:
                        update_email_template(
                            template_id,
                            source.get("couponCode", ""), code,
                            src.get("pcGetUrl", ""), url,
                            new_start, new_end,
                        )
                        print(f"    テンプレートID {template_id} を更新しました。")
                    except Exception as e:
                        print(f"    テンプレートID {template_id} の更新に失敗しました: {e}")
                        template_note = (
                            f"\n\n※ テンプレートID {template_id} の自動更新に失敗しました。"
                            f"手動で更新してください。\n  理由: {e}"
                        )

                post_chatwork_task(room_id, CW_ASSIGNEE_ID, build_success_message(
                    name, period_label, new_start, new_end, code, url, request_message) + template_note,
                    due_days=CW_TASK_DUE_DAYS)
            else:
                # 失敗も知らせる。気づかないまま月をまたぐのを防ぐため
                post_chatwork_task(room_id, CW_ASSIGNEE_ID,
                                    f"{CW_MENTION}\n"
                                    f"[info][title]楽天クーポン更新の失敗（{period_label}）[/title]"
                                    f"{name}\n{message}\n手動での対応をお願いします。[/info]",
                                    due_days=CW_TASK_DUE_DAYS)
            log_rows.append([now_label, name, period_label, message, code or "", url or ""])

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを「{LOG_SHEET}」タブに{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print("=== 月次クーポン自動発行 完了 ===")


# ══ テンプレートだけ同期する（発行済みクーポンに合わせて追いつかせる） ══
def sync_template_only(store: str, name: str):
    """
    クーポンを新規発行せず、既に存在する最新2件（現在有効なもの＝new、その1つ前＝old）から
    メールテンプレートだけを更新する。Chatworkへの通知も行わない。
    月次自動発行が既にクーポンだけ発行済みで、テンプレート更新だけ追いつかせたい場合に使う。
    """
    template_id = TEMPLATE_ID_BY_TARGET.get((store, name))
    if not template_id:
        print(f"「{store} / {name}」に対応するテンプレートIDが設定されていません。")
        sys.exit(1)

    store_creds = STORES.get(store)
    if not store_creds:
        print(f"未知の店舗キーです: {store}")
        sys.exit(1)

    headers = auth_headers(store_creds["service_secret"], store_creds["license_key"])
    coupons = search_all(headers)
    matches = sorted(
        [c for c in coupons if c.get("couponName") == name],
        key=lambda c: c.get("couponStartDate", ""),
        reverse=True,
    )
    if len(matches) < 2:
        print(f"「{name}」の過去クーポンが2件未満のため、新旧を判定できません（{len(matches)}件）。")
        sys.exit(1)

    new_summary, old_summary = matches[0], matches[1]
    print(f"新（現在有効）: {new_summary.get('couponCode')}"
          f"（{new_summary.get('couponStartDate')}〜{new_summary.get('couponEndDate')}）")
    print(f"旧: {old_summary.get('couponCode')}"
          f"（{old_summary.get('couponStartDate')}〜{old_summary.get('couponEndDate')}）")

    new_src, _ = get_coupon(headers, new_summary["couponCode"])
    old_src, _ = get_coupon(headers, old_summary["couponCode"])
    if new_src is None or old_src is None:
        print("クーポン詳細の取得に失敗しました。")
        sys.exit(1)

    update_email_template(
        template_id,
        old_src.get("couponCode", ""), new_src.get("couponCode", ""),
        old_src.get("pcGetUrl", ""), new_src.get("pcGetUrl", ""),
        new_src.get("couponStartDate", ""), new_src.get("couponEndDate", ""),
    )
    print(f"テンプレートID {template_id} を更新しました。")


if __name__ == "__main__":
    sync_target = os.environ.get("SYNC_TEMPLATE_ONLY_FOR", "").strip()
    if sync_target:
        sync_store = os.environ.get("SYNC_TEMPLATE_ONLY_STORE", DEFAULT_STORE).strip() or DEFAULT_STORE
        sync_template_only(sync_store, sync_target)
    else:
        main()
