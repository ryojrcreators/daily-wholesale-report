"""
Yahoo出荷通知専用のrefresh_token（Yahoo_Config_Orderタブ）を定期的に更新するだけの
軽量スクリプト。注文系API（orderInfo等）は一切呼ばず、Yahooのトークン更新エンドポイント
とGoogle Sheetsの読み書きだけを行う。

目的: yahoo_ship_notify.py はこのPCの稼働時間（7:45〜16:00 PT）内でしか実行されないため、
夜間（最後の実行〜翌朝の実行まで約16時間）refresh_tokenが一度も使われない期間ができる。
Yahoo側の注文系APIの仕様上、refresh_tokenの有効期間が他のAPIより短い可能性があるため
（2026-08-27、Yahooからの案内メールで「最大12時間」と説明あり）、この空白期間を埋めるために
GitHub Actionsで定期的にリフレッシュしておく（IP制限のかからない処理のみのため、
GitHub Actions上で問題なく実行できる）。
"""

import os
import requests

from case_orders_auto_close import (
    YAHOO_TOKEN_URL,
    get_spreadsheet,
)

# 2026-09-03、YahooのIP許可申請が承認されたため、出荷通知専用アプリ
# （YAHOO_ORDER_CLIENT_ID/SECRET）に戻した。詳細はyahoo_ship_notify.py側の同日コメント参照。
YAHOO_ORDER_CLIENT_ID = os.environ["YAHOO_ORDER_CLIENT_ID"]
YAHOO_ORDER_CLIENT_SECRET = os.environ["YAHOO_ORDER_CLIENT_SECRET"]

ORDER_CONFIG_SHEET_NAME = "Yahoo_Config_Order"


def load_order_refresh_token(spreadsheet) -> str:
    ws = spreadsheet.worksheet(ORDER_CONFIG_SHEET_NAME)
    for row in ws.get_all_values():
        if row and row[0] == "refresh_token":
            return row[1]
    raise RuntimeError(f"「{ORDER_CONFIG_SHEET_NAME}」タブに refresh_token が見つかりません。")


def save_order_refresh_token(spreadsheet, new_token: str):
    ws = spreadsheet.worksheet(ORDER_CONFIG_SHEET_NAME)
    for i, row in enumerate(ws.get_all_values(), start=1):
        if row and row[0] == "refresh_token":
            ws.update(range_name=f"A{i}:B{i}", values=[["refresh_token", new_token]])
            return
    ws.append_row(["refresh_token", new_token])


def main():
    print("=== Yahoo出荷通知専用トークン 延命更新 開始 ===")
    spreadsheet = get_spreadsheet()
    current = load_order_refresh_token(spreadsheet)

    res = requests.post(
        YAHOO_TOKEN_URL,
        auth=(YAHOO_ORDER_CLIENT_ID, YAHOO_ORDER_CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": current},
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"トークン更新失敗（status={res.status_code}）: {res.text[:300]}")

    data = res.json()
    save_order_refresh_token(spreadsheet, data.get("refresh_token", current))
    print("refresh_tokenを更新しました。")
    print("=== 完了 ===")


if __name__ == "__main__":
    main()
