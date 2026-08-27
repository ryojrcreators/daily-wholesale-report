"""一時デバッグ: 認可コード(authorization_code)を新しいrefresh_tokenに交換し、
Yahoo_Configタブへ保存する。orderList/orderInfo/orderChange APIの再認可対応（2026-08-26）。"""
import os
import requests

from case_orders_auto_close import (
    YAHOO_CLIENT_ID,
    YAHOO_CLIENT_SECRET,
    YAHOO_TOKEN_URL,
    get_spreadsheet,
    save_refresh_token,
)

AUTH_CODE = os.environ["YAHOO_AUTH_CODE"]
REDIRECT_URI = "https://app.jrcreators.com/"


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
    save_refresh_token(spreadsheet, new_refresh_token)
    print("\n新しいrefresh_tokenをYahoo_Configタブに保存しました。")
    print(f"scope: {data.get('scope')}")
    print(f"expires_in(access_token): {data.get('expires_in')}")


if __name__ == "__main__":
    main()
