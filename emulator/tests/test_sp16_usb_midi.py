"""The separate USB MIDI byte stream reaches its CoreMIDI endpoints."""
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from midi.usb import bridge, signal_stop  # noqa: E402
from midi_bridge.midi import Message, Port, list_ports  # noqa: E402


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
