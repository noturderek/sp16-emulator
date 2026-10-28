"""MIDI clock engine: the DLL recovers a jittered clock, the generator derives pulses from the sample position.

Golden values are computed from the 24 PPQN definition, not from the code's own tables: a 120 BPM clock has period
60 / (120 * 24) s = 20.8333 ms; a ±2 ms jittered stream of it must be recovered within 0.1 BPM and 0.3 ms.
"""
import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from midi import clock  # noqa: E402

TRUE_PERIOD = 60.0 / (120.0 * 24.0)  # 20.8333 ms at 120 BPM


def jittered_clock(bpm, jitter_ms, count, seed=1):
    """A synthetic clock: events at i*period plus uniform +/-jitter_ms, as host timestamps in seconds."""
    rng = random.Random(seed)
    period = clock.clock_period_seconds(bpm)
    times = []
    t = 0.0
    for _ in range(count):
        t += period
        times.append(t + rng.uniform(-jitter_ms, jitter_ms) / 1000.0)
    return times


class ClockDLLTests(unittest.TestCase):
    def test_recovers_a_jittered_120_bpm_clock(self):
        events = jittered_clock(120.0, 2.0, 400)
        dll = clock.ClockDLL(nominal_bpm=120.0)
        for t in events:
            dll.tick(t)
        self.assertTrue(dll.locked)
        # The last 200 events are well past lock-in; the estimate must sit within the acceptance window.
        self.assertAlmostEqual(dll.bpm, 120.0, delta=0.1)
        self.assertAlmostEqual(dll.period, TRUE_PERIOD, delta=0.0003)

    def test_rejects_bad_constructor_arguments(self):
        for args in ((0.0,), (-1.0,), (120.0, 0.0), (120.0, -2.0)):
            with self.assertRaises(ValueError, msg=args):
                clock.ClockDLL(*args)

    def test_first_event_sets_the_centre_and_is_not_locked(self):
        dll = clock.ClockDLL()
        dll.tick(100.0)
        self.assertFalse(dll.locked)
        self.assertEqual(dll.centre, 100.0)
        self.assertEqual(dll.period, TRUE_PERIOD)  # still the nominal period

    def test_release_time_is_a_fixed_latency_after_the_edge(self):
        dll = clock.ClockDLL()
        self.assertAlmostEqual(dll.release_time(12.5), 12.5 + clock.RELEASE_LATENCY)

    def test_period_bpm_helpers_round_trip(self):
        for bpm in (60.0, 120.0, 174.0):
            self.assertAlmostEqual(clock.bpm_from_period(clock.clock_period_seconds(bpm)), bpm)


class ClockGeneratorTests(unittest.TestCase):
    def test_emits_24_pulses_per_beat_at_sample_accuracy(self):
        gen = clock.ClockGenerator(bpm=120.0, sample_rate=44100.0)
        # One beat = 0.5 s = 22050 samples; pulses land every 22050/24 = 918.75 samples, so the integer grid
        # alternates 918 and 919 samples and the count over one beat is exactly 24. The first pulse is one period
        # after the transport start (Start precedes the first clock).
        pulses = gen.pulses(22050)
        self.assertEqual(len(pulses), 24)
        self.assertEqual(pulses[0], 918)
        self.assertEqual(sorted(pulses), pulses)
        spacing = [b - a for a, b in zip(pulses, pulses[1:])]
        self.assertTrue(all(918 <= s <= 919 for s in spacing), spacing[:6])

    def test_bpm_change_rescales_without_losing_the_grid(self):
        gen = clock.ClockGenerator(bpm=120.0)
        gen.pulses(1000)
        gen.set_bpm(60.0)
        pulses = gen.pulses(22050)  # at 60 BPM one beat is 1 s = 44100 samples -> 11 pulses in half of it
        self.assertGreater(len(pulses), 0)
        spacing = [b - a for a, b in zip(pulses, pulses[1:])]
        self.assertTrue(all(1837 <= s <= 1838 for s in spacing), spacing[:6])

    def test_rejects_bad_arguments(self):
        for args in ((0.0,), (-120.0,), (120.0, 0.0)):
            with self.assertRaises(ValueError, msg=args):
                clock.ClockGenerator(*args)
        gen = clock.ClockGenerator()
        with self.assertRaises(ValueError):
            gen.set_bpm(0.0)


if __name__ == "__main__":
    unittest.main()
