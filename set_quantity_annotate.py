"""
「ASINあり」シートに、セット数・ASIN入数・購入倍率・Amazon商品名を書き出す（可視化のみ）。

目的:
  rakuten_price_check.py の原価計算は「ASIN価格 = 1個ぶんの価格」という前提で作られているが、
  実際には ASIN が複数個入りのこともある（例: スピードスティックは Pack of 6 で、
  楽天では単品・3個・6個に分けて売っている）。そのため単品が不当に赤字判定されていた。

  正しくは「楽天で売る個数 ÷ ASIN入数 = 必要な購入量」で原価を出す必要がある。
  ただしこの倍率をいきなり判定に反映すると、20%近い行の赤字判定が一斉に変わるため、
  まずシートに書き出して人が妥当性を確認できるようにする。

  このスクリプトは適正価格や赤字判定を書き換えない。追加した列に情報を足すだけ。

書き出す列（ヘッダー名で場所を決めるので、既存の列を壊さない）:
  楽天販売個数   … 商品名から推測した個数
  抽出パターン   … どの表記で当たったか（推測の妥当性を人が確認するため）
  ASIN入数       … Keepa の packageQuantity
  購入倍率       … 楽天販売個数 ÷ ASIN入数
  手修正倍率     … 人が入れる欄。スクリプトは書き込まない
  Amazon商品名   … 楽天の商品名と見比べて、ASINの取り違えに気づけるようにする

処理量が多いため、まだ「ASIN入数」が空の行だけを対象にして少しずつ進める（再実行で続きから）。
"""

import os
import time
import json

import requests
import gspread
from google.oauth2.service_account import Credentials

from set_quantity import (
    extract_quantity,
    ensure_annotation_columns,
    annotation_updates,
)

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"
KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]

BATCH_SIZE = 100
BATCHES_PER_RUN = int(os.environ.get("BATCHES_PER_RUN", "10"))  # 1回の実行で1,000件

# セット表記のある行だけに絞るか。Keepaトークンを節約したいときに使う
ONLY_SETS = os.environ.get("ONLY_SETS", "true").lower() == "true"

# トークンが尽きたときに補充を待つ上限（秒）。GitHub Actionsのジョブ上限は6時間
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "14400"))

COL_ITEM_ID = 0
COL_NAME = 1
COL_ASIN = 2

def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def fetch_keepa_batch(asins: list):
    """Keepaバッチ取得。失敗時は None（Noneと空辞書を区別して誤書き込みを防ぐ）"""
    url = (
        f"https://api.keepa.com/product?key={KEEPA_API_KEY}"
        f"&domain=1&asin={','.join(asins)}"
    )
    try:
        res = requests.get(url, timeout=60)
        res.raise_for_status()
        return {p["asin"]: p for p in res.json().get("products", [])}
    except Exception as e:
        print(f"Keepa APIエラー: {e}")
        return None


def get_tokens_left() -> int:
    try:
        res = requests.get(f"https://api.keepa.com/token?key={KEEPA_API_KEY}", timeout=10)
        res.raise_for_status()
        return res.json().get("tokensLeft", -1)
    except Exception:
        return -1


def main():
    print("=== セット数・ASIN入数の書き出し 開始 ===")
    sheet = get_sheet()
    all_rows = sheet.get_all_values()
    header = all_rows[0]
    rows = all_rows[1:]
    print(f"総行数: {len(rows)}")

    pos = ensure_annotation_columns(sheet, header)
    col_pkg = pos["ASIN入数"]  # 処理済みかどうかの判定に使う

    targets = []
    for i, row in enumerate(rows):
        sheet_row = i + 2  # ヘッダー1行ぶん + 1始まり
        asin = row[COL_ASIN].strip() if len(row) > COL_ASIN else ""
        if not asin:
            continue
        # すでに書き出し済みの行はとばす（再実行で続きから進められる）
        if len(row) > col_pkg and row[col_pkg].strip():
            continue

        name = row[COL_NAME] if len(row) > COL_NAME else ""
        qty, _, _ = extract_quantity(name)
        if ONLY_SETS and qty == 1:
            continue
        targets.append((sheet_row, asin, name))

    print(f"未処理の対象: {len(targets)}件（ONLY_SETS={ONLY_SETS}）")
    if not targets:
        print("対象なし。終了。")
        return

    targets = targets[:BATCH_SIZE * BATCHES_PER_RUN]
    print(f"今回処理: {len(targets)}件\n")

    for start in range(0, len(targets), BATCH_SIZE):
        # トークンが尽きたら、その場で補充を待つ（Keepaは毎分61トークン補充される）。
        # 途中終了して再実行するより、1回の実行で進めたほうが手間が少ないため。
        waited = 0
        tokens = get_tokens_left()
        while 0 <= tokens < BATCH_SIZE and waited < MAX_WAIT_SECONDS:
            print(f"  トークン残量 {tokens}。補充を待ちます（経過 {waited // 60}分）...")
            time.sleep(60)
            waited += 60
            tokens = get_tokens_left()

        print(f"Keepaトークン残量: {tokens}")
        if 0 <= tokens < BATCH_SIZE:
            print("待っても足りませんでした。ここで終了します（次回、続きから再開できます）。")
            break

        batch = targets[start:start + BATCH_SIZE]
        asins = sorted({a for _, a, _ in batch})
        print(f"Keepa取得: {len(asins)}ASIN（{len(batch)}行ぶん）")

        products = fetch_keepa_batch(asins)
        if products is None:
            print("Keepaエラーのため、このバッチはスキップします（空欄のまま＝次回再取得）。")
            break

        updates = []
        for sheet_row, asin, name in batch:
            updates += annotation_updates(pos, sheet_row, name, products.get(asin))

        # 1リクエストにまとめて書く（Sheetsの書き込み回数制限対策）
        sheet.batch_update(updates)
        print(f"  {len(batch)}行を書き込みました")

        if start + BATCH_SIZE < len(targets):
            time.sleep(5)

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
