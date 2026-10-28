"""DIN UART framing, byte pacing, and a virtual CoreMIDI endpoint round trip."""
import sys
import socket
import tempfile
import threading
import time
import uuid
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from midi.din import Messages, PacedInput, bridge  # noqa: E402
from midi_bridge.midi import Message, Port  # noqa: E402


class UARTTests(unittest.TestCase):
    def test_running_status_realtime_and_sysex(self):
        parser = Messages()
        self.assertEqual(parser.feed(bytes([0x90, 60])), [])
        self.assertEqual(parser.feed(bytes([0xF8, 100, 61, 80, 0xF0, 1, 2])),
                         [b"\xf8", bytes([0x90, 60, 100]), bytes([0x90, 61, 80])])
        self.assertEqual(parser.feed(bytes([0xF7])), [b"\xf0\x01\x02\xf7"])

    def test_three_bytes_take_960_microseconds_and_realtime_jumps_queue(self):
        now = [0]
        sent = []

        def sleep(seconds):
            now[0] += round(seconds * 1_000_000_000)

        uart = PacedInput(lambda data: sent.append((now[0], data)), clock=lambda: now[0], sleep=sleep)
        uart.add(bytes([0x90, 60, 100]), release_ns=0)
        uart.add(b"\xf8", release_ns=0)
        uart.close()
        uart.run()
        self.assertEqual(sent, [(320_000, b"\xf8"), (640_000, b"\x90"),
                                (960_000, b"<"), (1_280_000, b"d")])

    def test_cancel_discards_pending_bytes_and_rejects_new_input(self):
        sent = []
        uart = PacedInput(sent.append)
        uart.add(b"\x90\x3c\x64")
        uart.close(discard=True)
        uart.add(b"\xf8")
        uart.run()
        self.assertEqual(sent, [])

    @unittest.skipUnless(sys.platform == "darwin", "CoreMIDI needs macOS")
    def test_virtual_ports_cross_the_uart_socket_both_ways(self):
        label = "SP-16 UART " + uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "midi.sock")
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
                # Port creation is asynchronous; wait for the actual published source.
                from midi_bridge.midi import list_ports
                deadline = time.monotonic() + 10
                while label not in list_ports()["source"]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                monitor = Port(["run", "--in", label, "--out", label])
                monitor.send(Message("note_on", 0, 60, 100))
                received = bytearray()
                while len(received) < 3:
                    received.extend(peer.recv(3 - len(received)))
                self.assertEqual(received, b"\x90\x3c\x64")
                peer.sendall(b"\x80\x3c\x00\xf8")
                self.assertEqual(monitor.get(3), Message("note_off", 0, 60, 0))
                self.assertEqual(monitor.get(3), Message("clock", None, 0, 0))
            finally:
                if monitor:
                    monitor.close()
                peer.close()
                listener.close()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])

    @unittest.skipUnless(sys.platform == "darwin", "CoreMIDI needs macOS")
    def test_coremidi_virtual_endpoint_round_trip(self):
        virtual = Port(["virtual", "SP-16 DIN Contract"])
        monitor = None
        try:
            monitor = Port(["run", "--in", "SP-16 DIN Contract", "--out", "SP-16 DIN Contract"])
            virtual.send(Message("note_on", 0, 60, 100))
            self.assertEqual(monitor.get(3), Message("note_on", 0, 60, 100))
            monitor.send(Message("note_off", 0, 60, 0))
            self.assertEqual(virtual.get(3), Message("note_off", 0, 60, 0))
        finally:
            if monitor:
                monitor.close()
            virtual.close()


if __name__ == "__main__":
    unittest.main()
