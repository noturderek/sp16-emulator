"""timing_probe.py on synthetic data: onset jitter from a WAV, TX stalls from arrival times, WAV and log readers."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import timing_probe as probe  # noqa: E402

try:
    import numpy as np
except ImportError:  # the suite's own audio checks need numpy too
    np = None

STEP = 60 / 120 / 4 * probe.RATE  # 5512.5 samples


def clicks(positions, length, level=0.8):
    x = np.zeros(length)
    for p in positions:
        x[p:p + 40] = level * np.hanning(40)[:min(40, length - p)] + 0.0
        x[p] = level  # a hard first sample, as the click sample has
    return x


def ringing_clicks(positions, length):
    """The suite's click fixture: an 80 ms 440 Hz burst with a 20 ms decay (above 30 % of its peak for about 24 ms)."""
    x = np.zeros(length)
    n = np.arange(int(0.08 * probe.RATE))
    burst = 0.5 * np.exp(-n / probe.RATE / 0.02) * np.sin(2 * np.pi * 440 * n / probe.RATE + 1.5)
    for p in positions:
        end = min(length, p + len(burst))
        x[p:end] += burst[:end - p]
    return x


@unittest.skipUnless(np, "numpy not available")
class OnsetTests(unittest.TestCase):
    def test_a_click_already_sounding_at_the_window_start_is_not_a_late_onset(self):
        positions = [round(20000 + i * STEP) for i in range(40)]
        samples = ringing_clicks(positions, positions[-1] + 6000)
        start = positions[3] + int(0.010 * probe.RATE)          # the window opens 10 ms into the fourth click
        window = samples[start:]
        # without look-back the ringing tail is taken for an onset at the first sample
        naive = probe.onset_jitter(probe.find_onsets(window, refractory=int(0.5 * STEP)), STEP)
        self.assertGreater(naive["jitter_max_ms"], 5.0)
        preroll = probe.PREROLL
        found = probe.onsets_in_window(samples[start - preroll:], preroll, int(0.5 * STEP))
        self.assertEqual(found[0], positions[4] - start)
        report = probe.onset_jitter(found, STEP)
        self.assertLess(report["jitter_max_samples"], 1.0)
        self.assertEqual(report["onsets"], len(positions) - 4)

    def test_perfect_clock_has_no_jitter_and_the_exact_tempo(self):
        positions = [round(1000 + i * STEP) for i in range(32)]
        report = probe.onset_jitter(probe.find_onsets(clicks(positions, positions[-1] + 3000), refractory=int(0.5 * STEP)), STEP)
        self.assertEqual(report["onsets"], 32)
        self.assertLessEqual(report["jitter_sigma_samples"], 0.5)  # rounding of the half sample only
        self.assertAlmostEqual(report["tempo_bpm"], 120.0, places=1)
        self.assertEqual(report["missing_steps"], 0)

    def test_a_late_click_is_reported_in_samples(self):
        positions = [round(1000 + i * STEP) for i in range(32)]
        positions[10] += 441  # 10 ms late
        report = probe.onset_jitter(probe.find_onsets(clicks(positions, positions[-1] + 3000), refractory=int(0.5 * STEP)), STEP)
        self.assertGreater(report["jitter_max_samples"], 400)
        self.assertGreater(report["jitter_sigma_ms"], 1.0)
        self.assertGreater(report["interval_max_ms"], 130)

    def test_one_dropout_inflates_sigma_but_not_the_robust_figure(self):
        positions = [round(1000 + i * STEP) for i in range(60)]
        positions[30] += 22 * 44  # one click detected 22 ms late, as when its start fell into a hole in the recording
        report = probe.onset_jitter(probe.find_onsets(clicks(positions, positions[-1] + 3000), refractory=int(0.5 * STEP)), STEP)
        self.assertGreater(report["jitter_sigma_ms"], 2.0)
        self.assertLess(report["jitter_sigma_robust_ms"], 0.1)
        self.assertEqual(report["onsets_off_by_over_1ms"], 1)
        self.assertEqual((report["short_intervals"], report["long_intervals"]), (1, 1))

    def test_a_tempo_error_is_absorbed_by_the_fit(self):
        step = STEP * 1.0007
        positions = [round(1000 + i * step) for i in range(40)]
        report = probe.onset_jitter(probe.find_onsets(clicks(positions, positions[-1] + 3000), refractory=int(0.5 * STEP)), STEP)
        self.assertLess(report["jitter_sigma_ms"], 0.1)
        self.assertAlmostEqual(report["tempo_bpm"], 120 / 1.0007, places=2)

    def test_a_missing_click_does_not_shift_the_step_numbers(self):
        positions = [round(1000 + i * STEP) for i in range(32) if i != 7]
        report = probe.onset_jitter(probe.find_onsets(clicks(positions, positions[-1] + 3000), refractory=int(0.5 * STEP)), STEP)
        self.assertEqual(report["missing_steps"], 1)
        self.assertLessEqual(report["jitter_sigma_samples"], 0.5)

    def test_wav_reader_returns_one_channel_between_frames(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "x.wav"
            frames, channels = 100, 4
            body = b"".join(struct.pack("<4h", f, -f, 2 * f, 0) for f in range(frames))
            head = (b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, channels, 44100,
                    44100 * 8, 8, 16) + b"data" + struct.pack("<I", len(body)))
            path.write_bytes(head + body)
            ch2 = probe.read_wav_channel(path, 10, 20, 2)
            self.assertEqual(list(np.round(ch2 * 32768).astype(int)), [2 * f for f in range(10, 20)])


class TxTests(unittest.TestCase):
    def test_steady_frames_have_no_stalls(self):
        report = probe.tx_stalls([i * 0.015 for i in range(1000)])
        self.assertAlmostEqual(report["period_mean_ms"], 15.0, places=1)
        self.assertEqual(report["stalls_over_limit"], 0)
        self.assertLess(report["stall_max_ms"], 16)

    def test_a_stall_is_counted_and_its_length_reported(self):
        times = [i * 0.015 for i in range(200)] + [200 * 0.015 + 0.164 + i * 0.015 for i in range(200)]
        report = probe.tx_stalls(times)
        self.assertEqual(report["stalls_over_limit"], 1)
        self.assertAlmostEqual(report["stall_max_ms"], 179.0, delta=1)

    def test_log_reader_takes_times_from_a_byte_offset(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "tx-frames.jsonl"
            lines = [json.dumps({"t": 1.0 + i * 0.01, "hex": "00"}) for i in range(5)]
            path.write_text("\n".join(lines) + "\n{partial")
            offset = len(lines[0]) + 1
            self.assertEqual(probe.read_tx_times(path, offset), [1.01, 1.02, 1.03, 1.04])
            self.assertEqual(probe.read_tx_times(Path(work)), [1.0, 1.01, 1.02, 1.03, 1.04])

    def test_verdict_uses_the_task_targets(self):
        good = {"audio": {"jitter_sigma_ms": 0.2, "gap_frames_in_window": 0, "guest_drops_in_window": 0},
                "tx": {"stall_max_ms": 30.0}}
        bad = {"audio": {"jitter_sigma_ms": 1.5, "gap_frames_in_window": 4096, "guest_drops_in_window": 4096},
               "tx": {"stall_max_ms": 164.0}}
        self.assertEqual(probe.verdict(good), {"onset_sigma_below_1ms": True, "tx_stall_max_below_40ms": True,
                                               "audio_window_clean": True})
        self.assertEqual(probe.verdict(bad), {"onset_sigma_below_1ms": False, "tx_stall_max_below_40ms": False,
                                              "audio_window_clean": False})

    def test_zero_is_a_measurement_and_a_missing_figure_is_not(self):
        perfect = {"audio": {"jitter_sigma_ms": 0.0, "gap_frames_in_window": 0, "guest_drops_in_window": 0},
                   "tx": {"stall_max_ms": 0}}
        self.assertEqual(probe.verdict(perfect), {"onset_sigma_below_1ms": True, "tx_stall_max_below_40ms": True,
                                                  "audio_window_clean": True})
        none_at_all = {"audio": {"error": "too few onsets"}, "tx": {"error": "too few frames"}}
        self.assertEqual(probe.verdict(none_at_all), {"onset_sigma_below_1ms": False, "tx_stall_max_below_40ms": False,
                                                      "audio_window_clean": False})
        self.assertEqual(probe.verdict({}), {"onset_sigma_below_1ms": False, "tx_stall_max_below_40ms": False,
                                             "audio_window_clean": False})

    def test_the_limits_are_strict(self):
        at_limit = {"audio": {"jitter_sigma_ms": 1.0, "gap_frames_in_window": 0, "guest_drops_in_window": 0},
                    "tx": {"stall_max_ms": 40.0}}
        under = {"audio": {"jitter_sigma_ms": 0.999, "gap_frames_in_window": 0, "guest_drops_in_window": 0},
                 "tx": {"stall_max_ms": 39.9}}
        self.assertEqual(probe.verdict(at_limit)["onset_sigma_below_1ms"], False)
        self.assertEqual(probe.verdict(at_limit)["tx_stall_max_below_40ms"], False)
        self.assertEqual(probe.verdict(under)["onset_sigma_below_1ms"], True)
        self.assertEqual(probe.verdict(under)["tx_stall_max_below_40ms"], True)

    def test_a_stall_the_bridge_also_saw_sits_at_or_before_its_receive(self):
        report = {"tx": {"stalls_over_limit": 1},
                  "link": {"gaps_over_limit": 1, "gap_max_ms": 76.5}}
        self.assertEqual(probe.stall_hop(report), "guest-to-bridge")

    def test_a_stall_only_the_app_logged_sits_after_the_bridge(self):
        report = {"tx": {"stalls_over_limit": 1}, "link": {"gaps_over_limit": 0, "gap_max_ms": None}}
        self.assertEqual(probe.stall_hop(report), "bridge-to-app")

    def test_a_clean_window_says_none_and_a_blind_one_says_nothing(self):
        self.assertEqual(probe.stall_hop({"tx": {"stalls_over_limit": 0}, "link": {"gaps_over_limit": 0}}), "none")
        self.assertIsNone(probe.stall_hop({"tx": {"error": "too few frames"}, "link": {"gaps_over_limit": 0}}))
        self.assertIsNone(probe.stall_hop({"tx": {"stalls_over_limit": 2}, "link": None}))

    def test_the_link_window_takes_counter_differences_and_a_growing_maximum(self):
        before = {"tx_recv": 4000, "tx_gaps_over_40ms": 2, "tx_gap_max_ms": 50.0, "lock_wait_max_ms": 3.0,
                  "frame_write_max_ms": 9.0, "outbox_dropped": 0}
        after = {"tx_recv": 4100, "tx_gaps_over_40ms": 3, "tx_gap_max_ms": 76.5, "lock_wait_max_ms": 3.0,
                 "frame_write_max_ms": 9.0, "outbox_dropped": 0}
        window = probe.link_window(before, after)
        self.assertEqual(window["tx_recv"], 100)
        self.assertEqual(window["gaps_over_limit"], 1)
        self.assertEqual(window["gap_max_ms"], 76.5)          # it grew, so this is the window's own maximum
        self.assertEqual(window["gap_max_ms_at_session_start"], 50.0)
        self.assertIsNone(probe.link_window(before, {**after, "tx_gap_max_ms": 50.0})["gap_max_ms"])
        self.assertIsNone(probe.link_window(None, after))
        self.assertEqual(probe.link_window(before, {**after, "tx_recv": 4100})["tx_recv"], 100)

    def test_a_lost_frame_makes_the_window_unclean_whichever_counter_shows_it(self):
        base = {"jitter_sigma_ms": 0.0}
        self.assertFalse(probe.verdict({"audio": {**base, "gap_frames_in_window": 64, "guest_drops_in_window": 0}})["audio_window_clean"])
        self.assertFalse(probe.verdict({"audio": {**base, "gap_frames_in_window": 0, "guest_drops_in_window": 64}})["audio_window_clean"])


def load_bridge():
    """Counters, LinkTiming and board_line from the guest bridge (the module itself needs the guest's libraries)."""
    source = (TOOLS / "guest-bridge.py").read_text()
    head = source[source.index("class Counters:"):source.index("def main():")]
    scope = {}
    exec("import re, threading, time\n" + head, scope)
    return scope


class LinkTimingTests(unittest.TestCase):
    def test_gaps_waits_and_frame_writes_are_counted_and_published(self):
        scope = load_bridge()
        counters = scope["Counters"]()
        link = scope["LinkTiming"](counters)
        for t in (0.0, 0.015, 0.030, 0.200, 0.215):  # one 170 ms gap
            link.arrival(t)
        link.write(1_536_000, waited=0.050, took=0.030)
        link.write(40, waited=0.001, took=0.0001)  # a lamp message: not a frame
        link.outbox(7)
        link.outbox(4096, dropped=True)
        link.report(100.0)
        published = counters.snapshot()["board"]["bridge_timing"]
        self.assertEqual((published["tx_recv"], published["tx_gaps_over_40ms"], published["tx_gap_max_ms"]), (5, 1, 170.0))
        self.assertEqual((published["lock_wait_max_ms"], published["frame_writes"], published["frame_write_max_ms"]),
                         (50.0, 1, 30.0))
        self.assertEqual((published["outbox_max"], published["outbox_dropped"]), (4096, 1))

    def test_report_is_throttled_to_once_a_second(self):
        scope = load_bridge()
        counters = scope["Counters"]()
        link = scope["LinkTiming"](counters)
        link.report(10.0)
        link.arrival(10.1)
        link.report(10.5)
        self.assertEqual(counters.snapshot()["board"]["bridge_timing"]["tx_recv"], 0)
        link.report(11.1)
        self.assertEqual(counters.snapshot()["board"]["bridge_timing"]["tx_recv"], 1)

    def test_guest_side_drops_reach_the_metrics(self):
        board_line = load_bridge()["board_line"]
        self.assertEqual(board_line("[sp16-ui-stub] panel TX frames dropped=8 errno=11"), ("tx_dropped_in_guest", 8))

    def test_stats_stay_under_the_wire_limit_with_every_board_report(self):
        scope = load_bridge()
        counters = scope["Counters"]()
        link = scope["LinkTiming"](counters)
        link.arrival(0.0); link.write(1_536_000, 0.1, 0.1); link.report(5.0)
        for line in ("[sp16-ui-stub] dac hpf word=0x1abc ctrl=1 code=2748 writes=7",
                     "[sp16-ui-stub] filter loop send_peak=0.5000 return_peak=0.2500 frames=44100 underruns=0 drops=0 lp_hz=1000 hp=dac",
                     "[sp16-ui-stub] analog levels limiter=1 in_peak=0.9770 in_dbu_peak=15.8 out_peak=0.2500 out_dbu_peak=4.0 limited=412 "
                     "main_out_gain=0.2500 input_gain=1.0000 source=wav",
                     "[sp16-ui-stub] gpio GPIO5_IO05=1 SEL_xTHRU/DSF", "[sp16-ui-stub] gpio GPIO5_IO06=1 LMT_xOFF/ON",
                     "[sp16-ui-stub] panel TX frames dropped=1048576 errno=11"):
            counters.board_update(*scope["board_line"](line))
        stats = {**counters.snapshot(), "adc": {"main_out": 512, "drive": 1023, "hp_cutoff": 1023, "0x26": 1, "0x28": 2, "0x2a": 3},
                 "rx": {"buttons": "0" * 10, "pads": []}}
        self.assertLess(len(json.dumps(stats, sort_keys=True, separators=(",", ":"))), 1500)  # the wire limit is 2048


if __name__ == "__main__":
    unittest.main()
