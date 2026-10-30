"""
Yahooの再認証（認可コード → 新しいrefresh_token）を行い、スプレッドシートへ保存し、認証日も記録する。
結果（成功・失敗）はChatworkにも知らせる。GitHub Actionsの「Yahoo 再認証」から、スマホやほかのPCでも
実行できる（2026-10-30、ユーザーの長期不在に備えて追加）。

使い方（トークンやシークレットは画面に出さない）:
    python yahoo_reauth.py shared <認可コード または 許可後のURL全体>   # 共有アプリ（Close・価格調整・出品データ同期）
    python yahoo_reauth.py order  <認可コード または 許可後のURL全体>   # 出荷通知専用アプリ
必要な環境変数: GOOGLE_CREDENTIALS_JSON（または GOOGLE_CREDENTIALS）、RAKUTEN_LISTING_SPREADSHEET_ID、
    shared → YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET、order → YAHOO_ORDER_CLIENT_ID / YAHOO_ORDER_CLIENT_SECRET、CW_TOKEN
コールバックURLは https://app.jrcreators.com/ 。認可コードは、認証用URLで許可したあとにリダイレクトされる
URLの code= の値（数分で失効）。URL全体を貼れば、そこから code を取り出す。
"""

import os
import re
import sys
from urllib.parse import parse_qs, urlparse

import requests

import case_orders_auto_close as co
import yahoo_token_expiry as exp

REDIRECT_URI = "https://app.jrcreators.com/"
APP_LABELS = {
    "shared": "共有アプリ（Close・価格調整・Yahoo出品データ同期）",
    "order": "出荷通知専用アプリ（N100のYahoo出荷通知）",
}


def extract_code(text: str):
    """認可コード単体、または code= を含むURL全体から、認可コードを取り出す。取り出せなければ None。"""
    text = (text or "").strip()
    if not text:
        return None
    if "code=" in text:
        query = parse_qs(urlparse(text).query)
        if query.get("code"):
            return query["code"][0].strip()
        m = re.search(r"[?&#]code=([^&\s#]+)", text)
        return m.group(1) if m else None
    return text if re.fullmatch(r"[A-Za-z0-9_\-]{4,64}", text) else None


def notify(message: str):
    if not exp.CW_TOKEN:
        print("  CW_TOKENが未設定のため、Chatworkには知らせません。")
        return
    try:
        requests.post(
            f"https://api.chatwork.com/v2/rooms/{exp.CW_ROOM_ID}/messages",
            headers={"X-ChatWorkToken": exp.CW_TOKEN},
            data={"body": f"{exp.CW_MENTION}\n{message}"},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  Chatworkへの通知に失敗しました: {e}")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in APP_LABELS:
        print(__doc__)
        sys.exit(2)
    kind = sys.argv[1]
    label = APP_LABELS[kind]

    code = extract_code(sys.argv[2])
    if not code:
        msg = "貼られた内容から、認可コード（code=…）を取り出せませんでした。許可したあとに開いたページのアドレス全体を貼ってください。"
        print(msg)
        notify(f"[info][title]Yahoo再認証に失敗しました（{label}）[/title]{msg}[/info]")
        sys.exit(1)

    if kind == "shared":
        client_id, client_secret = os.environ["YAHOO_CLIENT_ID"], os.environ["YAHOO_CLIENT_SECRET"]
        sheet_name = "Yahoo_Config"
    else:
        client_id, client_secret = os.environ["YAHOO_ORDER_CLIENT_ID"], os.environ["YAHOO_ORDER_CLIENT_SECRET"]
        sheet_name = "Yahoo_Config_Order"

    res = requests.post(
        co.YAHOO_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        timeout=30,
    )
    if res.status_code != 200:
        print(f"トークン交換失敗（status={res.status_code}）: {res.text[:300]}")
        notify(
            f"[info][title]Yahoo再認証に失敗しました（{label}）[/title]"
            "Yahooに断られました。認可コードは数分で使えなくなります。もう一度、認証用のURLを開いて許可し直してから、"
            "すぐに実行してください。[/info]"
        )
        sys.exit(1)
    new_refresh = res.json().get("refresh_token")
    if not new_refresh:
        print("レスポンスにrefresh_tokenが含まれていません。")
        notify(f"[info][title]Yahoo再認証に失敗しました（{label}）[/title]Yahooの応答に、新しいトークンが含まれていませんでした。[/info]")
        sys.exit(1)

    spreadsheet = co.get_spreadsheet()
    ws = spreadsheet.worksheet(sheet_name)
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0] == "refresh_token":
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_refresh]])
            break
    else:
        ws.append_row(["refresh_token", new_refresh])
    exp.set_authorized_at(spreadsheet, sheet_name)

    print(f"新しいrefresh_tokenの保存と、認証日（{exp._today()}）の記録: 成功（{sheet_name}）")
    notify(
        f"[info][title]Yahooの再認証が完了しました（{label}）[/title]"
        f"認証日: {exp._today()}。次の再認証の目安は、21日後（{exp.WARN_DAYS}日たったらお知らせします）です。[/info]"
    )


if __name__ == "__main__":
    main()
