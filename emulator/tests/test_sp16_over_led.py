"""Tests for emulator/probes/over_led.py: which lamp channels a window lit, and what one pass reports."""
import argparse
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(over_led.board_evidence(board), {"main_out_gain_min": 1.0, "main_out_gain_max": 1.0, "shim_out_peak": 0.5295})

    def test_an_empty_window_has_no_evidence(self):
        self.assertEqual(over_led.board_evidence([]), {"main_out_gain_min": None, "main_out_gain_max": None, "shim_out_peak": None})

    def test_an_attenuating_main_out_is_reported(self):
        board = [(0.25, 0.1), (0.25, 0.12)]
        self.assertEqual(over_led.board_evidence(board)["main_out_gain_max"], 0.25)

    def test_a_dip_in_the_gain_is_kept_as_the_minimum(self):
        board = [(1.0, 0.1), (0.25, 0.2), (1.0, 0.5295)]
        self.assertEqual((over_led.board_evidence(board)["main_out_gain_min"],
                          over_led.board_evidence(board)["main_out_gain_max"]), (0.25, 1.0))

    def test_a_none_gain_is_skipped_not_a_type_error(self):
        # the board block reports main_out_gain: null between updates, which max() over a raw list would raise on
        board = [(None, 0.1), (1.0, 0.5295), (None, 0.5296)]
        self.assertEqual(over_led.board_evidence(board),
                         {"main_out_gain_min": 1.0, "main_out_gain_max": 1.0, "shim_out_peak": 0.5296})

    def test_a_carried_over_peak_is_not_used(self):
        board = [(1.0, 1.0), (1.0, 1.0), (1.0, 0.5295), (1.0, 0.5295)]
        self.assertEqual(over_led.board_evidence(board),
                         {"main_out_gain_min": 1.0, "main_out_gain_max": 1.0, "shim_out_peak": 0.5295})

    def test_an_explicit_carried_peak_is_discarded_even_when_the_window_opens_on_the_fresh_value(self):
        # A report fired during the strike: the window opens on its fresh value, but the pre-strike carry-over is
        # the value to discard, so the fresh reading is still the window's own peak.
        board = [(1.0, 0.5295), (1.0, 0.5295)]
        self.assertEqual(over_led.board_evidence(board, carried=1.0)["shim_out_peak"], 0.5295)

    def test_a_window_that_only_shows_the_carried_peak_has_no_evidence(self):
        board = [(1.0, 1.0), (1.0, 1.0)]
        self.assertEqual(over_led.board_evidence(board),
                         {"main_out_gain_min": None, "main_out_gain_max": None, "shim_out_peak": None})


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
        self.assertEqual((figures["main_out_gain_min"], figures["main_out_gain_max"]), (1.0, 1.0))
        self.assertEqual(figures["shim_out_peak"], 0.999)

    def test_a_missing_board_block_means_no_shim_evidence(self):
        seen = {"left": [0, 32767], "start": 0, "to_time": lambda frame: frame / 44100,
                "records": [(0.0, decoded([]))]}
        figures = over_led.pass_figures(seen)
        self.assertIsNone(figures["main_out_gain_min"])
        self.assertIsNone(figures["main_out_gain_max"])
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


class LabelTests(unittest.TestCase):
    """What a report line says when there was nothing to measure, as opposed to a measured zero."""

    def test_a_gain_with_no_reading_reads_na(self):
        self.assertEqual(over_led.gain_label(None, None), "MAIN OUT gain n/a")

    def test_agreeing_gain_bounds_read_as_one_value(self):
        self.assertEqual(over_led.gain_label(1.0, 1.0), "MAIN OUT gain 1.0")

    def test_a_dip_reads_as_a_range(self):
        self.assertEqual(over_led.gain_label(0.25, 1.0), "MAIN OUT gain 0.25..1.0")

    def test_a_measured_level_reads_with_its_unit(self):
        self.assertEqual(over_led.level_label(-36.72), "-36.72 dBFS")
        self.assertEqual(over_led.level_label(0.0), "0.0 dBFS")  # full scale is a value, not silence

    def test_a_silent_window_reads_silent(self):
        self.assertEqual(over_led.level_label(None), "silent")


class VerdictTest(unittest.TestCase):
    def figures(self, gain=1.0, low=None, shim=1.0, peak=32767, control=False):
        return {"main_out_gain_min": gain if low is None else low, "main_out_gain_max": gain,
                "shim_out_peak": shim, "peak": peak, "control": control}

    def test_a_pass_whose_shim_peak_diverges_from_its_wav_peak_fails(self):
        passes = {"full@127": self.figures(shim=1.0, peak=32767),
                  "full@102": self.figures(shim=1.0, peak=17352)}
        self.assertEqual(over_led.divergent_passes(passes),
                         [("full@102", "shim peak 1.0 vs WAV peak 17352")])

    def test_a_matching_pass_is_not_divergent(self):
        # The shim reports out_peak to four decimals, so 0.5295 stands for 17352/32768; the verdict rounds it back.
        passes = {"full@102": self.figures(shim=0.5295, peak=17352)}
        self.assertEqual(over_led.divergent_passes(passes), [])

    def test_a_quantized_shim_peak_is_judged_within_the_printing_step(self):
        # 1e-4 of full scale is 3.2768 samples, so a printed value spans +/-1.6384 around the true peak
        self.assertTrue(over_led.shim_matches(0.5295, 17352))
        self.assertTrue(over_led.shim_matches(0.6592, 21600))
        self.assertTrue(over_led.shim_matches(1.0, 32767))
        self.assertFalse(over_led.shim_matches(1.0, 17352))  # the 127 pass's peak carried into volume 102

    def test_the_tolerance_is_two_samples(self):
        self.assertTrue(over_led.shim_matches(20000.0 / over_led.S16_FULL, 20001.9))  # just inside
        self.assertTrue(over_led.shim_matches(20000.0 / over_led.S16_FULL, 20002.0))  # exactly on it
        self.assertFalse(over_led.shim_matches(20000.0 / over_led.S16_FULL, 20002.1))  # just outside

    def test_a_gain_that_is_not_neutral_fails(self):
        passes = {"full@127": self.figures(gain=0.25, shim=0.25, peak=32767)}
        self.assertEqual(over_led.divergent_passes(passes),
                         [("full@127", "MAIN OUT gain 0.25")])

    def test_a_gain_that_dips_mid_window_is_reported_as_a_range(self):
        passes = {"full@127": self.figures(gain=1.0, low=0.25, shim=0.9999, peak=32767)}
        self.assertEqual(over_led.divergent_passes(passes),
                         [("full@127", "MAIN OUT gain 0.25..1.0")])

    def test_a_pass_without_shim_evidence_is_worded_as_no_evidence_not_divergence(self):
        passes = {"full@127": {"main_out_gain_min": None, "main_out_gain_max": None,
                                "shim_out_peak": None, "peak": 32767}}
        self.assertEqual(over_led.divergent_passes(passes), [("full@127", "no shim evidence")])
        verdict = over_led.shim_verdict(passes)
        self.assertIn("no shim evidence for full@127", verdict)
        self.assertIn("no fresh output-meter reading", verdict)
        self.assertNotIn("diverged", verdict)

    def test_the_control_passes_peak_match_is_not_judged(self):
        passes = {"control": self.figures(gain=1.0, shim=1.0, peak=549, control=True)}
        self.assertEqual(over_led.divergent_passes(passes), [])

    def test_the_control_is_gain_checked_like_every_other_pass(self):
        passes = {"full@127": self.figures(),
                  "control": self.figures(gain=0.25, shim=0.25, peak=549, control=True)}
        self.assertEqual(over_led.divergent_passes(passes), [("control", "MAIN OUT gain 0.25")])

    def test_a_clean_sweep_says_the_level_is_the_firmwares(self):
        passes = {"full@127": self.figures(shim=1.0, peak=32767),
                  "full@102": self.figures(shim=0.5295, peak=17352),
                  "control": self.figures(shim=0.0168, peak=549, control=True)}
        self.assertEqual(over_led.divergent_passes(passes), [])
        verdict = over_led.shim_verdict(passes)
        self.assertIn("neutral (1.0)", verdict)
        self.assertNotIn("diverged", verdict)
        self.assertNotIn("no shim evidence", verdict)


class WarmUpTest(unittest.TestCase):
    def test_the_warm_up_strikes_the_pad_and_discards_the_window(self):
        calls = []

        class Session:
            def press(self, name, seconds, value):
                calls.append(("press", name, seconds, value))

        class Runner:
            session = Session()

            def load_sample(self, track, sample):
                calls.append(("load", track, sample))

            def select_track(self, track):
                calls.append(("select", track))
                return True

        slept = []
        real_sleep = over_led.time.sleep
        over_led.time.sleep = slept.append
        try:
            over_led.warm_up(Runner(), 3)
        finally:
            over_led.time.sleep = real_sleep
        self.assertEqual([call[0] for call in calls], ["load", "select", "press"])
        self.assertEqual(calls[2], ("press", "pad3", 0.15, 127))  # the same full-velocity strike a pass uses
        self.assertEqual(calls[0], ("load", 3, "hw-tone-full.wav"))
        self.assertGreater(slept[0], 0.5)  # the strike has to finish before the first measured pass

    def test_a_track_that_cannot_be_selected_fails_loudly(self):
        class Runner:
            class session:
                @staticmethod
                def press(*args):
                    raise AssertionError("no strike on a track that was not selected")

            @staticmethod
            def load_sample(track, sample):
                pass

            @staticmethod
            def select_track(track):
                return False

        with self.assertRaises(RuntimeError):
            over_led.warm_up(Runner(), 3)


class SweepTests(unittest.TestCase):
    """`run()`'s order: the warm-up strike comes before the first measured pass, and the control is labelled by the
    level it measured."""

    def sweep(self, quiet_extra=None):
        order = []
        figures = {"peak": 32767, "window_samples": 132000, "nonzero_samples": 132000, "peak_dbfs": 0.0,
                   "near_full_scale_samples": 132000, "over_lit": False, "over_frames": 0,
                   "over_first_s_after_loud_sample": None, "mono_channels_lit": [6, 25], "tx_frames": 60,
                   "main_out_gain_min": 1.0, "main_out_gain_max": 1.0, "shim_out_peak": 1.0}
        quiet = dict(figures, peak=549, peak_dbfs=-35.5, near_full_scale_samples=0, shim_out_peak=0.0168)
        quiet.update(quiet_extra or {})

        class Runner:
            def __init__(self, *args):
                self.session = type("S", (), {"txlog": lambda self, on: None,
                                             "close": lambda self: None})()

            def normalize(self):
                pass

            def set_knob(self, knob, wanted, value_box, maximum):
                order.append(("volume", wanted))
                return wanted

        class Hwsuite:
            @staticmethod
            def make_fixtures(path, sample_dir):
                pass

            @staticmethod
            def fixture_wavs():
                return []

            @staticmethod
            def stick_image(wavs, sample_dir):
                return b"", 0

            @staticmethod
            def write_sparse(path, payload, total):
                pass

            @staticmethod
            def launch(build, profile, stick, ephemeral=False):
                return None, "runtime", 1.0  # no pid: nothing to stop

            @staticmethod
            def stop(build, pid):
                order.append(("stop", pid))

            @staticmethod
            def Session(runtime):
                return Runner().session

        Hwsuite.Runner = Runner

        class Profiles:
            @staticmethod
            def validate(profile):
                return profile

        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        build = work / "build"
        profile = {"usb": {"sample_dir": "PIONEER DJ SAMPLER/Samples"},
                   "recipes": {"volume_knob": {"knob": "volume", "value_box": "v", "max": 127}},
                   "identity": {"version": "1.60"}, "panel": {"lamp_roles": {"over_led": 34}},
                   "__path__": ""}
        profile_path = work / "profile.json"
        profile_path.write_text(json.dumps(profile))
        args = argparse.Namespace(build=str(build), profile=str(profile_path), track=3,
                                  volumes=[127], seconds=0.1)

        def fake_warm_up(runner, track, *a, **kw):
            order.append(("warm-up", track))

        def fake_window(runner, track, sample, seconds=3.0):
            order.append(("window", sample))
            return {"sample": sample}

        def fake_pass_figures(seen):
            return dict(quiet if seen["sample"] == "hw-tone-m30.wav" else figures)

        patches = [mock.patch.dict(sys.modules, {"hwsuite": Hwsuite, "profiles": Profiles}),
                   mock.patch.object(over_led, "warm_up", fake_warm_up),
                   mock.patch.object(over_led, "window", fake_window),
                   mock.patch.object(over_led, "pass_figures", fake_pass_figures),
                   mock.patch.object(over_led.time, "sleep", lambda seconds: None)]
        for patcher in patches:
            patcher.start()
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                report = over_led.run(args)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        return report, order, printed.getvalue()

    def test_the_warm_up_strike_runs_before_the_first_measured_pass(self):
        report, order, _ = self.sweep()
        self.assertEqual(order[0], ("warm-up", 3))
        self.assertEqual(order[1], ("volume", 127))
        self.assertEqual(order[2], ("window", "hw-tone-full.wav"))
        self.assertEqual(list(report["passes"]), ["full@127", "control"])

    def test_the_control_is_labelled_by_the_level_it_measured(self):
        report, _, _ = self.sweep()
        control = report["passes"]["control"]
        self.assertTrue(control["control"])
        self.assertEqual(control["peak_dbfs"], -35.5)
        self.assertEqual(control["label"], "-30 dBFS fixture control (measured -35.5 dBFS)")
        self.assertNotIn("m30", report["passes"])  # no pass is named after the fixture's nominal level

    def test_a_silent_control_is_labelled_silent_not_none(self):
        # the first strike of a run is silent, so a control that measures nothing is a real outcome, not a missing one
        report, _, printed = self.sweep(quiet_extra={"peak": 0, "peak_dbfs": None, "nonzero_samples": 0,
                                                     "main_out_gain_min": None, "main_out_gain_max": None,
                                                     "shim_out_peak": None})
        self.assertEqual(report["passes"]["control"]["label"],
                         "-30 dBFS fixture control (measured silent)")
        self.assertIn("measured silent", printed)
        self.assertNotIn("measured None", printed)

    def test_a_pass_with_no_gain_reading_prints_na(self):
        _, _, printed = self.sweep(quiet_extra={"main_out_gain_min": None, "main_out_gain_max": None,
                                                "shim_out_peak": None})
        self.assertIn("main_out_gain=MAIN OUT gain n/a", printed)
        self.assertNotIn("gain None", printed)


if __name__ == "__main__":
    unittest.main()
