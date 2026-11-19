import copy
import unittest

from app.case2 import build_scenario
from tools.dev_eval import check_sleep_rules, compare_request, matches_expectation


class DevEvaluationTests(unittest.TestCase):
    def test_comparison_uses_direction_tolerance_and_time(self):
        initial = {"climate": {"temperature_c": 20}, "alarm": {"at": "2026-11-13T07:00:00+03:00"}}
        final = {"climate": {"temperature_c": 21}, "alarm": {"at": "2026-11-13T07:29:00+03:00"}}
        self.assertTrue(matches_expectation({"device_id": "climate", "field_id": "temperature_c", "comparison": "increase"}, initial, final))
        self.assertTrue(matches_expectation({"device_id": "climate", "field_id": "temperature_c", "comparison": "within", "value": 21.5, "tolerance": 0.5, "unit": "native"}, initial, final))
        self.assertTrue(matches_expectation({"device_id": "alarm", "field_id": "at", "comparison": "within", "value": "2026-11-13T07:30:00+03:00", "tolerance": 5, "unit": "minutes"}, initial, final))

    def test_sleep_rules_check_interval_changes(self):
        stay = {"check_in_at": "2026-11-12T20:00:00+03:00", "target_wake_at": "2026-11-13T07:00:00+03:00", "hotel_timezone": "Europe/Moscow"}
        user = {"timezone": "Europe/Moscow", "weekday_bedtime": "23:00", "sleep_preferences": {"temperature": "normal", "favorite_track_ids": [], "dislikes": []}}
        reference = {"resolved_bedtime_at": "2026-11-12T23:00:00+03:00", "resolved_preferences": {"temperature": "normal", "favorite_track_ids": [], "gentle_alarm": False}}
        initial = {"light": {"brightness": 50, "color_temperature_k": 4000}, "climate": {"temperature_c": 20}, "sound": {"track_id": "track_01", "volume": 0, "timer_minutes": 0}, "curtains": {"position": 100}, "alarm": {"at": None, "mode": "both", "window_minutes": 0}, "dnd": {"enabled": False}}
        steps = build_scenario(stay, user, [])
        self.assertTrue(all(check_sleep_rules(stay, reference, initial, steps).values()))
        steps.append({"at": "2026-11-13T01:00:00+03:00", "device_id": "light", "values": {"brightness": 50}})
        self.assertFalse(check_sleep_rules(stay, reference, initial, steps)["R2"])

    def test_reports_unexpected_changes(self):
        state = {"climate": {"temperature_c": 20}, "dnd": {"enabled": False}}
        request = {"initial_state": state}
        reference = {"expected_reply_classes": ["done"], "expectations": [{"device_id": "climate", "field_id": "temperature_c", "comparison": "increase"}], "preserve_other_fields": True}
        response = {"replies": [{"response_class": "done"}]}
        final = copy.deepcopy(state)
        final["climate"]["temperature_c"] = 21
        final["dnd"]["enabled"] = True
        self.assertEqual(compare_request(request, reference, response, final), ["dnd.enabled изменено"])


if __name__ == "__main__":
    unittest.main()
