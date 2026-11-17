"""未入力の必須属性だけを '-' で補完する（外部通信なし）。"""
from copy import deepcopy
import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {name: "test" for name in (
    "LOGIN_ID_1", "LOGIN_PASS_1", "LOGIN_ID_2", "LOGIN_PASS_2",
    "YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "GOOGLE_CREDENTIALS",
    "RAKUTEN_LISTING_SPREADSHEET_ID", "RAKUTEN_SPREADSHEET_ID",
)}):
    import rakuten_price_adjust as api


def definition(name, required=True, **kwargs):
    return {"nameJa": name, "properties": {"rmsMandatoryFlg": required}, **kwargs}


def response(status, data=None, text=""):
    return Mock(status_code=status, json=Mock(return_value=data), text=text)


class RequiredAttributesTest(unittest.TestCase):
    def setUp(self):
        self.item = {"genreId": "123456", "variants": {"v1": {
            "standardPrice": 100,
            "attributes": [{"name": "ブランド", "values": ["Known brand"]},
                           {"name": "任意属性", "values": ["keep"], "unit": "kg"}],
        }}}
        self.definitions = [definition("ブランド"), definition("型番"), definition("任意", False)]

    def test_existing_required_and_optional_values_are_preserved(self):
        original = deepcopy(self.item)
        payload, filled = api.build_required_attribute_patch(self.item, self.definitions, 200)
        self.assertEqual(filled, 1)
        self.assertEqual(payload["variants"]["v1"]["attributes"], [
            *original["variants"]["v1"]["attributes"], {"name": "型番", "values": ["-"]},
        ])
        self.assertEqual(self.item, original)

    def test_blank_values_only_are_filled_and_unit_is_preserved(self):
        self.item["variants"]["v1"]["attributes"] = [{"name": "容量", "values": ["", " "], "unit": "ml"}]
        payload, filled = api.build_required_attribute_patch(self.item, [definition("容量", unit="L")], 200)
        self.assertEqual(filled, 1)
        self.assertEqual(payload["variants"]["v1"]["attributes"], [{"name": "容量", "values": ["-"], "unit": "ml"}])

    def test_definition_unit_is_used_for_missing_attribute(self):
        payload, _ = api.build_required_attribute_patch(self.item, [definition("重量", unit="g")], 200)
        self.assertEqual(payload["variants"]["v1"]["attributes"][-1], {"name": "重量", "values": ["-"], "unit": "g"})

    def test_all_skus_are_checked_without_copying_other_sku_values(self):
        self.item["variants"]["v2"] = {"standardPrice": 200}
        payload, filled = api.build_required_attribute_patch(self.item, self.definitions, 200)
        self.assertEqual(filled, 3)
        self.assertEqual(payload["variants"]["v2"]["attributes"], [
            {"name": "ブランド", "values": ["-"]}, {"name": "型番", "values": ["-"]},
        ])
        self.assertEqual(payload["variants"]["v2"]["standardPrice"], 200)

    def test_missing_definitions_or_nothing_to_fill_stops_retry(self):
        for defs in ([], [definition("ブランド")], [definition("")]):
            with self.subTest(defs=defs), self.assertRaises(ValueError):
                api.build_required_attribute_patch(self.item, defs, 200)

    def run_update(self, gets, patches, dry_run=False):
        with patch.object(api, "get_rakuten_stores", return_value=[{"name": "test-shop"}]), \
                patch.object(api, "rakuten_auth_headers", return_value={}), \
                patch.object(api, "DRY_RUN", dry_run), \
                patch.object(api.time, "sleep"), \
                patch.object(api.requests, "get", side_effect=gets) as get, \
                patch.object(api.requests, "patch", side_effect=patches) as update:
            stores = []
            results = api.rakuten_update_price("test-sku", 200, updated_stores=stores)
        return results, stores, get, update

    def missing_error(self):
        return response(400, text='{"errors":[{"code":"IE0418","metadata":{"details":[{"code":"invalidAllMandatoryAttributes"}]}}]}')

    def normal_gets(self):
        return [response(200, self.item), response(200, self.item),
                response(200, {"genre": {"attributes": self.definitions}})]

    def test_missing_attributes_retry_updates_price_and_records_store(self):
        results, stores, get, update = self.run_update(self.normal_gets(), [self.missing_error(), response(204)])
        self.assertTrue(results[0][2])
        self.assertEqual(stores, ["test-shop"])
        self.assertEqual(update.call_count, 2)
        payload = update.call_args.kwargs["json"]
        self.assertEqual(payload["variants"]["v1"]["attributes"][-1], {"name": "型番", "values": ["-"]})
        self.assertEqual(payload["variants"]["v1"]["standardPrice"], 200)
        self.assertIn("/navigation/genres/123456/attributes/", get.call_args.args[0])

    def test_latest_attribute_values_are_used_before_retry(self):
        latest = deepcopy(self.item)
        latest["variants"]["v1"]["attributes"][0]["values"] = ["Recent edit"]
        gets = self.normal_gets()
        gets[1] = response(200, latest)
        _, _, _, update = self.run_update(gets, [self.missing_error(), response(204)])
        self.assertEqual(update.call_args.kwargs["json"]["variants"]["v1"]["attributes"][0]["values"], ["Recent edit"])

    def test_rejected_dash_does_not_record_success_or_retry_again(self):
        results, stores, _, update = self.run_update(self.normal_gets(), [
            self.missing_error(), response(400, text="attribute value '-' is invalid"),
        ])
        self.assertFalse(results[0][2])
        self.assertEqual(stores, [])
        self.assertEqual(update.call_count, 2)
        self.assertIn("必須属性", results[0][1])

    def test_navigation_permission_failure_does_not_send_attribute_patch(self):
        gets = self.normal_gets()
        gets[-1] = response(403)
        results, stores, _, update = self.run_update(gets, [self.missing_error()])
        self.assertFalse(results[0][2])
        self.assertEqual(stores, [])
        update.assert_called_once()
        self.assertIn("NavigationAPI", results[0][1])

    def test_other_errors_do_not_trigger_attribute_updates(self):
        for error in (response(400, text="IE0418 invalid genre"), response(500, text="server error")):
            with self.subTest(error=error):
                results, _, get, update = self.run_update([response(200, self.item)], [error])
                self.assertFalse(results[0][2])
                get.assert_called_once()
                update.assert_called_once()

    def test_item_hidden_during_update_stops_attribute_retry(self):
        results, stores, get, update = self.run_update([
            response(200, self.item), response(200, {**self.item, "hideItem": True}),
        ], [self.missing_error()])
        self.assertFalse(results[0][2])
        self.assertEqual(stores, [])
        self.assertEqual(get.call_count, 2)
        update.assert_called_once()

    def test_hidden_item_and_dry_run_do_not_update_attributes(self):
        for item, dry_run in (({**self.item, "hideItem": True}, False), (self.item, True)):
            with self.subTest(dry_run=dry_run):
                _, stores, get, update = self.run_update([response(200, item)], [], dry_run)
                self.assertEqual(stores, [])
                get.assert_called_once()
                update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
