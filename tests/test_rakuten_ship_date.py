"""楽天の発送日を日本時間で登録する回帰テスト（外部通信なし）。"""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "rakuten_ship_notify.py"
spec = importlib.util.spec_from_file_location("rakuten_ship_notify_test", SCRIPT)
shipping = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {name: "test" for name in (
    "APP_DOMAIN", "LOGIN_ID_1", "LOGIN_PASS_1", "LOGIN_ID_2", "LOGIN_PASS_2",
)}):
    spec.loader.exec_module(shipping)


class RakutenShipDateTest(unittest.TestCase):
    def test_winter_shipping_date_uses_japan_day(self):
        self.assertEqual(shipping.to_rakuten_ship_date("11/9/26, 9:00 AM"), "2026-11-10")

    def test_summer_shipping_date_uses_japan_day(self):
        self.assertEqual(shipping.to_rakuten_ship_date("7/1/26, 8:00 AM"), "2026-07-02")

    def test_japan_midnight_boundary(self):
        for ship_time, expected in (
            ("11/9/26, 6:59 AM", "2026-11-09"),
            ("11/9/26, 7:00 AM", "2026-11-10"),
            ("7/1/26, 7:59 AM", "2026-07-01"),
            ("7/1/26, 8:00 AM", "2026-07-02"),
        ):
            with self.subTest(ship_time=ship_time):
                self.assertEqual(shipping.to_rakuten_ship_date(ship_time), expected)

    def test_year_boundary(self):
        self.assertEqual(shipping.to_rakuten_ship_date("12/31/26, 9:00 AM"), "2027-01-01")

    def test_invalid_date_is_not_replaced_with_today(self):
        for ship_time in ("", "invalid", "2/30/26, 9:00 AM"):
            with self.subTest(ship_time=ship_time):
                with self.assertRaises(ValueError):
                    shipping.to_rakuten_ship_date(ship_time)

    def test_shared_local_date_parser_is_preserved(self):
        self.assertEqual(shipping.parse_ship_date("11/9/26, 9:00 AM"), "2026-11-09")

    def run_main(self, ship_time="11/9/26, 9:00 AM", already_registered=False):
        number = "296610-20261110-0000000001"
        record = {"order_number": number, "ship_time": ship_time,
                  "ship_method": "Sagawa CDS", "tracking_num": "500000000000"}
        order = {"orderNumber": number, "PackageModelList": [{
            "basketId": 1234567890,
            "ShippingModelList": [{"shippingDate": "2026-11-10"}] if already_registered else [],
        }]}
        response = Mock(status_code=200)
        response.json.return_value = {"MessageModelList": []}
        with patch.object(shipping, "DRY_RUN", False), \
                patch.object(shipping, "ONLY_ORDER_NUMBERS", set()), \
                patch.object(shipping, "MAX_PER_RUN", None), \
                patch.object(shipping, "sync_playwright"), \
                patch.object(shipping, "login"), \
                patch.object(shipping, "collect_shipped_orders", return_value=[record]), \
                patch.object(shipping, "get_rakuten_stores", return_value={"americana": {}}), \
                patch.object(shipping, "auth_headers", return_value={}), \
                patch.object(shipping, "get_orders", return_value={number: order}), \
                patch.object(shipping, "post_chatwork_task") as notify, \
                patch.object(shipping.requests, "post", return_value=response) as post:
            shipping.main()
        return post, notify

    def test_update_payload_sends_japan_shipping_date(self):
        post, notify = self.run_main()
        post.assert_called_once()
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["orderNumber"], "296610-20261110-0000000001")
        basket = payload["BasketidModelList"][0]
        self.assertEqual(basket["basketId"], 1234567890)
        self.assertEqual(basket["ShippingModelList"][0]["shippingDate"], "2026-11-10")
        notify.assert_not_called()

    def test_invalid_ship_time_skips_update_and_reports(self):
        post, notify = self.run_main(ship_time="invalid")
        post.assert_not_called()
        notify.assert_called_once()

    def test_existing_shipping_is_not_updated(self):
        post, notify = self.run_main(already_registered=True)
        post.assert_not_called()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
