"""
一時調査用（ローカル実行専用。WowmaのAPIはIP制限がありGitHub Actionsからは呼べない）:
楽天・Yahooで確定した「9/30出品禁止カテゴリ」該当商品が、Wowma（LA Express）にも
出品されているか、searchItemInfosで全件走査して調べる。
実際の削除・販売終了はまだ行わない。
"""

import re

from case_orders_wowma import wowma_search_items

PATTERNS = [
    r"Keystone Pantry", r"ブラウンライスシロップ",
    r"Jovial", r"カッペリーニ", r"玄米ショートパスタ",
    r"Minsley",
    r"玄米茶", r"Genmaicha",
    r"ライスクリスプ", r"Rice Crisps",
    r"発芽玄米プロテイン", r"Sprouted.{0,3}Brown Rice Protein",
    r"Carna4",
    r"Pet Head",
    r"チョウメイン.{0,10}テリヤキビーフ", r"Teriyaki Beef",
    r"ビーフボーン.{0,3}ブロスパウダー", r"Bone Broth.{0,3}Beef",
    r"トップラーメン.{0,10}ビーフ", r"スターフライ.{0,10}ビーフ",
    r"ビーフパスタ.{0,5}マリナーラ", r"ビーフストロガノフ", r"スリー.{0,3}ビーン.{0,5}チリ.{0,3}マック",
    r"テリヤキライス.{0,10}ビーフ",
    r"炒めごはん.{0,10}スパイシービーフ",
    r"Heinz.{0,10}グレービー", r"Beef Gravy",
    r"牛肉風味.{0,10}代用肉",
]
pattern_re = re.compile("|".join(PATTERNS), re.IGNORECASE)


def main():
    start = 1
    total_hits = []
    scanned = 0
    while True:
        items, max_count = wowma_search_items(start, 500)
        if start == 1:
            print(f"[Wowma] 総商品数: {max_count}")
        if not items:
            break
        for item in items:
            name = item.get("itemName", "")
            if pattern_re.search(name):
                total_hits.append((item.get("itemCode", ""), name))
        scanned += len(items)
        print(f"  {scanned}/{max_count} 件走査済み...")
        if scanned >= max_count:
            break
        start += 500

    print(f"\n=== [Wowma] 該当: {len(total_hits)}件（走査{scanned}件） ===")
    for code, name in total_hits:
        print(f"{code}\t{name}")


if __name__ == "__main__":
    main()
