"""--net modes: validation, the environment the QEMU wrapper reads, and the wrapper's NIC arguments."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import native


class NetEnvironmentTests(unittest.TestCase):
    def test_none_adds_nothing(self):
        self.assertEqual(native.net_environment(None), {})
        self.assertEqual(native.net_environment("none"), {})

    def test_mcast_needs_no_daemon(self):
        self.assertEqual(native.net_environment("mcast"),
                         {"SP16_NET_KIND": "mcast", "SP16_NET_TARGET": "230.0.0.1:34567", "SP16_NET_MODE": "mcast",
                          "SP16_PYTHON": sys.executable})
        self.assertEqual(native.net_environment("mcast:40000")["SP16_NET_TARGET"], "230.0.0.1:40000")

    def test_refused_modes(self):
        for mode in ("wifi", "mcast:80", "mcast:x", "bridged:", "bridged:en0;rm", "bridged:../x"):
            with self.assertRaises(ValueError, msg=mode):
                native.net_environment(mode)

    def test_host_and_bridged_name_the_missing_daemon(self):
        with mock.patch.object(native, "vmnet_socket_path", return_value=None):
            for mode, name in (("host", "socket_vmnet"), ("bridged:en0", "socket_vmnet.bridged.en0")):
                with self.assertRaises(ValueError) as caught:
                    native.net_environment(mode)
                self.assertIn(name, str(caught.exception))
                self.assertIn("--net mcast", str(caught.exception))

    def test_host_uses_the_daemon_socket(self):
        with mock.patch.object(native, "vmnet_socket_path", return_value="/x/socket_vmnet") as found:
            self.assertEqual(native.net_environment("host"),
                             {"SP16_NET_KIND": "unix", "SP16_NET_TARGET": "/x/socket_vmnet", "SP16_NET_MODE": "host"})
            native.net_environment("bridged:en0")
        found.assert_called_with("socket_vmnet.bridged.en0")

    def test_net_ip_is_validated_and_normalised(self):
        with mock.patch.object(native, "vmnet_socket_path", return_value="/x/socket_vmnet.bridged.en0"):
            env = native.net_environment("bridged:en0", "192.168.1.50/24")
            self.assertEqual(env["SP16_NET_MODE"], "bridged:en0")
            self.assertEqual(env["SP16_NET_IP"], "192.168.1.50/24")
            self.assertEqual(native.net_environment("bridged:en0", "192.168.1.50")["SP16_NET_IP"], "192.168.1.50/24")
        for bad in ("192.168.1.50/", "192.168.1.50/33", "300.1.2.3", "not-an-ip", "192.168.1.50/24/x"):
            with self.assertRaises(ValueError, msg=bad):
                native.net_environment("bridged:en0", bad)

    def test_net_ip_requires_bridged(self):
        with mock.patch.object(native, "vmnet_socket_path", return_value="/x/socket_vmnet"):
            with self.assertRaises(ValueError):
                native.net_environment("host", "192.168.1.50/24")
        for mode in ("mcast", "mcast:40000", None, "none"):
            with self.assertRaises(ValueError, msg=mode):
                native.net_environment(mode, "192.168.1.50/24")


class WrapperTests(unittest.TestCase):
    def run_wrapper(self, args=("-m", "1"), **env):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "qemu-net.sh").write_bytes((TOOLS / "net/qemu-net.sh").read_bytes())
            Path(folder, "qemu-net.sh").chmod(0o700)
            Path(folder, "qemu-system-aarch64").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            Path(folder, "qemu-system-aarch64").chmod(0o700)
            done = subprocess.run([folder + "/qemu-net.sh", *args], capture_output=True, text=True,
                                  env={"PATH": os.environ["PATH"], **env})
        return done.returncode, done.stdout.split("\n")[:-1]

    def test_without_net_the_arguments_are_unchanged(self):
        self.assertEqual(self.run_wrapper(), (0, ["-m", "1"]))

    def test_mcast_uses_the_relay_stream_backend(self):
        macs = set()
        for _ in range(3):
            code, args = self.run_wrapper(SP16_NET_KIND="mcast", SP16_NET_TARGET="230.0.0.1:1234",
                                          SP16_NET_NO_RELAY="1", SP16_NET_SOCKET="/tmp/sp16-test.sock")
            self.assertEqual(code, 0)
            self.assertEqual(args[:3], ["-m", "1", "-netdev"])
            self.assertEqual(args[3], "stream,id=sp16net,server=off,addr.type=unix,addr.path=/tmp/sp16-test.sock")
            device = args[5]
            self.assertTrue(device.startswith("virtio-net-device,netdev=sp16net,mac=52:54:00:"), device)
            macs.add(device.rsplit("=", 1)[1])
        self.assertEqual(len(macs), 3)

    def test_mcast_relay_socket_defaults_under_tmp(self):
        import re
        code, args = self.run_wrapper(SP16_NET_KIND="mcast", SP16_NET_TARGET="230.0.0.1:34567",
                                      SP16_NET_NO_RELAY="1")
        self.assertEqual(code, 0)
        self.assertRegex(args[3], r"^stream,id=sp16net,server=off,addr.type=unix,addr.path="
                                 r".*sp16r-\d+\.sock$")

    def test_mcast_relay_failure_stops_qemu(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "qemu-net.sh").write_bytes((TOOLS / "net/qemu-net.sh").read_bytes())
            Path(folder, "qemu-net.sh").chmod(0o700)
            Path(folder, "qemu-system-aarch64").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            Path(folder, "qemu-system-aarch64").chmod(0o700)
            Path(folder, "noop-python").write_text('#!/bin/sh\nexit 0\n')
            Path(folder, "noop-python").chmod(0o700)
            done = subprocess.run([folder + "/qemu-net.sh"], capture_output=True, text=True,
                                  env={"PATH": os.environ["PATH"],
                                       "SP16_NET_KIND": "mcast", "SP16_NET_TARGET": "230.0.0.1:1234",
                                       "SP16_PYTHON": folder + "/noop-python"})
        self.assertEqual(done.returncode, 2)
        self.assertIn("relay did not start", done.stderr)

    def test_unix_uses_the_stream_backend(self):
        _, args = self.run_wrapper(SP16_NET_KIND="unix", SP16_NET_TARGET="/x/socket_vmnet")
        self.assertEqual(args[3], "stream,id=sp16net,server=off,addr.type=unix,addr.path=/x/socket_vmnet")

    def test_bridged_appends_the_mode_and_address_to_the_cmdline(self):
        args = ("-m", "1", "-append", "console=ttyAMA0 rdinit=/init", "-device", "virtio-serial-device")
        code, lines = self.run_wrapper(args, SP16_NET_KIND="unix", SP16_NET_TARGET="/x/sock",
                                       SP16_NET_MODE="bridged:en0", SP16_NET_IP="192.168.1.50/24")
        self.assertEqual(code, 0)
        at = lines.index("-append")
        self.assertEqual(lines[at + 1], "console=ttyAMA0 rdinit=/init sp16.net=bridged:en0 sp16.netip=192.168.1.50/24")

    def test_mcast_appends_only_the_mode(self):
        args = ("-m", "1", "-append", "console=ttyAMA0 rdinit=/init")
        code, lines = self.run_wrapper(args, SP16_NET_KIND="mcast", SP16_NET_TARGET="230.0.0.1:34567",
                                       SP16_NET_NO_RELAY="1", SP16_NET_MODE="mcast")
        self.assertEqual(code, 0)
        at = lines.index("-append")
        self.assertEqual(lines[at + 1], "console=ttyAMA0 rdinit=/init sp16.net=mcast")

    def test_cmdline_metacharacters_stay_literal(self):
        value = "console=$HOME `touch /tmp/sp16-unexpected`"
        code, lines = self.run_wrapper(("-append", value, "-device", "x=y"), SP16_NET_MODE="mcast")
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["-append", value + " sp16.net=mcast", "-device", "x=y"])

    def test_net_mode_requires_append(self):
        code, _ = self.run_wrapper(SP16_NET_MODE="mcast")
        self.assertEqual(code, 2)



class FakeMasterTests(unittest.TestCase):
    """Packet layouts of net/fake_master.py (also checked by hand against python-prodj-link's parser)."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fake_master", TOOLS / "net/fake_master.py")
        self.master = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.master)

    def test_keepalive_is_54_bytes_with_name_mac_and_ip(self):
        packet = self.master.keepalive("CDJ-3000", 2, bytes.fromhex("020000000007"), bytes([169, 254, 1, 2]))
        self.assertEqual((len(packet), packet[:10], packet[0x0a]), (54, b"Qspt1WmJOL", 0x06))
        self.assertEqual(packet[0x0c:0x14], b"CDJ-3000")
        self.assertEqual((packet[0x24], packet[0x26:0x2c], packet[0x2c:0x30]),
                         (2, bytes.fromhex("020000000007"), bytes([169, 254, 1, 2])))

    def test_beat_packet_carries_pitch_bpm_beat_and_distances(self):
        import struct
        packet = self.master.beat_packet("CDJ-3000", 1, 128.0, 0.02, 3, 100)
        self.assertEqual((len(packet), packet[0x0a], packet[0x23]), (96, 0x28, 0x3c))
        distances = struct.unpack(">6I", packet[0x24:0x3c])
        self.assertEqual(distances[0], 100)
        self.assertAlmostEqual(distances[2] - distances[0], 60000 / 130.56, delta=1)  # beat 3: one beat after the next, the bar
        pitch, _, bpm, beat = struct.unpack(">IHHB", packet[0x54:0x5d])
        self.assertEqual((pitch, bpm, beat), (round(1.02 * 0x100000), 12800, 3))

    def test_beat_packet_ends_with_the_redundant_device_number(self):
        # The follower's beat handler reads byte 0x5f and requires it to be 1-4 and equal to the followed master, so
        # the 96-byte packet ends 0x5d=0x00, 0x5e=0x00, 0x5f=player for every player number.
        for player in (1, 2, 3, 4):
            packet = self.master.beat_packet("CDJ-3000", player, 128.0, 0.0, 1, 100)
            self.assertEqual(len(packet), 96)
            self.assertEqual((packet[0x5d], packet[0x5e], packet[0x5f]), (0x00, 0x00, player))

    def test_status_packet_flags_master_sync_play(self):
        import struct
        packet = self.master.status_packet("CDJ-3000", 1, 128.0, 0.02, 2, 7)
        self.assertEqual((len(packet), packet[0x0a], packet[0x21]), (212, 0x0a, 1))
        self.assertEqual(packet[0x89] & 0x78, 0x78)
        self.assertEqual(struct.unpack(">H", packet[0x92:0x94])[0], 12800)
        self.assertEqual((struct.unpack(">I", packet[0xa0:0xa4])[0], packet[0xa6]), (7, 2))

    def test_status_packet_sets_p3_and_master_meaningful(self):
        import struct
        playing = self.master.status_packet("CDJ-3000", 1, 128.0, 0.02, 2, 7)
        stopped = self.master.status_packet("CDJ-3000", 1, 128.0, 0.02, 2, 7, playing=False)
        # P3 at 0x9d: 0x0d (track loaded, playing forward) passes the follower's (P3 & 1) || (P3 & 8) gate.
        self.assertEqual(playing[0x9d], 0x0d)
        self.assertEqual(stopped[0x9d], 0x00)
        # Mm (master meaningful) and Mv (rekordbox-track multiplier) match a real CDJ.
        self.assertEqual((playing[0x9e], struct.unpack(">H", playing[0x90:0x92])[0]), (0x01, 0x8000))

    def test_status_packet_keeps_playing_and_counter_positional(self):
        import struct
        # fake_master and segment_master call status_packet(..., True, status_count) positionally: the first status
        # (counter 0) must be a master and the counter must serialize at 0xc8, not be swallowed as the master flag.
        first = self.master.status_packet("CDJ-3000", 1, 128.0, 0.0, 1, 1, True, 0)
        self.assertTrue(first[0x89] & 0x20)                          # the first status is master
        self.assertEqual(struct.unpack(">I", first[0xc8:0xcc])[0], 0)
        stepped = self.master.status_packet("CDJ-3000", 1, 128.0, 0.0, 1, 1, True, 7)
        self.assertTrue(stepped[0x89] & 0x20)                        # still master
        self.assertEqual(struct.unpack(">I", stepped[0xc8:0xcc])[0], 7)   # the counter increments
        off = self.master.status_packet("CDJ-3000", 1, 128.0, 0.0, 1, 1, True, 7, master=False)
        self.assertFalse(off[0x89] & 0x20)                           # master is keyword-only and still overridable

    def test_packet_name_reads_each_family_at_its_own_offset(self):
        mac, ip = bytes.fromhex("020000000007"), bytes([169, 254, 7, 7])
        self.assertEqual(self.master.packet_name(self.master.keepalive("TORAIZ", 2, mac, ip), 50000), "TORAIZ")
        self.assertEqual(self.master.packet_name(self.master.beat_packet("CDJ-3000", 1, 128.0, 0.0, 1, 100), 50001),
                         "CDJ-3000")
        self.assertEqual(self.master.packet_name(self.master.status_packet("CDJ-3000", 1, 128.0, 0.0, 1, 1), 50002),
                         "CDJ-3000")
        # A join-stage hello on 50000 carries the same reserved byte before the name.
        hello = b"Qspt1WmJOL\x0a\x00" + b"CDJ-3000".ljust(20, b"\0") + b"\x01\x02\x00\x25\x01"
        self.assertEqual(self.master.packet_name(hello, 50000), "CDJ-3000")

    def test_name_offset_falls_back_to_shape_without_a_known_port(self):
        keepalive = self.master.keepalive("TORAIZ", 2, bytes(6), bytes(4))
        beat = self.master.beat_packet("CDJ-3000", 1, 128.0, 0.0, 1, 100)
        self.assertEqual(self.master.name_offset(keepalive, 50000), 0x0c)
        self.assertEqual(self.master.name_offset(beat, 50001), 0x0b)
        self.assertEqual(self.master.name_offset(beat, 50002), 0x0b)
        self.assertEqual(self.master.name_offset(keepalive), 0x0c)   # 54-byte type 0x06: the 50000 layout
        self.assertEqual(self.master.name_offset(beat), 0x0b)

    def test_packet_name_refuses_a_payload_too_short_for_the_field(self):
        for payload in (b"Qspt1WmJOL\x06", b"Qspt1WmJOL\x06\x00" + b"TORAIZ"):
            with self.assertRaises(ValueError, msg=payload):
                self.master.packet_name(payload, 50000)


if __name__ == "__main__":
    unittest.main()
