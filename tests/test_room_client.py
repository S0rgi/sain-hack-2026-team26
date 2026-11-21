import io
import json
import unittest
from unittest.mock import patch

from app.room_client import RoomClient


class RoomClientTests(unittest.TestCase):
    @patch("app.room_client.urlopen")
    def test_devices_uses_guest_run_id(self, urlopen):
        urlopen.return_value.__enter__.return_value = io.BytesIO(b"{}")
        RoomClient("https://example.test", "team-token").devices("run-123")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://example.test/room/devices?run_id=run-123")

    @patch("app.room_client.urlopen")
    def test_action_uses_run_token_and_contract(self, urlopen):
        response = io.BytesIO(b'{"ok": true}')
        urlopen.return_value.__enter__.return_value = response
        client = RoomClient("https://example.test", "run-token")

        result = client.action("session-1", "alarm", {"at": "2026-11-13T07:30:00+03:00"})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/room/session-1/action")
        self.assertEqual(request.get_header("Authorization"), "Bearer run-token")
        self.assertEqual(json.loads(request.data), {
            "device_id": "alarm", "values": {"at": "2026-11-13T07:30:00+03:00"}
        })
        self.assertEqual(result, {"ok": True})

    @patch("app.room_client.urlopen")
    def test_state_encodes_timezone_offset(self, urlopen):
        urlopen.return_value.__enter__.return_value = io.BytesIO(b"{}")
        client = RoomClient("https://example.test", "run-token")

        client.state("session-1", "2026-11-13T07:30:00+03:00")

        self.assertIn("%2B03%3A00", urlopen.call_args.args[0].full_url)


if __name__ == "__main__":
    unittest.main()
