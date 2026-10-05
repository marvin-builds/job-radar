import unittest
from datetime import datetime

from job_radar import schedule

MONDAY_9 = datetime(2026, 10, 5, 9, 4)
SATURDAY_13 = datetime(2026, 10, 10, 13, 7)


class DueTest(unittest.TestCase):
    def test_default_is_three_times_a_day(self):
        self.assertEqual(schedule.DEFAULT, {"weekdays": [9, 13, 18], "weekend": [9, 13, 18]})

    def test_due_by_hour_and_day(self):
        workdays = schedule.PRESETS["workdays"][1]
        self.assertTrue(schedule.due(workdays, MONDAY_9))
        self.assertFalse(schedule.due(workdays, MONDAY_9.replace(hour=10)))
        self.assertFalse(schedule.due(workdays, SATURDAY_13))
        self.assertTrue(schedule.due(schedule.DEFAULT, SATURDAY_13))

    def test_presets_fit_the_timer(self):
        for label, plan in schedule.PRESETS.values():
            for hours in plan.values():
                self.assertTrue(all(schedule.FIRST_HOUR <= h <= schedule.LAST_HOUR for h in hours), label)


class DescribeTest(unittest.TestCase):
    def test_same_every_day(self):
        self.assertEqual(schedule.describe(schedule.DEFAULT), "jeden Tag um 9, 13 und 18 Uhr")

    def test_hourly_and_every_two_hours(self):
        self.assertEqual(schedule.describe(schedule.PRESETS["hourly"][1]),
                         "Mo–Fr stündlich von 9 bis 21 Uhr, Sa/So alle 2 Stunden von 9 bis 19 Uhr")

    def test_not_on_weekends(self):
        self.assertEqual(schedule.describe(schedule.PRESETS["workdays"][1]),
                         "Mo–Fr um 9, 13 und 18 Uhr, am Wochenende nicht")

    def test_one_hour(self):
        self.assertEqual(schedule.describe({"weekdays": [9], "weekend": [9]}), "jeden Tag um 9 Uhr")


class ParseTest(unittest.TestCase):
    def test_hours_for_every_day(self):
        self.assertEqual(schedule.parse("8 12 17"), {"weekdays": [8, 12, 17], "weekend": [8, 12, 17]})
        self.assertEqual(schedule.parse("17, 8 Uhr, 8"), {"weekdays": [8, 17], "weekend": [8, 17]})

    def test_invalid_input_raises(self):
        for text in ("", "nachts", "3", "23", "8 9 10 11 12 13 14", "8 abc"):
            with self.assertRaises(ValueError, msg=text):
                schedule.parse(text)


if __name__ == "__main__":
    unittest.main()
