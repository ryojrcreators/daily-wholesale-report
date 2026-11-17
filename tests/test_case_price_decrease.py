"""Change Price依頼の値下げ・返信・失敗時の再試行を外部通信なしで検証する。"""
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


class CasePriceDecreaseTest(unittest.TestCase):
    def run_case(self, mall, current=15690, target=9818, *, dry_run=False,
                 failed=False, sale=False):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            artifact = Path(tmp) / "changes.json"
            for name, value in {
                "DRY_RUN": dry_run, "WOWMA_ENABLED": False, "MAX_PER_RUN": 5,
                "APPLIED_CHANGES_FILE": str(artifact),
            }.items():
                stack.enter_context(patch.object(adjust, name, value))
            for name, value in {
                "get_spreadsheet": Mock(), "get_yahoo_access_token": "token",
                "fetch_target_cases": [{"id": "161883", "caseType": "Change Price", "product": "test"}],
                "fetch_price_rows": [{"mall": mall, "sku": "test-sku",
                                      "salesPrice": str(current), "rowIndex": 0}],
                "check_case": None, "get_case_purchase_price_usd": 35.95,
                "calc_sell_price": (target, "test"), "get_yahoo_stores": [],
                "load_reported_anomalies": set(), "rakuten_get_current_prices": {},
                "yahoo_check_sale_conflict": sale,
            }.items():
                stack.enter_context(patch.object(adjust, name, return_value=value))
            mocks = {}
            for name in ("sync_playwright", "login", "check_yahoo_token_expiry", "append_log",
                         "post_chatwork", "mark_anomalies_reported", "update_case",
                         "notify_attribute_results", "recheck_rakuten_price", "recheck_yahoo_price"):
                mocks[name] = stack.enter_context(patch.object(adjust, name))
            stack.enter_context(patch.object(adjust.time, "sleep"))

            def rakuten_update(sku, price, *, updated_stores, attribute_results):
                if not failed:
                    updated_stores.append("store")
                return [("store", "failure" if failed else "updated", not failed)]

            mocks["rakuten_update_price"] = stack.enter_context(
                patch.object(adjust, "rakuten_update_price", side_effect=rakuten_update))
            mocks["yahoo_update_price"] = stack.enter_context(
                patch.object(adjust, "yahoo_update_price",
                             return_value=[("store", "failure" if failed else "updated", not failed)]))
            adjust.main()
            changes = json.loads(artifact.read_text()) if artifact.exists() else []
        return mocks, changes

    def test_decrease_and_increase_are_applied_for_both_malls(self):
        for mall in (adjust.SHOP_RAKUTEN, adjust.SHOP_YAHOO):
            for current, target in ((15690, 9818), (9000, 9818)):
                with self.subTest(mall=mall, current=current):
                    mocks, changes = self.run_case(mall, current, target)
                    update = mocks["rakuten_update_price" if mall == adjust.SHOP_RAKUTEN
                                   else "yahoo_update_price"]
                    update.assert_called_once()
                    self.assertEqual(changes[0]["price"], target)
                    self.assertEqual(changes[0]["case_id"], "161883")
                    self.assertEqual(mocks["update_case"].call_args.args[2],
                                     "Rakuten/Yahoo prices updated.")

    def test_equal_price_does_not_claim_an_update(self):
        for mall in (adjust.SHOP_RAKUTEN, adjust.SHOP_YAHOO):
            with self.subTest(mall=mall):
                mocks, changes = self.run_case(mall, current=9818)
                mocks["rakuten_update_price"].assert_not_called()
                mocks["yahoo_update_price"].assert_not_called()
                self.assertEqual(changes, [])
                self.assertEqual(mocks["update_case"].call_args.args[2],
                                 "Rakuten/Yahoo prices checked; no changes made.")

    def test_failed_update_leaves_case_for_retry(self):
        for mall in (adjust.SHOP_RAKUTEN, adjust.SHOP_YAHOO):
            with self.subTest(mall=mall):
                mocks, _ = self.run_case(mall, failed=True)
                mocks["update_case"].assert_not_called()

    def test_dry_run_does_not_write_price_or_reply(self):
        for mall in (adjust.SHOP_RAKUTEN, adjust.SHOP_YAHOO):
            with self.subTest(mall=mall):
                mocks, changes = self.run_case(mall, dry_run=True)
                mocks["rakuten_update_price"].assert_not_called()
                mocks["yahoo_update_price"].assert_not_called()
                mocks["update_case"].assert_not_called()
                self.assertEqual(changes, [])

    def test_sale_conflict_blocks_yahoo_decrease(self):
        mocks, changes = self.run_case(adjust.SHOP_YAHOO, sale=True)
        mocks["yahoo_update_price"].assert_not_called()
        mocks["update_case"].assert_not_called()
        self.assertEqual(changes, [])

    def test_nonpositive_price_never_written_or_completed(self):
        for target in (0, -1):
            with self.subTest(target=target):
                mocks, changes = self.run_case(adjust.SHOP_RAKUTEN, target=target)
                mocks["rakuten_update_price"].assert_not_called()
                mocks["update_case"].assert_not_called()
                self.assertEqual(changes, [])

    def test_calculator_rejects_nonpositive_sell_price(self):
        for value in ("0", "-1"):
            page = Mock()
            page.evaluate.return_value = {"sellPrice": value}
            self.assertIsNone(adjust.read_sell_price(page)[0])


if __name__ == "__main__":
    unittest.main()
