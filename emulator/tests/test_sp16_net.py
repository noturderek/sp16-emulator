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


if __name__ == "__main__":
    unittest.main()
