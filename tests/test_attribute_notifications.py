"""商品属性の補完結果をYokoさんへ通知する。Chatwork・Sheetsの実通信は行わない。"""
import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {name: "test" for name in (
    "LOGIN_ID_1", "LOGIN_PASS_1", "LOGIN_ID_2", "LOGIN_PASS_2",
    "YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "GOOGLE_CREDENTIALS",
    "RAKUTEN_LISTING_SPREADSHEET_ID", "RAKUTEN_SPREADSHEET_ID",
)}):
    import case_orders_price_adjust as adjust


class AttributeNotificationTest(unittest.TestCase):
    def event(self, success=True, price=200):
        return {"case_id": "123", "sku": "test-sku", "store": "test-shop",
                "success": success, "price": price, "filled": 2}

    def notify(self, events, reported=None, status=200, dry_run=False):
        with patch.object(adjust, "DRY_RUN", dry_run), \
                patch.object(adjust, "CW_TOKEN", "test"), \
                patch.object(adjust, "load_reported_anomalies", return_value=reported or set()) as load, \
                patch.object(adjust, "mark_anomalies_reported") as mark, \
                patch.object(adjust.requests, "post", return_value=Mock(status_code=status, text="test")) as post:
            adjust.notify_attribute_results(Mock(), events, "test-date")
        return post, mark, load

    def test_success_and_failure_address_yoko_in_existing_room(self):
        for success, phrase in ((True, "商品属性を自動補完いたしました。念のため確認をお願いいたします。"),
                                (False, "自動補完失敗のため、手動での更新をお願いします。")):
            with self.subTest(success=success):
                post, mark, _ = self.notify([self.event(success)])
                post.assert_called_once()
                self.assertIn("/rooms/83994351/messages", post.call_args.args[0])
                body = post.call_args.kwargs["data"]["body"]
                self.assertTrue(body.startswith("[To:2158846]Yoko Matsusakaさん"))
                self.assertIn(phrase, body)
                self.assertIn("test-sku", body)
                self.assertIn("test-shop", body)
                self.assertIn("ケース123", body)
                self.assertNotIn("[To:2618849]", body)
                mark.assert_called_once()

    def test_same_result_with_different_price_is_not_resent(self):
        _, mark, _ = self.notify([self.event(False)])
        entry = mark.call_args.args[1][0]
        reported = {(entry["case_id"], entry["key"])}
        post, mark, _ = self.notify([self.event(False, price=210)], reported)
        post.assert_not_called()
        mark.assert_not_called()

    def test_success_after_reported_failure_is_not_suppressed(self):
        _, mark, _ = self.notify([self.event(False)])
        entry = mark.call_args.args[1][0]
        post, _, _ = self.notify([self.event(True)], {(entry["case_id"], entry["key"])})
        post.assert_called_once()

    def test_duplicate_events_in_one_run_are_sent_once(self):
        post, mark, _ = self.notify([self.event(), self.event()])
        post.assert_called_once()
        self.assertEqual(len(mark.call_args.args[1]), 1)

    def test_failed_chatwork_delivery_is_not_marked_reported(self):
        post, mark, _ = self.notify([self.event()], status=500)
        post.assert_called_once()
        mark.assert_not_called()

    def test_dry_run_and_no_attribute_attempt_do_not_notify(self):
        for events, dry_run in (([], False), ([self.event()], True)):
            with self.subTest(dry_run=dry_run):
                post, mark, load = self.notify(events, dry_run=dry_run)
                post.assert_not_called()
                mark.assert_not_called()
                load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
