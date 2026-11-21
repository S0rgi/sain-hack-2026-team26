import unittest

from app.commands import plan_command


class PersonalCommandTests(unittest.TestCase):
    def setUp(self):
        self.state = {'light': {'brightness': 0, 'color_temperature_k': 4000},
                      'climate': {'temperature_c': 20},
                      'sound': {'track_id': 'track_01', 'volume': 0, 'timer_minutes': 7},
                      'curtains': {'position': 0}, 'alarm': {'at': None}, 'dnd': {'enabled': True}}
        self.tracks = [{'track_id': 'track_01', 'title': 'Лес'}, {'track_id': 'track_02', 'title': 'Морской прибой'}]
        self.notes = ['[2026-10-10T07:00:00+03:00 hotel] На ночь включали «Морской прибой», громкость 10, таймер 30 минут.',
                      '[2026-10-11T07:00:00+03:00 hotel] Утром просит яркий холодный свет на полную, около 5000 K, и открытые шторы.']

    def plan(self, text):
        return plan_command(text, self.state, '2026-10-12T08:00:00+03:00', [], {}, self.notes, self.tracks)

    def test_morning_preferences_and_unrelated_fields(self):
        changes = {a.device_id: a.values for a in self.plan('Я встала').actions}
        self.assertEqual(changes['light'], {'brightness': 100, 'color_temperature_k': 5000})
        self.assertEqual(changes['curtains'], {'position': 100})
        self.assertNotIn('sound', changes)

    def test_sound_requests_do_not_change_timer(self):
        for text in ('Включи что-нибудь послушать.\nМой обычный, для сна.', 'Не спится. Включи мой прибой ещё раз.'):
            with self.subTest(text=text):
                changes = {a.device_id: a.values for a in self.plan(text).actions}
                self.assertEqual(changes['sound']['track_id'], 'track_02')
                self.assertGreater(changes['sound']['volume'], 0)
                self.assertNotIn('timer_minutes', changes['sound'])


if __name__ == '__main__':
    unittest.main()
