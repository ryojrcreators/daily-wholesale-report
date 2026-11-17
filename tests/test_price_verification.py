"""更新店舗の限定と、価格に依存しない通知履歴を外部通信なしで検証する。"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {name: "test" for name in (
    "LOGIN_ID_1", "LOGIN_PASS_1", "LOGIN_ID_2", "LOGIN_PASS_2",
    "YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "GOOGLE_CREDENTIALS",
    "RAKUTEN_LISTING_SPREADSHEET_ID", "RAKUTEN_SPREADSHEET_ID",
)}):
    import case_orders_price_adjust as adjust
    import case_orders_price_verify_delayed as delayed
    import rakuten_price_adjust as api


class PriceVerificationTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(api.time, "sleep"))
        self.get = self.stack.enter_context(patch.object(api.requests, "get", side_effect=AssertionError("external GET")))
        self.patch = self.stack.enter_context(patch.object(api.requests, "patch", side_effect=AssertionError("external PATCH")))

    def test_api_records_only_successful_patch_stores(self):
        stores = [{"name": name} for name in ("hidden", "failed", "updated", "already")]
        self.get.side_effect = [Mock(status_code=200, json=Mock(return_value=item)) for item in (
            {"hideItem": True},
            {"variants": {"v": {"standardPrice": 100}}},
            {"variants": {"v": {"standardPrice": 100}}},
            {"variants": {"v": {"standardPrice": 200}}},
        )]
        self.patch.side_effect = [Mock(status_code=400, text="IE0418"), Mock(status_code=204)]
        updated = []
        with patch.object(api, "get_rakuten_stores", return_value=stores), \
                patch.object(api, "rakuten_auth_headers", return_value={}), \
                patch.object(api, "DRY_RUN", False):
            results = api.rakuten_update_price("test-sku", 200, updated_stores=updated)
        self.assertEqual(updated, ["updated"])
        self.assertEqual([ok for _, _, ok in results], [True, False, True, True])
        self.assertEqual(self.patch.call_count, 2)

    def run_adjust(self, updated_stores, results, events=()):
        def update(sku, price, *, updated_stores, attribute_results=None):
            updated_stores.extend(confirmed)
            attribute_results.extend(events)
            return results
        confirmed = updated_stores
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            artifact = Path(tmp) / "changes.json"
            for name, value in {
                "DRY_RUN": False, "WOWMA_ENABLED": False, "MAX_PER_RUN": 5,
                "APPLIED_CHANGES_FILE": str(artifact),
            }.items():
                stack.enter_context(patch.object(adjust, name, value))
            for name, value in {
                "get_spreadsheet": Mock(), "get_yahoo_access_token": "token",
                "fetch_target_cases": [{"id": "123", "caseType": "Change Price", "product": "test"}],
                "fetch_price_rows": [{"mall": adjust.SHOP_RAKUTEN, "sku": "test-sku",
                                      "salesPrice": "100", "rowIndex": 0}],
                "check_case": None, "get_case_purchase_price_usd": 10,
                "calc_sell_price": (200, "test"), "get_yahoo_stores": [],
                "load_reported_anomalies": set(),
            }.items():
                stack.enter_context(patch.object(adjust, name, return_value=value))
            for name in ("sync_playwright", "login", "check_yahoo_token_expiry", "append_log",
                         "post_chatwork", "mark_anomalies_reported", "update_case"):
                stack.enter_context(patch.object(adjust, name))
            stack.enter_context(patch.object(adjust, "rakuten_update_price", side_effect=update))
            recheck = stack.enter_context(patch.object(adjust, "recheck_rakuten_price"))
            adjust.main()
            changes = json.loads(artifact.read_text()) if artifact.exists() else []
        return changes, recheck

    def test_hidden_plus_failed_store_creates_no_verification(self):
        changes, recheck = self.run_adjust([], [
            ("hidden", "倉庫に入っているため価格変更の対象外", True),
            ("failed", "IE0418", False),
        ])
        self.assertEqual(changes, [])
        recheck.assert_not_called()

    def test_partial_success_limits_artifact_and_immediate_check(self):
        changes, recheck = self.run_adjust(["updated"], [
            ("updated", "更新しました", True), ("failed", "IE0418", False),
        ])
        self.assertEqual(changes[0]["stores"], ["updated"])
        self.assertEqual(recheck.call_args.kwargs["store_names"], ["updated"])

    def test_attribute_result_is_passed_to_notifier_with_case_id(self):
        event = {"store": "updated", "sku": "test-sku", "success": True,
                 "price": 200, "filled": 1}
        with patch.object(adjust, "notify_attribute_results") as notify:
            self.run_adjust(["updated"], [("updated", "更新しました", True)], events=[event])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[1], [{**event, "case_id": "123"}])

    def test_immediate_and_delayed_checks_ignore_unupdated_store(self):
        actual = {"updated": 200, "failed": 100}
        with patch.object(adjust, "calc_sell_price", return_value=(200, "test")), \
                patch.object(adjust, "rakuten_get_current_prices", return_value=actual), \
                patch.object(delayed, "rakuten_get_current_prices", return_value=actual):
            anomalies = []
            adjust.recheck_rakuten_price(None, "123", 0, "test-sku", 200, anomalies,
                                        store_names=["updated"])
            self.assertEqual(anomalies, [])
            self.assertEqual(delayed.check_rakuten("test-sku", 200, ["updated"]), [])
            self.assertEqual(len(delayed.check_rakuten("test-sku", 300, ["updated"])), 1)

    def run_delayed(self, price, reported, stores=("updated",)):
        change = {"case_id": "123", "mall": adjust.SHOP_RAKUTEN,
                  "sku": "test-sku", "price": price}
        if stores is not None:
            change["stores"] = list(stores)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            artifact = Path(tmp) / "changes.json"
            artifact.write_text(json.dumps([change]))
            stack.enter_context(patch.object(delayed, "APPLIED_CHANGES_FILE", str(artifact)))
            for name in ("get_spreadsheet", "get_yahoo_access_token"):
                stack.enter_context(patch.object(delayed, name))
            stack.enter_context(patch.object(delayed, "rakuten_get_current_prices", return_value={"updated": 100}))
            stack.enter_context(patch.object(delayed, "load_reported_anomalies", return_value=reported))
            notify = stack.enter_context(patch.object(delayed, "post_chatwork"))
            mark = stack.enter_context(patch.object(delayed, "mark_anomalies_reported"))
            delayed.main()
        return notify, mark

    def test_price_change_does_not_resend_same_unresolved_error(self):
        notify, mark = self.run_delayed(200, set())
        notify.assert_called_once()
        first = mark.call_args.args[1][0]
        reported = {(first["case_id"], first["key"])}
        notify, mark = self.run_delayed(210, reported)
        notify.assert_not_called()
        mark.assert_not_called()

    def test_old_price_based_history_is_preserved(self):
        sheet = Mock()
        sheet.worksheet.return_value.get_all_values.return_value = [
            adjust.ANOMALY_LOG_HEADER, ["date", "123", "delayed:楽天:test-sku:200", "old"],
        ]
        reported = adjust.load_reported_anomalies(sheet)
        notify, _ = self.run_delayed(210, reported)
        notify.assert_not_called()
        self.assertFalse(adjust.is_anomaly_reported(reported, {
            "case_id": "different-case", "key": "delayed:楽天:test-sku:store:updated",
        }))

    def test_store_notifications_remain_distinct(self):
        reported = {("123", "delayed:楽天:test-sku:store:updated")}
        self.assertFalse(adjust.is_anomaly_reported(reported, {
            "case_id": "123", "key": "delayed:楽天:test-sku:store:other",
        }))

    def test_old_artifact_without_updated_stores_is_not_verified(self):
        notify, mark = self.run_delayed(200, set(), stores=None)
        notify.assert_not_called()
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
