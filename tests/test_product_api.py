import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import main, product_api
from test_case2 import FakeRoom, STAY, USER


class ProductApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ROOM_DB_PATH": os.path.join(self.tmp.name, "rooms.sqlite3"),
                                          "FRONTEND_ADMIN_KEY": "admin-secret-long-enough"})
        self.env.start()
        self.room = FakeRoom('', '')
        self.room.devices = lambda run_id: {"devices": [{"device_id": "light"}]}
        self.created = []
        def create(record_id):
            self.created.append(record_id)
            return {"client": self.room, "session_id": f"room-{len(self.created)}", "run_id": f"run-{len(self.created)}", "at": STAY["check_in_at"],
                    "check_in": STAY["check_in_at"], "wake": STAY["target_wake_at"],
                    "expires_at": "2099-01-01T00:00:00+00:00", "scenario": [], "history": [],
                    "user": USER, "notes": [], "track_ids": ["track_01"]}
        def execute(client, session_id, state, at, text, llm, history, user, notes, track_ids, action_at,
                    track_catalog=None, hotel_timezone=None):
            state = client.action(session_id, "light", {"brightness": 30}, action_at)["state"]
            return state, "done", "Готово", [{"device_id": "light", "values": {"brightness": 30}}]
        router = product_api.build_router(create, execute)
        self.endpoints = {(route.path, next(iter(route.methods))): route.endpoint for route in router.routes}
        self.client_patch = patch('app.product_api.room_client', return_value=self.room)
        self.client_patch.start()
        self.llm_patch = patch('app.product_api.LLMClient', return_value=object())
        self.llm_patch.start()

    def tearDown(self):
        self.llm_patch.stop()
        self.client_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, path, method):
        return self.endpoints[('/api/v1' + path, method)]

    def create(self):
        return self.call('/sessions', 'POST')(product_api.CreateSession(), x_admin_key='admin-secret-long-enough')

    def test_admin_required_and_session_tokens_are_separate(self):
        with self.assertRaises(HTTPException) as error:
            self.call('/sessions', 'POST')(product_api.CreateSession(), x_admin_key='wrong')
        self.assertEqual(error.exception.status_code, 401)
        first, second = self.create(), self.create()
        self.assertNotEqual(first['access_token'], second['access_token'])
        self.assertEqual(len(self.created), 2)
        with self.assertRaises(HTTPException) as error:
            self.call('/room', 'GET')(authorization='Bearer invalid')
        self.assertEqual(error.exception.status_code, 404)
        catalog = self.call('/catalog', 'GET')(authorization='Bearer ' + first['access_token'])
        self.assertEqual(catalog['devices'][0]['device_id'], 'light')

    def test_openapi_exposes_request_and_response_contracts(self):
        schema = main.app.openapi()
        create = schema['paths']['/api/v1/sessions']['post']
        self.assertIn('requestBody', create)
        self.assertIn('application/json', create['responses']['201']['content'])
        self.assertIn('/api/v1/actions', schema['paths'])
        self.assertIn('/api/v1/catalog', schema['paths'])

    @patch.dict(os.environ, {'ENABLE_LEGACY_DEMO': 'false', 'FRONTEND_ADMIN_KEY': ''})
    def test_unconfigured_product_api_and_legacy_demo_fail_closed(self):
        self.assertEqual(main.start_demo().status_code, 404)
        with self.assertRaises(HTTPException) as error:
            self.call('/sessions', 'POST')(product_api.CreateSession(), x_admin_key='anything')
        self.assertEqual(error.exception.status_code, 503)

    def test_history_and_time_are_isolated_and_persisted(self):
        first, second = self.create(), self.create()
        a, b = ('Bearer ' + first['access_token']), ('Bearer ' + second['access_token'])
        result = self.call('/time', 'POST')(product_api.TimeSelection(at='2026-11-12T23:00:00+03:00'), authorization=a)
        self.assertEqual(result['at'], '2026-11-12T23:00:00+03:00')
        self.assertEqual(self.call('/room', 'GET')(authorization=b)['at'], STAY['check_in_at'])
        answer = self.call('/messages', 'POST')(product_api.GuestMessage(text='Темнее'), authorization=a)
        self.assertEqual(answer['state']['light']['brightness'], 30)
        self.assertEqual(len(self.call('/messages', 'GET')(authorization=a)['messages']), 2)
        self.assertEqual(self.call('/messages', 'GET')(authorization=b)['messages'], [])
        # Reading through a new SQLite connection exercises persistence, not a process-local dict.
        self.assertEqual(product_api.load_session(product_api.token_hash(a))['history'][0]['text'], 'Темнее')

    def test_validates_device_actions_and_expiry(self):
        session = self.create()
        auth = 'Bearer ' + session['access_token']
        response = self.call('/actions', 'POST')(product_api.DeviceAction(device_id='light', values={'brightness': 30}), authorization=auth)
        self.assertEqual(response['state']['light']['brightness'], 30)
        with self.assertRaises(HTTPException) as error:
            self.call('/actions', 'POST')(product_api.DeviceAction(device_id='climate', values={'temperature_c': 50}), authorization=auth)
        self.assertEqual(error.exception.status_code, 422)
        key = product_api.token_hash(auth)
        data = product_api.load_session(key)
        data['expires_at'] = '2000-01-01T00:00:00+00:00'
        product_api.save_session(key, data)
        with self.assertRaises(HTTPException) as error:
            self.call('/room', 'GET')(authorization=auth)
        self.assertEqual(error.exception.status_code, 410)
        self.assertEqual(product_api.cleanup_expired(), 1)
        with self.assertRaises(HTTPException) as error:
            self.call('/room', 'GET')(authorization=auth)
        self.assertEqual(error.exception.status_code, 404)

    def test_failed_time_change_does_not_update_cursor(self):
        session = self.create()
        auth = 'Bearer ' + session['access_token']
        with patch.object(self.room, 'state', side_effect=OSError('offline')):
            with self.assertRaises(HTTPException) as error:
                self.call('/time', 'POST')(product_api.TimeSelection(at='2026-11-12T23:00:00+03:00'), authorization=auth)
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(product_api.load_session(product_api.token_hash(auth))['at'], STAY['check_in_at'])
