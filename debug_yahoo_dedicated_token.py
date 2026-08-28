"""一時デバッグ: Yahoo出荷通知専用の独立したrefresh_tokenを取得し、
既存のYahoo_Configタブとは別の「Yahoo_Config_Order」タブに保存する。
Close/価格調整とトークンを共有すると注文系APIでセッション競合が起きた
（2026-08-27、px-04102）ため分離する。"""
import os
import requests
import gspread

from case_orders_auto_close import (
    YAHOO_CLIENT_ID,
    YAHOO_CLIENT_SECRET,
    YAHOO_TOKEN_URL,
    get_spreadsheet,
)

AUTH_CODE = os.environ["YAHOO_AUTH_CODE"]
REDIRECT_URI = "https://app.jrcreators.com/"
ORDER_CONFIG_SHEET_NAME = "Yahoo_Config_Order"


def save_order_refresh_token(spreadsheet, new_token: str):
    try:
        ws = spreadsheet.worksheet(ORDER_CONFIG_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=ORDER_CONFIG_SHEET_NAME, rows=10, cols=2)
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0] == "refresh_token":
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_token]])
            return
    ws.append_row(["refresh_token", new_token])


def main():
    res = requests.post(
        YAHOO_TOKEN_URL,
        auth=(YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET),
        data={
            "grant_type": "authorization_code",
            "code": AUTH_CODE,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    print(f"status={res.status_code}")
    print(res.text[:500])
    res.raise_for_status()
    data = res.json()

    new_refresh_token = data["refresh_token"]
    spreadsheet = get_spreadsheet()
    save_order_refresh_token(spreadsheet, new_refresh_token)
    print(f"\n新しいrefresh_tokenを「{ORDER_CONFIG_SHEET_NAME}」タブに保存しました。")


if __name__ == "__main__":
    main()
