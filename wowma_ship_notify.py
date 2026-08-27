"""
社内システム（app.jrcreators.com）でWowma!（現au PAYマーケット、社内システム上の店舗名は
"LA Express"）チャネルの注文が発送済みになったら、発送日・追跡番号・配送会社を
Wowma!のWow!managerAPIへ自動反映する。yahoo_ship_notify.pyと同じ構成。

Wowma!のAPIは登録した固定IPからしか呼べないため、GitHub Actionsからは実行できず、
このPC（固定IPを許可登録済み）上でのローカル実行のみに対応する
（case_orders_auto_close.pyのWowma対応と同じ制約。詳細はそちらのdocstring参照）。

処理の流れ:
  1. rakuten_ship_notify.pyのlogin/collect_shipped_ordersをそのまま再利用し、
     直近LOOKBACK_DAYS日以内に発送された全チャネルの注文を取得する
  2. shop_name（社内システム上の店舗名）が"LA Express"の注文だけに絞り込む
  3. 社内システムのorder_number（例: "746423033"、再送の場合は末尾に"-R"等が付く）から
     再送マーカーを取り除いたものをWowmaのorderIdとして使う
     （Wowma!の注文はこの社内システム上では他モールのような店舗名接頭辞が付かず、
     数字そのものがorderIdになっている。2026-08-27、shop_name一覧調査で確認）
  4. 受注情報取得API（searchTradeInfoProc）でshippingNumberが既に入っていれば
     登録済みとみなしてスキップする
  5. 未処理のものだけ、受注情報更新API（updateTradeInfoProc）で発送日・配送業者・
     追跡番号をまとめて登録する
  6. エラー・要確認（未知の配送会社名等）があった場合のみChatworkに通知する

未確定点（実地テストで確認が必要。case_orders_wowma.pyのdocstring/コメントも参照）:
  - searchTradeInfoProc/updateTradeInfoProcのXMLタグ名が実際のレスポンスと一致するか
  - Wowma側の配送会社コードの完全な一覧（仕様書記載の1/2/4/5/6/7/9のうち、
    実際に使っているのは1・2・6のみ確認）
"""

import os
import re
import sys
import time

from rakuten_ship_notify import (
    LA_TZ,
    CREATED_TIME_LOOKBACK_DAYS,
    ONLY_ORDER_NUMBERS,
    MAX_PER_RUN,
    login,
    collect_shipped_orders,
    parse_ship_date,
    post_chatwork_task,
    CW_ROOM_ID,
    CW_ASSIGNEE_ID,
    CW_MENTION,
)
from case_orders_wowma import (
    WOWMA_CARRIER_CODES,
    wowma_get_order_info,
    wowma_update_trade_info,
)

from playwright.sync_api import sync_playwright

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

SHOP_NAME_LA_EXPRESS = "LA Express"

CW_TITLE = "Wowma出荷通知の自動反映でエラー・要確認がありました"


def build_wowma_order_id(order_number: str) -> str:
    """末尾の"-R", "-R2"のような再送マーカーを取り除く。
    yahoo_ship_notify.pyのbuild_order_idと同じ考え方（rakuten_ship_notify.pyの
    api_order_number()と同じ正規表現）。"""
    return re.sub(r"-R\d*$", "", order_number.strip())


def build_report(unmapped_carriers: list, errors: list) -> str:
    lines = [CW_MENTION, f"[info][title]{CW_TITLE}[/title]", ""]
    if unmapped_carriers:
        lines.append("■ 未知の配送会社名（要マッピング追加）")
        for r in unmapped_carriers:
            lines.append(f"・注文番号 {r['order_number']}: {r['ship_method']!r}")
        lines.append("")
    if errors:
        lines.append("■ エラー")
        for r in errors:
            lines.append(f"・注文番号 {r['order_number']}: {r['message']}")
        lines.append("")
    lines.append("[/info]")
    return "\n".join(lines)


def main():
    print("=== Wowma 出荷通知 自動反映 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：発送情報更新APIは呼びません")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1800, "height": 900},
                device_scale_factor=2,
            )
            page = context.new_page()
            login(page)
            orders = collect_shipped_orders(page, context)
        finally:
            browser.close()

    print(f"取得した発送済み注文（全チャネル・重複除去後）: {len(orders)}件")

    wowma_orders = [o for o in orders if o["shop_name"] == SHOP_NAME_LA_EXPRESS]
    for o in wowma_orders:
        o["wowma_order_id"] = build_wowma_order_id(o["order_number"])
    print(f"Wowma対象（{SHOP_NAME_LA_EXPRESS}）: {len(wowma_orders)}件")

    if ONLY_ORDER_NUMBERS:
        wowma_orders = [o for o in wowma_orders if o["order_number"] in ONLY_ORDER_NUMBERS]
        print(f"ONLY_ORDER_NUMBERS指定により絞り込み: {len(wowma_orders)}件（対象: {sorted(ONLY_ORDER_NUMBERS)}）")

    missing_info = 0
    unmapped_carriers = []
    errors = []
    registered = 0
    skipped_already = 0
    not_found = 0

    for o in wowma_orders:
        if MAX_PER_RUN is not None and registered >= MAX_PER_RUN:
            print(f"MAX_PER_RUN={MAX_PER_RUN}に達したため、残りは今回スキップします。")
            break

        if not o["tracking_num"]:
            missing_info += 1
            continue

        if not o["ship_method"] or o["ship_method"].strip().lower() == "none":
            carrier_code = WOWMA_CARRIER_CODES.get("Sagawa CDS")
        else:
            carrier_code = WOWMA_CARRIER_CODES.get(o["ship_method"])
            if carrier_code is None:
                unmapped_carriers.append(o)
                continue

        order_id = o["wowma_order_id"]
        try:
            info = wowma_get_order_info(order_id)
        except Exception as e:
            print(f"  {o['order_number']}（{order_id}）: 受注情報取得エラー: {e}")
            errors.append({"order_number": o["order_number"], "message": f"受注情報取得エラー: {e}"})
            continue

        if info is None:
            not_found += 1
            print(f"  {o['order_number']}（{order_id}）: 受注情報取得で見つかりませんでした")
            errors.append({"order_number": o["order_number"], "message": f"受注情報取得で見つかりませんでした（orderId={order_id}）"})
            continue

        if info.get("shippingNumber"):
            skipped_already += 1
            print(f"  {o['order_number']}（{order_id}）: 既に追跡番号が登録済みのためスキップ")
            continue

        shipping_date = parse_ship_date(o["ship_time"]) or ""

        if DRY_RUN:
            print(f"  【DRY RUN】{o['order_number']}（{order_id}）: "
                  f"{o['ship_method'] or 'Sagawa CDS(既定)'}({carrier_code}) / {o['tracking_num']} / {shipping_date}")
            registered += 1
            continue

        ok, message = wowma_update_trade_info(order_id, shipping_date, carrier_code, o["tracking_num"], DRY_RUN)
        if ok:
            registered += 1
            print(f"  {o['order_number']}（{order_id}）: {message}")
        else:
            errors.append({"order_number": o["order_number"], "message": message})
            print(f"  {o['order_number']}（{order_id}）: {message}")

    print(f"\n=== 完了: 登録{registered}件 / 既登録スキップ{skipped_already}件 / "
          f"情報不足{missing_info}件 / 見つからず{not_found}件 / エラー{len(errors)}件 / "
          f"要確認{len(unmapped_carriers)}件 ===")

    if unmapped_carriers or errors:
        post_chatwork_task(CW_ROOM_ID, CW_ASSIGNEE_ID, build_report(unmapped_carriers, errors))
    else:
        print("エラー・要確認とも無かったため、Chatworkへは通知しません。")

    print("=== Wowma 出荷通知 自動反映 完了 ===")


if __name__ == "__main__":
    main()
