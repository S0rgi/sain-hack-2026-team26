"""Repeatable HTTP integration test; the simulator and LLM are local fakes."""
import concurrent.futures
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from tests import fake_simulator as fake


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(url, method='GET', body=None, headers=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = dict(headers or {})
    if data is not None:
        headers['Content-Type'] = 'application/json'
    try:
        with build_opener(ProxyHandler({})).open(Request(url, data=data, headers=headers, method=method), timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


class ApiV1HttpTests(unittest.TestCase):
    def setUp(self):
        with fake.LOCK:
            fake.RUNS.clear()
            fake.SESSIONS.clear()
            fake.AUDIT.clear()
            fake.CONTROL.update(fail_state=False, fail_device=None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'rooms.sqlite3')
        self.simulator = ThreadingHTTPServer(('127.0.0.1', 0), fake.Handler)
        self.sim_thread = threading.Thread(target=self.simulator.serve_forever, daemon=True)
        self.sim_thread.start()
        self.sim_url = f'http://127.0.0.1:{self.simulator.server_port}'
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.env = {**os.environ, 'HACK_API_URL': self.sim_url, 'HACK_TEAM_TOKEN': 'fake-team-token',
                    'FRONTEND_ADMIN_KEY': 'test-admin-key-123456', 'ROOM_DB_PATH': self.db,
                    'SESSION_CLEANUP_INTERVAL_SECONDS': '60'}
        self.process = None
        self.addCleanup(self.stop_backend)
        self.addCleanup(self.simulator.server_close)
        self.addCleanup(self.simulator.shutdown)
        self.log = tempfile.TemporaryFile(mode='w+')
        self.addCleanup(self.log.close)
        self.start_backend()

    def start_backend(self):
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'tests.fake_llm_app:app', '--host', '127.0.0.1',
             '--port', str(self.port), '--log-level', 'warning'], env=self.env,
            stdout=self.log, stderr=self.log)
        for _ in range(400):
            if self.process.poll() is not None:
                self.fail('Backend exited before startup')
            try:
                if request(self.base + '/health')[0] == 200:
                    return
            except (URLError, OSError):
                time.sleep(0.05)
        self.log.seek(0)
        self.fail('Backend startup timed out: ' + self.log.read()[-2000:])

    def stop_backend(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def tearDown(self):
        self.stop_backend()
        self.simulator.shutdown()
        self.simulator.server_close()
        self.sim_thread.join(timeout=5)
        self.tmp.cleanup()

    def api(self, path, method='GET', body=None, token=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {'X-Admin-Key': 'test-admin-key-123456'}
        return request(self.base + '/api/v1' + path, method, body, headers)

    def control(self, **values):
        self.assertEqual(request(self.sim_url + '/_control', 'POST', values)[0], 200)

    def test_isolation_recovery_cleanup_failures_and_concurrency(self):
        status, a = self.api('/sessions', 'POST', {})
        self.assertEqual(status, 201)
        status, b = self.api('/sessions', 'POST', {})
        self.assertEqual(status, 201)
        ta, tb = a['access_token'], b['access_token']
        self.assertNotEqual(ta, tb)
        with fake.LOCK:
            created = [x['body']['run_id'] for x in fake.AUDIT if x['path'] == '/room/session']
            sids = list(fake.SESSIONS)
        self.assertEqual(len(created), 2)
        self.assertEqual(len(sids), 2)
        self.assertEqual(self.api('/catalog', token=ta)[0], 200)
        self.assertEqual(self.api('/catalog', token=tb)[0], 200)
        with fake.LOCK:
            catalog_requests = [x for x in fake.AUDIT if x['path'] == '/room/devices']
        self.assertEqual([x['query']['run_id'] for x in catalog_requests[-2:]], [[created[0]], [created[1]]])
        at = '2026-11-21T23:00:00+03:00'
        self.assertEqual(self.api('/time', 'POST', {'at': at}, ta)[0], 200)
        self.assertEqual(self.api('/actions', 'POST', {'device_id': 'light', 'values': {'brightness': 13}}, ta)[1]['state']['light']['brightness'], 13)
        self.assertEqual(self.api('/messages', 'POST', {'text': 'Первое'}, ta)[0], 200)
        status, room_b = self.api('/room', token=tb)
        self.assertEqual((status, room_b['at'], room_b['state']['light']['brightness']), (200, b['room']['at'], 50))
        self.assertEqual(self.api('/messages', token=tb)[1]['messages'], [])
        with fake.LOCK:
            used = [x['path'] for x in fake.AUDIT if x['path'].endswith(('/state', '/action'))]
        self.assertTrue(any(f'/room/{sids[0]}/action' == p for p in used))
        self.assertTrue(any(f'/room/{sids[1]}/state' == p for p in used))

        self.stop_backend()
        self.start_backend()
        self.assertEqual(self.api('/room', token=ta)[1]['at'], at)
        self.assertEqual(self.api('/room', token=ta)[1]['state']['light']['brightness'], 30)
        self.assertEqual(self.api('/room', token=tb)[1]['state']['light']['brightness'], 50)
        self.assertEqual(len(self.api('/messages', token=ta)[1]['messages']), 2)
        with fake.LOCK:
            self.assertEqual(len(fake.RUNS), 2)

        self.control(fail_state=True)
        self.assertEqual(self.api('/time', 'POST', {'at': '2026-11-22T06:00:00+03:00'}, ta)[0], 502)
        self.control(fail_state=False)
        self.assertEqual(self.api('/room', token=ta)[1]['at'], at)
        self.control(fail_device='curtains')
        status, partial = self.api('/messages', 'POST', {'text': 'частично'}, ta)
        self.assertEqual((status, partial['outcome']), (200, 'refused'))
        self.assertEqual([x['device_id'] for x in partial['actions']], ['light'])
        self.assertIn('Часть настроек', partial['reply'])
        self.control(fail_device=None)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda text: self.api('/messages', 'POST', {'text': text}, ta), ['один', 'два']))
        self.assertEqual([x[0] for x in responses], [200, 200])
        history = self.api('/messages', token=ta)[1]['messages']
        self.assertEqual(len(history), 8)
        self.assertEqual({x['text'] for x in history if x['role'] == 'user'}, {'Первое', 'частично', 'один', 'два'})

        key = hashlib.sha256(ta.encode()).hexdigest()
        with sqlite3.connect(self.db) as conn:
            row = conn.execute('SELECT data FROM sessions WHERE token_hash = ?', (key,)).fetchone()
            data = json.loads(row[0])
            data['expires_at'] = '2000-01-01T00:00:00+00:00'
            conn.execute('UPDATE sessions SET data = ? WHERE token_hash = ?', (json.dumps(data), key))
        self.assertEqual(self.api('/room', token=ta)[0], 410)
        self.stop_backend()
        self.env['SESSION_CLEANUP_INTERVAL_SECONDS'] = '1'
        self.start_backend()
        for _ in range(40):
            status, _ = self.api('/room', token=ta)
            if status == 404:
                break
            time.sleep(0.2)
        self.assertEqual(status, 404)
        self.assertEqual(self.api('/room', token=tb)[0], 200)
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(conn.execute('SELECT data FROM sessions WHERE token_hash = ?', (key,)).fetchone())


if __name__ == '__main__':
    unittest.main()
