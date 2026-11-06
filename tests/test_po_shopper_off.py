"""休みの連絡の回帰テスト。外部通信・PO作成・Chatwork送信は行わない。"""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).resolve().parents[1] / "po_auto" / "fill_po_qty.py"
spec = importlib.util.spec_from_file_location("fill_po_qty", SCRIPT)
po = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {name: "test" for name in (
    "APP_DOMAIN", "LOGIN_ID_1", "LOGIN_PASS_1", "LOGIN_ID_2", "LOGIN_PASS_2",
)}):
    spec.loader.exec_module(po)


class ShopperOffTest(unittest.TestCase):
    def setUp(self):
        self.today = datetime(2026, 11, 6, 9, 0)
        # 実際の投稿と同じ日本時間 11/7 1:22 = LA時間 11/6 8:22。
        self.message = {
            "account": {"account_id": 10892606},
            "send_time": int(datetime(2026, 11, 7, 1, 22,
                                      tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()),
            "body": "I am off today",
        }

    def check_messages(self, messages, run_main=False):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(messages).encode()
        with patch.dict(os.environ, {"CW_TOKEN": "test"}), \
                patch.object(po, "CW_SHOPPER_ID", 10892606), \
                patch.object(po, "now_la", return_value=self.today), \
                patch.object(po.urllib.request, "urlopen", return_value=response):
            return po.main() if run_main else po.shopper_is_off_today()

    def test_off_wording(self):
        for body in ("I am off today", "I'm off today", "I’m off today",
                     "Im off today", "I`m off today", "I AM OFF TODAY.",
                     "Hello!\nI am off today. Thank you.", "I  am off today"):
            with self.subTest(body=body):
                self.assertTrue(self.check_messages([{**self.message, "body": body}]))

    def test_other_shopper_is_ignored(self):
        self.assertFalse(self.check_messages([
            {**self.message, "account": {"account_id": 2618849}},
        ]))

    def test_previous_la_day_is_ignored(self):
        self.assertFalse(self.check_messages([
            {**self.message, "send_time": self.message["send_time"] - 86400},
        ]))

    def test_quoted_and_other_messages_are_ignored(self):
        for body in ("[qt]I am off today[/qt]Understood.",
                     "[qt]I'm off today[/qt]Understood.",
                     "I am off tomorrow", "I am not off today", "I'm working today"):
            with self.subTest(body=body):
                self.assertFalse(self.check_messages([{**self.message, "body": body}]))

    def test_no_messages(self):
        self.assertFalse(self.check_messages([]))

    def test_off_stops_before_browser_or_po_creation(self):
        with patch.object(po.sys, "argv", [str(SCRIPT), "--headless", "--auto-submit"]), \
                patch.object(po, "announce_shopper_off") as announce, \
                patch.object(po, "sync_playwright") as browser:
            self.check_messages([self.message], run_main=True)
            announce.assert_called_once_with()
            browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
