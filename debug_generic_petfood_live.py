"""
一時調査用: ブランド名に頼らない広いスキャン。
「動物を指す語」×「餌・おやつ・サプリ・ヘアケアを指す語」の組み合わせで
Yahoo・楽天の出品スナップショットから候補を抽出し、さらにAPIで「今も実際に公開中か」を
確認して、公開中のものだけを出力する（スナップショットは古いことがあるため）。
"""

import os
import re
import json
import time

import gspread
import requests
from google.oauth2.service_account import Credentials

from case_orders_auto_close import (
    get_rakuten_stores,
    rakuten_auth_headers,
    RMS_BASE,
    get_yahoo_access_token,
    get_yahoo_stores,
    yahoo_get_item,
)

LISTING_SPREADSHEET_ID = os.environ["RAKUTEN_LISTING_SPREADSHEET_ID"]

ANIMAL = re.compile(
    r"鳥|バード|bird|インコ|オウム|フィンチ|カナリア|小鳥|野鳥|犬|猫|ドッグ|キャット|dog|cat|pet|ペット|"
    r"小動物|ハムスター|うさぎ|ウサギ|rabbit|モルモット|guinea|フェレット|ferret|デグー|degu|チンチラ|"
    r"爬虫類|reptile|亀|turtle|トカゲ|lizard|魚|fish|金魚|熱帯魚|馬|horse|鶏|チキンフィード|マーモセット|"
    r"parrot|parakeet|cockatiel|finch|canary|hamster|gerbil|ジャービル|リス|squirrel",
    re.IGNORECASE,
)
FOODISH = re.compile(
    r"フード|餌|エサ|えさ|おやつ|トリーツ|treat|food|ペレット|pellet|シード|seed|ダイエット|diet|"
    r"サプリ|supplement|ビタミン|vitamin|シャンプー|shampoo|コンディショナー|conditioner|"
    r"ケアスプレー|グルーミングスプレー|毛づや|デンタルチュー|チュー|chew|ジャーキー|jerky|ビスケット",
    re.IGNORECASE,
)
NOISE = re.compile(
    r"フィーダー|feeder|ディスペンサー|dispenser|ボウル|bowl|皿|食器|おもちゃ|トイ|toy|ベッド|bed|ハウス|house|"
    r"ケージ|cage|カバー|cover|首輪|collar|リード|leash|服|レインコート|raincoat|フード付き|hooded|hood|"
    r"回し車|ホイール|wheel|キャリー|carrier|トレイ|tray|マット|mat|クッション|床材|bedding|砂|litter|"
    r"ブラシ|brush|クリッパー|clipper|爪切り|ハーネス|harness|ポーチ|pouch|メーカー|maker|"
    r"スタンド|stand|止まり木|perch|水槽|aquarium|フィルター|filter|ポンプ|pump|ヒーター|heater|"
    r"カーペット|carpet|ペットボトル|bottle|ペットシーツ|シーツ|消臭|クリーナー|cleaner|"
    r"キャットフード缶切り|キャットタワー|タワー|爪とぎ|スクラッチ|scratch",
    re.IGNORECASE,
)


def get_spreadsheet():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds).open_by_key(LISTING_SPREADSHEET_ID)


def load_candidates(ss, sheet_name, code_col_name):
    rows = ss.worksheet(sheet_name).get_all_values()
    header = rows[0]
    c_shop = header.index("店舗名")
    c_code = header.index(code_col_name)
    c_name = header.index("商品名")
    out = []
    for row in rows[1:]:
        if len(row) <= c_name:
            continue
        name = row[c_name]
        if ANIMAL.search(name) and FOODISH.search(name) and not NOISE.search(name):
            out.append((row[c_shop], row[c_code], name))
    return out


def main():
    ss = get_spreadsheet()

    yahoo_cands = load_candidates(ss, "Yahoo_出品データ", "商品コード")
    rakuten_cands = load_candidates(ss, "楽天_出品データ", "商品管理番号")
    print(f"スナップショット上の候補: Yahoo {len(yahoo_cands)}件 / 楽天 {len(rakuten_cands)}件")

    # ── Yahoo: 今も存在するか ──
    token_state = {"token": get_yahoo_access_token(ss), "t": time.time()}
    stores = {s["name"]: s for s in get_yahoo_stores()}
    live_yahoo = []
    for shop, code, name in yahoo_cands:
        if time.time() - token_state["t"] > 300:
            token_state["token"] = get_yahoo_access_token(ss)
            token_state["t"] = time.time()
        store = stores.get(shop)
        if store is None:
            print(f"  [SKIP] 店舗名が一致しません: {shop!r}（候補 {code}）")
            continue
        try:
            item = yahoo_get_item(token_state["token"], store, code)
        except Exception as e:
            print(f"  [ERR] {shop}/{code}: {e}")
            continue
        finally:
            time.sleep(1.0)
        if item is not None:
            live_yahoo.append((shop, code, name))

    print(f"\n=== Yahoo 今も存在（要確認）: {len(live_yahoo)}件 ===")
    for shop, code, name in live_yahoo:
        print(f"{shop}\t{code}\t{name}")

    # ── 楽天: 今も公開中（hideItem != true）か ──
    rstores = {s["name"]: s for s in get_rakuten_stores()}
    live_rakuten = []
    seen = set()
    for shop, code, name in rakuten_cands:
        key = (shop, code)
        if key in seen:
            continue
        seen.add(key)
        store = rstores.get(shop)
        if store is None:
            print(f"  [SKIP] 店舗名が一致しません: {shop!r}（候補 {code}）")
            continue
        try:
            res = requests.get(f"{RMS_BASE}/{code}", headers=rakuten_auth_headers(store), timeout=30)
        except Exception as e:
            print(f"  [ERR] {shop}/{code}: {e}")
            continue
        finally:
            time.sleep(1.0)
        if res.status_code == 404:
            continue
        if res.status_code >= 400:
            print(f"  [ERR] {shop}/{code}: status={res.status_code}")
            continue
        if res.json().get("hideItem") is not True:
            live_rakuten.append((shop, code, name))

    print(f"\n=== 楽天 今も公開中（要確認）: {len(live_rakuten)}件 ===")
    for shop, code, name in live_rakuten:
        print(f"{shop}\t{code}\t{name}")


if __name__ == "__main__":
    main()
