"""emulator/net/segment_relay.py: the mcast frame relay between QEMU's stream framing and the multicast segment."""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import net.segment_relay as segment_relay  # noqa: E402


def free_udp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def ethernet(src_mac, payload):
    return bytes.fromhex("ffffffffffff") + bytes.fromhex(src_mac.replace(":", "")) + struct.pack(">H", 0x0800) + payload


def recv_frame(sock, timeout=0.5):
    """One 4-byte-length + frame record from a QEMU stream peer; raises on EOF or timeout."""
    sock.settimeout(timeout)
    data = b""
    while len(data) < 4:
        chunk = sock.recv(4 - len(data))
        if not chunk:
            raise EOFError("peer closed")
        data += chunk
    length = struct.unpack(">I", data[:4])[0]
    while len(data) < 4 + length:
        chunk = sock.recv(4 + length - len(data))
        if not chunk:
            raise EOFError("peer closed")
        data += chunk
    return data[4:4 + length]


class ParseTests(unittest.TestCase):
    def test_parse_target(self):
        self.assertEqual(segment_relay.parse_target("230.0.0.1:34567"), ("230.0.0.1", 34567))
        self.assertEqual(segment_relay.parse_target("224.0.0.99:1"), ("224.0.0.99", 1))

    def test_parse_target_refuses_malformed(self):
        for bad in ("", "230.0.0.1", ":34567", "230.0.0.1:", "230.0.0.1:0", "230.0.0.1:65536", "230.0.0.1:x"):
            with self.assertRaises(ValueError, msg=bad):
                segment_relay.parse_target(bad)

    def test_mac_key(self):
        self.assertEqual(segment_relay.mac_key("52:54:00:AA:BB:CC"), "525400aabbcc")
        self.assertEqual(segment_relay.mac_key(""), "")


class RelayTests(unittest.TestCase):
    def test_relay_exits_without_qemu(self):
        with tempfile.TemporaryDirectory() as folder:
            sock_path = os.path.join(folder, "relay.sock")
            code = segment_relay.relay(sock_path, f"230.0.0.1:{free_udp_port()}", "52:54:00:00:00:01",
                                       idle_timeout=0.2)
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(sock_path))

    def test_relays_both_ways_and_filters_own_echo(self):
        port = free_udp_port()
        group, mac = "230.0.0.1", "52:54:00:00:00:01"
        with tempfile.TemporaryDirectory() as folder:
            sock_path = os.path.join(folder, "relay.sock")
            proc = subprocess.Popen([sys.executable, str(TOOLS / "net" / "segment_relay.py"),
                                     "--socket", sock_path, "--target", f"{group}:{port}",
                                     "--mac", mac, "--idle-timeout", "10"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            watcher = None
            qemu = None
            try:
                deadline = time.time() + 5
                while not os.path.exists(sock_path) and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(sock_path), "relay socket did not appear")

                # The host watcher joins the group exactly like segment_watch.py.
                watcher = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                watcher.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    watcher.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
                watcher.bind(("", port))
                watcher.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                   socket.inet_aton(group) + socket.inet_aton("0.0.0.0"))
                watcher.settimeout(0.5)

                qemu = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                qemu.settimeout(5)
                qemu.connect(sock_path)

                # guest -> segment
                own = ethernet(mac, bytes([0xAA] * 40))
                qemu.sendall(struct.pack(">I", len(own)) + own)
                self.assertEqual(watcher.recvfrom(65535)[0], own)

                # segment -> guest (another relay's frame, different source MAC)
                other = ethernet("52:54:00:00:00:02", bytes([0xBB] * 32))
                watcher.sendto(other, (group, port))
                self.assertEqual(recv_frame(qemu), other)

                # own echo (same source MAC) is filtered out, never handed back to the guest
                watcher.sendto(own, (group, port))
                with self.assertRaises(socket.timeout):
                    recv_frame(qemu, timeout=0.4)

                qemu.close()
                qemu = None
                self.assertEqual(proc.wait(timeout=5), 0)
                self.assertFalse(os.path.exists(sock_path))
            finally:
                if qemu is not None:
                    qemu.close()
                if watcher is not None:
                    watcher.close()
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)

    def test_relay_bounds_the_outbox_under_sustained_backpressure(self):
        port = free_udp_port()
        group, mac = "230.0.0.1", "52:54:00:00:00:01"
        size = 1200
        sent = 2000                       # ~2.4 MB, several times the 1 MiB outbox
        record = size + 14 + 4             # 4-byte length prefix + Ethernet frame
        with tempfile.TemporaryDirectory() as folder:
            sock_path = os.path.join(folder, "relay.sock")
            proc = subprocess.Popen([sys.executable, str(TOOLS / "net" / "segment_relay.py"),
                                     "--socket", sock_path, "--target", f"{group}:{port}",
                                     "--mac", mac, "--idle-timeout", "10"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            watcher = None
            qemu = None
            try:
                deadline = time.time() + 5
                while not os.path.exists(sock_path) and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(sock_path), "relay socket did not appear")

                watcher = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                watcher.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    watcher.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
                watcher.bind(("", port))
                watcher.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                   socket.inet_aton(group) + socket.inet_aton("0.0.0.0"))

                qemu = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                qemu.settimeout(5)
                qemu.connect(sock_path)

                # The guest never reads: flood the segment (a foreign MAC) far past the outbox limit, paced so the
                # relay itself (not the OS receive buffer) is what overflows.
                for seq in range(sent):
                    frame = ethernet("52:54:00:00:00:02", struct.pack(">I", seq) + bytes([0xBB] * (size - 4)))
                    watcher.sendto(frame, (group, port))
                    time.sleep(0.0005)
                time.sleep(1.0)            # let the relay queue (and drop) them
                self.assertIsNone(proc.poll(), "the relay crashed under backpressure")

                # The guest now drains: only the newest frames survive (the oldest were dropped), so the count is
                # bounded by the outbox limit rather than by the flood size.
                qemu.settimeout(15)
                got = []
                deadline = time.time() + 15
                while time.time() < deadline:
                    try:
                        got.append(recv_frame(qemu, timeout=0.5))
                    except socket.timeout:
                        break
                seqs = [struct.unpack(">I", f[14:18])[0] for f in got]
                self.assertGreater(len(got), 0)              # the newest frames were delivered
                self.assertLess(len(got), sent)              # drops happened, not an unbounded buffer
                self.assertEqual(seqs, sorted(seqs))         # drop-oldest preserves order
                # The delivered count is bounded by the outbox cap (plus the kernel send buffer), never by the flood.
                self.assertLessEqual(len(got) * record, 2 * segment_relay.OUTBOX_LIMIT)

                qemu.close()
                qemu = None
                self.assertEqual(proc.wait(timeout=5), 0)
                self.assertIn("dropping the oldest queued frames", proc.stderr.read())
                self.assertFalse(os.path.exists(sock_path))
            finally:
                if qemu is not None:
                    qemu.close()
                if watcher is not None:
                    watcher.close()
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)

    def test_flush_stream_buffers_a_full_socket_without_losing_bytes(self):
        class Blocked:
            def send(self, data):
                raise BlockingIOError()
        data = b"x" * 100
        self.assertEqual(segment_relay.flush_stream(Blocked(), data), data)

    def test_flush_stream_keeps_the_unsent_tail_of_a_partial_send(self):
        class Partial:
            def send(self, data):
                return 10
        self.assertEqual(segment_relay.flush_stream(Partial(), b"x" * 100), b"x" * 90)


class OutboxTests(unittest.TestCase):
    def test_overflow_preserves_a_frame_already_started_on_the_qemu_stream(self):
        frames = [letter * 12 for letter in (b"A", b"B", b"C", b"D", b"E")]
        records = [struct.pack(">I", len(frame)) + frame for frame in frames]
        # Split both the length prefix and the payload, including halfway through the record.
        for offset in (1, 3, 4, 8, 15):
            for oversized in (False, True):
                with self.subTest(offset=offset, oversized=oversized):
                    writer, qemu = socket.socketpair()
                    with writer, qemu:
                        class Partial:
                            def send(self, data):
                                return writer.send(data[:offset])

                        outbox = segment_relay.Outbox(limit=48)
                        for record in records[:3]:
                            outbox.push(record)
                        outbox.flush(Partial())
                        for record in records[3:]:
                            outbox.push(record)
                        if oversized:
                            outbox.push(struct.pack(">I", 60) + b"X" * 60)

                        outbox.flush(writer)
                        expected = [frames[0]] if oversized else [frames[0], *frames[3:]]
                        for frame in expected:
                            self.assertEqual(recv_frame(qemu), frame)
                        self.assertFalse(outbox)
                        self.assertEqual(outbox.bytes, 0)
                        self.assertEqual(outbox.dropped_frames, 5 if oversized else 2)
                        self.assertEqual(outbox.dropped_bytes, 128 if oversized else 32)

                        # Completing a partial frame must also clear its protection for later overflows.
                        for record in records[1:]:
                            outbox.push(record)
                        self.assertEqual(outbox.bytes, 48)
                        outbox.flush(writer)
                        for frame in frames[2:]:
                            self.assertEqual(recv_frame(qemu), frame)
                        writer.shutdown(socket.SHUT_WR)
                        self.assertEqual(qemu.recv(1), b"")

    def test_outbox_drops_the_oldest_records_to_stay_under_the_limit(self):
        outbox = segment_relay.Outbox(limit=100)
        for _ in range(5):
            outbox.push(b"x" * 30)           # 150 bytes over a 100-byte budget
        self.assertLessEqual(outbox.bytes, 100)
        self.assertEqual(outbox.dropped_frames, 2)  # the two oldest records were dropped
        self.assertEqual(outbox.dropped_bytes, 60)
        self.assertEqual(outbox.bytes, 90)          # the three newest (90 bytes) remain

    def test_outbox_flushes_oldest_first_and_keeps_a_partial_tail(self):
        class Partial:
            def __init__(self):
                self.sends = []
            def send(self, data):
                self.sends.append(data)
                if len(self.sends) == 1:
                    return 3                      # first record partially sent
                raise BlockingIOError()           # then the socket is full
        outbox = segment_relay.Outbox(limit=1000)
        outbox.push(b"aaaaa")
        outbox.push(b"bbbbb")
        outbox.flush(Partial())
        self.assertEqual(list(outbox.queue), [b"aaaaa", b"bbbbb"])
        self.assertEqual(outbox.sent_offset, 3)     # resume the head before sending the next record
        self.assertEqual(outbox.bytes, 7)

    def test_outbox_refuses_a_non_positive_limit(self):
        with self.assertRaises(ValueError):
            segment_relay.Outbox(limit=0)


if __name__ == "__main__":
    unittest.main()
