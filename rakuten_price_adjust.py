"""
「楽天在庫&価格チェック」スプレッドシート（ASINありシート）の価格チェック結果に応じて、
🔴赤字の商品を「適正価格」（G列、rakuten_price_check.pyが計算した損益分岐点の価格）に
自動で値上げする。

対象:
  F列（価格チェック）が「🔴 赤字」かつ I列（価格調整対応済み）が空かつ
  G列（適正価格）に「¥数値」の値がある行

Yahoo側で特価(SalePrice)が通常価格(Price)と異なる＝セール中の商品は、
価格調整で意図せずセールを解除してしまわないよう、その商品ごと処理対象から除外し、
I列に「要確認(Yahooセール中)」と書き込んで人の確認に回す（楽天側も含め一切変更しない）。

楽天: PATCH /es/2.0/items/manage-numbers/{商品管理番号} で
      variants.{variantId}.standardPrice を更新する（rakuten_hideと同じGET→PATCH方式）。
Yahoo: 商品一括更新API（updateItems）で price と sale_price を同時に新価格へ更新する
       （このAPIはprice更新時、price/sale_price両方を指定する必要がある）。

成功したらI列に処理日時を書き込み、以後の実行では対象から除外する
（1つでも失敗したら空のまま残し、次回再挑戦させる）。
実行結果は「価格調整_ログ」タブに追記する。
"""

import os
import re
import json
import time
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials

from case_orders_auto_close import (
    DRY_RUN,
    JST,
    YAHOO_SUFFIXES,
    RAKUTEN_STORES,
    YAHOO_STORES,
    YAHOO_BASE,
    RMS_BASE,
    API_INTERVAL,
    get_spreadsheet,
    get_yahoo_access_token,
    rakuten_auth_headers,
    yahoo_get_item,
)

MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))
# テスト用：指定した場合、この商品管理番号だけを対象にする（カンマ区切りで複数可）
ONLY_ITEM_NUMBERS = {
    s.strip() for s in os.environ.get("ONLY_ITEM_NUMBERS", "").split(",") if s.strip()
}

PRICE_SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
PRICE_SHEET_NAME = "ASINあり"
LOG_SHEET_NAME = "価格調整_ログ"
LOG_HEADER = ["実行日時(JST)", "-", "種別", "モール", "店舗", "商品コード", "結果"]

COL_ITEM_NUMBER = 0    # A列：商品管理番号
COL_NAME = 1           # B列：商品名
COL_PRICE_CHECK = 5    # F列：価格チェック
COL_PROPER_PRICE = 6   # G列：適正価格
COL_STOCK_DONE = 7     # H列：在庫対応済み（rakuten_shortage_auto_close.pyが使用）
COL_PRICE_DONE = 8     # I列：価格調整対応済み（このスクリプトが書き込む）

DEFICIT_LABEL = "🔴 赤字"
SALE_CONFLICT_MESSAGE = "要確認(Yahooセール中)"


def append_log(spreadsheet, rows: list):
    if not rows:
        return
    try:
        ws = spreadsheet.worksheet(LOG_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=len(LOG_HEADER))
        ws.append_row(LOG_HEADER)
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def find_row_by_item_number(ws, item_number: str):
    """A列（商品管理番号）を検索し、その行番号（1始まり）を返す。見つからなければNone。"""
    try:
        cell = ws.find(item_number, in_column=COL_ITEM_NUMBER + 1)
    except gspread.exceptions.CellNotFound:
        return None
    return cell.row if cell else None


def get_price_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(PRICE_SPREADSHEET_ID).worksheet(PRICE_SHEET_NAME)


def parse_proper_price(value: str):
    """「¥4,123」のような文字列を整数4123に変換する。パースできなければNoneを返す。"""
    match = re.search(r"[\d,]+", value)
    if not match:
        return None
    try:
        return int(match.group().replace(",", ""))
    except ValueError:
        return None


def find_price_targets(values: list) -> list:
    """(行番号, 商品管理番号, 適正価格) のリストを返す。行番号は1始まり。"""
    targets = []
    for i, row in enumerate(values[1:], start=2):  # 1行目はヘッダー
        if len(row) <= COL_PROPER_PRICE:
            continue
        if row[COL_PRICE_CHECK].strip() != DEFICIT_LABEL:
            continue
        done = row[COL_PRICE_DONE].strip() if len(row) > COL_PRICE_DONE else ""
        if done:
            continue
        item_number = row[COL_ITEM_NUMBER].strip()
        if not item_number:
            continue
        proper_price = parse_proper_price(row[COL_PROPER_PRICE])
        if proper_price is None:
            continue
        if ONLY_ITEM_NUMBERS and item_number not in ONLY_ITEM_NUMBERS:
            continue
        targets.append((i, item_number, proper_price))
    return targets


# ══ 楽天：価格を更新する ══════════════════════════
def rakuten_update_price(manage_number: str, new_price: int) -> list:
    """
    2店舗を順に確認し、存在する店舗すべてでvariantsのstandardPriceを更新する。
    戻り値は [(店舗名, 結果文字列, 成功したか)] のリスト。
    """
    results = []
    for store in RAKUTEN_STORES:
        headers = rakuten_auth_headers(store)
        url = f"{RMS_BASE}/{manage_number}"

        try:
            res = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            results.append((store["name"], f"取得エラー: {e}", False))
            continue
        finally:
            time.sleep(API_INTERVAL)

        if res.status_code == 404:
            continue  # この店舗には存在しない
        if res.status_code >= 400:
            results.append((store["name"], f"取得エラー({res.status_code})", False))
            continue

        item = res.json()
        variants = item.get("variants") or {}
        if not variants:
            results.append((store["name"], "variantsが空のため更新できません", False))
            continue

        variant_updates = {}
        already_ok = True
        for variant_id, variant in variants.items():
            current_price = variant.get("standardPrice")
            try:
                current_price_int = int(float(current_price)) if current_price is not None else None
            except (TypeError, ValueError):
                current_price_int = None
            if current_price_int != new_price:
                variant_updates[variant_id] = {"standardPrice": new_price}
                already_ok = False

        if already_ok:
            results.append((store["name"], f"すでに¥{new_price:,}", True))
            continue

        if DRY_RUN:
            results.append((store["name"], f"【DRY RUN】¥{new_price:,}へ更新の対象", True))
            continue

        try:
            patch = requests.patch(url, headers=headers, json={"variants": variant_updates}, timeout=30)
            if patch.status_code == 204:
                results.append((store["name"], f"¥{new_price:,}に更新しました", True))
            else:
                results.append((store["name"], f"更新失敗({patch.status_code}) {patch.text[:200]}", False))
        except Exception as e:
            results.append((store["name"], f"更新エラー: {e}", False))
        finally:
            time.sleep(API_INTERVAL)

    if not results:
        results.append(("-", "楽天には出品が見つかりませんでした", True))
    return results


# ══ Yahoo：価格を更新する ══════════════════════════
def yahoo_check_sale_conflict(token: str, candidates: list) -> bool:
    """
    候補の商品コードを2店舗それぞれで実在確認し、特価(SalePrice)が通常価格(Price)と
    異なる＝セール中のものが1つでもあればTrueを返す。
    """
    for store in YAHOO_STORES:
        for item_code in candidates:
            try:
                item = yahoo_get_item(token, store, item_code)
            except Exception:
                continue
            finally:
                time.sleep(API_INTERVAL)

            if item is None:
                continue

            price = item.get("Price")
            sale_price = item.get("SalePrice")
            if sale_price and price and sale_price != price:
                return True
    return False


def yahoo_update_price(token: str, candidates: list, new_price: int) -> list:
    """
    候補の商品コードを2店舗それぞれで実在確認し、見つかったものすべての価格を更新する。
    updateItemsはprice更新時にsale_priceも同時指定が必要なため、両方を新価格にする
    （セール中の商品はyahoo_check_sale_conflictで事前に除外されている前提）。
    """
    results = []
    found_any = False

    for store in YAHOO_STORES:
        for item_code in candidates:
            try:
                item = yahoo_get_item(token, store, item_code)
            except Exception as e:
                results.append((store["name"], f"{item_code}: 取得エラー: {e}", False))
                continue
            finally:
                time.sleep(API_INTERVAL)

            if item is None:
                continue

            found_any = True
            current_price = item.get("Price")

            if current_price == str(new_price):
                results.append((store["name"], f"{item_code}: すでに¥{new_price:,}", True))
                continue

            if DRY_RUN:
                results.append((store["name"], f"{item_code}: 【DRY RUN】¥{current_price}→¥{new_price:,}の対象", True))
                continue

            item_field = f"item_code={item_code}&price={new_price}&sale_price={new_price}"

            try:
                # item1の値はrequestsのform-urlencode処理で自動的にパーセントエンコードされる。
                # ここで自前でquote()すると二重エンコードになりYahoo側でitem_codeが
                # 読み取れなくなる（実機で確認済み）。
                res = requests.post(
                    f"{YAHOO_BASE}/updateItems",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"seller_id": store["seller_id"], "item1": item_field},
                    timeout=30,
                )
                if res.status_code < 400 and "<Status>OK</Status>" in res.text:
                    results.append((store["name"], f"{item_code}: ¥{current_price}→¥{new_price:,}に更新しました", True))
                else:
                    results.append((store["name"], f"{item_code}: 更新失敗({res.status_code}) {res.text[:200]}", False))
            except Exception as e:
                results.append((store["name"], f"{item_code}: 更新エラー: {e}", False))
            finally:
                time.sleep(API_INTERVAL)

    if not found_any:
        results.append(("-", "Yahooには出品が見つかりませんでした", True))
    return results


def main():
    print("=== 赤字商品 適正価格 自動反映 開始 ===")
    if DRY_RUN:
        print("※ DRY RUN モード：モール側もシート側も一切変更しません")

    spreadsheet = get_spreadsheet()
    yahoo_token_state = {"token": get_yahoo_access_token(spreadsheet), "fetched_at": time.time()}
    YAHOO_TOKEN_REFRESH_SEC = 40 * 60

    def get_fresh_yahoo_token():
        if time.time() - yahoo_token_state["fetched_at"] > YAHOO_TOKEN_REFRESH_SEC:
            print("  （Yahooアクセストークンを再取得します）")
            yahoo_token_state["token"] = get_yahoo_access_token(spreadsheet)
            yahoo_token_state["fetched_at"] = time.time()
        return yahoo_token_state["token"]

    price_ws = get_price_sheet()

    header = price_ws.row_values(1)
    if len(header) <= COL_STOCK_DONE or header[COL_STOCK_DONE].strip() != "在庫対応済み":
        if price_ws.col_count <= COL_STOCK_DONE:
            price_ws.add_cols(COL_STOCK_DONE + 1 - price_ws.col_count)
        price_ws.update_cell(1, COL_STOCK_DONE + 1, "在庫対応済み")
    if len(header) <= COL_PRICE_DONE or header[COL_PRICE_DONE].strip() != "価格調整対応済み":
        if price_ws.col_count <= COL_PRICE_DONE:
            price_ws.add_cols(COL_PRICE_DONE + 1 - price_ws.col_count)
        price_ws.update_cell(1, COL_PRICE_DONE + 1, "価格調整対応済み")
        print("I列にヘッダー「価格調整対応済み」を設定しました。")

    values = price_ws.get_all_values()
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    log_rows = []

    targets = find_price_targets(values)
    print(f"\n--- 価格調整対象（🔴赤字・未対応）: {len(targets)}件 ---")
    updated = 0
    flagged = 0

    for row_num, item_number, proper_price in targets[:MAX_PER_RUN]:
        print(f"\n--- [価格調整] {item_number}（{row_num}行目） → ¥{proper_price:,} ---")

        candidates = [item_number + suffix for suffix in YAHOO_SUFFIXES]

        try:
            sale_conflict = yahoo_check_sale_conflict(get_fresh_yahoo_token(), candidates)
        except Exception as e:
            print(f"  ⚠️ Yahooセール確認でエラー: {e}。未対応のまま残します。")
            continue

        if sale_conflict:
            print(f"  🟡 Yahooで特価(セール)設定を検出。対象から除外し、人の確認に回します。")
            log_rows.append([now, "-", "価格調整", "Yahoo", "-", item_number, "セール中のためスキップ"])
            flagged += 1
            if DRY_RUN:
                print("  【DRY RUN】本番ならここでI列に「要確認(Yahooセール中)」を記録します。")
                continue
            try:
                current_row = find_row_by_item_number(price_ws, item_number)
                if current_row is not None:
                    price_ws.update_cell(current_row, COL_PRICE_DONE + 1, SALE_CONFLICT_MESSAGE)
            except Exception as e:
                print(f"  ⚠️ シート更新に失敗しました: {e}")
            continue

        all_ok = True

        for store_name, message, ok in rakuten_update_price(item_number, proper_price):
            print(f"    [楽天] {store_name}: {message}")
            log_rows.append([now, "-", "価格調整", "楽天", store_name, item_number, message])
            if not ok:
                all_ok = False

        for store_name, message, ok in yahoo_update_price(get_fresh_yahoo_token(), candidates, proper_price):
            print(f"    [Yahoo] {store_name}: {message}")
            log_rows.append([now, "-", "価格調整", "Yahoo", store_name, item_number, message])
            if not ok:
                all_ok = False

        if not all_ok:
            print("  ⚠️ 失敗があったため、未対応のまま残します（次回再挑戦）。")
            continue

        updated += 1
        if DRY_RUN:
            print("  【DRY RUN】本番ならここでI列に価格調整対応済みを記録します。")
            continue

        try:
            current_row = find_row_by_item_number(price_ws, item_number)
            if current_row is None:
                print(f"  ⚠️ シート上で{item_number}が見つからず、I列を更新できませんでした（モール側は更新済み）。")
            else:
                price_ws.update_cell(current_row, COL_PRICE_DONE + 1, now)
                print(f"  ✅ I列（{current_row}行目）に価格調整対応済み（{now}）を記録しました。")
        except Exception as e:
            print(f"  ⚠️ シート更新に失敗しました（モール側は更新済み）: {e}")

    try:
        append_log(spreadsheet, log_rows)
        print(f"\nログを「{LOG_SHEET_NAME}」タブに{len(log_rows)}行追記しました。")
    except Exception as e:
        print(f"ログ書き込みに失敗しました: {e}")

    print(f"\n=== 完了: 価格更新 {updated}件 / セール中で保留 {flagged}件 "
          f"（残り{len(targets) - min(len(targets), MAX_PER_RUN)}件） ===")
    print("=== 赤字商品 適正価格 自動反映 完了 ===")


if __name__ == "__main__":
    main()
