"""The channel-to-jack map: the JSON, the Swift monitor table and the shim's MAIN OUT pair must agree."""
import json
from pathlib import Path
import re
import unittest

TOOLS = Path(__file__).resolve().parents[1]


class JackMapTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((TOOLS / "audio-jack-map.json").read_text())

    def test_every_monitor_jack_is_mapped_once_with_a_grade(self):
        names = [j["jack"] for j in self.data["jacks"]]
        self.assertEqual(names, ["MAIN", "OUT3-4", "OUT5-6", "OUT7-8", "PHONES"])
        for jack in self.data["jacks"]:
            self.assertIn(jack["grade"], "OISU")
            self.assertTrue(jack["evidence"])

    def test_master_pairs_use_the_audio_channel_of_each_slot(self):
        padding = set(self.data["streams"]["ak4458audio-0"]["padding_channels"])
        used = []
        for jack in self.data["jacks"]:
            if jack["stream"] != "ak4458audio-0":
                continue
            for channel in (jack["left"], jack["right"]):
                self.assertNotIn(channel, padding)
                self.assertTrue(0 <= channel < 16)
                used.append(channel)
        self.assertEqual(len(used), len(set(used)))

    def test_swift_monitor_table_matches(self):
        swift = (TOOLS / "audio-sink.swift").read_text()
        table = dict((name, (int(a), int(b), int(c))) for name, a, b, c in
                     re.findall(r"case \.(\w+): return \((\d), (\d+), (\d+)\)", swift))
        names = {"MAIN": "main", "OUT3-4": "out34", "OUT5-6": "out56", "OUT7-8": "out78", "PHONES": "phones"}
        streams = {"ak4458audio-0": 0, "ak4452audio-0": 1}
        for jack in self.data["jacks"]:
            self.assertEqual(table[names[jack["jack"]]], (streams[jack["stream"]], jack["left"], jack["right"]), jack["jack"])

    def test_shim_scales_only_the_main_pair(self):
        source = (TOOLS / "hardware_stub.c").read_text()
        main = next(j for j in self.data["jacks"] if j["jack"] == "MAIN")
        self.assertIn("#define MAIN_PAIR_LEFT %du" % main["left"], source)
        self.assertIn("#define MAIN_PAIR_RIGHT %du" % main["right"], source)


if __name__ == "__main__":
    unittest.main()
