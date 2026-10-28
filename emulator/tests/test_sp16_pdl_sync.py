"""emulator/sync/pdl_sync.py: Pro DJ Link tempo -> MIDI clock and Ableton Link (Carabiner)."""
import contextlib
import io
import socket
import unittest
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "net"))
import sync.pdl_sync as pdl  # noqa: E402
import fake_master  # noqa: E402


def beat_packet(bpm=128.0, pitch_frac=0.02, beat_in_bar=1):
    """A master 0x28 beat packet built by the shared packet builder (net/fake_master.py), not by hand."""
    return fake_master.beat_packet("CDJ-3000", 1, bpm, pitch_frac, beat_in_bar, 100)


def status_packet(bpm=128.0, pitch_frac=0.02, beat_in_bar=1, beat_count=0, playing=True):
    """A master 0x0a status packet built by the shared packet builder, not by hand."""
    return fake_master.status_packet("CDJ-3000", 1, bpm, pitch_frac, beat_in_bar, beat_count, playing)


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

    def test_parse_status_reply(self):
        self.assertEqual(pdl.CarabinerLink.parse_status(b"{:peers 1 :bpm 120.0 :start 5082765300 :beat 3.0}"),
                         {"peers": 1, "bpm": 120.0, "start": 5082765300, "beat": 3.0})

    def test_parse_status_refuses_an_incomplete_reply(self):
        self.assertIsNone(pdl.CarabinerLink.parse_status(b"{:peers 1 :bpm 120.0}"))

    def test_tempo_and_phase_report_one_diagnostic_without_a_carabiner(self):
        # a loopback port that nothing listens on: connect() gets ECONNREFUSED immediately
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        link = pdl.CarabinerLink(port=port)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(link.set_tempo(130.56))
            self.assertFalse(link.force_beat_at_time(0.0, 12345678, 4))
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1)  # one diagnostic, not one per call
        self.assertIn(f"127.0.0.1:{port}", lines[0])
        self.assertIn("Link tempo/phase sync is off", lines[0])
        self.assertIsNone(link.sock)  # the refused socket is dropped, so a later send retries the connect
        link.close()

    def test_tempo_is_sent_when_a_carabiner_listens(self):
        server = socket.create_server(("127.0.0.1", 0))
        try:
            link = pdl.CarabinerLink(port=server.getsockname()[1])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertTrue(link.set_tempo(130.56))
            conn, _ = server.accept()
            try:
                self.assertEqual(conn.recv(64), b"set-tempo 130.560000\n")
            finally:
                conn.close()
            link.close()
            self.assertEqual(err.getvalue(), "")  # a listening Carabiner produces no diagnostic
        finally:
            server.close()


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
                self.forced = []
            def set_tempo(self, bpm):
                self.tempos.append(bpm)
            def force_beat_at_time(self, beat, when, quantum):
                self.forced.append((beat, when, quantum))
        link = FakeLink()
        bridge = pdl.PdlClockBridge(link=link)
        bridge.feed_beat(beat_packet(128.0, 0.02, 1), 0.0)
        bridge.feed_beat(beat_packet(128.0, 0.02, 2), 1.0)
        self.assertEqual(len(link.tempos), 1)  # one tempo, no repeat for the same BPM
        self.assertAlmostEqual(link.tempos[0], 130.56, delta=0.01)

    def test_bridge_forces_phase_on_the_downbeat(self):
        class FakeLink:
            def __init__(self):
                self.forced = []
            def set_tempo(self, bpm):
                pass
            def force_beat_at_time(self, beat, when, quantum):
                self.forced.append((beat, when, quantum))
        link = FakeLink()
        bridge = pdl.PdlClockBridge(link=link, now_micros=lambda: 1_000_000, host_now=lambda: 0.0)
        bridge.feed_beat(beat_packet(128.0, 0.02, 1), 0.0)  # a downbeat
        self.assertEqual(len(link.forced), 1)
        beat, when, quantum = link.forced[0]
        self.assertEqual(beat, 0.0)          # beat 0 of the next bar
        self.assertEqual(quantum, 4)
        self.assertGreater(when, 1_000_000)  # a future Link time, one bar-period ahead
        self.assertAlmostEqual(when - 1_000_000, 4 * 60 / 130.56 * 1e6, delta=10)

    def test_bridge_does_not_force_on_non_downbeats(self):
        class FakeLink:
            def __init__(self):
                self.forced = []
            def set_tempo(self, bpm):
                pass
            def force_beat_at_time(self, beat, when, quantum):
                self.forced.append((beat, when, quantum))
        link = FakeLink()
        bridge = pdl.PdlClockBridge(link=link)
        bridge.feed_beat(beat_packet(128.0, 0.02, 2), 0.0)
        bridge.feed_beat(beat_packet(128.0, 0.02, 3), 0.5)
        self.assertEqual(link.forced, [])


if __name__ == "__main__":
    unittest.main()
