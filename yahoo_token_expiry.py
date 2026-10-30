"""
Yahooのリフレッシュトークンの「認証した日」を記録し、期限が近づいたらChatworkのタスクで知らせる
（2026-10-29、ユーザー依頼）。

背景: Yahooのリフレッシュトークンは、認証（ブラウザでの許可）から約4週間（28日）で期限切れになる
（出荷通知専用アプリ 9/3→10/1、共有アプリ 約9月末→10/27ごろで確認。更新＝ローテーションでは延びない）。
期限が切れると、Close・価格調整・出品データ同期・Yahoo出荷通知が止まる。

記録の場所: 各トークンを保存しているタブ（Yahoo_Config／Yahoo_Config_Order）の
「authorized_at」行（B列に YYYY-MM-DD）。再認証したら set_authorized_at() で更新する
（yahoo_reauth.py が、トークンの保存と同時に行う）。
通知: 認証から WARN_DAYS 日以上たっていたら、同じ日に1回だけ「last_warned_on」行で重複を防いで通知する。
"""

import os
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))

LIFETIME_DAYS = 28
WARN_DAYS = 21

CW_TOKEN = os.environ.get("CW_TOKEN", "")
CW_ROOM_ID = "212267044"  # 2026-10-30、ユーザー指定（再認証のタスク・完了/失敗の通知の送り先）
CW_ASSIGNEE_ID = "2618849"  # Ryo Higuchiさん
CW_MENTION = "[To:2618849]Ryo Higuchiさん"

# 表示名 → トークンを保存しているタブ名
TOKEN_SHEETS = {
    "共有アプリ（Close・価格調整・Yahoo出品データ同期）": "Yahoo_Config",
    "出荷通知専用アプリ（N100のYahoo出荷通知）": "Yahoo_Config_Order",
}

KEY_AUTHORIZED = "authorized_at"
KEY_WARNED = "last_warned_on"

# 再認証の手順（Chatworkの通知に入れる）。スマホからでもできるよう、認証用URLとワークフローのリンクを載せる。
REAUTH_WORKFLOW_URL = "https://github.com/ryojrcreators/daily-wholesale-report/actions/workflows/yahoo_reauth.yml"
REDIRECT_URI = "https://app.jrcreators.com/"
# タブ名 → (クライアントIDの環境変数, 再認証ワークフローで選ぶ「アプリ」の値)
APP_INFO = {
    "Yahoo_Config": ("YAHOO_CLIENT_ID", "shared"),
    "Yahoo_Config_Order": ("YAHOO_ORDER_CLIENT_ID", "order"),
}


def build_auth_url(sheet_name: str):
    """そのアプリの認証用URL。クライアントIDは秘密情報ではない。環境変数が無ければ None。"""
    from urllib.parse import quote
    client_id = os.environ.get(APP_INFO[sheet_name][0], "")
    if not client_id:
        return None
    return (
        "https://auth.login.yahoo.co.jp/yconnect/v2/authorization?response_type=code"
        f"&client_id={client_id}&redirect_uri={quote(REDIRECT_URI, safe='')}&scope=openid&bail=1&state=reauth"
    )


def _today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def _read_rows(ws) -> dict:
    """{キー: (行番号, 値)}"""
    out = {}
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0]:
            out[row[0]] = (i, row[1] if len(row) > 1 else "")
    return out


def _set_row(ws, key: str, value: str):
    rows = _read_rows(ws)
    if key in rows:
        ws.update(range_name=f"A{rows[key][0]}:B{rows[key][0]}", values=[[key, value]])
    else:
        ws.append_row([key, value])


def set_authorized_at(spreadsheet, sheet_name: str, date: str = None):
    """再認証した日（既定は今日）を記録し、通知済みの印をリセットする。"""
    ws = spreadsheet.worksheet(sheet_name)
    _set_row(ws, KEY_AUTHORIZED, date or _today())
    _set_row(ws, KEY_WARNED, "")


def _post_task(body: str, due: datetime):
    if not CW_TOKEN:
        print("  CW_TOKENが未設定のため、Yahooトークン期限の通知を送れません。")
        return False
    data = {"body": body, "to_ids": CW_ASSIGNEE_ID}
    due_day = due.replace(hour=0, minute=0, second=0, microsecond=0)
    data["limit"] = int(due_day.timestamp())
    data["limit_type"] = "date"
    res = requests.post(
        f"https://api.chatwork.com/v2/rooms/{CW_ROOM_ID}/tasks",
        headers={"X-ChatWorkToken": CW_TOKEN},
        data=data,
        timeout=30,
    )
    print(f"  Chatworkタスク作成（Yahooトークン期限）: status={res.status_code}")
    return res.status_code < 400


def check_and_warn(spreadsheet) -> list:
    """期限が近い（認証からWARN_DAYS日以上）トークンについて、Chatworkのタスクで知らせる。
    戻り値は通知した表示名のリスト。失敗しても、呼び出し元の処理は止めない（例外は握りつぶす）。"""
    warned = []
    now = datetime.now(JST)
    for label, sheet_name in TOKEN_SHEETS.items():
        try:
            ws = spreadsheet.worksheet(sheet_name)
            rows = _read_rows(ws)
            authorized = rows.get(KEY_AUTHORIZED, (None, ""))[1].strip()
            if not authorized:
                print(f"  Yahooトークン期限: {label} は認証日が未記録のため確認できません。")
                continue
            authorized_dt = datetime.strptime(authorized, "%Y-%m-%d").replace(tzinfo=JST)
            age_days = (now - authorized_dt).days
            if age_days < WARN_DAYS:
                continue
            if rows.get(KEY_WARNED, (None, ""))[1].strip() == _today():
                continue  # 今日はすでに通知済み
            expire_dt = authorized_dt + timedelta(days=LIFETIME_DAYS)
            remaining = (expire_dt.date() - now.date()).days
            if remaining >= 0:
                state = f"期限切れの見込みは {expire_dt.strftime('%Y-%m-%d')}（あと{remaining}日）です。"
            else:
                state = f"期限（{expire_dt.strftime('%Y-%m-%d')}）を過ぎています。すでに止まっている可能性があります。"
            auth_url = build_auth_url(sheet_name)
            app_value = APP_INFO[sheet_name][1]
            steps = (
                "\n\n【再認証の手順（スマホでもできます）】\n"
                + (f"1. 次のURLを開いて、Yahooにログインして許可する\n{auth_url}\n" if auth_url
                   else "1. Yahooの認証用URLを開いて、ログインして許可する（URLは、Claudeに「Yahooの再認証をお願い」と伝えると出せます）\n")
                + "2. 許可したあとに開いたページの、アドレス（URL全体）をコピーする\n"
                + f"3. 次のページで「Run workflow」を開き、アプリは「{app_value}」を選び、コピーしたURLを貼って実行する\n"
                + f"{REAUTH_WORKFLOW_URL}\n"
                + "4. 完了すると、このルームに「Yahooの再認証が完了しました」と届きます"
            )
            body = (
                f"{CW_MENTION}\n"
                f"[info][title]Yahooのトークンの再認証が必要です[/title]"
                f"対象: {label}\n"
                f"最後に認証してから{age_days}日たちました。{state}{steps}[/info]"
            )
            if _post_task(body, expire_dt):
                _set_row(ws, KEY_WARNED, _today())
                warned.append(label)
        except Exception as e:  # noqa: BLE001
            print(f"  Yahooトークン期限の確認でエラー（{label}）: {e}")
    return warned
