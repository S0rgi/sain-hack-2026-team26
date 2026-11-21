import io
import json
import unittest
from unittest.mock import patch

from app import main
from app.room_client import RoomClient
from test_case2 import FakeRoom, STAY, USER


@patch.dict('os.environ', {'ENABLE_LEGACY_DEMO': 'true'})
class DemoTests(unittest.TestCase):
    def tearDown(self):
        main.live_room.clear()

    @patch('app.room_client.urlopen')
    def test_team_session_includes_run_id(self, urlopen):
        urlopen.return_value.__enter__.return_value = io.BytesIO(b'{"session_id":"demo"}')
        RoomClient('https://example.test', 'team-token').open_session('stay1', 'stay', 'run1')
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {'record_id': 'stay1', 'kind': 'stay', 'run_id': 'run1'})
        self.assertEqual(request.get_header('Authorization'), 'Bearer team-token')

    def demo(self, expires='2099-01-01T00:00:00+00:00'):
        return {'client': FakeRoom('', ''), 'session_id': 'demo', 'at': STAY['check_in_at'],
                'check_in': STAY['check_in_at'], 'wake': STAY['target_wake_at'],
                'expires_at': expires, 'scenario': [], 'history': [], 'user': USER,
                'notes': [], 'track_ids': []}

    @patch('app.demo.create_demo')
    def test_start_is_separate_and_time_is_validated(self, create):
        create.return_value = self.demo()
        result = main.start_demo()
        self.assertEqual(result['at'], STAY['check_in_at'])
        result = main.room('2026-11-12T23:00:00+03:00')
        self.assertEqual(result['at'], '2026-11-12T23:00:00+03:00')
        self.assertEqual(main.room('2026-11-12T23:00:00').status_code, 400)
        self.assertEqual(main.room('2026-11-14T23:00:00+03:00').status_code, 400)
        self.assertEqual(create.call_count, 1)

    @patch('app.demo.create_demo')
    def test_expired_demo_is_recreated(self, create):
        main.live_room.update(self.demo('2000-01-01T00:00:00+00:00'))
        create.return_value = self.demo()
        self.assertEqual(main.room().status_code, 409)
        create.assert_not_called()
        self.assertEqual(main.start_demo()['at'], STAY['check_in_at'])
        self.assertEqual(create.call_count, 1)
