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

import gspread

# シートに書き出す列。ヘッダー名で場所を決めるので、既存の列を壊さない。
ANNOTATION_HEADERS = [
    "楽天販売個数",
    "抽出パターン",
    "ASIN入数",
    "購入倍率",
    "手修正倍率",   # 人が入れる欄。スクリプトは書き込まない
    "Amazon商品名",
]

# 数量を表しそうな単位
UNIT_PATTERN = r"個|本|袋|箱|缶|パック|pack|Pack|PACK|セット|set|Set|SET|入|枚|包|瓶"

FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 「3個セット」→ N個セット のように、当たったパターンの名前も返す。
# シートに書き出して、推測の妥当性を人が確認できるようにするため。
#
# 「セット×N」「×N」は末尾アンカー($)と単位語を必須にしている。理由:
# 2026-08-28、家具の寸法（「22 x 16 x 22」）や写真フレームのサイズ（「4x6」）、
# 型番（「セット X27」）を誤って数量として拾ってしまうケースが実データで多数見つかった
# （「×N」該当11件中10件がこの手の誤検出）。末尾＋単位語必須にすることで、
# 「28.3ｇ　×40パック」のような本物の複数個セット表記だけを拾うようにする。
PATTERNS = [
    (rf"(\d+)\s*(?:{UNIT_PATTERN})\s*(?:セット|SET|set|Set)", "N個セット"),
    (r"(?:セット|SET|set)\s*[×xX]\s*(\d+)\s*(?:個|本|袋|箱|缶|パック)?\s*$", "セット×N"),
    (r"[×xX]\s*(\d+)\s*(?:個|本|袋|箱|缶|パック)\s*$", "×N"),
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


def ensure_annotation_columns(sheet, header: list, extra_headers: list = None) -> dict:
    """
    書き出し用の列の位置をヘッダー名から決める。無ければ右端に作る。
    シートの列数が足りない場合は先に広げる（get_all_values は値のある範囲しか
    返さないため、実際の列数と食い違うことがある）。
    """
    positions = {}
    next_col = len(header)
    to_add = []

    for name in ANNOTATION_HEADERS + (extra_headers or []):
        if name in header:
            positions[name] = header.index(name)
        else:
            positions[name] = next_col
            to_add.append((next_col, name))
            next_col += 1

    if to_add:
        print(f"追加する列: {[n for _, n in to_add]}")
        if sheet.col_count < next_col:
            print(f"  列数を {sheet.col_count} → {next_col} に拡張します")
            sheet.add_cols(next_col - sheet.col_count)
        sheet.batch_update([
            {"range": gspread.utils.rowcol_to_a1(1, col + 1), "values": [[name]]}
            for col, name in to_add
        ])

    return positions


def compute_pkg_and_ratio(name: str, product) -> tuple:
    """
    商品名とKeepa商品情報から (ASIN入数, 購入倍率) を計算する。
    原価計算（rakuten_price_check.judge）とシート書き出し（annotation_updates）の
    両方から呼ばれる共通ロジック。product が None なら ("", "") を返す。
    """
    qty, pattern, _ = extract_quantity(name)
    pkg = package_quantity(product)
    # 「N個」「N個入」は出品者が複数個をセットにしたのではなく、商品自体の梱包を
    # 説明した表記（例: 「30個入り」＝ASIN自体が30粒入りパッケージ）であるため、
    # 実際の購入倍率は常に1とみなす。「N個セット」「セット×N」「×N」（出品者が
    # 複数パックをまとめて売っている表記）とは区別する。2026-08-25、ユーザー確認済み。
    if pattern in ("N個", "N個入"):
        ratio = 1
    else:
        ratio = round(qty / pkg, 3)
    return pkg, ratio


def annotation_updates(positions: dict, sheet_row: int, name: str, product) -> list:
    """
    1行ぶんの書き出し内容を batch_update 用の形で返す。

    product は Keepa の商品情報。None の場合（Keepaにデータが無いASIN）は
    取り違えや廃盤に気づけるよう、その旨をAmazon商品名の列に残す。
    """
    qty, pattern, _ = extract_quantity(name)

    if product is None:
        pkg, ratio, title = "", "", "（Keepaにデータなし）"
    else:
        pkg, ratio = compute_pkg_and_ratio(name, product)
        title = (product.get("title") or "")[:120]

    def cell(col_key):
        return gspread.utils.rowcol_to_a1(sheet_row, positions[col_key] + 1)

    return [
        {"range": cell("楽天販売個数"), "values": [[qty]]},
        {"range": cell("抽出パターン"), "values": [[pattern]]},
        {"range": cell("ASIN入数"), "values": [[pkg]]},
        {"range": cell("購入倍率"), "values": [[ratio]]},
        {"range": cell("Amazon商品名"), "values": [[title]]},
    ]


def package_quantity(product: dict) -> int:
    """
    Keepaの商品情報から「1購入あたりの入数」を返す。判別できなければ 1。

    packageQuantity は「梱包（パッケージ）の数」、numberOfItems は「実際の個数」を
    表すことが多く、単品梱包の商品では packageQuantity=1・numberOfItems=実個数
    となってズレることがある（例: "30 Count (Pack of 1)" は packageQuantity=1、
    numberOfItems=30。2026-08-25、ロリポップ商品で実際に発見・購入倍率が誤って
    30になっていた）。numberOfItems を優先する。
    """
    for key in ("numberOfItems", "packageQuantity"):
        value = product.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 1
