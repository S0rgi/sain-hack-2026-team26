import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from case2 import build_scenario


STATE = {
    "light": {"brightness": 50, "color_temperature_k": 4000},
    "climate": {"temperature_c": 20},
    "sound": {"track_id": "track_01", "volume": 0, "timer_minutes": 0},
    "curtains": {"position": 100},
    "alarm": {"at": None, "mode": "both", "window_minutes": 0},
    "dnd": {"enabled": False},
}
STAY = {"record_id": "stay1", "user_id": "u1", "check_in_at": "2026-11-12T20:00:00+03:00", "target_wake_at": "2026-11-13T07:00:00+03:00", "hotel_timezone": "Europe/Moscow"}
USER = {"user_id": "u1", "weekday_bedtime": "23:00", "sleep_preferences": {"temperature": "normal", "favorite_track_ids": [], "dislikes": []}}


class FakeRoom:
    def __init__(self, base_url, token):
        self.state_data = copy.deepcopy(STATE)
        self.calls = []

    def open_session(self, record_id, kind):
        return {"session_id": record_id, "initial_state": copy.deepcopy(STATE)}

    def set_scenario(self, session_id, steps):
        self.calls.append((session_id, steps))

    def action(self, session_id, device, values, at=None):
        self.state_data[device].update(values)
        return {"state": copy.deepcopy(self.state_data)}

    def state(self, session_id, at=None):
        return {"state": copy.deepcopy(self.state_data)}


class Case2Tests(unittest.TestCase):
    def test_scenario_uses_bedtime_and_preferences(self):
        steps = build_scenario(STAY, USER, ["Гость мерзлявый и не любит резкий будильник"])
        self.assertTrue(any(s["device_id"] == "climate" and s["values"]["temperature_c"] == 21.5 for s in steps))
        self.assertTrue(any(s["device_id"] == "alarm" and s["values"]["mode"] == "light" for s in steps))
        self.assertTrue(all(STAY["check_in_at"] <= s["at"] <= STAY["target_wake_at"] for s in steps))

    def test_request_uses_simulator_state_between_utterances(self):
        client = FakeRoom("https://example.test", "token")
        request = {
            "record_id": "req1", "at": STAY["check_in_at"],
            "utterances": [
                {"at": STAY["check_in_at"], "text": "Разбуди в семь"},
                {"at": STAY["check_in_at"], "text": "На полчаса позже"},
            ],
        }
        answer = main.process_request(client, request)
        self.assertEqual([r["response_class"] for r in answer["replies"]], ["done", "done"])
        self.assertEqual(client.state_data["alarm"]["at"], "2026-11-13T07:30:00+03:00")

    @patch.dict("os.environ", {"HACK_API_URL": "https://example.test"})
    @patch("main.RoomClient", FakeRoom)
    @patch("main.fetch_dataset")
    def test_run_and_interactive_alarm(self, fetch):
        fetch.return_value = {"users": [USER], "stays": [STAY], "requests": [], "events": [], "chat_history": []}
        with TestClient(main.app) as client:
            response = client.post("/run", headers={"Authorization": "Bearer test"}, json={"case": 2, "dataset_code": "dev"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["stays"][0]["record_id"], "stay1")
            first = client.post("/api/message", json={"text": "Разбуди в семь"}).json()
            self.assertEqual(first["state"]["alarm"]["at"], "2026-11-13T07:00:00+03:00")
            second = client.post("/api/message", json={"text": "На полчаса позже"}).json()
            self.assertEqual(second["state"]["alarm"]["at"], "2026-11-13T07:30:00+03:00")
            refused = client.post("/api/message", json={"text": "Поставь 35 градусов"}).json()
            self.assertEqual(refused["state"]["climate"]["temperature_c"], 20)
            self.assertIn("16 до 26", refused["reply"])


if __name__ == "__main__":
    unittest.main()
