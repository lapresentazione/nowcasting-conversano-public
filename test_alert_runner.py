import unittest

from alert_runner import decide, DEFAULT


def state():
    return dict(DEFAULT)


class RadarStateTest(unittest.TestCase):
    def test_first_alert(self):
        st, alert, save = decide(state(), 1, 300000)
        self.assertTrue(alert)
        self.assertTrue(save)
        self.assertTrue(st["active"])
        self.assertEqual(st["level"], 1)

    def test_duplicate_suppressed(self):
        active, _, _ = decide(state(), 1, 300000)
        same, alert, save = decide(active, 1, 600000)
        self.assertFalse(alert)
        self.assertTrue(save)
        self.assertEqual(same["last_scan"], 600000)
        self.assertEqual(same["level"], 1)

    def test_stronger_event_alert(self):
        active, _, _ = decide(state(), 1, 300000)
        stronger, alert, save = decide(active, 2, 600000)
        self.assertTrue(alert)
        self.assertTrue(save)
        self.assertEqual(stronger["level"], 2)

    def test_clear_after_two_distinct_scans(self):
        active, _, _ = decide(state(), 1, 300000)
        first_clear, alert, save = decide(active, 0, 600000)
        self.assertFalse(alert)
        self.assertTrue(save)
        self.assertTrue(first_clear["active"])
        same, alert, save = decide(first_clear, 0, 600000)
        self.assertFalse(save)
        self.assertEqual(same["quiet"], 1)
        second_clear, alert, save = decide(first_clear, 0, 900000)
        self.assertFalse(second_clear["active"])
        new_episode, alert, save = decide(second_clear, 1, 1200000)
        self.assertTrue(alert)

    def test_brief_clear_not_new_episode(self):
        active, _, _ = decide(state(), 1, 300000)
        one_clear, _, _ = decide(active, 0, 600000)
        wet_again, alert, save = decide(one_clear, 1, 900000)
        self.assertFalse(alert)
        self.assertTrue(save)
        self.assertEqual(wet_again["quiet"], 0)

    def test_no_writes_when_dry(self):
        s, alert, save = decide(state(), 0, 300000)
        self.assertFalse(alert)
        self.assertFalse(save)
        self.assertEqual(s, DEFAULT)

    def test_same_frame_twice_cannot_raise_new_event(self):
        active, _, _ = decide(state(), 1, 300000)
        again, alert, save = decide(active, 2, 300000)
        self.assertFalse(alert)
        self.assertFalse(save)
        self.assertEqual(again, active)

    def test_continuous_rain_is_not_notified_again(self):
        active, _, _ = decide(state(), 1, 300000)
        for timestamp in (600000, 900000, 1200000, 1500000):
            active, alert, save = decide(active, 1, timestamp)
            self.assertFalse(alert)
            self.assertTrue(save)
        self.assertEqual(active["last_scan"], 1500000)
        self.assertEqual(active["last_alert_scan"], 300000)


if __name__ == "__main__":
    unittest.main()
