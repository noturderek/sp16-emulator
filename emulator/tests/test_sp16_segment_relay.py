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


if __name__ == "__main__":
    unittest.main()
