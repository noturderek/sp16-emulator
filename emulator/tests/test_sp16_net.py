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
                         {"SP16_NET_KIND": "mcast", "SP16_NET_TARGET": "230.0.0.1:34567"})
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
                             {"SP16_NET_KIND": "unix", "SP16_NET_TARGET": "/x/socket_vmnet"})
            native.net_environment("bridged:en0")
        found.assert_called_with("socket_vmnet.bridged.en0")


class WrapperTests(unittest.TestCase):
    def run_wrapper(self, **env):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "qemu-net.sh").write_bytes((TOOLS / "net/qemu-net.sh").read_bytes())
            Path(folder, "qemu-net.sh").chmod(0o700)
            Path(folder, "qemu-system-aarch64").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            Path(folder, "qemu-system-aarch64").chmod(0o700)
            done = subprocess.run([folder + "/qemu-net.sh", "-m", "1"], capture_output=True, text=True,
                                  env={"PATH": os.environ["PATH"], **env})
        return done.returncode, done.stdout.split("\n")[:-1]

    def test_without_net_the_arguments_are_unchanged(self):
        self.assertEqual(self.run_wrapper(), (0, ["-m", "1"]))

    def test_mcast_adds_a_nic_with_its_own_mac(self):
        macs = set()
        for _ in range(3):
            code, args = self.run_wrapper(SP16_NET_KIND="mcast", SP16_NET_TARGET="230.0.0.1:1234")
            self.assertEqual(code, 0)
            self.assertEqual(args[:3], ["-m", "1", "-netdev"])
            self.assertEqual(args[3], "socket,id=sp16net,mcast=230.0.0.1:1234")
            device = args[5]
            self.assertTrue(device.startswith("virtio-net-device,netdev=sp16net,mac=52:54:00:"), device)
            macs.add(device.rsplit("=", 1)[1])
        self.assertEqual(len(macs), 3)

    def test_unix_uses_the_stream_backend(self):
        _, args = self.run_wrapper(SP16_NET_KIND="unix", SP16_NET_TARGET="/x/socket_vmnet")
        self.assertEqual(args[3], "stream,id=sp16net,server=off,addr.type=unix,addr.path=/x/socket_vmnet")



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

    def test_status_packet_flags_master_sync_play(self):
        import struct
        packet = self.master.status_packet("CDJ-3000", 1, 128.0, 0.02, 2, 7)
        self.assertEqual((len(packet), packet[0x0a], packet[0x21]), (212, 0x0a, 1))
        self.assertEqual(packet[0x89] & 0x78, 0x78)
        self.assertEqual(struct.unpack(">H", packet[0x92:0x94])[0], 12800)
        self.assertEqual((struct.unpack(">I", packet[0xa0:0xa4])[0], packet[0xa6]), (7, 2))


if __name__ == "__main__":
    unittest.main()
