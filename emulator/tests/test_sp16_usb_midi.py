"""The separate USB MIDI byte stream reaches its CoreMIDI endpoints."""
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from midi.usb import bridge  # noqa: E402
from midi_bridge.midi import Message, Port, list_ports  # noqa: E402


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
