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


class BoardEvidenceTest(unittest.TestCase):
    def test_neutral_gain_and_peak_survive_a_window(self):
        board = [(1.0, 0.3), (1.0, 0.5295), (None, None)]
        self.assertEqual(over_led.board_evidence(board), {"main_out_gain": 1.0, "shim_out_peak": 0.5295})

    def test_an_empty_window_has_no_evidence(self):
        self.assertEqual(over_led.board_evidence([]), {"main_out_gain": None, "shim_out_peak": None})

    def test_an_attenuating_main_out_is_reported(self):
        board = [(0.25, 0.1), (0.25, 0.12)]
        self.assertEqual(over_led.board_evidence(board)["main_out_gain"], 0.25)

    def test_a_carried_over_peak_is_not_used(self):
        board = [(1.0, 1.0), (1.0, 1.0), (1.0, 0.5295), (1.0, 0.5295)]
        self.assertEqual(over_led.board_evidence(board),
                         {"main_out_gain": 1.0, "shim_out_peak": 0.5295})

    def test_an_explicit_carried_peak_is_discarded_even_when_the_window_opens_on_the_fresh_value(self):
        # A report fired during the strike: the window opens on its fresh value, but the pre-strike carry-over is
        # the value to discard, so the fresh reading is still the window's own peak.
        board = [(1.0, 0.5295), (1.0, 0.5295)]
        self.assertEqual(over_led.board_evidence(board, carried=1.0)["shim_out_peak"], 0.5295)

    def test_a_window_that_only_shows_the_carried_peak_has_no_evidence(self):
        board = [(1.0, 1.0), (1.0, 1.0)]
        self.assertEqual(over_led.board_evidence(board),
                         {"main_out_gain": None, "shim_out_peak": None})


class PassFiguresTest(unittest.TestCase):
    def test_a_full_scale_window_that_never_lit_the_over_lamp_says_so(self):
        seen = {"left": [0, 32767, -32768, 100], "start": 0, "to_time": lambda frame: frame / 44100,
                "records": [(0.0, decoded([6, 25])), (0.1, decoded([6, 25]))],
                "board": [(1.0, None), (1.0, 0.999)]}
        figures = over_led.pass_figures(seen)
        self.assertEqual(figures["peak"], 32768)  # -32768 is one step past the positive full scale
        self.assertEqual(figures["peak_dbfs"], 0.0)
        self.assertEqual(figures["near_full_scale_samples"], 2)
        self.assertEqual(figures["over_lit"], False)
        self.assertEqual(figures["mono_channels_lit"], [6, 25])
        self.assertEqual(figures["main_out_gain"], 1.0)
        self.assertEqual(figures["shim_out_peak"], 0.999)

    def test_a_missing_board_block_means_no_shim_evidence(self):
        seen = {"left": [0, 32767], "start": 0, "to_time": lambda frame: frame / 44100,
                "records": [(0.0, decoded([]))]}
        figures = over_led.pass_figures(seen)
        self.assertIsNone(figures["main_out_gain"])
        self.assertIsNone(figures["shim_out_peak"])

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


class VerdictTest(unittest.TestCase):
    def figures(self, gain=1.0, shim=1.0, peak=32767):
        return {"main_out_gain": gain, "shim_out_peak": shim, "peak": peak}

    def test_a_pass_whose_shim_peak_diverges_from_its_wav_peak_fails(self):
        passes = {"full@127": self.figures(shim=1.0, peak=32767),
                  "full@102": self.figures(shim=1.0, peak=17352)}
        self.assertEqual(over_led.divergent_passes(passes),
                         [("full@102", "shim peak 1.0 vs WAV peak 17352")])

    def test_a_matching_pass_is_not_divergent(self):
        # The shim reports out_peak to four decimals, so 0.5295 stands for 17352/32768; the verdict rounds it back.
        passes = {"full@102": self.figures(shim=0.5295, peak=17352)}
        self.assertEqual(over_led.divergent_passes(passes), [])

    def test_a_quantized_shim_peak_is_judged_within_one_lsb(self):
        self.assertTrue(over_led.shim_matches(0.5295, 17352))
        self.assertTrue(over_led.shim_matches(0.6592, 21600))
        self.assertTrue(over_led.shim_matches(1.0, 32767))
        self.assertFalse(over_led.shim_matches(1.0, 17352))  # the 127 pass's peak carried into volume 102

    def test_a_gain_that_is_not_neutral_fails(self):
        passes = {"full@127": self.figures(gain=0.25, shim=0.25, peak=32767)}
        self.assertEqual(over_led.divergent_passes(passes),
                         [("full@127", "MAIN OUT gain 0.25")])

    def test_a_pass_without_shim_evidence_fails(self):
        passes = {"full@127": {"main_out_gain": None, "shim_out_peak": None, "peak": 32767}}
        self.assertEqual(over_led.divergent_passes(passes),
                         [("full@127", "no shim evidence")])

    def test_the_control_pass_is_never_judged(self):
        passes = {"m30": {"main_out_gain": None, "shim_out_peak": None, "peak": 549}}
        self.assertEqual(over_led.divergent_passes(passes), [])


if __name__ == "__main__":
    unittest.main()
