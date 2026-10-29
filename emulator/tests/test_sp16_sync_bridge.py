"""emulator/sync/bridge.py: the segment -> tempo -> MIDI clock pipeline, without CoreMIDI."""
import socket
import statistics
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "net"))

import net.segment as segment  # noqa: E402
import sync.bridge as bridge  # noqa: E402
import sync.pdl_sync as pdl  # noqa: E402


def load_fake_master():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fake_master", TOOLS / "net" / "fake_master.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_master = load_fake_master()


def framed(payload, dport):
    return segment.udp_frame("02:00:00:00:00:07", "ff:ff:ff:ff:ff:07", "169.254.7.7", "169.254.255.255",
                             dport, dport, payload)


class FeedTests(unittest.TestCase):
    def test_feed_retunes_the_bridge_from_a_beat_packet(self):
        bridge_obj = pdl.PdlClockBridge()
        beat = fake_master.beat_packet("CDJ-3000", 1, 128.0, 0.02, 1, 100)
        parsed = segment.parse_frame(framed(beat, 50001))
        result = bridge.feed(bridge_obj, parsed, 0.0)
        self.assertEqual(result["bpm"], 128.0)
        self.assertAlmostEqual(bridge_obj.tracker.effective_bpm, 130.56, delta=0.01)

    def test_feed_ignores_non_master_packets(self):
        bridge_obj = pdl.PdlClockBridge()
        arp = bytes.fromhex("ffffffffffff") + bytes.fromhex("020000000007") + b"\x08\x06" + bytes(46)
        self.assertIsNone(bridge.feed(bridge_obj, {"payload": b"Qspt1WmJOL", "dst_port": 50000}, 0.0))
        self.assertIsNone(bridge.feed(bridge_obj, {"payload": b"not a packet"}, 0.0))


class ClockEmitterTests(unittest.TestCase):
    def test_the_emitter_sends_24_ppqn_at_the_bpm(self):
        sent = []
        emitter = bridge.ClockEmitter(sent.append)
        emitter.set_bpm(120.0)
        emitter.start()
        time.sleep(0.6)
        emitter.stop()
        self.assertGreater(len(sent), 20)  # 120 BPM = 48 pulses/s, so ~29 in 0.6 s
        self.assertLess(len(sent), 60)

    def test_the_emitter_retunes_without_restarting(self):
        sent = []
        emitter = bridge.ClockEmitter(sent.append)
        emitter.set_bpm(60.0)
        emitter.start()
        time.sleep(0.5)
        emitter.set_bpm(240.0)
        time.sleep(0.5)
        emitter.stop()
        # 60 BPM = 24 p/s (12 in 0.5 s) then 240 BPM = 96 p/s (48 in 0.5 s): clearly more than a steady 60 BPM stream
        self.assertGreater(len(sent), 40)

    def test_a_second_start_does_not_double_the_clock(self):
        sent = []
        emitter = bridge.ClockEmitter(sent.append)
        emitter.set_bpm(120.0)
        emitter.start()
        emitter.start()  # a second thread would double the rate
        time.sleep(0.6)
        emitter.stop()
        self.assertGreater(len(sent), 20)   # 120 BPM = 48 pulses/s, so ~29 in 0.6 s
        self.assertLess(len(sent), 45)      # two threads would give ~58

    def test_a_worker_that_died_lets_start_emit_again(self):
        sent = []

        def send(byte):
            sent.append(byte)
            if len(sent) == 1:
                raise OSError("the MIDI sink went away")

        emitter = bridge.ClockEmitter(send)
        emitter.set_bpm(120.0)
        with mock.patch.object(threading, "excepthook", lambda args: None):  # the worker's death is the point
            emitter.start()
            emitter._thread.join(timeout=2.0)  # the sink raised on the first pulse, so the worker is gone
            self.assertFalse(emitter._thread.is_alive())
            self.assertEqual(sent, [0xF8])
            emitter.start()  # a dead worker must not be mistaken for a running one
            time.sleep(0.5)
            emitter.stop()
        self.assertGreater(len(sent), 10)  # 120 BPM = 48 pulses/s, so ~24 more in 0.5 s


class ClockSinkTests(unittest.TestCase):
    def test_clock_sink_builds_a_four_field_clock_message(self):
        import midi_bridge.midi as midi
        sent = []

        class FakePort:
            def __init__(self, args):
                pass

            def send(self, msg):
                sent.append(msg)

            def close(self):
                pass

        with mock.patch.object(midi, "Port", FakePort):
            sink = bridge.clock_sink("Test Bus")
            sink(0xF8)   # a clock byte: one message
            sink(0x90)   # not a clock byte: ignored
            sink.close()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].type, "clock")
        self.assertEqual((sent[0].channel, sent[0].number, sent[0].value), (None, 0, 0))


class RunTests(unittest.TestCase):
    def test_run_feeds_master_packets_and_emits_clock(self):
        class FakeSeg:
            def __init__(self, frames):
                self.frames = list(frames)

            def settimeout(self, t):
                pass

            def recvfrom(self, n):
                if self.frames:
                    return self.frames.pop(0), None
                time.sleep(0.01)  # a real socket blocks on its recv timeout; don't starve the clock thread
                raise socket.timeout()

        beat = fake_master.beat_packet("CDJ-3000", 1, 128.0, 0.02, 1, 100)
        frame = framed(beat, 50001)
        sent = []
        fed = bridge.run(FakeSeg([frame]), sent.append, duration=0.6)
        self.assertEqual(fed, 1)
        self.assertGreater(len(sent), 20)  # the emitter ran at 130.56 BPM for the window

    def test_run_survives_a_malformed_frame(self):
        class FakeSeg:
            def __init__(self, frames):
                self.frames = list(frames)

            def settimeout(self, t):
                pass

            def recvfrom(self, n):
                if self.frames:
                    return self.frames.pop(0), None
                time.sleep(0.01)
                raise socket.timeout()

        malformed = b"Qspt1WmJOL" + b"\x28" + b"\x00" * 20   # the magic and beat type, far short of a beat packet
        beat = fake_master.beat_packet("CDJ-3000", 1, 128.0, 0.02, 1, 100)
        sent = []
        fed = bridge.run(FakeSeg([framed(malformed, 50001), framed(beat, 50001)]), sent.append, duration=0.6)
        self.assertEqual(fed, 1)           # the bad frame was skipped, the good one still fed
        self.assertGreater(len(sent), 20)


if __name__ == "__main__":
    unittest.main()
