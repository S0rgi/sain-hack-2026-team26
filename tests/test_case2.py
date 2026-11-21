import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import main
from app.case2 import build_scenario, resolve_routine
from app.commands import plan_command
from app.llm import Action, LLMError, Plan


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

    def open_session(self, record_id, kind, run_id=None):
        return {"session_id": record_id, "initial_state": copy.deepcopy(STATE)}

    def set_scenario(self, session_id, steps):
        self.calls.append((session_id, steps))

    def action(self, session_id, device, values, at=None):
        self.state_data[device].update(values)
        return {"state": copy.deepcopy(self.state_data)}

    def state(self, session_id, at=None):
        return {"state": copy.deepcopy(self.state_data)}


class FakeLLM:
    def __init__(self):
        self.histories = []
        self.requests = []
        self.note_inputs = []

    def plan(self, text, state, at, history, user, notes, track_ids):
        self.histories.append(copy.deepcopy(history))
        self.requests.append(text)
        self.note_inputs.append(copy.deepcopy(notes))
        if "35" in text:
            return Plan(outcome="refused", reason="invalid_value", actions=[])
        value = "2026-11-13T07:30:00+03:00" if "позже" in text else "2026-11-13T07:00:00+03:00"
        return Plan(outcome="done", reason="", actions=[Action(device_id="alarm", values={"at": value})])


class Case2Tests(unittest.TestCase):
    def test_scenario_uses_bedtime_and_preferences(self):
        steps = build_scenario(STAY, USER, ["Гость мерзлявый и не любит резкий будильник"])
        self.assertTrue(any(s["device_id"] == "climate" and s["values"]["temperature_c"] == 21.5 for s in steps))
        self.assertTrue(any(s["device_id"] == "alarm" and s["values"]["mode"] == "light" for s in steps))
        self.assertTrue(all(STAY["check_in_at"] <= s["at"] <= STAY["target_wake_at"] for s in steps))

    def test_bedtime_uses_home_timezone_and_adjusts_for_jet_lag(self):
        cases = [
            ("Asia/Novosibirsk", "00:30", "Europe/Moscow", "2026-11-12T18:30:00+03:00", "2026-11-13T09:00:00+03:00", "2026-11-12T21:30:00+03:00"),
            ("Europe/Kaliningrad", "01:30", "Asia/Yekaterinburg", "2026-11-13T23:30:00+05:00", "2026-11-14T10:30:00+05:00", "2026-11-14T03:30:00+05:00"),
            ("Asia/Yekaterinburg", "23:30", "Europe/Moscow", "2026-11-12T20:40:00+03:00", "2026-11-13T06:30:00+03:00", "2026-11-12T21:30:00+03:00"),
        ]
        for home, bedtime, hotel, check_in, wake, expected in cases:
            with self.subTest(home=home, hotel=hotel):
                stay = {**STAY, "check_in_at": check_in, "target_wake_at": wake, "hotel_timezone": hotel}
                user = {**USER, "timezone": home, "weekday_bedtime": bedtime}
                steps = build_scenario(stay, user, [])
                lights_off = [step for step in steps if step["device_id"] == "light" and step["values"].get("brightness") == 0]
                self.assertEqual(lights_off[0]["at"], expected)

    def test_dawn_brightens_three_times_in_the_allowed_window(self):
        wake = datetime.fromisoformat(STAY["target_wake_at"])
        steps = build_scenario(STAY, USER, [])
        dawn = [step for step in steps if wake - timedelta(minutes=30) <= datetime.fromisoformat(step["at"]) <= wake - timedelta(minutes=15)]
        self.assertEqual([step["values"]["brightness"] for step in dawn if step["device_id"] == "light"], [10, 30, 60])
        self.assertEqual([step["values"]["position"] for step in dawn if step["device_id"] == "curtains"], [50])

    def test_during_the_repeated_hour_bedtime_uses_the_last_occurrence(self):
        stay = {**STAY, "check_in_at": "2026-10-24T22:00:00+02:00", "target_wake_at": "2026-10-25T04:00:00+01:00", "hotel_timezone": "Europe/Berlin"}
        user = {**USER, "timezone": "Europe/Berlin", "weekday_bedtime": "02:30"}
        steps = build_scenario(stay, user, [])
        lights_out = next(step for step in steps if step["device_id"] == "light" and step["values"]["brightness"] == 0)
        self.assertEqual(lights_out["at"], "2026-10-25T02:30:00+01:00")

    def test_night_notes_override_profile_and_exclude_rejected_sounds(self):
        user = {**USER, "sleep_preferences": {"temperature": "warm", "favorite_track_ids": ["rain", "sea"]}}
        tracks = [{"track_id": "rain", "title": "Дождь"}, {"track_id": "sea", "title": "Море"}]
        notes = ["На ночь ставит 18 градусов. Не любит дождь. Не использовать резкий звуковой будильник."]
        routine = resolve_routine(user, notes, tracks)
        self.assertEqual(routine.temperature, 17.5)
        self.assertEqual(routine.favorite_track, "sea")
        self.assertTrue(routine.gentle_alarm)
        self.assertEqual(resolve_routine(user, ["Любит тёплый свет в номере"], tracks).temperature, 21.5)

    def test_selects_hotel_notes_and_neighbouring_chat(self):
        dataset = {
            "chat_history": [
                {"user_id": "u1", "role": "user", "at": "1", "text": "Предыдущая реплика"},
                {"user_id": "u1", "role": "agent", "at": "2", "text": "Настроил свет"},
                {"user_id": "u1", "role": "user", "at": "3", "text": "Да, так"},
                {"user_id": "u1", "role": "user", "at": "4", "text": "Другой разговор"},
            ],
            "events": [
                {"user_id": "u1", "source": "hotel", "started_at": "5", "payload": {"text": "Тихий гость"}},
                {"user_id": "u1", "source": "app", "started_at": "6", "payload": {"text": "Купил билет"}},
            ],
        }
        notes = main.notes_for(dataset, "u1")
        self.assertEqual(len(notes), 4)
        self.assertTrue(any("Предыдущая реплика" in item for item in notes))
        self.assertTrue(any("Да, так" in item for item in notes))
        self.assertTrue(any("Тихий гость" in item for item in notes))
        self.assertFalse(any("Купил билет" in item for item in notes))

    @patch.dict("os.environ", {"HACK_API_URL": "https://example.test"})
    @patch("app.main.RoomClient", FakeRoom)
    @patch("app.main.LLMClient")
    @patch("app.main.fetch_dataset")
    def test_run_passes_only_existing_room_evidence_for_contextual_request(self, fetch, llm_factory):
        llm = llm_factory.return_value = FakeLLM()
        request = {"record_id": "req1", "user_id": "u1", "at": STAY["check_in_at"], "utterances": [{"at": STAY["check_in_at"], "text": "Сделай как дома"}]}
        known = {"user_id": "u1", "started_at": "2026-11-11T21:00:00+03:00", "source": "hotel", "payload": {"text": "Дома предпочитает тёплый свет"}}
        future = {**known, "started_at": "2026-11-13T21:00:00+03:00", "payload": {"text": "Следующая запись ещё неизвестна"}}
        fetch.return_value = {"users": [USER], "stays": [STAY], "requests": [request], "events": [known, future], "chat_history": []}
        main.run(main.RunRequest(case=2, dataset_code="dev"), authorization="Bearer test")
        self.assertEqual(llm.note_inputs[0], ["[2026-11-11T21:00:00+03:00 hotel] Дома предпочитает тёплый свет"])

    def test_two_utterances_are_one_request(self):
        client, llm, history = FakeRoom("", ""), FakeLLM(), []
        request = {
            "record_id": "req1", "at": STAY["check_in_at"],
            "utterances": [
                {"at": STAY["check_in_at"], "text": "Разбуди в семь"},
                {"at": STAY["check_in_at"], "text": "На полчаса позже"},
            ],
        }
        answer = main.process_request(client, request, llm, history, USER, [], [])
        self.assertEqual([r["response_class"] for r in answer["replies"]], ["done"])
        self.assertEqual(llm.requests, [])
        self.assertEqual([item["text"] for item in history[:2]], ["Разбуди в семь", "На полчаса позже"])
        self.assertEqual(client.state_data["alarm"]["at"], "2026-11-13T07:30:00+03:00")

    def test_history_is_shared_between_separate_requests(self):
        client, llm, history = FakeRoom("", ""), FakeLLM(), []
        first = {"record_id": "one", "at": STAY["check_in_at"], "utterances": [{"at": STAY["check_in_at"], "text": "Разбуди в семь"}]}
        second = {"record_id": "two", "at": STAY["check_in_at"], "utterances": [{"at": STAY["check_in_at"], "text": "На полчаса позже"}]}
        main.process_request(client, first, llm, history, USER, [], [])
        main.process_request(client, second, llm, history, USER, [], [])
        self.assertEqual(llm.requests, [])
        self.assertEqual(history[0]["text"], "Разбуди в семь")
        self.assertEqual(history[1]["actions"][0]["values"]["at"], "2026-11-13T07:00:00+03:00")
        self.assertEqual(history[-1]["actions"][0]["values"]["at"], "2026-11-13T07:30:00+03:00")

    def test_clear_room_requests_keep_unrelated_settings(self):
        cases = [
            ("Сделай чуть прохладнее, пожалуйста.", "climate", {"temperature_c": 19.5}),
            ("Поставь яркость света на 42 процента", "light", {"brightness": 42}),
            ("Закрой шторы", "curtains", {"position": 0}),
            ("Отмени будильник", "alarm", {"at": None}),
            ("Включи режим «Не беспокоить»", "dnd", {"enabled": True}),
        ]
        for phrase, device, values in cases:
            with self.subTest(phrase=phrase):
                plan = plan_command(phrase, STATE, STAY["check_in_at"], [], USER, [], None)
                self.assertEqual([(action.device_id, action.values) for action in plan.actions], [(device, values)])

    def test_local_refusal_makes_no_llm_or_room_calls(self):
        for phrase, reason in (("Что выпить, чтобы уснуть?", "medical"), ("Поставь 35 градусов", "invalid_value")):
            with self.subTest(phrase=phrase):
                plan = plan_command(phrase, STATE, STAY["check_in_at"], [], USER, [], None)
                self.assertEqual((plan.outcome, plan.reason, plan.actions), ("refused", reason, []))

    def test_compound_request_applies_only_the_setting_in_range(self):
        client = FakeRoom("", "")
        state, outcome, reply, actions = main.execute_message(
            client, "demo", STATE, STAY["check_in_at"],
            "Сделай температуру 21 градус и яркость 150 процентов", None, [], USER, [], [],
        )
        self.assertEqual(outcome, "refused")
        self.assertEqual(actions, [{"device_id": "climate", "values": {"temperature_c": 21.0}}])
        self.assertEqual(state["climate"]["temperature_c"], 21)
        self.assertEqual(state["light"], STATE["light"])
        self.assertIn("от 0 до 100%", reply)

    def test_compound_request_does_not_apply_earlier_conflicting_setting(self):
        client = FakeRoom("", "")
        state, outcome, _, actions = main.execute_message(
            client, "demo", STATE, STAY["check_in_at"],
            "Яркость 30 процентов и яркость 150 процентов", None, [], USER, [], [],
        )
        self.assertEqual((state, outcome, actions), (STATE, "refused", []))

    def test_medical_request_offers_supported_room_settings(self):
        client = FakeRoom("", "")
        state, outcome, reply, actions = main.execute_message(
            client, "demo", STATE, STAY["check_in_at"],
            "Что выпить, чтобы уснуть?", None, [], USER, [], [],
        )
        self.assertEqual((state, outcome, actions), (STATE, "refused", []))
        self.assertIn("приглушить свет", reply)

    def test_provider_failure_does_not_discard_other_requests(self):
        class UnavailableModel:
            def plan(self, *args):
                raise LLMError("LLM HTTP 429")

        client = FakeRoom("", "")
        history = []
        args = (client, "demo", STATE, STAY["check_in_at"])
        result = main.execute_message(*args, "Сделай как дома", UnavailableModel(), history, USER, [], [])
        self.assertEqual((result[1], result[3]), ("refused", []))
        result = main.execute_message(
            *args, "Сделай как дома", UnavailableModel(), history, USER,
            ["Дома температура 18 °C, яркость света 35%, шторы закрыты."], [],
        )
        self.assertEqual({action["device_id"]: action["values"] for action in result[3]}, {
            "climate": {"temperature_c": 18.0}, "light": {"brightness": 35}, "curtains": {"position": 0},
        })
        result = main.execute_message(*args, "Поставь 22 градуса", UnavailableModel(), history, USER, [], [])
        self.assertEqual(result[1], "done")
        self.assertEqual(result[0]["climate"]["temperature_c"], 22)

    @patch.dict("os.environ", {"HACK_API_URL": "https://example.test"})
    @patch("app.main.RoomClient", FakeRoom)
    @patch("app.main.LLMClient", FakeLLM)
    @patch("app.main.fetch_dataset")
    def test_run_and_interactive_alarm(self, fetch):
        fetch.return_value = {"users": [USER], "stays": [STAY], "requests": [], "events": [], "chat_history": []}
        main.live_room.clear()
        result = main.run(main.RunRequest(case=2, dataset_code="dev"), authorization="Bearer test")
        self.assertEqual(result["stays"][0]["record_id"], "stay1")
        self.assertEqual(main.live_room, {})
        main.live_room.update({"client": FakeRoom('', ''), "session_id": "demo", "at": STAY["check_in_at"],
                               "expires_at": "2099-01-01T00:00:00+00:00", "history": [], "user": USER,
                               "notes": [], "track_ids": [], "track_catalog": [], "stage": "dev"})
        first = main.message(main.Message(text="Разбуди в семь"))
        self.assertEqual(first["state"]["alarm"]["at"], "2026-11-13T07:00:00+03:00")
        second = main.message(main.Message(text="На полчаса позже"))
        self.assertEqual(second["state"]["alarm"]["at"], "2026-11-13T07:30:00+03:00")
        refused = main.message(main.Message(text="Поставь 35 градусов"))
        self.assertEqual(refused["state"]["climate"]["temperature_c"], 20)
        self.assertIn("пределы", refused["reply"])
        main.live_room.clear()


if __name__ == "__main__":
    unittest.main()
