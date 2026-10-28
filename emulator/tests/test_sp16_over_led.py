"""Tests for emulator/probes/over_led.py: which lamp channels a window lit, and what one pass reports."""
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS / "probes"))

import over_led  # noqa: E402


def decoded(lit_channels, over=False):
    """One TX frame whose mono channels in `lit_channels` are lit (34 when `over`)."""
    lit = set(lit_channels) | ({over_led.OVER_CHANNEL} if over else set())
    return {"mono": {channel: (1 if channel in lit else 0, None) for channel in range(35)}}


class LampChannelTest(unittest.TestCase):
    def test_lit_channels_lists_only_the_lit_ones(self):
        self.assertEqual(over_led.lit_channels(decoded([6, 12, 25, 26])), [6, 12, 25, 26])

    def test_a_dark_frame_lists_nothing(self):
        self.assertEqual(over_led.lit_channels(decoded([])), [])


class PassFiguresTest(unittest.TestCase):
    def test_a_full_scale_window_that_never_lit_the_over_lamp_says_so(self):
        seen = {"left": [0, 32767, -32768, 100], "start": 0, "to_time": lambda frame: frame / 44100,
                "records": [(0.0, decoded([6, 25])), (0.1, decoded([6, 25]))]}
        figures = over_led.pass_figures(seen)
        self.assertEqual(figures["peak"], 32768)  # -32768 is one step past the positive full scale
        self.assertEqual(figures["peak_dbfs"], 0.0)
        self.assertEqual(figures["near_full_scale_samples"], 2)
        self.assertEqual(figures["over_lit"], False)
        self.assertEqual(figures["mono_channels_lit"], [6, 25])

    def test_a_lit_over_lamp_is_timed_from_the_first_loud_sample(self):
        seen = {"left": [0, 32767, 32767, 0], "start": 1000, "to_time": lambda frame: (frame - 1000) / 44100,
                "records": [(0.0, decoded([25])), (0.02, decoded([25], over=True)), (0.4, decoded([25], over=True))]}
        figures = over_led.pass_figures(seen)
        self.assertEqual(figures["over_lit"], True)
        self.assertEqual(figures["over_frames"], 2)
        self.assertEqual(figures["over_first_s_after_loud_sample"], 0.02)
        self.assertEqual(over_led.OVER_CHANNEL, 34)

    def test_a_quiet_window_is_measured_as_its_own_peak(self):
        seen = {"left": [0, 549, 0], "start": 0, "to_time": lambda frame: frame / 44100,
                "records": [(0.0, decoded([6]))]}
        figures = over_led.pass_figures(seen)
        self.assertEqual(figures["peak"], 549)
        self.assertLess(figures["peak_dbfs"], -30)
        self.assertEqual(figures["over_lit"], False)


if __name__ == "__main__":
    unittest.main()
