"""Wowma受注取得の再試行と出荷登録の停止を検証する（外部通信なし）。"""
import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {name: "test" for name in (
    "WOWMA_API_KEY", "WOWMA_SHOP_ID", "APP_DOMAIN", "LOGIN_ID_1",
    "LOGIN_PASS_1", "LOGIN_ID_2", "LOGIN_PASS_2",
)}):
    import case_orders_wowma as api
    import wowma_ship_notify as shipping


SUCCESS = b"<response><result><status>0</status></result><orderId>123456789</orderId></response>"
# エラー通知と同様、2行目でXML解析が失敗する応答を再現する。
BROKEN = b"<!DOCTYPE html>\n<html lang=en><body>private response</body></html>"


def response(body=SUCCESS, status=200, content_type="application/xml;charset=UTF-8"):
    return Mock(status_code=status, content=body, headers={"Content-Type": content_type})


class WowmaOrderReadTest(unittest.TestCase):
    def setUp(self):
        self.get = self.enterContext(patch.object(api.requests, "get"))
        self.sleep = self.enterContext(patch.object(api.time, "sleep"))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    # unittest.TestCase.enterContextは本番のPython 3.11で利用可。
    # ローカルのPython 3.9でも同じテストを実行する。
    def enterContext(self, context):
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def test_success_without_retry(self):
        self.get.return_value = response()
        self.assertEqual(api.wowma_get_order_info("123456789")["orderId"], "123456789")
        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_malformed_xml_then_success(self):
        self.get.side_effect = [response(BROKEN, content_type="text/html"), response()]
        self.assertEqual(api.wowma_get_order_info("123456789")["status"], "0")
        self.assertEqual(self.get.call_count, 2)
        self.sleep.assert_called_once_with(5)

    def test_transient_http_errors_then_success(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.get.reset_mock()
                self.get.side_effect = [response(BROKEN, status, "text/html"), response()]
                self.assertEqual(api.wowma_get_order_info("123456789")["status"], "0")
                self.assertEqual(self.get.call_count, 2)

    def test_timeout_then_success(self):
        self.get.side_effect = [api.requests.Timeout(), response()]
        self.assertEqual(api.wowma_get_order_info("123456789")["status"], "0")
        self.assertEqual(self.get.call_count, 2)

    def test_permanent_http_error_is_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.get.reset_mock()
                self.get.return_value = response(BROKEN, status, "text/html")
                with self.assertRaisesRegex(api.WowmaOrderReadError, f"HTTP {status}"):
                    api.wowma_get_order_info("123456789")
                self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_api_error_is_not_retried_or_treated_as_missing_order(self):
        self.get.return_value = response(b"<response><result><status>1</status>"
                                        b"<error><code>0002</code></error></result></response>")
        with self.assertRaisesRegex(api.WowmaOrderReadError, "code=0002"):
            api.wowma_get_order_info("123456789")
        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_malformed_response_retries_are_bounded_and_body_is_not_logged(self):
        self.get.return_value = response(BROKEN, content_type="text/html")
        with self.assertRaises(api.WowmaOrderReadError) as caught:
            api.wowma_get_order_info("123456789")
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [5, 15])
        message = str(caught.exception)
        self.assertIn("HTTP 200", message)
        self.assertIn("text/html", message)
        self.assertIn("試行3回", message)
        self.assertNotIn("private response", message)

    def test_well_formed_html_is_not_accepted_as_order(self):
        self.get.return_value = response(b"<html><body>Maintenance</body></html>", content_type="text/html")
        with self.assertRaisesRegex(api.WowmaOrderReadError, "statusが不正"):
            api.wowma_get_order_info("123456789")
        self.assertEqual(self.get.call_count, 3)

    def test_failed_read_never_updates_shipping_or_status(self):
        self.get.return_value = response(BROKEN, content_type="text/html")
        order = {"order_number": "123456789-R", "shop_name": "LA Express",
                 "tracking_num": "500000000000", "ship_method": "Sagawa CDS",
                 "ship_time": "11/16/26, 9:00 AM"}
        with patch.object(shipping, "DRY_RUN", False), \
                patch.object(shipping, "ONLY_ORDER_NUMBERS", set()), \
                patch.object(shipping, "MAX_PER_RUN", None), \
                patch.object(shipping, "sync_playwright"), \
                patch.object(shipping, "login"), \
                patch.object(shipping, "collect_shipped_orders", return_value=[order]), \
                patch.object(shipping, "wowma_update_trade_info") as update, \
                patch.object(shipping, "wowma_update_trade_status") as status, \
                patch.object(shipping, "post_chatwork_task") as notify:
            shipping.main()
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.get.call_args.kwargs["params"]["orderId"], "123456789")
        update.assert_not_called()
        status.assert_not_called()
        notify.assert_called_once()
        self.assertIn("XML解析エラー", notify.call_args.args[2])
        self.assertNotIn("private response", notify.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
