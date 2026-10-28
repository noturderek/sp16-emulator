"""Tests for emulator/probes/filter_send.py: which stream figures it reads, and how it folds a sampling window."""
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS / "probes"))

import filter_send  # noqa: E402


class FakeSession:
    """A session whose metrics the test sets by hand, one reading per call."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = 0

    def metrics(self):
        index = min(self.calls, len(self.readings) - 1)
        self.calls += 1
        return self.readings[index]


def reading(send_peak=0.0, nonzero=0, loop=(0.0, 0.0, 0), master_peak=0.0):
    return {"audio": {"streams": {
        "ak4452audio-1": {"peak": send_peak, "nonzero_frames": nonzero, "frames": 1000},
        "ak4458audio-0": {"peak": master_peak, "nonzero_frames": 1000, "frames": 1000}}},
        "guest": {"board": {"filter_loop": {"send_peak": loop[0], "return_peak": loop[1], "underruns": loop[2],
                                            "frames": 5000}}}}


class MetricTest(unittest.TestCase):
    def test_a_stream_name_becomes_the_sinks_key(self):
        figures = filter_send.stream_figures(reading(send_peak=0.25, nonzero=12), "hw:ak4452audio,1")
        self.assertEqual(figures["peak"], 0.25)
        self.assertEqual(figures["nonzero_frames"], 12)

    def test_a_missing_stream_reads_as_none_not_as_zero(self):
        figures = filter_send.stream_figures({"audio": {"streams": {}}, "guest": {}}, "hw:ak4452audio,1")
        self.assertIsNone(figures["peak"])
        self.assertIsNone(figures["nonzero_frames"])

    def test_the_loop_figures_come_from_the_board_report(self):
        self.assertEqual(filter_send.loop_figures(reading(loop=(0.5, 0.25, 3))),
                         {"send_peak": 0.5, "return_peak": 0.25, "underruns": 3, "frames": 5000})

    def test_one_reading_carries_every_figure(self):
        seen = filter_send.measure(reading(send_peak=0.1, nonzero=4, loop=(0.2, 0.3, 1), master_peak=0.9))
        self.assertEqual(seen, {"send_peak": 0.1, "nonzero_send_frames": 4, "master_peak": 0.9,
                                "loop_send_peak": 0.2, "loop_return_peak": 0.3, "loop_underruns": 1})

    def test_a_silent_reading_is_zero_not_missing(self):
        self.assertEqual(filter_send.measure(reading())["send_peak"], 0.0)


class SampleWindowTest(unittest.TestCase):
    def test_each_figure_is_the_worst_seen_in_the_window(self):
        session = FakeSession([reading(send_peak=0.1, nonzero=5, loop=(0.2, 0.3, 1), master_peak=0.4),
                               reading(send_peak=0.3, nonzero=2, loop=(0.1, 0.5, 0), master_peak=0.9),
                               reading(send_peak=0.2, nonzero=9, loop=(0.4, 0.2, 2), master_peak=0.5)])
        with mock.patch.object(filter_send.time, "sleep"), mock.patch.object(filter_send.time, "monotonic",
                                                                            side_effect=[0, 0, 0, 0, 5]):
            seen = filter_send.sample(session, 1.0)
        self.assertEqual(seen["send_peak"], 0.3)
        self.assertEqual(seen["nonzero_send_frames"], 9)
        self.assertEqual(seen["loop_send_peak"], 0.4)
        self.assertEqual(seen["loop_return_peak"], 0.5)
        self.assertEqual(seen["loop_underruns"], 2)
        self.assertEqual(seen["master_peak"], 0.9)

    def test_a_wholly_silent_window_reports_zeroes(self):
        session = FakeSession([reading()])
        with mock.patch.object(filter_send.time, "sleep"), mock.patch.object(filter_send.time, "monotonic",
                                                                            side_effect=[0, 0, 5]):
            seen = filter_send.sample(session, 1.0)
        self.assertEqual(seen["send_peak"], 0.0)
        self.assertEqual(seen["nonzero_send_frames"], 0)

    def test_an_empty_metrics_reply_does_not_raise(self):
        session = FakeSession([{}])
        with mock.patch.object(filter_send.time, "sleep"), mock.patch.object(filter_send.time, "monotonic",
                                                                            side_effect=[0, 0, 5]):
            seen = filter_send.sample(session, 1.0)
        self.assertEqual(seen["send_peak"], 0.0)


if __name__ == "__main__":
    unittest.main()
