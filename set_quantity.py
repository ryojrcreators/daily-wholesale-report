"""
楽天の商品名から「その出品で何個売っているか」を推測する。

原価計算では、これを Keepa の packageQuantity（ASIN 1購入あたりの入数）で割った値が
必要な購入量になる。

  必要な購入量 = 楽天で売る個数 ÷ ASIN 1購入あたりの入数

例（実データで確認済み）:
  スピードスティック単品   楽天1個 ÷ ASIN6個入 = 0.17購入  ← 6本パックを分けて売っている
  同 3個セット             楽天3個 ÷ ASIN6個入 = 0.5購入
  ティックタック12個セット  楽天12個 ÷ ASIN12個入 = 1購入   ← Amazon側が12個入りの箱
  オールドスパイス2本セット 楽天2個 ÷ ASIN1個入 = 2購入

「N個セット」だから掛ける、という単純な判断ができないのはこのため。
商品名から取れるのはあくまで「楽天で売る個数」であって、原価の倍率ではない。
"""

import re

# 数量を表しそうな単位
UNIT_PATTERN = r"個|本|袋|箱|缶|パック|pack|Pack|PACK|セット|set|Set|SET|入|枚|包|瓶"

FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 「3個セット」→ N個セット のように、当たったパターンの名前も返す。
# シートに書き出して、推測の妥当性を人が確認できるようにするため。
PATTERNS = [
    (rf"(\d+)\s*(?:{UNIT_PATTERN})\s*(?:セット|SET|set|Set)", "N個セット"),
    (r"(?:セット|SET|set)\s*[×xX]\s*(\d+)", "セット×N"),
    (r"[×xX]\s*(\d+)\s*(?:個|本|袋|箱|缶|パック)?\s*$", "×N"),
    (rf"(\d+)\s*(?:{UNIT_PATTERN})\s*入", "N個入"),
    (rf"(\d+)\s*(?:個|本|パック|缶|袋)(?![ぁ-んァ-ヶ一-龠])", "N個"),
]

# 数量として妥当な範囲。容量表記（14oz など）を数量と誤認しないための下限・上限
MIN_QTY = 2
MAX_QTY = 50


def extract_quantity(name: str):
    """
    商品名から販売個数を推測する。
    戻り値は (個数, パターン名, 一致した文字列)。見つからなければ (1, "", "")。
    """
    if not name:
        return 1, "", ""

    text = name.translate(FULLWIDTH_DIGITS)
    for pattern, label in PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            qty = int(m.group(1))
        except ValueError:
            continue
        if MIN_QTY <= qty <= MAX_QTY:
            return qty, label, m.group(0)
    return 1, "", ""


def package_quantity(product: dict) -> int:
    """
    Keepaの商品情報から「1購入あたりの入数」を返す。判別できなければ 1。

    packageQuantity と numberOfItems は同じ値を返すことが多いが、
    未設定だと -1 が入るため、有効な値だけを採用する。
    """
    for key in ("packageQuantity", "numberOfItems"):
        value = product.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 1
