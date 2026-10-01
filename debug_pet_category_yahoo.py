"""
一時調査用: Yahooのストアカテゴリのうち「ペット系」の枝（その配下すべて）に属する
現在の出品商品を、カテゴリ経由で全部洗い出す（商品名のキーワードに依存しない）。
"""

import re
import time
from xml.etree import ElementTree

import yahoo_listing_sync as ys

PET_CAT = re.compile(
    r"ペット|犬|猫|鳥|バード|bird|小動物|魚|アクア|爬虫|ドッグ|キャット|dog|cat|pet|動物|ハムスター|うさぎ|フェレット",
    re.IGNORECASE,
)
NOISE = re.compile(
    r"フィーダー|feeder|ディスペンサー|dispenser|ボウル|bowl|皿|食器|おもちゃ|玩具|トイ|toy|ベッド|bed|ハウス|house|"
    r"ケージ|cage|カバー|首輪|collar|リード|leash|コスチューム|costume|レインコート|回し車|キャリー|carrier|"
    r"トレイ|マット|クッション|床材|砂|litter|ブラシ|brush|クリッパー|爪切り|ハーネス|ポーチ|スタンド|止まり木|"
    r"水槽|フィルター|ポンプ|ヒーター|消臭|クリーナー|タワー|爪とぎ|トング|カメラ|デバイス",
    re.IGNORECASE,
)


def walk_categories(spreadsheet, token_state, store):
    """(page_key, name, in_pet) の一覧。親が「ペット系」ならその配下はすべて in_pet。"""
    out = []
    printed = {"first": False}

    def walk(page_key, parent_in_pet, depth):
        params = {"seller_id": store["seller_id"]}
        if page_key:
            params["page_key"] = page_key
        res = ys.authed_get(spreadsheet, token_state, ys.STCAT_LIST_URL, params, store["name"], "stCategoryList")
        root = ElementTree.fromstring(res.content)
        for result in root.findall("Result"):
            key = (result.findtext("PageKey") or "").strip()
            if not key:
                continue
            name = (result.findtext("Name") or result.findtext("CategoryName") or "").strip()
            if not printed["first"]:
                printed["first"] = True
                print("  [構造確認] Resultの子要素:", [c.tag for c in result])
            in_pet = parent_in_pet or bool(PET_CAT.search(name) or PET_CAT.search(key))
            out.append((key, name, in_pet, depth))
            time.sleep(ys.PAGE_INTERVAL)
            walk(key, in_pet, depth + 1)

    walk(None, False, 0)
    return out


def main():
    spreadsheet = ys.get_spreadsheet()
    token_state = ys.TokenState(ys.refresh_access_token(spreadsheet))

    for store in ys.STORES:
        print(f"\n######## {store['name']} ########")
        cats = walk_categories(spreadsheet, token_state, store)
        pet_cats = [c for c in cats if c[2]]
        print(f"全カテゴリ数: {len(cats)} / ペット系（配下含む）: {len(pet_cats)}")
        for key, name, _, depth in pet_cats:
            print(f"  {'  ' * depth}{key}\t{name}")

        items_by_code = {}
        cat_of = {}
        for key, name, _, _ in pet_cats:
            before = set(items_by_code)
            ys.fetch_items_in_category(spreadsheet, token_state, store, key, items_by_code)
            for code in set(items_by_code) - before:
                cat_of[code] = f"{key}:{name}"

        print(f"\n=== [{store['name']}] ペット系カテゴリの現在の商品: {len(items_by_code)}件 ===")
        real, noise = [], []
        for code, row in items_by_code.items():
            line = f"{code}\t在庫{row[4]}\t{row[2][:70]}\t[{cat_of.get(code, '')}]"
            (noise if NOISE.search(row[2]) else real).append(line)
        print(f"--- 要確認（ノイズ語を含まない）: {len(real)}件 ---")
        for line in sorted(real):
            print(line)
        print(f"--- 参考（給餌器・おもちゃ等のノイズ語を含む）: {len(noise)}件 ---")
        for line in sorted(noise):
            print(line)


if __name__ == "__main__":
    main()
