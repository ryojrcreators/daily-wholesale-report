"""属性抽出は、商品の明記値だけを使い、曖昧な値は採用しない。"""
import unittest

from rakuten_attribute_values import infer_attribute_value


class AttributeValuesTest(unittest.TestCase):
    def infer(self, name, title="", description="", variant=None, **definition):
        variant = variant or {}
        item = {"title": title, "productDescription": {"pc": description}, "variants": {"v": variant}}
        return infer_attribute_value(item, variant, {"nameJa": name, **definition})

    def test_labelled_brand_and_model_in_html_table(self):
        text = "<table><tr><th>ブランド名</th><td>Cuisinart</td></tr><tr><th>型番</th><td>CSB-77</td></tr></table>"
        self.assertEqual(self.infer("ブランド", description=text), "Cuisinart")
        self.assertEqual(self.infer("メーカー型番", description=text), "CSB-77")

    def test_inline_labels_are_separated(self):
        title = "Brand: Cuisinart Model: CSB-77 Color: Black"
        self.assertEqual(self.infer("ブランド名", title=title), "Cuisinart")
        self.assertEqual(self.infer("型番", title=title), "CSB-77")
        self.assertEqual(self.infer("カラー", title=title), "Black")

    def test_explicit_specs_and_alias_attribute(self):
        variant = {"specs": [{"label": "Brand", "value": "Cuisinart"}],
                   "attributes": [{"name": "Color", "values": ["Blue"]}]}
        self.assertEqual(self.infer("ブランド名", variant=variant), "Cuisinart")
        self.assertEqual(self.infer("カラー", variant=variant), "Blue")

    def test_volume_and_weight_in_title_with_unit_conversion(self):
        self.assertEqual(self.infer("内容量", title="ローション 0.25L", unit="mL", dataType="NUMBER"), "250")
        self.assertEqual(self.infer("重量", title="粉末 500mg", unit="g", dataType="NUMBER"), "0.5")

    def test_text_attribute_preserves_unit(self):
        self.assertEqual(self.infer("容量", title="ローション236ml"), "236ml")

    def test_explicit_pack_count(self):
        self.assertEqual(self.infer("総個数", title="スポンジ 3個セット", dataType="NUMBER"), "3")
        self.assertEqual(self.infer("入数", variant={"specs": [{"label": "入数", "value": "2"}]}), "2")

    def test_ambiguous_or_different_candidates_are_unknown(self):
        for text in ("カラー: 赤/青", "Color: Red or Blue", "<p>ブランド: A</p><p>ブランド: B</p>"):
            name = "ブランド" if "ブランド" in text else "カラー"
            with self.subTest(text=text):
                self.assertIsNone(self.infer(name, description=text))

    def test_pc_sp_conflict_is_not_ignored(self):
        item = {"productDescription": {"pc": "Brand: A", "sp": "Brand: B"}, "variants": {"v": {}}}
        self.assertIsNone(infer_attribute_value(item, {}, {"nameJa": "ブランド"}))

    def test_ambiguous_sku_spec_does_not_fall_back_to_description(self):
        self.assertIsNone(self.infer("カラー", description="Color: Black", variant={
            "specs": [{"label": "カラー", "value": "赤/青"}],
        }))

    def test_empty_spec_can_use_labelled_description(self):
        self.assertEqual(self.infer("型番", description="Model: CSB-77", variant={
            "specs": [{"label": "型番", "value": ""}],
        }), "CSB-77")

    def test_set_volume_and_multiple_volumes_are_unknown(self):
        for title in ("500ml×2本", "500ml 2個セット", "250ml / 500ml", "500ml x 2"):
            with self.subTest(title=title):
                self.assertIsNone(self.infer("容量", title=title, unit="ml"))

    def test_total_count_is_not_inferred_from_boxes_or_multipacks(self):
        for title in ("3箱セット", "2パック", "10枚×3個セット"):
            with self.subTest(title=title):
                self.assertIsNone(self.infer("総個数", title=title, dataType="NUMBER"))

    def test_compatibility_model_is_not_product_model(self):
        self.assertIsNone(self.infer("型番", description="対応型番: CSB-77"))
        self.assertIsNone(self.infer("型番", title="Cuisinart replacement for CSB-77"))

    def test_brand_and_color_are_not_guessed_from_product_name(self):
        self.assertIsNone(self.infer("ブランド", title="Cuisinart Hand Blender"))
        self.assertIsNone(self.infer("カラー", title="Black Opium"))

    def test_script_and_style_are_not_product_data(self):
        self.assertIsNone(self.infer("ブランド", description="<script>Brand: Wrong</script><style>Brand: Wrong</style>"))

    def test_sku_selector_takes_precedence_over_shared_text(self):
        red = {"selectorValues": {"color": "Red"}}
        blue = {"selectorValues": {"color": "Blue"}}
        item = {"variants": {"red": red, "blue": blue},
                "variantSelectors": [{"key": "color", "displayName": "カラー"}],
                "productDescription": {"pc": "Color: Black"}}
        self.assertEqual(infer_attribute_value(item, red, {"nameJa": "カラー"}), "Red")
        self.assertEqual(infer_attribute_value(item, blue, {"nameJa": "カラー"}), "Blue")

    def test_shared_description_does_not_leak_into_different_skus(self):
        item = {"variants": {"a": {}, "b": {}}, "title": "ローション 250ml",
                "productDescription": {"pc": "Color: Black\nBrand: Known"}}
        self.assertIsNone(infer_attribute_value(item, {}, {"nameJa": "カラー"}))
        self.assertIsNone(infer_attribute_value(item, {}, {"nameJa": "容量", "unit": "ml"}))
        self.assertEqual(infer_attribute_value(item, {}, {"nameJa": "ブランド"}), "Known")

    def test_explicit_sku_unified_definition_can_use_shared_data(self):
        item = {"variants": {"a": {}, "b": {}}, "productDescription": {"pc": "Model: CSB-77"}}
        self.assertEqual(infer_attribute_value(item, {}, {
            "nameJa": "型番", "properties": {"rmsSkuUnifyFlg": True},
        }), "CSB-77")

    def test_incompatible_units_and_invalid_limits_are_unknown(self):
        self.assertIsNone(self.infer("容量", description="容量: 500g", unit="ml"))
        self.assertIsNone(self.infer("容量", description="容量: 500", unit="ml"))
        self.assertIsNone(self.infer("容量", title="液体 500ml", unit="ml", maxValue=100))
        self.assertIsNone(self.infer("型番", description="型番: ABCDEF", maxLength=3))
        self.assertIsNone(self.infer("総個数", description="総個数: 2.5", dataType="NUMBER"))


if __name__ == "__main__":
    unittest.main()
