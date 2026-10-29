"""
Yahooの再認証（認可コード → 新しいrefresh_token）を行い、スプレッドシートへ保存し、認証日も記録する。
使い方（トークンやシークレットは画面に出さない）:
    python yahoo_reauth.py shared <認可コード>   # 共有アプリ（Close・価格調整・出品データ同期）
    python yahoo_reauth.py order  <認可コード>   # 出荷通知専用アプリ
必要な環境変数: GOOGLE_CREDENTIALS_JSON（または GOOGLE_CREDENTIALS）、RAKUTEN_LISTING_SPREADSHEET_ID、
    shared → YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET、order → YAHOO_ORDER_CLIENT_ID / YAHOO_ORDER_CLIENT_SECRET
コールバックURLは https://app.jrcreators.com/ 。認可コードは、認証用URLで許可したあとにリダイレクトされる
URLの code= の値（数分で失効）。
"""

import os
import sys

import requests

import case_orders_auto_close as co
import yahoo_token_expiry as exp

REDIRECT_URI = "https://app.jrcreators.com/"


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("shared", "order"):
        print(__doc__)
        sys.exit(2)
    kind, code = sys.argv[1], sys.argv[2]

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
        sys.exit(1)
    new_refresh = res.json().get("refresh_token")
    if not new_refresh:
        print("レスポンスにrefresh_tokenが含まれていません。")
        sys.exit(1)

    spreadsheet = co.get_spreadsheet()
    ws = spreadsheet.worksheet(sheet_name)
    rows = ws.get_all_values()
    for i, row in enumerate(rows, start=1):
        if row and row[0] == "refresh_token":
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_refresh]])
            break
    else:
        ws.append_row(["refresh_token", new_refresh])
    exp.set_authorized_at(spreadsheet, sheet_name)
    print(f"新しいrefresh_tokenの保存と、認証日（{exp._today()}）の記録: 成功（{sheet_name}）")


if __name__ == "__main__":
    main()
