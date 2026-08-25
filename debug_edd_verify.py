"""一時デバッグ: availabilityAmazonDelay（時間単位と推測）から計算したEDDが
実際のAmazon商品ページの表示と合っているか、人が見比べられるようにサンプルを出す。"""
import os
import json
from datetime import datetime, timedelta, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["RAKUTEN_SPREADSHEET_ID"]
SHEET_NAME = "ASINあり"
KEEPA_API_KEY = os.environ["KEEPA_API_KEY"]

JST = timezone(timedelta(hours=9))
SAMPLE_ROWS = 1500   # シート上から何行ぶんASINを集めるか
MAX_SAMPLES_TO_SHOW = 15


def get_spreadsheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def fetch_batch(asins):
    res = requests.get(
        "https://api.keepa.com/product",
        params={"key": KEEPA_API_KEY, "domain": 1, "asin": ",".join(asins), "stats": 1},
        timeout=60,
    )
    res.raise_for_status()
    return {p["asin"]: p for p in res.json().get("products", [])}


def main():
    sheet = get_spreadsheet().worksheet(SHEET_NAME)
    header = sheet.row_values(1)
    rows = sheet.get_all_values()[1:1 + SAMPLE_ROWS]
    asin_col = header.index("ASIN") if "ASIN" in header else 2
    name_col = header.index("商品名") if "商品名" in header else 1

    asin_to_name = {}
    for row in rows:
        asin = row[asin_col].strip() if len(row) > asin_col else ""
        if asin and asin not in asin_to_name:
            asin_to_name[asin] = row[name_col].strip() if len(row) > name_col else ""

    asins = list(asin_to_name.keys())
    print(f"サンプル対象ASIN数: {len(asins)}")

    found = []
    now_jst = datetime.now(JST)

    for start in range(0, len(asins), 100):
        batch = asins[start:start + 100]
        try:
            products = fetch_batch(batch)
        except Exception as e:
            print(f"  Keepaエラー: {e}")
            continue
        for asin, p in products.items():
            delay = p.get("availabilityAmazonDelay")
            avail = p.get("availabilityAmazon")
            if delay and isinstance(delay, list) and any(v and v > 0 for v in delay):
                found.append((asin, avail, delay, p.get("title") or ""))
        if len(found) >= MAX_SAMPLES_TO_SHOW:
            break

    print(f"\navailabilityAmazonDelayが設定されている商品: {len(found)}件見つかりました（最大{MAX_SAMPLES_TO_SHOW}件表示）\n")

    for asin, avail, delay, title in found[:MAX_SAMPLES_TO_SHOW]:
        lo, hi = delay[0], delay[-1]
        lo_date = (now_jst + timedelta(hours=lo)).strftime("%Y/%m/%d") if lo else "?"
        hi_date = (now_jst + timedelta(hours=hi)).strftime("%Y/%m/%d") if hi else "?"
        print(f"ASIN: {asin}  availabilityAmazon={avail}  delay(hours)={delay}")
        print(f"  推定EDD（時間単位と仮定）: {lo_date} 〜 {hi_date}")
        print(f"  商品名（社内シート）: {asin_to_name.get(asin, '')[:80]}")
        print(f"  Amazon商品ページ: https://www.amazon.com/dp/{asin}")
        print()


if __name__ == "__main__":
    main()
