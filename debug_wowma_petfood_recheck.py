"""
一時調査用（ローカル実行専用。WowmaのAPIはIP制限がありGitHub Actionsからは呼べない）:
Greenies・鳥の餌系で、Wowma（LA Express）にも見落としがないか全件走査して調べる。
"""

import re

from case_orders_wowma import wowma_search_items

PATTERNS = [
    r"Greenies", r"グリニーズ",
    r"Milk-Bone", r"ミルクボーン",
    r"Sunseed", r"サンシード",
    r"Degu", r"デグー",
    r"鳥の餌", r"鳥用.{0,5}(餌|フード|飼料)", r"バードフード", r"Bird Food",
    r"インコ.{0,5}(餌|フード)", r"小鳥.{0,5}(餌|フード)", r"文鳥.{0,5}(餌|フード)",
    r"Vitakraft", r"Kaytee", r"ケイティー", r"ZuPreem", r"ズプリーム",
    r"Parrot Food", r"オウムフード", r"オウム.{0,3}フード",
]
NOISE = re.compile(r"フード付き|フードフィーダー|餌箱|えさやり器|ディスペンサー")
pattern_re = re.compile("|".join(PATTERNS), re.IGNORECASE)


def main():
    start = 1
    total_hits = []
    scanned = 0
    while True:
        try:
            items, max_count = wowma_search_items(start, 500)
        except Exception as e:
            print(f"  [ページ {start} エラー: {e} → スキップして次へ]")
            start += 500
            if start > scanned + 5000:
                break
            continue
        if start == 1:
            print(f"[Wowma] 総商品数: {max_count}")
        if not items:
            break
        for item in items:
            name = item.get("itemName", "")
            if pattern_re.search(name) and not NOISE.search(name):
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
