import json
import unittest
from unittest.mock import Mock, call, patch

from app import demo
from app.room_client import RoomApiError


BASE_URL = "https://organizer.example"
STAY = {
    "record_id": "stay-alex",
    "user_id": "guest-alex",
    "hotel_timezone": "Europe/Moscow",
    "check_in_at": "2026-11-22T20:00:00+03:00",
    "target_wake_at": "2026-11-23T08:00:00+03:00",
}
USER = {
    "user_id": "guest-alex",
    "timezone": "Europe/Moscow",
    "weekday_bedtime": "23:00",
    "sleep_preferences": {"temperature": "warm", "favorite_track_ids": ["rain"]},
}
TRACKS = [{"track_id": "rain", "title": "Дождь"}]


class DemoLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {
            "HACK_API_URL": BASE_URL,
            "HACK_TEAM_TOKEN": "team-secret",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.notes_for = Mock(return_value=["Гость предпочитает тишину."])

    def prepare_run(self, stage, stay=None, user=None, saved=None, snapshot=None, use_default=False):
        info = {
            "stage": stage,
            "run_id": "run1",
            "run_token": "run-secret",
            "dataset_id": f"case2-{stage}",
            "expires_at": "2099-11-22T23:59:00+03:00",
        }
        dataset = {"stays": [stay or STAY], "users": [user or USER], "tracks": TRACKS}
        client = Mock()
        client.open_session.return_value = {"session_id": "session-one"}
        client.set_scenario.return_value = snapshot
        client.state.return_value = {"state": {"light": {"brightness": 20}}}
        with (
            patch.dict("os.environ", {} if use_default else {"HACK_RUN_STAGE": stage}),
            patch("app.demo.read_json", side_effect=[info, {"dataset": dataset}]) as read_json,
            patch("app.demo.RoomClient", return_value=client) as room_client,
        ):
            room = demo.create_demo(self.notes_for, saved)
        return room, client, room_client, read_json

    @patch.dict("os.environ", {"HACK_RUN_STAGE": "dev"})
    def test_dev_uses_team_token_and_explicit_run_id(self):
        room, client, room_client, read_json = self.prepare_run("dev")
        create, fetch = [request.args[0] for request in read_json.call_args_list]
        self.assertEqual(create.method, "POST")
        self.assertEqual(json.loads(create.data), {"case": 2})
        self.assertEqual(create.get_header("Authorization"), "Bearer team-secret")
        self.assertEqual(fetch.full_url, BASE_URL + "/case/2/getData?dataset_code=case2-dev")
        self.assertEqual(fetch.get_header("Authorization"), "Bearer team-secret")
        room_client.assert_called_once_with(BASE_URL, "team-secret")
        client.open_session.assert_called_once_with(STAY["record_id"], "stay", run_id="run1")
        self.assertEqual(room["stage"], "dev")

    def test_default_final_uses_scoped_token_no_dataset_code_and_one_stay_session(self):
        room, client, room_client, read_json = self.prepare_run("final", use_default=True)
        create, fetch = [request.args[0] for request in read_json.call_args_list]
        self.assertEqual(json.loads(create.data), {"case": 2, "stage": "final"})
        self.assertEqual(create.get_header("Authorization"), "Bearer team-secret")
        self.assertEqual(fetch.full_url, BASE_URL + "/case/2/getData")
        self.assertEqual(fetch.get_header("Authorization"), "Bearer run-secret")
        self.assertEqual(read_json.call_args_list[0].kwargs, {"timeout": 30})
        room_client.assert_called_once_with(BASE_URL, "run-secret")
        self.notes_for.assert_called_once_with(
            {"stays": [STAY], "users": [USER], "tracks": TRACKS},
            USER["user_id"],
            as_of=STAY["check_in_at"],
        )
        view = demo.demo_view(room, "2026-11-23T04:30:00+00:00")
        self.assertEqual(view["at"], "2026-11-23T07:30:00+03:00")
        self.assertEqual(view["stage"], "final")
        self.assertEqual(view["expires_at"], "2099-11-22T23:59:00+03:00")
        self.assertEqual(view["hotel_timezone"], "Europe/Moscow")
        self.assertEqual(client.mock_calls, [
            call.open_session(STAY["record_id"], "stay"),
            call.set_scenario("session-one", room["scenario"]),
            call.state("session-one", "2026-11-23T07:30:00+03:00"),
        ])
        self.assertNotIn("run-secret", json.dumps(view))
        self.assertNotIn(USER["user_id"], json.dumps(view))
        self.assertNotIn("session-one", json.dumps(view))

    @patch.dict("os.environ", {"HACK_RUN_STAGE": ""})
    def test_empty_stage_uses_final(self):
        self.assertEqual(demo.demo_stage(), "final")

    def test_preparation_reuses_only_snapshot_for_right_session_and_time(self):
        snapshot = {
            "session_id": "session-one",
            "at": "2026-11-22T17:00:00+00:00",
            "state": {"light": {"brightness": 55}},
        }
        room, client, _, _ = self.prepare_run("dev", snapshot=snapshot)
        view = demo.demo_view(room, use_confirmed_state=True)
        self.assertEqual(view["state"]["light"]["brightness"], 55)
        client.state.assert_not_called()
        demo.demo_view(room)
        client.state.assert_called_once_with("session-one", STAY["check_in_at"])

        for invalid in ({**snapshot, "session_id": "different"},
                        {**snapshot, "at": STAY["target_wake_at"]},
                        {**snapshot, "at": "invalid"}):
            with self.subTest(snapshot=invalid):
                room, client, _, _ = self.prepare_run("dev", snapshot=invalid)
                view = demo.demo_view(room, use_confirmed_state=True)
                self.assertEqual(view["state"]["light"]["brightness"], 20)
                client.state.assert_called_once_with("session-one", STAY["check_in_at"])

    def test_virtual_time_changes_only_after_successful_readback(self):
        room, client, _, _ = self.prepare_run("dev")
        for invalid in ("2026-11-23T07:00:00", "2026-11-23T09:00:00+03:00", "now"):
            with self.subTest(invalid=invalid), self.assertRaises(demo.DemoTimeError):
                demo.demo_view(room, invalid)
        client.state.assert_not_called()
        client.state.side_effect = RoomApiError("Offline")
        with self.assertRaises(RoomApiError):
            demo.demo_view(room, "2026-11-22T22:00:00+03:00")
        self.assertEqual(room["at"], STAY["check_in_at"])

    def test_checkout_and_restore_are_limited_to_confirmed_choices_for_same_guest(self):
        room, *_ = self.prepare_run("dev")
        demo.remember_actions(room, [
            {"device_id": "light", "values": {"brightness": 45}},
            {"device_id": "climate", "values": {"temperature_c": 22}},
            {"device_id": "alarm", "values": {"at": "2026-11-23T07:45:00+03:00"}},
            {"device_id": "curtains", "values": {"position": 30}},
        ])
        self.assertIsNone(demo.saved_preferences(room))
        recipe = demo.checkout_demo(room)
        self.assertIs(recipe, demo.checkout_demo(room))
        self.assertEqual(demo.demo_view(room)["checkout"], recipe)
        self.assertEqual(recipe["settings"]["climate"], {"temperature_c": 22})
        self.assertEqual(recipe["settings"]["light"]["brightness"], 45)
        self.assertEqual(recipe["settings"]["sound"]["track_id"], "rain")
        self.assertEqual(recipe["changed"], ["light", "climate", "alarm"])
        self.assertIsNotNone(recipe["dawn_at"])
        saved = demo.saved_preferences(room)

        later_stay = {**STAY, "check_in_at": "2026-12-22T20:00:00+03:00",
                      "target_wake_at": "2026-12-23T08:00:00+03:00"}
        next_room, *_ = self.prepare_run("dev", stay=later_stay, saved=saved)
        next_view = demo.demo_view(next_room)
        self.assertTrue(next_view["preferences_reused"])
        self.assertNotIn("checkout", next_view)
        self.assertEqual(next_room["history"], [])
        next_recipe = demo.checkout_demo(next_room)
        self.assertEqual(next_recipe["settings"]["alarm"]["at"], "2026-12-23T07:45:00+03:00")
        self.assertEqual(next_recipe["settings"]["climate"]["temperature_c"], 22)
        self.assertEqual(next_recipe["settings"]["light"]["brightness"], 20)
        self.assertNotIn("light", demo.saved_preferences(next_room)["changes"])

        other_stay = {**later_stay, "user_id": "other-guest"}
        other_user = {**USER, "user_id": other_stay["user_id"]}
        other_room, *_ = self.prepare_run("dev", stay=other_stay, user=other_user, saved=saved)
        self.assertFalse(demo.demo_view(other_room)["preferences_reused"])
        self.assertEqual(demo.checkout_demo(other_room)["settings"]["climate"]["temperature_c"], 21.5)

    @patch.dict("os.environ", {"HACK_RUN_STAGE": "final"})
    def test_final_does_not_fall_back_to_dev(self):
        with patch("app.demo.read_json", return_value={"stage": "dev"}), self.assertRaises(RoomApiError):
            demo.create_demo(self.notes_for)

    def test_expiration_uses_the_organizer_timestamp(self):
        room, *_ = self.prepare_run("dev")
        self.assertFalse(demo.is_expired(room))
        expired_room = {**room, "expires_at": "2026-01-01T00:00:00+00:00"}
        self.assertTrue(demo.is_expired(expired_room))

    def test_checkout_replay_preserves_required_ranges_and_next_alarm(self):
        room, *_ = self.prepare_run("dev")
        demo.remember_actions(room, [
            {"device_id": "alarm", "values": {"at": None}},
            {"device_id": "light", "values": {"brightness": 0, "color_temperature_k": 4500}},
            {"device_id": "climate", "values": {"temperature_c": 25}},
        ])
        current = demo.checkout_demo(room)
        self.assertIsNone(current["settings"]["alarm"]["at"])
        self.assertEqual(current["settings"]["climate"]["temperature_c"], 25)
        next_room, *_ = self.prepare_run("dev", saved=demo.saved_preferences(room))
        following = demo.checkout_demo(next_room)["settings"]
        self.assertEqual(following["alarm"]["at"], STAY["target_wake_at"])
        self.assertEqual(following["climate"]["temperature_c"], 21.5)
        self.assertEqual(following["light"], {"brightness": 20, "color_temperature_k": 2700})
