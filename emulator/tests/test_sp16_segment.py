"""emulator/net/segment.py: the IPv4/UDP/Ethernet framing a host peer uses on an `--net mcast` segment."""
import contextlib
import io
import socket
import struct
import unittest
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import net.segment as segment  # noqa: E402


SP16_MAC, SP16_IP = bytes.fromhex("020000000007"), bytes([169, 254, 0, 23])


def load_tool(name):
    """One of the host-side segment tools, imported from net/ by file name."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, TOOLS / "net" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def framed(payload, sport, dport):
    return segment.udp_frame("52:54:00:a1:00:17", "ff:ff:ff:ff:ff:ff", "169.254.0.23", "169.254.255.255",
                             sport, dport, payload)


def sp16_keepalive(name):
    """A 54-byte type-0x06 keep-alive laid out like the SP-16's own: type at 0x0a, a reserved 0x00 at 0x0b, the
    20-byte name at 0x0c, then the player number, MAC, IP and flags."""
    packet = bytearray(54)
    packet[0:10] = b"Qspt1WmJOL"
    packet[0x0a], packet[0x0b] = 0x06, 0x00
    packet[0x0c:0x20] = name.encode("ascii").ljust(20, b"\0")
    packet[0x20:0x24] = b"\x01\x02\x00\x36"
    packet[0x24], packet[0x25] = 2, 1
    packet[0x26:0x2c] = SP16_MAC
    packet[0x2c:0x30] = SP16_IP
    packet[0x30:0x36] = bytes([2, 0, 0, 0, 1, 0x64])
    return bytes(packet)


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

    def test_parse_rejects_a_udp_length_that_overruns_the_frame(self):
        frame = segment.udp_frame("02:00:00:00:00:07", "ff:ff:ff:ff:ff:ff", "169.254.7.7", "169.254.255.255",
                                  50000, 50000, b"x" * 10)
        ihl = (frame[14] & 0x0F) * 4
        corrupted = bytearray(frame)
        corrupted[14 + ihl + 4:14 + ihl + 6] = b"\x7f\xff"  # udp_length claims 32767 bytes
        with self.assertRaises(KeyError):
            segment.parse_frame(bytes(corrupted))

    def test_parse_rejects_a_udp_length_below_eight(self):
        frame = segment.udp_frame("02:00:00:00:00:07", "ff:ff:ff:ff:ff:ff", "169.254.7.7", "169.254.255.255",
                                  50000, 50000, b"x")
        ihl = (frame[14] & 0x0F) * 4
        corrupted = bytearray(frame)
        corrupted[14 + ihl + 4:14 + ihl + 6] = b"\x00\x00"
        with self.assertRaises(KeyError):
            segment.parse_frame(bytes(corrupted))


class SegmentMasterTests(unittest.TestCase):
    """The segment fake master imports and carries the same packet layouts as fake_master."""

    def setUp(self):
        self.master = load_tool("segment_master")

    def test_master_uses_fake_master_packet_builders(self):
        self.assertEqual(self.master.MAGIC, b"Qspt1WmJOL")
        self.assertEqual(self.master.MASTER_IP, "169.254.7.7")

    def test_master_logs_the_guest_keepalive_name(self):
        # The SP-16's own keep-alive: 54 bytes, type 0x06 at 0x0a, a reserved 0x00 at 0x0b, the name at 0x0c.
        parsed = segment.parse_frame(framed(sp16_keepalive("TORAIZ"), 50000, 50000))
        line = self.master.keepalive_line(3, parsed)
        self.assertIn("guest keep-alive 3: 52:54:00:a1:00:17 169.254.0.23", line)
        self.assertIn("type 0x06 name 'TORAIZ'", line)

    def test_master_reads_beat_and_status_names_at_0x0b(self):
        master = load_tool("fake_master")
        mac, ip = bytes.fromhex("020000000007"), bytes([169, 254, 7, 7])
        beat = master.beat_packet("CDJ-3000", 1, 128.0, 0.0, 1, 100)
        status = master.status_packet("CDJ-3000", 1, 128.0, 0.0, 1, 1)
        for port, payload in ((50001, beat), (50002, status)):
            self.assertEqual(self.master.frame_name(segment.parse_frame(framed(payload, port, port))), "CDJ-3000",
                             msg=port)

    def test_keepalive_fields_skips_a_malformed_magic_frame(self):
        # A MAGIC-only payload has no type byte or name: skipped, not a crash.
        parsed = segment.parse_frame(framed(b"Qspt1WmJOL", 50000, 50000))
        self.assertIsNone(self.master.keepalive_fields(parsed))
        # A name shorter than its packet family's field: skipped.
        short = b"Qspt1WmJOL" + b"\x06\x00" + b"short"
        self.assertIsNone(self.master.keepalive_fields(segment.parse_frame(framed(short, 50000, 50000))))

    def test_keepalive_fields_reads_a_well_formed_keepalive(self):
        parsed = segment.parse_frame(framed(sp16_keepalive("TORAIZ"), 50000, 50000))
        self.assertEqual(self.master.keepalive_fields(parsed), (0x06, "TORAIZ"))

    def test_drain_frames_drains_a_burst_and_skips_malformed(self):
        class FakeSeg:
            def __init__(self, frames):
                self.frames = list(frames)
                self.timeout = 0.2
            def gettimeout(self):
                return self.timeout
            def settimeout(self, t):
                self.timeout = t
            def recvfrom(self, _n):
                if self.frames:
                    return self.frames.pop(0), None
                raise BlockingIOError()
        arp = bytes.fromhex("ffffffffffff") + bytes.fromhex("020000000007") + b"\x08\x06" + bytes(46)
        malformed = framed(b"Qspt1WmJOL", 50000, 50000)  # MAGIC only, too short
        good = framed(sp16_keepalive("TORAIZ"), 50000, 50000)
        seg = FakeSeg([arp, malformed, good, good])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            seen = self.master.drain_frames(seg, self.master.MASTER_MAC, 0)
        self.assertEqual(seen, 2)
        self.assertEqual(seg.timeout, 0.2)  # the non-blocking drain restored the socket timeout
        self.assertIn("name 'TORAIZ'", out.getvalue())


class SegmentWatchTests(unittest.TestCase):
    """net/segment_watch.py decodes the name at each packet family's offset, not one offset for every type."""

    def setUp(self):
        self.watch = load_tool("segment_watch")

    def test_describe_reads_the_keepalive_name_and_mac_and_ip(self):
        # Built like the SP-16's own keep-alive, not from this repo's builder.
        text = self.watch.describe(framed(sp16_keepalive("TORAIZ"), 50000, 50000))
        self.assertIn("PRODJLINK type 0x06 name 'TORAIZ'", text)
        self.assertIn("mac 02:00:00:00:00:07", text)
        self.assertIn("ip 169.254.0.23", text)

    def test_describe_reads_beat_and_status_names(self):
        master = load_tool("fake_master")
        beat = master.beat_packet("CDJ-3000", 1, 128.0, 0.02, 3, 100)
        status = master.status_packet("CDJ-3000", 1, 128.0, 0.02, 2, 7)
        beat_text = self.watch.describe(framed(beat, 50001, 50001))
        self.assertIn("PRODJLINK type 0x28 name 'CDJ-3000'", beat_text)
        self.assertIn("bpm 128.00 pitch +2.00%", beat_text)
        self.assertIn("PRODJLINK type 0x0a name 'CDJ-3000'", self.watch.describe(framed(status, 50002, 50002)))


if __name__ == "__main__":
    unittest.main()
