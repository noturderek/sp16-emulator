"""emulator/sync/pdl_sync.py: Pro DJ Link tempo -> MIDI clock and Ableton Link (Carabiner)."""
import socket
import struct
import unittest
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import sync.pdl_sync as pdl  # noqa: E402


def beat_packet(bpm=128.0, pitch_frac=0.02, beat_in_bar=1):
    packet = bytearray(96)
    packet[0:10] = pdl.MAGIC
    packet[0x0a] = 0x28
    struct.pack_into(">IHHBHB", packet, 0x54, int(round((1 + pitch_frac) * pdl.PITCH_SCALE)), 0,
                     int(round(bpm * 100)), beat_in_bar, 0, 1)
    return bytes(packet)


def status_packet(bpm=128.0, pitch_frac=0.02, beat_in_bar=1, beat_count=0, playing=True):
    packet = bytearray(212)
    packet[0:10] = pdl.MAGIC
    packet[0x0a] = 0x0a
    packet[0x89] = 0x84 | (0x40 if playing else 0) | 0x20 | 0x10 | 0x08
    struct.pack_into(">I", packet, 0x8c, int(round((1 + pitch_frac) * pdl.PITCH_SCALE)))
    struct.pack_into(">H", packet, 0x92, int(round(bpm * 100)))
    struct.pack_into(">I", packet, 0xa0, beat_count)
    packet[0xa6] = beat_in_bar
    return bytes(packet)


class TempoTests(unittest.TestCase):
    def test_effective_bpm_is_bpm_times_pitch(self):
        self.assertAlmostEqual(pdl.effective_bpm(128.0, 0.02), 130.56)
        self.assertAlmostEqual(pdl.effective_bpm(128.0, 0.0), 128.0)
        self.assertAlmostEqual(pdl.effective_bpm(124.0, 0.0), 124.0)

    def test_pitch_fraction_round_trips(self):
        self.assertAlmostEqual(pdl.pitch_fraction(int(1.02 * pdl.PITCH_SCALE)), 0.02, places=6)
        self.assertAlmostEqual(pdl.pitch_fraction(pdl.PITCH_SCALE), 0.0)

    def test_parse_beat_packet(self):
        beat = pdl.parse_beat_packet(beat_packet(128.0, 0.02, 3))
        self.assertAlmostEqual(beat["bpm"], 128.0)
        self.assertAlmostEqual(beat["pitch_frac"], 0.02, places=5)
        self.assertEqual(beat["beat_in_bar"], 3)

    def test_parse_beat_rejects_non_beat(self):
        with self.assertRaises(ValueError):
            pdl.parse_beat_packet(status_packet())

    def test_parse_status_packet(self):
        status = pdl.parse_status_packet(status_packet(124.0, 0.0, 2, 7))
        self.assertAlmostEqual(status["bpm"], 124.0)
        self.assertAlmostEqual(status["pitch_frac"], 0.0, places=5)
        self.assertEqual((status["beat_in_bar"], status["beat_count"]), (2, 7))
        self.assertTrue(status["playing"] and status["master"])


class TrackerTests(unittest.TestCase):
    def test_tracker_tracks_tempo_and_downbeat(self):
        tracker = pdl.TempoTracker()
        tracker.update(128.0, 0.02, 4, 100.0)
        self.assertAlmostEqual(tracker.effective_bpm, 130.56)
        tracker.update(128.0, 0.02, 1, 100.5)  # downbeat
        period = tracker.bar_period
        self.assertAlmostEqual(period, 4 * 60 / 130.56)
        # the downbeat itself is the next one at-or-after 100.5; the next bar starts one period later
        self.assertAlmostEqual(tracker.next_downbeat(100.5), 100.5)
        self.assertAlmostEqual(tracker.next_downbeat(100.6), 100.5 + period)

    def test_tracker_requires_tempo_before_bar_period(self):
        with self.assertRaises(ValueError):
            pdl.TempoTracker().bar_period


class CarabinerTests(unittest.TestCase):
    def test_message_formatting(self):
        self.assertEqual(pdl.CarabinerLink.set_tempo_message(130.56), b"set-tempo 130.560000\n")
        self.assertEqual(pdl.CarabinerLink.force_beat_at_time_message(1.0, 12345678, 4),
                         b"force-beat-at-time 1.000000 12345678 4\n")


class BridgeTests(unittest.TestCase):
    def test_bridge_retunes_midi_clock_on_tempo_change(self):
        from midi import clock
        generator = clock.ClockGenerator(bpm=120.0)
        bridge = pdl.PdlClockBridge(generator=generator)
        bridge.feed_beat(beat_packet(128.0, 0.02, 1), 0.0)
        self.assertAlmostEqual(generator.bpm, 130.56, delta=0.01)
        # a change to 124 BPM retunes the generator and would set-tempo again
        bridge.feed_beat(beat_packet(124.0, 0.0, 1), 1.0)
        self.assertAlmostEqual(generator.bpm, 124.0)

    def test_bridge_sends_tempo_once_when_unchanged(self):
        class FakeLink:
            def __init__(self):
                self.tempos = []
            def set_tempo(self, bpm):
                self.tempos.append(bpm)
        link = FakeLink()
        bridge = pdl.PdlClockBridge(link=link)
        bridge.feed_beat(beat_packet(128.0, 0.02, 1), 0.0)
        bridge.feed_beat(beat_packet(128.0, 0.02, 2), 1.0)
        self.assertEqual(len(link.tempos), 1)  # one tempo, no repeat for the same BPM
        self.assertAlmostEqual(link.tempos[0], 130.56, delta=0.01)


if __name__ == "__main__":
    unittest.main()
