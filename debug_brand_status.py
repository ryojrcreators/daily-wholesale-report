"""一時調査用（読み取りのみ）: 指定ブランド（NAME_REGEX）の商品について、楽天2店舗とYahoo2店舗の公開状態を一覧にする。"""
import json, os, re, time
import gspread, requests
from google.oauth2.service_account import Credentials
from case_orders_auto_close import (
    RMS_BASE, get_rakuten_stores, get_yahoo_access_token, get_yahoo_stores, rakuten_auth_headers, yahoo_get_item,
)

RX = re.compile(os.environ["NAME_REGEX"], re.IGNORECASE)

def rows(ss, name, code_col):
    r = ss.worksheet(name).get_all_values(); h = r[0]
    i_s, i_c, i_n = h.index("店舗名"), h.index(code_col), h.index("商品名")
    return [(x[i_s], x[i_c], x[i_n]) for x in r[1:] if len(x) > i_n and RX.search(x[i_n])]

creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
ss = gspread.authorize(creds).open_by_key(os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"])

rh = rows(ss, "楽天_出品データ", "商品管理番号")
codes = sorted({c for _, c, _ in rh}); names = {c: n for _, c, n in rh}
print(f"[楽天] 該当 {len(rh)}行 / ユニーク商品 {len(codes)}件")
stores = get_rakuten_stores()
print("\n商品管理番号\t1店舗目(Americana)\t2店舗目(Founder)\t商品名")
for c in codes:
    st = []
    for s in stores:
        res = requests.get(f"{RMS_BASE}/{c}", headers=rakuten_auth_headers(s), timeout=30); time.sleep(1.0)
        st.append("なし" if res.status_code == 404 else (f"ERR{res.status_code}" if res.status_code >= 400
                  else ("非公開" if res.json().get("hideItem") is True else "公開中")))
    print(f"{c}\t{st[0]}\t{st[1]}\t{names[c][:60]}")

yh = rows(ss, "Yahoo_出品データ", "商品コード")
print(f"\n[Yahoo] 該当 {len(yh)}行")
token, t0 = get_yahoo_access_token(ss), time.time()
ys = {s["name"]: s for s in get_yahoo_stores()}
print("\n商品コード\tDisplay\tEditingFlag\t在庫\t商品名")
for shop, code, name in sorted(yh, key=lambda x: (x[1], x[0])):
    if time.time() - t0 > 300: token, t0 = get_yahoo_access_token(ss), time.time()
    it = yahoo_get_item(token, ys[shop], code) if shop in ys else None; time.sleep(1.0)
    it = it or {}
    print(f"{code}\t{it.get('Display','なし')}\t{it.get('EditingFlag','-')}\t{it.get('Quantity','-')}\t{name[:50]}")
