"""The live-hardware helpers' decoding: net/pdl_scan.py (Pro DJ Link packets and hints) and midi/thru.py (line
forwarding and endpoint matching). No network or CoreMIDI needed."""
import importlib.util
import struct
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "net"))

import fake_master  # noqa: E402
import pdl_scan  # noqa: E402

spec = importlib.util.spec_from_file_location("midi_thru", TOOLS / "midi" / "thru.py")
thru = importlib.util.module_from_spec(spec)
spec.loader.exec_module(thru)


class PdlScanTests(unittest.TestCase):
    def test_beat_packet_gives_player_bpm_and_pitch(self):
        packet = pdl_scan.decode(50001, fake_master.beat_packet("CDJ-3000", 2, 114.99, -0.0865, 3, 500))
        self.assertEqual((packet["name"], packet["player"], packet["beat_in_bar"]), ("CDJ-3000", 2, 3))
        self.assertAlmostEqual(packet["bpm"], 114.99)
        self.assertAlmostEqual(packet["effective"], 114.99 * (1 - 0.0865), places=2)

    def test_status_packet_gives_master_and_play_state(self):
        packet = pdl_scan.decode(50002, fake_master.status_packet("CDJ-3000", 1, 128.0, 0.02, 1, 4))
        self.assertEqual(packet["player"], 1)
        self.assertTrue(packet["master"])
        self.assertEqual(packet["play_state"], 0x0d)

    def test_foreign_or_short_datagrams_are_ignored(self):
        self.assertIsNone(pdl_scan.decode(50001, b"not pro dj link at all, padding padding"))
        self.assertIsNone(pdl_scan.decode(50000, pdl_scan.MAGIC))

    def test_hints_name_the_follower_gates(self):
        devices = pdl_scan.summarise([
            {"port": 50001, "kind": 0x28, "name": "CDJ-3000", "player": 5, "bpm": 120.0, "pitch": 1.0,
             "effective": 120.0, "beat_in_bar": 1},
        ])
        text = " ".join(pdl_scan.hints(devices))
        self.assertIn("no TORAIZ", text)
        self.assertIn("players 1-4", text)
        self.assertIn("press MASTER", text)          # no status seen: the master hint
        devices = pdl_scan.summarise([
            {"port": 50000, "kind": 6, "name": "TORAIZ", "player": 0x41},
            {"port": 50002, "kind": 0x0a, "name": "CDJ-3000", "player": 2, "master": False, "play_state": 0x0d},
            {"port": 50001, "kind": 0x28, "name": "CDJ-3000", "player": 2, "bpm": 120.0, "pitch": 1.0,
             "effective": 120.0, "beat_in_bar": 2},
        ])
        self.assertEqual(pdl_scan.hints(devices), ["CDJ-3000 is not the tempo master: press MASTER on it"])


class ThruTests(unittest.TestCase):
    def test_in_lines_become_out_lines(self):
        self.assertEqual(thru.forward("in fa\n"), "out fa")
        self.assertEqual(thru.forward("in 90 3c 64"), "out 90 3c 64")
        self.assertIsNone(thru.forward("ready"))

    def test_clock_and_active_sensing_are_forwarded_but_not_shown(self):
        self.assertFalse(thru.shown("in f8"))
        self.assertFalse(thru.shown("in fe"))
        self.assertTrue(thru.shown("in fa"))

    def test_endpoint_matching_is_exact_for_the_emulator_and_partial_for_the_device(self):
        listing = "source PIONEER DJ TSP-16 MIDI\ndestination SP-16 USB [pr-check]\ndestination SP-16 DIN [pr-check]\n"
        self.assertEqual(thru.endpoints_present(listing, "tsp-16", thru.target_name("pr-check")), (True, True))
        self.assertEqual(thru.endpoints_present(listing, "TSP-16", thru.target_name("other")), (True, False))
        self.assertEqual(thru.endpoints_present("", "TSP-16", "x"), (False, False))
        self.assertEqual(thru.target_name("b", din=True), "SP-16 DIN [b]")


if __name__ == "__main__":
    unittest.main()
