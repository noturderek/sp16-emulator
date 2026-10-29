"""emulator/sync/pdl_sync.py: Pro DJ Link tempo -> MIDI clock and Ableton Link (Carabiner)."""
import contextlib
import io
import socket
import threading
import time
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


def status_packet(bpm=128.0, pitch_frac=0.02, beat_in_bar=1, beat_count=0, playing=True, master=True):
    """A master 0x0a status packet built by the shared packet builder, not by hand."""
    return fake_master.status_packet("CDJ-3000", 1, bpm, pitch_frac, beat_in_bar, beat_count, playing=playing,
                                       master=master)


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

    def test_parse_status_rejects_packets_shorter_than_the_fields_it_reads(self):
        # The last field is beat_in_bar at offset 0xa6, so anything under 0xa7 bytes is too short and must be a
        # ValueError, not an IndexError or struct.error escaping the receive loop.
        full = status_packet()
        for length in (0x93, 0xa0, 0xa6):
            with self.assertRaises(ValueError, msg=f"length {length}"):
                pdl.parse_status_packet(full[:length])


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
        # Carabiner's tempo command is `bpm` (its README); it ignores anything else
        self.assertEqual(pdl.CarabinerLink.set_tempo_message(130.56), b"bpm 130.560000\n")
        self.assertEqual(pdl.CarabinerLink.force_beat_at_time_message(1.0, 12345678, 4),
                         b"force-beat-at-time 1.000000 12345678 4\n")

    def test_parse_status_reply(self):
        self.assertEqual(pdl.CarabinerLink.parse_status(b"{:peers 1 :bpm 120.0 :start 5082765300 :beat 3.0}"),
                         {"peers": 1, "bpm": 120.0, "start": 5082765300, "beat": 3.0})

    def test_parse_status_refuses_an_incomplete_reply(self):
        self.assertIsNone(pdl.CarabinerLink.parse_status(b"{:peers 1 :bpm 120.0}"))

    def test_link_now_is_start_plus_elapsed_beats(self):
        # 4 beats at 120 BPM after beat 0 is 2 s after :start
        status = {"peers": 1, "bpm": 120.0, "start": 5_000_000, "beat": 4.0}
        self.assertEqual(pdl.CarabinerLink.link_now(status), 7_000_000)

    def test_status_skips_replies_to_earlier_commands(self):
        server = socket.create_server(("127.0.0.1", 0))
        try:
            link = pdl.CarabinerLink(port=server.getsockname()[1])
            self.assertTrue(link.set_tempo(130.56))
            conn, _ = server.accept()
            try:
                conn.recv(64)
                # the reply to `bpm`, then a stray non-status line, are waiting before `status` is asked
                conn.sendall(b"status { :peers 1 :bpm 130.560000 :start 1 :beat 0.0 }\nbad-beat \n")
                time.sleep(0.1)

                def answer():
                    conn.recv(64)
                    conn.sendall(b"status { :peers 1 :bpm 130.560000 :start 900 :beat 2.0 }\n")
                worker = threading.Thread(target=answer)
                worker.start()
                status = link.status()
                worker.join(2)
                self.assertEqual(status["start"], 900)  # the fresh reply, not the stale one
            finally:
                conn.close()
                link.close()
        finally:
            server.close()

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

    def test_an_outage_after_a_recovery_warns_once_more(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        link = pdl.CarabinerLink(port=port)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(link.set_tempo(130.56))  # nothing listening: one diagnostic, the latch is set
            server = socket.create_server(("127.0.0.1", port))
            try:
                self.assertTrue(link.set_tempo(130.56))  # Carabiner came up: the latch must clear
                conn, _ = server.accept()
                conn.close()
                server.close()  # Carabiner quit again
                link._drop()  # the dead socket is what the next send would find
                self.assertFalse(link.set_tempo(131.0))  # so the second outage warns rather than going silent
            finally:
                server.close()
                link.close()
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 2)  # one per outage, not one per run
        for line in lines:
            self.assertIn(f"127.0.0.1:{port}", line)

    def test_tempo_is_sent_when_a_carabiner_listens(self):
        server = socket.create_server(("127.0.0.1", 0))
        try:
            link = pdl.CarabinerLink(port=server.getsockname()[1])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertTrue(link.set_tempo(130.56))
            conn, _ = server.accept()
            try:
                self.assertEqual(conn.recv(64), b"bpm 130.560000\n")
            finally:
                conn.close()
            link.close()
            self.assertEqual(err.getvalue(), "")  # a listening Carabiner produces no diagnostic
        finally:
            server.close()


class BridgeTests(unittest.TestCase):
    def test_bridge_ignores_a_non_master_players_status(self):
        bridge = pdl.PdlClockBridge()
        bridge.feed_status(status_packet(128.0, 0.02, 1), 0.0)     # the master at 128 BPM +2 %
        self.assertAlmostEqual(bridge.tracker.effective_bpm, 130.56, delta=0.01)
        result = bridge.feed_status(status_packet(90.0, 0.0, 2, master=False), 1.0)  # a non-master player
        self.assertIsNone(result)                                   # not the master: no packet for the clock
        self.assertAlmostEqual(bridge.tracker.effective_bpm, 130.56, delta=0.01)     # the master's tempo is intact

    def test_bridge_retunes_midi_clock_on_tempo_change(self):
        from midi import clock
        generator = clock.ClockGenerator(bpm=120.0)
        bridge = pdl.PdlClockBridge(generator=generator)
        bridge.feed_beat(beat_packet(128.0, 0.02, 1), 0.0)
        self.assertAlmostEqual(generator.bpm, 130.56, delta=0.01)
        # a change to 124 BPM retunes the generator and would send `bpm` again
        bridge.feed_beat(beat_packet(124.0, 0.0, 1), 1.0)
        self.assertAlmostEqual(generator.bpm, 124.0)

    def test_bridge_sends_tempo_once_when_unchanged(self):
        class FakeLink:
            def __init__(self):
                self.tempos = []
                self.forced = []
            def set_tempo(self, bpm):
                self.tempos.append(bpm)
                return True
            def force_beat_at_time(self, beat, when, quantum):
                self.forced.append((beat, when, quantum))
        link = FakeLink()
        bridge = pdl.PdlClockBridge(link=link)
        bridge.feed_beat(beat_packet(128.0, 0.02, 1), 0.0)
        bridge.feed_beat(beat_packet(128.0, 0.02, 2), 1.0)
        self.assertEqual(len(link.tempos), 1)  # one tempo, no repeat for the same BPM
        self.assertAlmostEqual(link.tempos[0], 130.56, delta=0.01)

    def test_bridge_retries_a_failed_tempo_delivery(self):
        class FailingLink:
            def __init__(self):
                self.tempos = []
            def set_tempo(self, bpm):
                self.tempos.append(bpm)
                return False  # Carabiner not reachable: the delivery failed
            def force_beat_at_time(self, beat, when, quantum):
                pass
        link = FailingLink()
        bridge = pdl.PdlClockBridge(link=link)
        bridge.feed_beat(beat_packet(128.0, 0.02, 1), 0.0)
        bridge.feed_beat(beat_packet(128.0, 0.02, 2), 1.0)  # the same effective BPM
        self.assertEqual(len(link.tempos), 2)  # the failed delivery was not remembered, so it retried
        self.assertIsNone(bridge._sent_bpm)

    def test_bridge_forces_phase_on_the_downbeat(self):
        class FakeLink:
            def __init__(self):
                self.forced = []
            def set_tempo(self, bpm):
                return True
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
                return True
            def force_beat_at_time(self, beat, when, quantum):
                self.forced.append((beat, when, quantum))
        link = FakeLink()
        bridge = pdl.PdlClockBridge(link=link)
        bridge.feed_beat(beat_packet(128.0, 0.02, 2), 0.0)
        bridge.feed_beat(beat_packet(128.0, 0.02, 3), 0.5)
        self.assertEqual(link.forced, [])


if __name__ == "__main__":
    unittest.main()
