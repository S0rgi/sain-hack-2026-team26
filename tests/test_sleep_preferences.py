import unittest

from app.case2 import build_scenario, resolve_routine
from app.commands import plan_command
from app.main import notes_for


STAY = {
    "check_in_at": "2026-11-21T20:00:00+03:00",
    "target_wake_at": "2026-11-22T08:00:00+03:00",
    "hotel_timezone": "Europe/Moscow",
}
GUEST = {
    "user_id": "guest",
    "timezone": "Europe/Moscow",
    "sleep_preferences": {"temperature": "normal", "favorite_track_ids": ["rain"]},
}
TRACKS = [{"track_id": "rain", "title": "Дождь"}, {"track_id": "sea", "title": "Море"}]


class SleepPreferenceTests(unittest.TestCase):
    def test_hotel_temperature_and_light_only_alarm_from_context(self):
        guest = {**GUEST, "sleep_preferences": {}}
        notes = [
            "[2026-11-19T08:00:00+03:00 hotel] Заезд в отель. Гость пожаловался, что в номере холодно, и попросил 22 °C.",
            "[2026-11-19T08:30:00+03:00 hotel] Утром попросил будить только светом: звонок пугает.",
        ]
        routine = resolve_routine(guest, notes, TRACKS)
        self.assertEqual(routine.temperature, 21.5)
        self.assertTrue(routine.gentle_alarm)

    def test_night_named_track_without_explicit_play_verb(self):
        guest = {**GUEST, "sleep_preferences": {}}
        notes = ["[2026-11-19T08:00:00+03:00 hotel] Ночь в отеле. На ночь «Море» на громкости 10, таймер 30 минут."]
        sound = resolve_routine(guest, notes, TRACKS).room_values()['sound']
        self.assertEqual(sound, {'track_id': 'sea', 'volume': 10, 'timer_minutes': 30})

    def test_failed_light_alarm_and_explicit_sound_off_are_not_preferences(self):
        guest = {**GUEST, "sleep_preferences": {}}
        notes = [
            "[2026-11-19T08:00:00+03:00 hotel] Заезд в отель. На ночь выключили «Море».",
            "[2026-11-19T08:10:00+03:00 hotel] Будильник светом не поднял гостя: проспал.",
        ]
        routine = resolve_routine(guest, notes, TRACKS)
        self.assertFalse(routine.gentle_alarm)
        self.assertIsNone(routine.favorite_track)

    def test_night_measurement_overrides_newer_guest_claim_in_stay_scenario(self):
        dataset = {
            "events": [{
                "user_id": "guest",
                "source": "hotel",
                "started_at": "2026-11-19T23:00:00+03:00",
                "payload": {"text": "Фактически ночью было 18 градусов."},
            }],
            "chat_history": [{
                "user_id": "guest",
                "role": "user",
                "at": "2026-11-21T10:00:00+03:00",
                "text": "Я сплю при 22 градусах.",
            }],
        }
        notes = notes_for(dataset, "guest", as_of=STAY["check_in_at"])
        scenario = build_scenario(STAY, GUEST, notes, TRACKS)
        climate = next(step["values"] for step in scenario if step["device_id"] == "climate")
        self.assertEqual(climate["temperature_c"], 17.5)

    def test_latest_measurement_wins_and_hotel_quote_stays_subjective(self):
        notes = [
            "[2026-11-19T23:00:00+03:00 hotel] Фактически ночью было 18 градусов.",
            "[2026-11-20T23:00:00+03:00 app] Телеметрия: ночью было 20 градусов.",
            "[2026-11-21T10:00:00+03:00 hotel] Гость сказал, что спит при 22 градусах.",
        ]
        self.assertEqual(resolve_routine(GUEST, notes, TRACKS).temperature, 19.5)

    def test_bedtime_command_restores_recorded_track_volume_and_timer(self):
        notes = [
            "[2026-11-19T23:00:00+03:00 hotel] При засыпании ставит звук Дождь. Громкость 8%. Таймер 15 минут.",
            "[2026-11-20T22:00:00+03:00 user] Перед сном хочу звук Дождь, громкость 12%, таймер 30 минут.",
        ]
        plan = plan_command("Я ложусь", {}, STAY["check_in_at"], [], GUEST, notes, TRACKS)
        sound = next(action.values for action in plan.actions if action.device_id == "sound")
        self.assertEqual(plan.outcome, "done")
        self.assertEqual(sound, {"track_id": "rain", "volume": 8, "timer_minutes": 15})

    def test_observed_volume_has_priority_over_track_specific_wish(self):
        notes = [
            "[2026-11-19T23:00:00+03:00 hotel] При засыпании выставлена громкость 8%.",
            "[2026-11-20T22:00:00+03:00 user] Перед сном хочу звук Дождь, громкость 12%, таймер 15 минут.",
        ]
        sound = resolve_routine(GUEST, notes, TRACKS).room_values()["sound"]
        self.assertEqual(sound, {"track_id": "rain", "volume": 8, "timer_minutes": 15})

    def test_night_sound_learned_without_a_profile_and_schedule_limits_preserved(self):
        guest = {**GUEST, "sleep_preferences": {}}
        notes = [
            "Перед сном включаю Дождь, громкость 35%, таймер 60 минут.",
            "Днем громкость 60%, таймер 90 минут.",
        ]
        routine = resolve_routine(guest, notes, TRACKS)
        self.assertEqual(routine.room_values()["sound"], {"track_id": "rain", "volume": 35, "timer_minutes": 60})
        scenario = build_scenario(STAY, guest, notes, TRACKS)
        sound = next(step["values"] for step in scenario if step["device_id"] == "sound")
        self.assertEqual(sound, {"track_id": "rain", "volume": 30, "timer_minutes": 45})
