"""一時復旧用: 共有Yahoo_Configのrefresh_tokenが失効したため、認可コードから再発行して保存する。"""

import requests

from case_orders_auto_close import (
    YAHOO_CLIENT_ID,
    YAHOO_CLIENT_SECRET,
    YAHOO_TOKEN_URL,
    get_spreadsheet,
    save_refresh_token,
)

AUTH_CODE = "ZWoxgmDQ"
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
    if res.status_code != 200:
        return
    data = res.json()
    spreadsheet = get_spreadsheet()
    save_refresh_token(spreadsheet, data["refresh_token"])
    print("Yahoo_Configタブのrefresh_tokenを更新しました。")


if __name__ == "__main__":
    main()
