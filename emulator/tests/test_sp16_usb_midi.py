"""The separate USB MIDI byte stream reaches its CoreMIDI endpoints."""
import socket
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from queue import Empty, Full, Queue
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from midi.usb import bridge, signal_stop  # noqa: E402
from midi_bridge.midi import HELPER_SOURCE, Message, Port, helper_binary, list_ports  # noqa: E402


class SignalStopTests(unittest.TestCase):
    """The shutdown sentinel must reach the input thread even when the bounded queue is full (pt-i44.81)."""

    def test_a_full_queue_makes_room_for_the_sentinel(self):
        pending = Queue(maxsize=2)
        pending.put(b"\x90\x3c\x64")
        pending.put(b"\x80\x3c\x00")
        self.assertTrue(signal_stop(pending))
        self.assertEqual([pending.get_nowait(), pending.get_nowait()], [b"\x80\x3c\x00", None])

    def test_an_empty_queue_gets_only_the_sentinel(self):
        pending = Queue(maxsize=2)
        self.assertTrue(signal_stop(pending))
        self.assertIsNone(pending.get_nowait())

    def test_the_input_thread_sees_the_sentinel_after_a_full_queue(self):
        pending = Queue(maxsize=1)
        pending.put(b"\x90\x3c\x64")
        self.assertTrue(signal_stop(pending))
        received = []

        def send_input():
            while True:
                data = pending.get()
                if data is None:
                    return
                received.append(data)

        worker = threading.Thread(target=send_input, daemon=True)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(received, [])  # the message the queue dropped, never a silent hang

    def test_a_queue_that_stays_full_times_out_with_backoff_not_a_spin(self):
        # A full queue whose next message is drained between the put and the get (a race): the sentinel cannot be
        # delivered, so signal_stop must back off rather than busy-spin and return False at the deadline.
        pending = mock.Mock()
        pending.put_nowait = mock.Mock(side_effect=Full)
        pending.get_nowait = mock.Mock(side_effect=Empty)
        sleeps = []
        with mock.patch.object(time, "sleep", side_effect=sleeps.append), \
                mock.patch.object(time, "monotonic", side_effect=[0.0, 0.001, 1.0]):
            self.assertFalse(signal_stop(pending, timeout=1.0))
        self.assertTrue(sleeps, "signal_stop spun without sleeping")


class CoreMIDIHelperBuildTests(unittest.TestCase):
    """The DIN and USB bridges both compile the CoreMIDI helper on first use; the build must be serialised so two
    bridges starting together do not race swiftc's linker on the same output path ("ld: File exists")."""

    def test_concurrent_bridges_compile_the_helper_once(self):
        with tempfile.TemporaryDirectory() as folder:
            build_dir = Path(folder)
            compiling = threading.Event()   # set when the (fake) swiftc starts, so both threads are racing
            release = threading.Event()     # lets the fake swiftc finish
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                compiling.set()
                release.wait(5)
                binary = Path(args[-1])          # the -o output path
                binary.write_text("#!/bin/sh\n")  # stand in for a real compile
                binary.chmod(0o755)
                return types.SimpleNamespace(returncode=0, stderr="")

            errors, results = [], [None, None]

            def build(index):
                try:
                    results[index] = helper_binary(build_dir)
                except Exception as error:  # noqa: BLE001 - surfaced below
                    errors.append(error)

            with mock.patch("midi_bridge.midi.subprocess.run", side_effect=fake_run):
                threads = [threading.Thread(target=build, args=(i,)) for i in range(2)]
                for thread in threads:
                    thread.start()
                self.assertTrue(compiling.wait(5), "the fake swiftc never started")
                release.set()
                for thread in threads:
                    thread.join(5)

            self.assertEqual(errors, [])
            self.assertEqual(results[0], results[1])   # both bridges got the same helper
            self.assertTrue(str(results[0]).endswith("coremidi-io"))
            self.assertEqual(len(calls), 1, "both bridges compiled the helper; the build is not serialised")

    def test_an_up_to_date_helper_is_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as folder:
            build_dir = Path(folder)
            (build_dir / "midi-bridge").mkdir(parents=True)
            binary = build_dir / "midi-bridge" / "coremidi-io"
            binary.write_text("#!/bin/sh\n")
            os.utime(binary, (HELPER_SOURCE.stat().st_mtime + 10, HELPER_SOURCE.stat().st_mtime + 10))
            with mock.patch("midi_bridge.midi.subprocess.run") as run:
                self.assertEqual(helper_binary(build_dir), binary)
            run.assert_not_called()


class USBMidiTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "CoreMIDI needs macOS")
    def test_virtual_usb_route_is_bidirectional_and_unpaced(self):
        label = "SP-16 USB " + uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "usb.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(1)
            errors = []

            def run():
                try:
                    bridge(path, name=label)
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=run)
            worker.start()
            peer, _ = listener.accept()
            peer.settimeout(5)
            monitor = None
            try:
                deadline = time.monotonic() + 10
                while label not in list_ports()["source"]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                monitor = Port(["run", "--in", label, "--out", label])
                monitor.send(Message("note_on", 0, 60, 100))
                self.assertEqual(peer.recv(3), b"\x90\x3c\x64")
                peer.sendall(b"\x80\x3c\x00\xfa\xf8\xfc")
                self.assertEqual([monitor.get(3) for _ in range(4)],
                                 [Message("note_off", 0, 60, 0), Message("start", None, 0, 0),
                                  Message("clock", None, 0, 0), Message("stop", None, 0, 0)])
            finally:
                if monitor:
                    monitor.close()
                peer.close()
                listener.close()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
