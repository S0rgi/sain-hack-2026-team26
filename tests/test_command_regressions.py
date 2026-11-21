import unittest
from unittest.mock import Mock, patch

from app import main, product_api
from test_case2 import FakeRoom, STAY, USER


class CommandRegressions(unittest.TestCase):
    def setUp(self):
        self.room = FakeRoom("", "")
        self.write = Mock(wraps=self.room.action)
        self.room.action = self.write
        self.llm = Mock()

    def execute(self, text, *, at=STAY["check_in_at"], hotel_timezone=None):
        return main.execute_message(
            self.room, "room-1", self.room.state("room-1")["state"], at, text,
            self.llm, [], USER, [], [], action_at=at, hotel_timezone=hotel_timezone,
        )

    def test_relative_commands_respect_explicit_device(self):
        state, outcome, reply, actions = self.execute("Сделай свет теплее")
        self.assertEqual(outcome, "done")
        self.assertEqual(actions, [{"device_id": "light", "values": {"color_temperature_k": 3600}}])
        self.assertEqual(state["climate"]["temperature_c"], 20)
        self.assertEqual(state["light"]["brightness"], 50)
        self.assertIn("3600 К", reply)

        self.write.reset_mock()
        _, outcome, _, actions = self.execute("Сделай звук ярче")
        self.assertEqual((outcome, actions), ("refused", []))
        self.llm.plan.assert_not_called()
        self.write.assert_not_called()

    def test_relative_boundaries_refuse_without_writes(self):
        self.room.state_data["light"]["brightness"] = 0
        self.room.state_data["climate"]["temperature_c"] = 26
        for phrase in ("Приглуши свет", "Сделай теплее"):
            with self.subTest(phrase=phrase):
                _, outcome, reply, actions = self.execute(phrase)
                self.assertEqual((outcome, actions), ("refused", []))
                self.assertIn("уже на пределе", reply)
        self.llm.plan.assert_not_called()
        self.write.assert_not_called()

    def test_boundary_in_compound_command_still_allows_other_device_change(self):
        self.room.state_data["light"]["brightness"] = 0
        _, outcome, reply, actions = self.execute("Приглуши свет и поставь температуру 21 градус")
        self.assertEqual(outcome, "refused")
        self.assertEqual(actions, [{"device_id": "climate", "values": {"temperature_c": 21}}])
        self.assertIn("уже на пределе", reply)
        self.llm.plan.assert_not_called()
        self.write.assert_called_once_with("room-1", "climate", {"temperature_c": 21}, STAY["check_in_at"])

    def test_interactive_alarm_uses_hotel_timezone_on_target_date(self):
        at = "2026-10-24T22:00:00+02:00"
        room = {"client": self.room, "session_id": "room-1", "at": at, "hotel_timezone": "Europe/Berlin",
                "user": USER, "notes": [], "history": [], "track_ids": [], "track_catalog": [],
                "stage": "final", "expires_at": "2099-01-01T00:00:00+00:00"}
        with (patch.dict("os.environ", {"ENABLE_LEGACY_DEMO": "true"}),
              patch.object(main, "live_room", room)):
            result = main.message(main.Message(text="Разбуди завтра в восемь"))
        self.assertEqual(result["response_class"], "done")
        self.assertEqual(result["state"]["alarm"]["at"], "2026-10-25T08:00:00+01:00")
        self.write.assert_called_once_with("room-1", "alarm", {"at": "2026-10-25T08:00:00+01:00"}, at)

    def test_product_alarm_uses_stored_hotel_timezone(self):
        at = "2026-10-24T22:00:00+02:00"
        data = {"session_id": "room-1", "at": at, "hotel_timezone": "Europe/Berlin",
                "user": USER, "notes": [], "history": [], "track_ids": []}
        router = product_api.build_router(Mock(), main.execute_message)
        message = next(route.endpoint for route in router.routes
                       if route.path == "/api/v1/messages" and "POST" in route.methods)
        with (patch.object(product_api, "room_client", return_value=self.room),
              patch.object(product_api, "load_session", return_value=data),
              patch.object(product_api, "save_session")):
            result = message(product_api.GuestMessage(text="Разбуди завтра в восемь"), authorization="Bearer test")
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["state"]["alarm"]["at"], "2026-10-25T08:00:00+01:00")

    def test_relative_alarm_shift_preserves_elapsed_minutes_through_dst(self):
        self.room.state_data["alarm"]["at"] = "2026-10-25T02:45:00+02:00"
        state, outcome, _, _ = self.execute("на полчаса позже", hotel_timezone="Europe/Berlin")
        self.assertEqual(outcome, "done")
        self.assertEqual(state["alarm"]["at"], "2026-10-25T02:15:00+01:00")

    def test_alarm_refuses_nonexistent_local_time(self):
        _, outcome, _, actions = self.execute("Разбуди завтра в 02:30", at="2026-03-28T22:00:00+01:00",
                                              hotel_timezone="Europe/Berlin")
        self.assertEqual((outcome, actions), ("refused", []))
        self.write.assert_not_called()
