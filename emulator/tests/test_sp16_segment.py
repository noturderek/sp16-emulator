"""emulator/net/segment.py: the IPv4/UDP/Ethernet framing a host peer uses on an `--net mcast` segment."""
import socket
import struct
import unittest
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import net.segment as segment  # noqa: E402


class ChecksumTests(unittest.TestCase):
    def test_ip_checksum_of_a_valid_header_is_zero_when_folded(self):
        # 20-byte header, checksum field zero; the computed checksum must be its own one's complement.
        header = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 40, 0, 0, 64, 17, 0,
                             socket.inet_aton("169.254.7.7"), socket.inet_aton("169.254.255.255"))
        csum = segment.ip_checksum(header)
        filled = header[:10] + struct.pack(">H", csum) + header[12:]
        self.assertEqual(segment.ip_checksum(filled), 0)


class FrameTests(unittest.TestCase):
    def test_udp_frame_round_trips(self):
        payload = b"Qspt1WmJOL" + b"\x06" + bytes(43)
        frame = segment.udp_frame("02:00:00:00:00:07", "ff:ff:ff:ff:ff:ff", "169.254.7.7", "169.254.255.255",
                                  50001, 50001, payload)
        self.assertEqual(frame[0:6], bytes.fromhex("ffffffffffff"))
        self.assertEqual(frame[6:12], bytes.fromhex("020000000007"))
        self.assertEqual(frame[12:14], b"\x08\x00")
        parsed = segment.parse_frame(frame)
        self.assertEqual(parsed["src_mac"], "02:00:00:00:00:07")
        self.assertEqual(parsed["dst_mac"], "ff:ff:ff:ff:ff:ff")
        self.assertEqual(parsed["src_ip"], "169.254.7.7")
        self.assertEqual(parsed["dst_ip"], "169.254.255.255")
        self.assertEqual(parsed["src_port"], 50001)
        self.assertEqual(parsed["dst_port"], 50001)
        self.assertEqual(parsed["payload"], payload)

    def test_udp_frame_ip_checksum_is_well_formed(self):
        frame = segment.udp_frame("02:00:00:00:00:07", "ff:ff:ff:ff:ff:ff", "169.254.7.7", "169.254.255.255",
                                  50000, 50000, b"x")
        ip = frame[14:34]
        self.assertEqual(segment.ip_checksum(ip), 0)

    def test_parse_rejects_non_ip_and_non_udp(self):
        with self.assertRaises(KeyError):
            segment.parse_frame(bytes(64))
        arp = bytes.fromhex("ffffffffffff") + bytes.fromhex("020000000007") + b"\x08\x06" + bytes(46)
        with self.assertRaises(KeyError):
            segment.parse_frame(arp)

    def test_bad_macs_are_refused(self):
        with self.assertRaises(ValueError):
            segment.udp_frame("zz", "ff:ff:ff:ff:ff:ff", "1.2.3.4", "5.6.7.8", 1, 1, b"")


class SegmentMasterTests(unittest.TestCase):
    """The segment fake master imports and carries the same packet layouts as fake_master."""

    def test_master_uses_fake_master_packet_builders(self):
        import importlib.util
        sys.path.insert(0, str(TOOLS / "net"))
        spec = importlib.util.spec_from_file_location("segment_master", TOOLS / "net/segment_master.py")
        master = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(master)
        self.assertEqual(master.MAGIC, b"Qspt1WmJOL")
        self.assertEqual(master.MASTER_IP, "169.254.7.7")


class SegmentWatchTests(unittest.TestCase):
    """net/segment_watch.py decodes the keep-alive name at offset 0x0c, not 0x0b."""

    def test_describe_reads_the_name_and_mac_and_ip(self):
        import importlib.util
        sys.path.insert(0, str(TOOLS / "net"))
        spec = importlib.util.spec_from_file_location("segment_watch", TOOLS / "net/segment_watch.py")
        watch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watch)
        import fake_master
        import segment
        keepalive = fake_master.keepalive("TORAIZ", 2, bytes.fromhex("020000000007"), bytes([169, 254, 0, 23]))
        frame = segment.udp_frame("52:54:00:a1:00:17", "ff:ff:ff:ff:ff:ff", "169.254.0.23", "169.254.255.255",
                                  50000, 50000, keepalive)
        text = watch.describe(frame)
        self.assertIn("PRODJLINK type 0x06 name 'TORAIZ'", text)
        self.assertIn("mac 02:00:00:00:00:07", text)
        self.assertIn("ip 169.254.0.23", text)


if __name__ == "__main__":
    unittest.main()
