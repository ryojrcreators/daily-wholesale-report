"""商品データに明記された属性だけを抽出する。外部通信・AIによる推測は行わない。"""
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
import unicodedata


GROUPS = {
    "brand": ("ブランド", "ブランド名", "Brand", "Brand Name"),
    "model": ("型番", "メーカー型番", "Model", "Model Number", "MPN"),
    "color": ("色", "カラー", "カラー名", "代表カラー", "Color", "Colour"),
    "volume": ("容量", "内容量", "Volume", "Net Volume", "Net Content"),
    "weight": ("重量", "本体重量", "商品重量", "Weight", "Net Weight"),
    "count": ("個数", "入数", "入り数", "Count", "Pack Size"),
    "total_count": ("総個数", "総数量"),
    "size": ("サイズ", "Size"),
}
UNITS = {
    "ml": ("volume", Decimal("1")), "l": ("volume", Decimal("1000")),
    "mg": ("weight", Decimal("0.001")), "g": ("weight", Decimal("1")),
    "kg": ("weight", Decimal("1000")),
    "mm": ("length", Decimal("1")), "cm": ("length", Decimal("10")),
    "m": ("length", Decimal("1000")),
}
NUMERIC_TYPES = {"NUMBER", "NUMERIC", "INTEGER", "DECIMAL", "INT", "FLOAT"}


def normalize(value):
    return unicodedata.normalize("NFKC", str(value)).strip()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        if not self.ignored:
            if tag in ("p", "div", "li", "tr", "br", "h1", "h2", "h3"):
                self.parts.append("\n")
            elif tag in ("td", "th"):
                self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)
        if not self.ignored and tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def plain_text(value):
    parser = _Text()
    parser.feed(str(value or ""))
    return normalize("".join(parser.parts))


def _family(name):
    key = normalize(name).casefold()
    for family, aliases in GROUPS.items():
        if key in {alias.casefold() for alias in aliases}:
            return family, aliases
    return None, (name,)


def _clean(value):
    value = normalize(value)
    if value.casefold() in ("", "-", "不明", "unknown", "n/a", "未設定"):
        return None
    if re.search(r"[,/、|]|または|\bor\b", value, re.I):
        return None
    return value


def _has_value(value):
    return value is not None and normalize(value).casefold() not in ("", "-", "不明", "unknown", "n/a", "未設定")


def _format(value, definition, unit, family):
    value = _clean(value)
    if value is None:
        return None
    numeric = str(definition.get("dataType", "")).upper() in NUMERIC_TYPES
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+|個|本|枚|袋|箱|錠|粒|パック|セット|pcs?)?", value, re.I)
    if unit or numeric or family in ("count", "total_count"):
        if not match:
            return None
        number, source_unit = Decimal(match[1]), (match[2] or "").casefold()
        target = normalize(unit or "").casefold()
        if source_unit in UNITS and target in UNITS:
            source_kind, source_factor = UNITS[source_unit]
            target_kind, target_factor = UNITS[target]
            if source_kind != target_kind:
                return None
            number = number * source_factor / target_factor
        elif source_unit != target:
            count_units = {"個", "本", "枚", "袋", "箱", "錠", "粒", "パック", "セット", "pc", "pcs"}
            if (family not in ("count", "total_count")
                    or source_unit not in count_units | {""}
                    or target not in count_units | {""}
                    or (source_unit and target and source_unit != target)):
                return None
        if family in ("count", "total_count") and (number <= 0 or number != number.to_integral_value()):
            return None
        for key, comparison in (("minValue", lambda a, b: a < b), ("maxValue", lambda a, b: a > b)):
            if definition.get(key) is not None:
                try:
                    if comparison(number, Decimal(str(definition[key]))):
                        return None
                except InvalidOperation:
                    return None
        value = format(number.normalize(), "f")
    for key, comparison in (("minLength", lambda a, b: a < b), ("maxLength", lambda a, b: a > b)):
        if definition.get(key) is not None and comparison(len(value), definition[key]):
            return None
    return value


def _unique(candidates, definition, unit, family):
    values = [_format(value, definition, unit, family) for value in candidates]
    # 無効な値が混ざる場合も、都合のよい候補だけを採用しない。
    if not values or None in values:
        return None
    unique = {value.casefold(): value for value in values}
    return next(iter(unique.values())) if len(unique) == 1 else None


def infer_attribute_value(item, variant, definition, unit=None):
    """確定できる候補が1つの場合だけ値を返す。未確定ならNone。"""
    family, aliases = _family(definition["nameJa"])
    names = {normalize(name).casefold() for name in aliases}
    unit = unit or definition.get("unit")
    local = []
    for spec in variant.get("specs") or []:
        if normalize(spec.get("label", "")).casefold() in names and _has_value(spec.get("value")):
            local.append(spec.get("value", ""))
    for attribute in variant.get("attributes") or []:
        if normalize(attribute.get("name", "")).casefold() in names:
            local.extend(v for v in attribute.get("values") or [] if _has_value(v))
    selectors = {selector.get("key"): selector.get("displayName", "")
                 for selector in item.get("variantSelectors") or []}
    for key, value in (variant.get("selectorValues") or {}).items():
        if normalize(selectors.get(key, "")).casefold() in names and _has_value(value):
            local.append(value)
    if local:
        return _unique(local, definition, unit, family)

    # 複数SKUでは、ブランドやSKU共通と定義された項目以外を共通説明から流用しない。
    shared = (definition.get("properties") or {}).get("rmsSkuUnifyFlg") is True
    if len(item.get("variants") or {}) > 1 and family != "brand" and not shared:
        return None
    description = item.get("productDescription") or {}
    texts = [plain_text(item.get("title", "")), plain_text(description.get("pc", "")),
             plain_text(description.get("sp", "")), plain_text(item.get("salesDescription", ""))]
    labels = sorted(set(aliases), key=len, reverse=True)
    label_pattern = "|".join(re.escape(label) for label in labels)
    all_labels = sorted({*aliases, *(alias for group in GROUPS.values() for alias in group)}, key=len, reverse=True)
    boundary = "|".join(re.escape(label) for label in all_labels)
    pattern = re.compile(
        rf"(?:^|[\s;|])(?:{label_pattern})[ \t]*(?:[:=]|\t)[ \t]*"
        rf"(.+?)(?=\n|\t|;|\||\s+(?:{boundary})\s*[:=]|$)", re.I,
    )
    candidates = [match[1].strip() for text in texts for match in pattern.finditer(text)]
    if candidates:
        return _unique(candidates, definition, unit, family)

    # ラベルのない商品名では、単位付き容量・重量と明記されたセット個数だけを扱う。
    title = texts[0]
    if family in ("volume", "weight"):
        # 500mL×2のようなセットから、単品量・総量を推測しない。
        if re.search(r"[×x*]|\d+\s*(?:個|本|枚|袋|箱|パック|セット|pcs?)", title, re.I):
            return None
        units = "ml|l" if family == "volume" else "mg|kg|g"
        candidates = re.findall(rf"(?<![a-zA-Z\d.])(\d+(?:\.\d+)?\s*(?:{units}))(?![a-zA-Z])", title, re.I)
    elif family in ("count", "total_count"):
        if re.search(r"[×x*]", title, re.I):
            return None
        candidates = re.findall(r"(?<![a-zA-Z\d.])(\d+\s*(?:個|本|枚|袋|箱|錠|粒|パック|pcs?))(?:入り|入|セット|\b)", title, re.I)
        if family == "total_count" and any(re.search(r"袋|箱|パック", value) for value in candidates):
            return None
    return _unique(candidates, definition, unit, family)
