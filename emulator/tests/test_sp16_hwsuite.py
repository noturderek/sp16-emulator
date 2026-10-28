"""Hardware conformance suite runner (hwsuite.py): pure logic only, no VM, no firmware, synthetic data."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import hwsuite
import profiles

SPEC_SMOKE = ["HW-01", "HW-05", "HW-06", "HW-07", "HW-08", "HW-16", "HW-18", "HW-19", "HW-21", "HW-24", "HW-31",
              "HW-32", "HW-33", "HW-34", "HW-43", "HW-45"]


def all_pass(ids=None):
    return [hwsuite.verdict(hw, "PASS") for hw in (ids or hwsuite.select_checks("full"))]


class CheckTableTests(unittest.TestCase):
    def test_table_matches_the_spec(self):
        self.assertEqual([c.id for c in hwsuite.CHECKS], [f"HW-{n:02d}" for n in range(1, 55)])
        self.assertEqual(list(hwsuite.SMOKE_IDS), SPEC_SMOKE)
        self.assertEqual([c.id for c in hwsuite.CHECKS if c.tier == "L"], ["HW-44"])
        self.assertEqual([c.id for c in hwsuite.CHECKS if c.tier == "X"], [f"HW-{n}" for n in range(47, 55)])

    def test_tiers_and_only(self):
        self.assertEqual(hwsuite.select_checks("smoke"), SPEC_SMOKE)
        self.assertEqual(len(hwsuite.select_checks("core")), 45)
        self.assertEqual(len(hwsuite.select_checks("full")), 46)
        self.assertEqual(hwsuite.select_checks("full", ["HW-18", "HW-05"]), ["HW-05", "HW-18"])
        self.assertEqual(hwsuite.select_checks("smoke", ["HW-02", "HW-05"]), ["HW-05"])
        # Extras stay out of full and core, and run by tier or by name.
        self.assertNotIn("HW-47", hwsuite.select_checks("full"))
        self.assertEqual(hwsuite.select_checks("extra"), [f"HW-{n}" for n in range(47, 55)])
        self.assertEqual(hwsuite.select_checks("full", ["HW-47", "HW-05"]), ["HW-05", "HW-47"])
        with self.assertRaises(ValueError):
            hwsuite.select_checks("full", ["HW-99"])
        with self.assertRaises(ValueError):
            hwsuite.select_checks("everything")

    def test_checks_hash_is_stable_and_sensitive(self):
        first = hwsuite.checks_hash()
        self.assertEqual(first, hwsuite.checks_hash())
        with mock.patch.object(hwsuite.CHECKS[0], "tier", "C"):
            self.assertNotEqual(first, hwsuite.checks_hash())

    def test_hardware_fingerprint_covers_the_boundary_sources(self):
        fingerprint = hwsuite.hardware_fingerprint()
        self.assertEqual(fingerprint["hardware_epoch"], hwsuite.HARDWARE_EPOCH)
        self.assertEqual(set(fingerprint["sources"]), set(hwsuite.FINGERPRINT_SOURCES))
        self.assertTrue(all(fingerprint["sources"].values()))


class ProfileFieldTests(unittest.TestCase):
    def test_missing_data_blocks(self):
        profile = {"identity": {"boot_timeout_s": 60}, "touch_probe": None, "touch_dead": {},
                   "recipes": {"sample_load": [], "velocity_enable": [{"press": "home"}]},
                   "panel": {"pad_curve": {"a_min": 1}}}
        self.assertEqual(hwsuite.missing_fields(profile, "HW-01"), [])
        self.assertEqual(hwsuite.missing_fields(profile, "HW-03"), ["touch_probe", "touch_dead"])
        self.assertEqual(hwsuite.missing_fields(profile, "HW-22"), ["recipes.sample_load"])

    def test_false_and_zero_are_authored(self):
        self.assertTrue(hwsuite.authored(False))
        self.assertTrue(hwsuite.authored(0))
        self.assertFalse(hwsuite.authored(None))
        self.assertFalse(hwsuite.authored([]))
        self.assertFalse(hwsuite.authored(""))

    def test_repository_profiles_author_every_check(self):
        for path in sorted((TOOLS / "profiles").glob("sp16-*.json")):
            profile = profiles.validate(json.loads(path.read_text()))
            for check in hwsuite.CHECKS:
                self.assertEqual(hwsuite.missing_fields(profile, check.id), [], f"{path.name} {check.id}")

    def test_absent_reason(self):
        profile = {"hardware": {"absent": [{"capability": "usb_stop_lamp", "reason": "no lamp on this version"}]}}
        self.assertEqual(hwsuite.absent_reason(profile, "usb_stop_lamp"), "no lamp on this version")
        self.assertIsNone(hwsuite.absent_reason(profile, "strip"))

    def test_known_failing(self):
        profile = {"hardware": {"known_failing": [{"check": "HW-35", "reason": "r", "owner": "owner",
                                                    "date": "2026-10-28", "gap": "g"}]}}
        self.assertEqual(hwsuite.known_failing(profile, "HW-35")["reason"], "r")
        self.assertIsNone(hwsuite.known_failing(profile, "HW-36"))
        self.assertIsNone(hwsuite.known_failing({}, "HW-35"))


class VerdictTests(unittest.TestCase):
    def test_na_needs_a_reason(self):
        with self.assertRaises(ValueError):
            hwsuite.verdict("HW-30", "N/A")
        with self.assertRaises(ValueError):
            hwsuite.verdict("HW-30", "MAYBE")
        with self.assertRaises(ValueError):
            hwsuite.verdict("HW-35", "KNOWN-FAIL")

    def test_aggregation(self):
        ok, bad = hwsuite.sub("a", True), hwsuite.sub("b", False, "lost a detent")
        blocked, na = hwsuite.sub("c", False, "no recipe", blocked=True), hwsuite.sub("d", True, "analog", na=True)
        self.assertEqual(hwsuite.from_subresults("HW-18", [ok, na])["status"], "PASS")
        self.assertEqual(hwsuite.from_subresults("HW-18", [ok, bad, blocked])["status"], "FAIL")
        self.assertIn("lost a detent", hwsuite.from_subresults("HW-18", [ok, bad])["reason"])
        self.assertEqual(hwsuite.from_subresults("HW-18", [ok, blocked])["status"], "BLOCKED")
        self.assertEqual(hwsuite.from_subresults("HW-18", [na])["status"], "N/A")
        self.assertEqual(hwsuite.from_subresults("HW-18", [])["status"], "BLOCKED")

    def test_summary_and_eligibility(self):
        verdicts = all_pass()
        self.assertTrue(hwsuite.summarize(verdicts, "full")["verified_eligible"])
        self.assertFalse(hwsuite.summarize(verdicts, "full", ["HW-01"])["verified_eligible"])
        self.assertFalse(hwsuite.summarize(verdicts, "core")["verified_eligible"])
        self.assertFalse(hwsuite.summarize(verdicts[:-1], "full")["verified_eligible"])
        verdicts[3] = hwsuite.verdict("HW-04", "BLOCKED", "x")
        self.assertFalse(hwsuite.summarize(verdicts, "full")["verified_eligible"])
        many_na = [hwsuite.verdict(v["id"], "N/A", "absent") if i < 6 else v for i, v in enumerate(all_pass())]
        summary = hwsuite.summarize(many_na, "full")
        self.assertTrue(summary["verified_eligible"])
        self.assertTrue(summary["needs_owner_signoff"])

    def test_exit_codes(self):
        self.assertEqual(hwsuite.exit_code(all_pass()), hwsuite.EXIT_OK)
        blocked = all_pass() + [hwsuite.verdict("HW-02", "BLOCKED", "x")]
        self.assertEqual(hwsuite.exit_code(blocked), hwsuite.EXIT_BLOCKED)
        self.assertEqual(hwsuite.exit_code(blocked + [hwsuite.verdict("HW-03", "FAIL", "x")]), hwsuite.EXIT_FAIL)
        self.assertEqual(hwsuite.exit_code(all_pass(), session_lost=True), hwsuite.EXIT_LOST)

    def test_known_fail_is_expected_so_does_not_block_or_fail(self):
        verdicts = all_pass()
        verdicts[34] = hwsuite.verdict("HW-35", "KNOWN-FAIL", "known-failing (owner, 2026-10-28): r")
        summary = hwsuite.summarize(verdicts, "full")
        self.assertTrue(summary["verified_eligible"])
        self.assertEqual(summary["counts"]["KNOWN-FAIL"], 1)
        self.assertEqual(hwsuite.exit_code(verdicts), hwsuite.EXIT_OK)

    def test_report_lists_failures_and_na(self):
        verdicts = all_pass()
        verdicts[17] = hwsuite.from_subresults("HW-18", [hwsuite.sub("wrap", False, "16 -> 2")])
        verdicts[29] = hwsuite.verdict("HW-30", "N/A", "analog-only")
        result = {"suite": {"version": "1", "checks_hash": "0" * 64,
                            "hardware_fingerprint": {"sha256": "1" * 64, "hardware_epoch": 1}},
                  "run": {"id": "r", "tier": "full", "start": "s", "end": "e", "firmware_version": "1.60"},
                  "verdicts": verdicts, "summary": hwsuite.summarize(verdicts)}
        text = hwsuite.render_report(result)
        self.assertIn("## HW-18 FAIL", text)
        self.assertIn("- HW-30: analog-only", text)
        self.assertEqual(text.count("| HW-"), len(verdicts))

    def test_report_lists_known_fail_with_its_reason(self):
        verdicts = all_pass()
        verdicts[34] = hwsuite.verdict("HW-35", "KNOWN-FAIL", "known-failing (owner, 2026-10-28): r; measured gap: g",
                                       subresults=[hwsuite.sub("full-scale tone reaches the output", False, "peak 17352")])
        result = {"suite": {"version": "1", "checks_hash": "0" * 64,
                            "hardware_fingerprint": {"sha256": "1" * 64, "hardware_epoch": 1}},
                  "run": {"id": "r", "tier": "full", "start": "s", "end": "e", "firmware_version": "1.60"},
                  "verdicts": verdicts, "summary": hwsuite.summarize(verdicts)}
        text = hwsuite.render_report(result)
        self.assertIn("## HW-35 KNOWN-FAIL", text)
        self.assertIn("known-failing (owner, 2026-10-28)", text)


class KnownFailingRecordTests(unittest.TestCase):
    def runner(self, profile):
        path = TOOLS / "profiles" / "sp16-1.60.json"
        return hwsuite.Runner(profile, path, None, None, TOOLS, TOOLS, "full")

    def test_a_declared_failure_is_relabelled_known_fail_and_keeps_the_measurement(self):
        profile = json.loads((TOOLS / "profiles" / "sp16-1.60.json").read_text())
        runner = self.runner(profile)
        observed = "peak 17352 (-5.52 dBFS)"
        runner.record("HW-35", lambda: hwsuite.verdict("HW-35", "FAIL", "",
                                                       subresults=[hwsuite.sub("full-scale tone reaches the output",
                                                                                False, observed)]))
        result = runner.verdicts["HW-35"]
        self.assertEqual(result["status"], "KNOWN-FAIL")
        self.assertIn("known-failing (owner, 2026-10-28)", result["reason"])
        self.assertIn("measured gap", result["reason"])
        self.assertEqual(result["subresults"][0]["observed"], observed)
        self.assertEqual(result["evidence"], ["known-failing"])

    def test_an_undeclared_failure_stays_fail(self):
        profile = json.loads((TOOLS / "profiles" / "sp16-1.60.json").read_text())
        profile["hardware"]["known_failing"] = []
        runner = self.runner(profile)
        runner.record("HW-36", lambda: hwsuite.verdict("HW-36", "FAIL", "x"))
        self.assertEqual(runner.verdicts["HW-36"]["status"], "FAIL")


class FrameTests(unittest.TestCase):
    def frame(self):
        frame = bytearray(128)
        frame[0], frame[0x77] = 0x02, 0xaa
        struct.pack_into("<H", frame, 124, hwsuite.crc16_x25(frame[:124]))
        return bytes(frame)

    def test_crc_x25_check_value(self):
        self.assertEqual(hwsuite.crc16_x25(b"123456789"), 0x906E)

    def test_tx_frame_criterion(self):
        good = self.frame()
        self.assertTrue(hwsuite.tx_frame_ok(good))
        self.assertFalse(hwsuite.tx_frame_ok(good[:127]))
        self.assertFalse(hwsuite.tx_frame_ok(b"\x01" + good[1:]))
        broken = bytearray(good)
        broken[10] ^= 1
        self.assertFalse(hwsuite.tx_frame_ok(bytes(broken)))

    def test_parse_tx_log_strips_the_prefix_and_skips_garbage(self):
        good = self.frame()
        data = (json.dumps({"t": 1.5, "hex": ("000001" + good.hex())}) + "\nnot json\n" +
                json.dumps({"t": 2}) + "\n").encode()
        self.assertEqual(hwsuite.parse_tx_log(data), [(1.5, good)])

    def test_playhead_and_revisit_period(self):
        white = (74, 74, 74)
        records = []
        for i in range(48):
            rgb = {ch: (0, 0, 0) for ch in range(32)}
            rgb[i % 16] = white
            records.append((i * 0.125, {"rgb": rgb}))
            records.append((i * 0.125 + 0.01, {"rgb": rgb}))  # repeated frames are not new steps
        heads = hwsuite.playhead_steps(records, white)
        self.assertEqual(len(heads), 48)
        self.assertAlmostEqual(hwsuite.revisit_period(heads), 2.0)
        self.assertIsNone(hwsuite.revisit_period(heads[:10]))

    def test_one_hot_respects_the_dim_flag(self):
        decoded = {"mono": {0: (1, 0), 1: (1, 1), 2: (0, 0), 12: (1, None)}}
        self.assertEqual(hwsuite.one_hot(decoded, [0, 1, 2]), [0])
        self.assertEqual(hwsuite.one_hot(decoded, [0, 1, 2], bright_only=False), [0, 1])
        self.assertEqual(hwsuite.one_hot(decoded, [12]), [12])

    def test_value_readings(self):
        self.assertEqual(hwsuite.read_number("Volume 127"), 127.0)
        self.assertEqual(hwsuite.read_number("+1,0"), 1.0)
        self.assertIsNone(hwsuite.read_number("OFF"))
        self.assertEqual(hwsuite.signed_value("L64"), -64.0)
        self.assertEqual(hwsuite.signed_value("R 3"), 3.0)
        self.assertEqual(hwsuite.signed_value("C"), 0.0)
        self.assertEqual(hwsuite.median([3, 1, 2, 10]), 2.5)


class FixtureTests(unittest.TestCase):
    def test_wav_fixtures(self):
        import wave
        import io
        wavs = hwsuite.fixture_wavs()
        self.assertEqual(set(wavs), {"hw-tone440.wav", "hw-click.wav", "hw-tone-full.wav", "hw-tone-m30.wav"})
        with wave.open(io.BytesIO(wavs["hw-tone440.wav"])) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                             (2, 2, 44100, 6 * 44100))
        self.assertEqual(wavs, hwsuite.fixture_wavs())  # deterministic

    def test_fat32_round_trip_and_dirty_flag(self):
        files = {"hw-a.wav": b"x" * 1500, "B.TXT": b"", "long name file.wav": bytes(range(256)) * 9}
        with tempfile.TemporaryDirectory() as tmp:
            for dirty in (False, True):
                payload, total = hwsuite.stick_image(files, "PIONEER DJ SAMPLER/Samples", dirty=dirty)
                self.assertEqual(total, 64 << 20)
                path = Path(tmp) / f"stick-{dirty}.img"
                hwsuite.write_sparse(path, payload, total)
                self.assertEqual(path.stat().st_size, total)
                reader = hwsuite.FatReader(path)
                try:
                    self.assertEqual(reader.dirty_flag, dirty)
                    self.assertEqual(sorted(reader.list("PIONEER DJ SAMPLER/Samples")), sorted(files))
                    for name, data in files.items():
                        self.assertEqual(reader.read(f"PIONEER DJ SAMPLER/Samples/{name}"), data)
                    self.assertIsNone(reader.read("PIONEER DJ SAMPLER/missing.wav"))
                    self.assertEqual(reader.fat[1], 0x07FFFFFF if dirty else 0x0FFFFFFF)
                finally:
                    reader.close()

    def test_directory_spanning_several_clusters(self):
        files = {f"sample-{n:03d}-with-a-long-name.wav": bytes([n]) * 10 for n in range(40)}
        payload, total = hwsuite.stick_image(files, "S")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.img"
            hwsuite.write_sparse(path, payload, total)
            reader = hwsuite.FatReader(path)
            try:
                self.assertEqual(len(reader.list("S")), 40)
                self.assertEqual(reader.read("S/sample-039-with-a-long-name.wav"), bytes([39]) * 10)
            finally:
                reader.close()

    def test_make_fixtures_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            hashes = hwsuite.make_fixtures(Path(tmp) / "fx")
            self.assertEqual(set(hashes), {"hw-tone440.wav", "hw-click.wav", "hw-tone-full.wav", "hw-tone-m30.wav",
                                           "hw-stick.img", "hw-dirty.img", "hw-blank.img"})
            self.assertEqual(hashes["hw-stick.img"], hwsuite.sha256_file(Path(tmp) / "fx" / "hw-stick.img"))
            again = hwsuite.make_fixtures(Path(tmp) / "fx2")
            self.assertEqual(hashes, again)


class FakeSession:
    def __init__(self, texts=()):
        self.calls, self.texts = [], list(texts)

    def press(self, name, hold=0.15, value=100):
        self.calls.append(("press", name))

    def event(self, name, action, value=0):
        self.calls.append(("event", name, action, value))

    def turn(self, name, detents, gap=0.3):
        self.calls.append(("turn", name, detents))

    def tap(self, x, y, hold=0.1):
        self.calls.append(("tap", x, y))

    def image(self, tag="x"):
        from PIL import Image
        return Image.new("RGB", (16, 16))


@unittest.skipUnless(importlib.util.find_spec("PIL"), "pillow is only needed for the hardware suite")
class RecipeTests(unittest.TestCase):
    def run_recipe(self, steps, texts, params=None):
        session = FakeSession()
        texts = list(texts)
        with mock.patch.object(hwsuite, "ocr", side_effect=lambda *a, **k: texts.pop(0)), \
                mock.patch.object(hwsuite.time, "sleep"):
            log = hwsuite.run_recipe(session, steps, params)
        return session.calls, log

    def test_placeholders_and_branches(self):
        steps = [{"press": "home"},
                 {"if_text": "{sample_re}", "box": [0, 0, 10, 10], "else": [{"tap": [615, 142]}]},
                 {"expect_text": "{sample_re}", "box": [0, 0, 10, 10]}]
        calls, log = self.run_recipe(steps, ["nothing", "HW-TONE440"], {"sample_re": "HW-TONE440"})
        self.assertEqual(calls, [("press", "home"), ("tap", 615, 142)])
        self.assertEqual(log[0]["branch"], "else")

    def test_expectation_failure_raises(self):
        with mock.patch.object(hwsuite.time, "monotonic", side_effect=[0, 0, 100]):
            with self.assertRaises(hwsuite.RecipeError):
                self.run_recipe([{"expect_text": "^ON", "box": [0, 0, 1, 1], "timeout": 1}], ["OFF", "OFF"])

    def test_unknown_step_kind(self):
        with self.assertRaises(hwsuite.RecipeError):
            self.run_recipe([{"dance": 1}], [])

    def test_regex_braces_are_not_format_fields(self):
        calls, log = self.run_recipe([{"expect_text": "^[0-9]{2}$", "box": [0, 0, 1, 1]}], ["12"], {"x": "y"})
        self.assertEqual(log[0]["observed"], "12")


@unittest.skipUnless(importlib.util.find_spec("PIL"), "pillow is only needed for the hardware suite")
class ImageTests(unittest.TestCase):
    def test_confined_and_highlight(self):
        from PIL import Image, ImageDraw
        base = Image.new("RGB", (100, 60), (10, 10, 10))
        changed = base.copy()
        ImageDraw.Draw(changed).rectangle((10, 10, 20, 20), fill=(200, 200, 200))
        self.assertTrue(hwsuite.Runner.confined(base, changed, [(5, 5, 25, 25)]))
        self.assertFalse(hwsuite.Runner.confined(base, changed, [(30, 5, 50, 25)]))
        self.assertEqual(hwsuite.diff_box(base, changed), (10, 10, 21, 21))
        listing = base.copy()
        ImageDraw.Draw(listing).rectangle((0, 30, 99, 44), fill=(225, 225, 230))
        self.assertEqual(hwsuite.highlighted_band(listing, (0, 0, 100, 60)), (30, 45))
        self.assertIsNone(hwsuite.highlighted_band(base, (0, 0, 100, 60)))


class TrackSelectionTests(unittest.TestCase):
    """`on_track`: HW-34 must not read a level from the VOLUME knob of a track nobody is playing."""

    def runner(self, tmp):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        runner = hwsuite.Runner(json.loads(profile_path.read_text()), profile_path, tmp, None, tmp, tmp, "core",
                                ["HW-34"])
        runner.session = FakeSession()
        return runner

    def on_track(self, tiles):
        """Run on_track with the tile screen reading `tiles` in turn; returns (result, calls made)."""
        calls = []

        def tile(image, probe):
            calls.append("tile")
            return tiles[min(len(calls) - 1, len(tiles) - 1)]

        def select(track):
            calls.append(("select", track))
            return True

        def home():
            calls.append("home")
            return True
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.runner(tmp)
            with mock.patch.object(hwsuite, "selected_tile", side_effect=tile), \
                    mock.patch.object(hwsuite.Runner, "select_track", side_effect=select), \
                    mock.patch.object(hwsuite.Runner, "go_home", side_effect=home):
                return runner.on_track(1), calls

    def test_a_track_that_is_selected_is_not_touched(self):
        (selected, shown), calls = self.on_track([1])
        self.assertEqual((selected, shown), (True, 1))
        self.assertEqual(calls, ["tile"])

    def test_the_wrong_track_is_selected_again(self):
        """The reported bug: TRACK 7 was selected after the knob step, so the level never moved. on_track puts TRACK 1
        back and says so."""
        (selected, shown), calls = self.on_track([7, 1])
        self.assertEqual((selected, shown), (True, 1))
        self.assertEqual(calls, ["tile", ("select", 1), "tile"])

    def test_a_track_that_stays_wrong_is_reported_not_guessed(self):
        (selected, shown), _ = self.on_track([7, 7])
        self.assertEqual((selected, shown), (False, 7))

    def test_another_screen_is_looked_at_again_on_the_home_screen(self):
        (selected, shown), calls = self.on_track([None, 1])
        self.assertEqual((selected, shown), (True, 1))
        self.assertEqual(calls, ["tile", "home", "tile"])

    def test_an_unreadable_screen_is_not_selected_and_moves_nothing(self):
        (selected, shown), calls = self.on_track([None, None])
        self.assertEqual((selected, shown), (False, None))
        self.assertEqual(calls, ["tile", "home", "tile"])


class TestmodePageTests(unittest.TestCase):
    """The factory test page readers and the checks' gating, on synthetic LCD images."""

    def gauge_image(self, fills):
        """Page 5 look: a light-blue bar from the gauge's left edge to `fill` of its width, and a red OK where asked."""
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (800, 480), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        for name, (fill, ok) in fills.items():
            top, bottom, _ = hwsuite.TESTMODE_GAUGES[name]
            left, right = hwsuite.TESTMODE_GAUGE_X
            draw.rectangle((left, top, right, bottom), outline=(200, 200, 200))
            if fill:
                draw.rectangle((left + 1, top + 1, left + 1 + round(fill * (right - left - 2)), bottom - 1),
                               fill=(208, 208, 255))
            if ok:
                draw.rectangle((670, top + 8, 700, bottom - 8), fill=(255, 0, 0))
        return image

    def test_a_plain_white_or_black_lcd_is_page_1_or_2_even_with_a_stray_row(self):
        from PIL import Image
        white = Image.new("RGB", (800, 480), (255, 255, 255))
        black = Image.new("RGB", (800, 480), (0, 0, 0))
        black.paste((255, 255, 255), (0, 479, 800, 480))                 # the snapshot's one bright edge row
        self.assertEqual(hwsuite.testmode_page_of(white), 1)
        self.assertEqual(hwsuite.testmode_page_of(black), 2)

    def test_gauge_fill_and_ok_are_measured_from_pixels(self):
        image = self.gauge_image({"main_out": (0.5, False), "drive": (1.0, True), "hp_cutoff": (0.0, False),
                                  "strip": (64 / 127, False)})
        fraction, ok = hwsuite.testmode_gauge(image, "main_out")
        self.assertAlmostEqual(fraction, 0.5, delta=0.02)
        self.assertFalse(ok)
        self.assertEqual(hwsuite.testmode_gauge(image, "drive"), (1.0, True))
        self.assertEqual(hwsuite.testmode_gauge(image, "hp_cutoff"), (0.0, False))
        self.assertAlmostEqual(hwsuite.testmode_gauge(image, "strip")[0], 64 / 127, delta=0.02)

    def test_the_button_test_covers_fifty_distinct_controls(self):
        controls = hwsuite.TESTMODE_LED_BUTTONS + hwsuite.TESTMODE_PLAIN_BUTTONS
        self.assertEqual((len(hwsuite.TESTMODE_LED_BUTTONS), len(controls), len(set(controls))), (48, 50, 50))
        import panel_map
        self.assertTrue(all(name in panel_map.CONTROL_IDS for name in controls))
        self.assertFalse({"selector", "back", "home"} & set(controls))

    def test_page_3_start_button_is_idle_when_red_and_busy_when_pale(self):
        from PIL import Image, ImageDraw
        for colour, idle in (((228, 60, 60), True), ((240, 160, 160), False)):
            image = Image.new("RGB", (800, 480), (255, 255, 255))
            ImageDraw.Draw(image).rectangle((726, 138, 795, 207), fill=colour)
            self.assertEqual(hwsuite.testmode_start_ready(image), idle, colour)

    def test_page_3_pad_cells_form_a_four_by_four_grid_from_the_bottom_left(self):
        seen = []

        def fake_ocr(image, box, **kwargs):
            seen.append(box)
            return "300"
        with mock.patch.object(hwsuite, "ocr", fake_ocr):
            self.assertEqual([hwsuite.testmode_pad_raw(None, pad) for pad in (1, 4, 13, 16)], [300] * 4)
        self.assertEqual([((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) for b in seen], [(45, 408), (585, 408), (45, 108), (585, 108)])

    def test_the_calibration_file_is_read_from_the_data_image(self):
        fat = hwsuite.Fat32(64 << 20)
        xml = "<PadCalibrationData " + " ".join(f'CALIB_VALUE_PAD{n}="{300 if n == 3 else 242}"' for n in range(1, 17)) + "/>"
        fat.add_file(fat.path([".sp16", "data"]), "padcalibration.xml", xml.encode())
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "emmc.img"
            payload, total = fat.image()
            hwsuite.write_sparse(image, payload, total)
            values = hwsuite.calibration_values(image)
            self.assertEqual((len(values), values[3], values[16]), (16, 300, 242))
            self.assertEqual(hwsuite.calibration_values(image, "elsewhere.xml"), {})

    def calibration_runner(self, tmp):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        runner = hwsuite.Runner(json.loads(profile_path.read_text()), profile_path, tmp, None, tmp, tmp, "extra",
                                ["HW-53"], testmode_build=tmp)
        runner.session = FakeSession()
        return runner

    def walk(self, runner, before, after, ready=True):
        with mock.patch.object(hwsuite.time, "sleep"), mock.patch.object(hwsuite, "wait_settled"), \
                mock.patch.object(hwsuite, "testmode_pad_raw", return_value=300), \
                mock.patch.object(hwsuite, "testmode_start_ready", return_value=ready), \
                mock.patch.object(hwsuite.Runner, "calibration_now", side_effect=after):
            return runner.calibration_walk(1, 300, before)

    def test_a_calibration_walk_holds_the_pad_makes_three_passes_and_lets_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.calibration_runner(tmp)
            result = self.walk(runner, {1: 242}, [{1: 242}, {1: 300}])
        self.assertEqual(result, (300, True, {1: 300}))
        self.assertEqual(runner.session.calls, [("event", "pressure1", "value", 300)] + [("press", "usb_stop")] * 3
                         + [("event", "pressure1", "value", 0)])

    def test_a_calibration_walk_lets_go_of_the_pad_even_when_the_session_fails(self):
        class Failing(FakeSession):
            def press(self, name, hold=0.15, value=100):
                super().press(name, hold, value)
                raise hwsuite.SessionLost("socket error")
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.calibration_runner(tmp)
            runner.session = Failing()
            with self.assertRaises(hwsuite.SessionLost):
                self.walk(runner, {1: 242}, [{1: 242}])
        self.assertEqual(runner.session.calls[-1], ("event", "pressure1", "value", 0))

    def run_hw53(self, outcomes, start=None):
        """hw53 with the walks stubbed: outcomes[pad] = (page reading, START idle, the file's value for that pad)."""
        walks = []
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.calibration_runner(tmp)
            default = runner.p["calibration"]["default"]
            staged = start if start is not None else {pad: default for pad in range(1, 17)}

            def walk(pad, raw, before):
                walks.append((pad, raw))
                shown, idle, value = outcomes[pad]
                return shown, idle, {**before, pad: value}
            with mock.patch.object(hwsuite.Runner, "testmode_page_is", return_value=hwsuite.sub("stepped to page 3", True)), \
                    mock.patch.object(hwsuite, "wait_settled"), \
                    mock.patch.object(hwsuite.Runner, "calibration_now", return_value=staged), \
                    mock.patch.object(hwsuite.Runner, "calibration_walk", side_effect=walk):
                return runner.hw53(), walks

    GOOD_WALKS = {1: (300, True, 300), 2: (48, True, 48), 3: (504, True, 504), 4: (47, True, 242), 5: (505, True, 242)}

    def test_hw53_passes_when_the_range_edges_are_honoured(self):
        result, walks = self.run_hw53(self.GOOD_WALKS)
        self.assertEqual((result["status"], len(result["subresults"])), ("PASS", 12))
        self.assertEqual(walks, [(1, 300), (2, 48), (3, 504), (4, 47), (5, 505)])

    def test_hw53_fails_when_an_out_of_range_value_is_recorded(self):
        result, _ = self.run_hw53({**self.GOOD_WALKS, 4: (47, True, 47)})
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("just below the range is not recorded", result["reason"])

    def test_hw53_fails_when_a_valid_value_is_not_recorded_or_the_passes_do_not_finish(self):
        self.assertEqual(self.run_hw53({**self.GOOD_WALKS, 2: (48, True, 242)})[0]["status"], "FAIL")
        result, _ = self.run_hw53({**self.GOOD_WALKS, 1: (300, False, 300)})
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("pad 1 held at 300: the page shows it", result["reason"])

    def test_hw53_fails_when_the_staged_file_does_not_start_at_the_default(self):
        result, _ = self.run_hw53(self.GOOD_WALKS, start={pad: 242 if pad != 7 else 300 for pad in range(1, 17)})
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("synthetic default", result["reason"])

    def hw54_runner(self, tmp):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        runner = hwsuite.Runner(json.loads(profile_path.read_text()), profile_path, tmp, None, tmp, tmp, "extra",
                                ["HW-54"], testmode_build=tmp)
        runner.session = FakeSession()
        return runner

    def test_hw54_passes_when_the_loopback_reads_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.hw54_runner(tmp)
            with mock.patch.object(hwsuite.Runner, "testmode_page_is", return_value=hwsuite.sub("stepped to page 8", True)), \
                    mock.patch.object(hwsuite, "wait_settled"), \
                    mock.patch.object(hwsuite, "wait_midi_check", return_value=(True, "MIDI CHECK : OK")), \
                    mock.patch.object(hwsuite.Runner, "shots_into"):
                result = runner.hw54()
        self.assertEqual((result["status"], len(result["subresults"])), ("PASS", 2))
        self.assertEqual(runner.session.calls, [("tap", 115, 112)])

    def test_hw54_fails_when_the_page_never_reads_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.hw54_runner(tmp)
            with mock.patch.object(hwsuite.Runner, "testmode_page_is", return_value=hwsuite.sub("stepped to page 8", True)), \
                    mock.patch.object(hwsuite, "wait_settled"), \
                    mock.patch.object(hwsuite, "wait_midi_check", return_value=(False, "MIDI CHECK : ---")), \
                    mock.patch.object(hwsuite.Runner, "shots_into"):
                result = runner.hw54()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("MIDI CHECK OK", result["reason"])
        self.assertIn("MIDI CHECK : ---", result["reason"])

    def test_hw54_fails_when_page_8_never_shows(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.hw54_runner(tmp)
            with mock.patch.object(hwsuite.Runner, "testmode_page_is", return_value=hwsuite.sub("stepped to page 8", False, "the page never showed")):
                result = runner.hw54()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(runner.session.calls, [])

    def test_wait_midi_check_reads_ok_and_reports_the_last_text_on_timeout(self):
        session = FakeSession()
        with mock.patch.object(hwsuite, "ocr", side_effect=["——", "OK"]) as reader, \
                mock.patch.object(hwsuite.time, "sleep"):
            ok, text = hwsuite.wait_midi_check(session, timeout=30)
        self.assertTrue(ok)
        self.assertEqual(text, "OK")
        self.assertEqual(reader.call_args.args[1], hwsuite.MIDI_CHECK_STATUS_BOX)
        with mock.patch.object(hwsuite, "ocr", return_value="---"), \
                mock.patch.object(hwsuite.time, "sleep"), \
                mock.patch.object(hwsuite.time, "monotonic", side_effect=[0.0, 0.0, 31.0]):
            ok, text = hwsuite.wait_midi_check(session, timeout=30)
        self.assertFalse(ok)
        self.assertIn("---", text)

    def test_the_page_4_status_cell_names_the_control_it_counted(self):
        """The status cell prints the control the page counted (`PAD 1 : 3`); the reader must name that control and no
        other, so a press the page misattributes is visible."""
        self.assertEqual(hwsuite.testmode_status_controls("PAD 1 : 3"), {"pad1"})
        self.assertEqual(hwsuite.testmode_status_controls("STEP 16 : 1"), {"step16"})
        self.assertEqual(hwsuite.testmode_status_controls("STEP 1 : 1"), {"step1"})
        self.assertEqual(hwsuite.testmode_status_controls("BAR 3 : 41"), {"bar3"})
        self.assertEqual(hwsuite.testmode_status_controls("USB STOP : 2"), {"usb_stop"})   # not STOP
        self.assertEqual(hwsuite.testmode_status_controls("USBSTOP : 2"), {"usb_stop"})    # OCR drops the space
        self.assertEqual(hwsuite.testmode_status_controls("PAD12 : 1"), {"pad12"})
        self.assertEqual(hwsuite.testmode_status_controls("STOP : 2"), {"stop"})
        self.assertEqual(hwsuite.testmode_status_controls("SHIFT"), {"shift"})
        self.assertEqual(hwsuite.testmode_status_controls("MODE : ---"), {"mode"})
        # Not a name: the idle placeholder, an empty cell, and a name that is not the cell's first tokens.
        self.assertEqual(hwsuite.testmode_status_controls("--------- : ---"), set())
        self.assertEqual(hwsuite.testmode_status_controls(""), set())
        self.assertEqual(hwsuite.testmode_status_controls("n/a : play"), set())

    def test_every_control_page_4_counts_has_a_printed_name(self):
        self.assertEqual(set(hwsuite.TESTMODE_STATUS_NAMES), set(hwsuite.TESTMODE_CONTROLS))
        self.assertEqual(len({words for words in hwsuite.TESTMODE_STATUS_NAMES.values()}), 50)

    def hw49_runner(self, tmp, skip=()):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        runner = hwsuite.Runner(json.loads(profile_path.read_text()), profile_path, tmp, None, tmp, tmp, "extra",
                                ["HW-49"], testmode_build=tmp, testmode_skip=skip)
        runner.session = FakeSession()
        return runner

    def run_hw49(self, misnaming=None, skip=()):
        """hw49 against a page that counts every press, and names the control in `misnaming` (default: the one pressed)
        as another control, which is what a swapped mapping looks like."""
        misnaming = misnaming or {}
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.hw49_runner(tmp, skip)

            def pressed():
                names = [call[1] for call in runner.session.calls if call[0] == "press" and call[1] != "selector"]
                return names[-1] if names else None

            def counted(image, row, total):
                presses = [call for call in runner.session.calls if call[0] == "press" and call[1] != "selector"]
                return min(len(presses), 48) if row == hwsuite.TESTMODE_LED_ROW else len(presses)

            def named(image):
                name = pressed()
                return None if name is None else misnaming.get(name, name)
            with mock.patch.object(hwsuite, "wait_settled"), mock.patch.object(hwsuite, "ocr", return_value="OK"), \
                    mock.patch.object(hwsuite, "testmode_counter", side_effect=counted), \
                    mock.patch.object(hwsuite, "testmode_status_name", side_effect=named), \
                    mock.patch.object(hwsuite.Runner, "testmode_page_is",
                                      return_value=hwsuite.sub("stepped to page 4", True)):
                return runner.hw49()

    def test_hw49_passes_when_every_press_is_counted_and_named(self):
        result = self.run_hw49()
        self.assertEqual(result["status"], "PASS")
        self.assertIn("the status line named the control that was pressed", [s["name"] for s in result["subresults"]])

    def test_hw49_fails_when_the_page_names_another_control_for_a_press(self):
        """Two controls with swapped mappings still move RESULT, so the totals pass; the page's own status line names
        the other control and that must fail the check."""
        result = self.run_hw49({"bar4": "bar3"})
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("bar4 -> bar3", result["reason"])
        self.assertEqual([s["status"] for s in result["subresults"] if s["name"].startswith("every control moved")],
                         ["PASS"])

    def test_hw49_reports_an_unreadable_status_line_as_not_applicable(self):
        """A cell the OCR cannot read is not proof of a wrong mapping: the totals still decide, and the gap is named."""
        result = self.run_hw49({"pad7": None})
        self.assertEqual(result["status"], "PASS")
        unread = [s for s in result["subresults"] if s["name"] == "the status line was readable for every press"]
        self.assertEqual([s["status"] for s in unread], ["N/A"])
        self.assertIn("pad7", unread[0]["observed"])

    def test_hw49_does_not_judge_a_control_it_was_told_to_skip(self):
        """--testmode-skip leaves a control unpressed (the totals then fall short by design); its name is never judged."""
        result = self.run_hw49({"bar4": "bar3"}, skip=("bar4",))
        status = [s["status"] for s in result["subresults"]
                  if s["name"] == "the status line named the control that was pressed"]
        self.assertEqual(status, ["PASS"])
        self.assertEqual(result["metrics"]["skipped"], ["bar4"])
        self.assertNotIn("bar4 -> bar3", result["reason"])

    def test_testmode_skip_names_must_be_controls_hw49_presses(self):
        self.assertEqual(hwsuite.check_testmode_skip([]), ())
        self.assertEqual(hwsuite.check_testmode_skip(["bar4", "pad16", "shift"]), ("bar4", "pad16", "shift"))
        for name in hwsuite.TESTMODE_CONTROLS:
            self.assertEqual(hwsuite.check_testmode_skip([name]), (name,))
        with self.assertRaises(ValueError) as caught:
            hwsuite.check_testmode_skip(["bar4", "bar44", "PLAY", "nothing-like-a-control"])
        message = str(caught.exception)
        self.assertIn("'bar44' (did you mean 'bar4'?)", message)
        self.assertIn("'PLAY' (did you mean 'play'?)", message)          # a capital letter is a typo too
        self.assertIn("'nothing-like-a-control'", message)
        self.assertEqual(message.count("did you mean"), 2)               # 'bar4' is a control, so it is not named
        self.assertIn("pad1..pad16, step1..step16, track", message)

    def test_the_runner_refuses_an_unknown_control_to_skip(self):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        profile = json.loads(profile_path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                hwsuite.Runner(profile, profile_path, tmp, None, tmp, tmp, "extra", ["HW-49"], testmode_skip=["bar44"])
            runner = hwsuite.Runner(profile, profile_path, tmp, None, tmp, tmp, "extra", ["HW-49"], testmode_skip=["bar4"])
        self.assertEqual(runner.testmode_skip, ("bar4",))

    def test_extras_without_a_testmode_build_are_n_a_with_a_precondition(self):
        profile = json.loads((TOOLS / "profiles" / "sp16-1.60.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            runner = hwsuite.Runner(profile, TOOLS / "profiles" / "sp16-1.60.json", tmp, None, tmp, tmp,
                                    "extra", list(hwsuite.TESTMODE_IDS))
            runner.testmode_checks()
        self.assertEqual({hw: v["status"] for hw, v in runner.verdicts.items()}, {hw: "N/A" for hw in hwsuite.TESTMODE_IDS})
        self.assertTrue(all(v["reason"].startswith("precondition:") for v in runner.verdicts.values()))

    def runner_with_testmode_build(self, tmp, app_sha=None, only=None):
        """A runner whose testmode build is a one-firmware library (`fw-1`) with `app_sha` in its manifest: the
        profile's by default. Returns (runner, testmode build folder, profile)."""
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        profile = json.loads(profile_path.read_text())
        build = self.library(Path(tmp) / "testmode", app_sha or profile["firmware"]["app_sha256"])
        runner = hwsuite.Runner(profile, profile_path, tmp, "fw-1", tmp, tmp, "extra", only or list(hwsuite.TESTMODE_IDS),
                                testmode_build=build)
        return runner, build, profile

    def test_a_testmode_session_that_cannot_start_blocks_the_checks_and_stops_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _ = self.runner_with_testmode_build(tmp)
            with mock.patch.object(hwsuite, "launch", side_effect=hwsuite.EnvError("a session is already running")), \
                    mock.patch.object(hwsuite, "stop") as stopped:
                runner.testmode_checks()
            stopped.assert_not_called()
        self.assertEqual({v["status"] for v in runner.verdicts.values()}, {"BLOCKED"})
        self.assertIn("already running", runner.verdicts["HW-49"]["reason"])

    def test_a_testmode_session_that_started_is_always_stopped_by_its_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, build, _ = self.runner_with_testmode_build(tmp)
            with mock.patch.object(hwsuite, "launch", return_value=(4242, Path(tmp) / "runtime.json", 1.0)), \
                    mock.patch.object(hwsuite, "Session", side_effect=hwsuite.SessionLost("control socket unavailable")), \
                    mock.patch.object(hwsuite, "stop") as stopped:
                runner.testmode_checks()
            stopped.assert_called_once_with(build, 4242, timeout=90)
        self.assertEqual({v["status"] for v in runner.verdicts.values()}, {"BLOCKED"})

    def test_a_testmode_build_with_the_profiles_firmware_is_launched_with_the_checked_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, build, profile = self.runner_with_testmode_build(tmp)
            runner.firmware_id = None                 # the build's own selected firmware is the one that is checked
            with mock.patch.object(hwsuite, "launch", return_value=(4242, Path(tmp) / "runtime.json", 1.0)) as launched, \
                    mock.patch.object(hwsuite, "Session", side_effect=hwsuite.SessionLost("no socket")), \
                    mock.patch.object(hwsuite, "stop"):
                runner.testmode_checks()
            launched.assert_called_once_with(build, "fw-1", None, ephemeral=True)
        self.assertEqual(runner.notes["testmode_manifest"]["app_sha256"], profile["firmware"]["app_sha256"])

    def test_a_testmode_build_holding_another_firmware_under_the_same_id_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _ = self.runner_with_testmode_build(tmp, app_sha="0" * 64)
            with mock.patch.object(hwsuite, "launch") as launched, mock.patch.object(hwsuite, "stop") as stopped:
                runner.testmode_checks()
            launched.assert_not_called()
            stopped.assert_not_called()
        self.assertEqual({hw: v["status"] for hw, v in runner.verdicts.items()}, {hw: "BLOCKED" for hw in hwsuite.TESTMODE_IDS})
        self.assertTrue(all("does not match the profile" in v["reason"] for v in runner.verdicts.values()))
        self.assertNotIn("testmode_manifest", runner.notes)

    def test_a_run_is_refused_before_any_session_starts_when_the_testmode_firmware_is_not_the_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, profile = self.runner_with_testmode_build(tmp, app_sha="0" * 64, only=["HW-48"])
            runner.build = self.library(Path(tmp) / "main", profile["firmware"]["app_sha256"])
            with mock.patch.object(hwsuite, "launch") as launched, mock.patch.object(hwsuite.Runner, "main_session") as main:
                with self.assertRaises(hwsuite.EnvError) as caught:
                    runner.run()
            launched.assert_not_called()
            main.assert_not_called()
        self.assertIn("testmode build's firmware does not match the profile", str(caught.exception))
        self.assertEqual(runner.verdicts, {})

    def test_a_testmode_session_running_another_firmware_than_the_checked_one_is_blocked_and_stopped(self):
        session = mock.Mock(runtime={"audio": "file", "firmware": "fw-other"})
        with tempfile.TemporaryDirectory() as tmp:
            runner, build, _ = self.runner_with_testmode_build(tmp)
            with mock.patch.object(hwsuite, "launch", return_value=(4242, Path(tmp) / "runtime.json", 1.0)), \
                    mock.patch.object(hwsuite, "Session", return_value=session), mock.patch.object(hwsuite, "stop") as stopped:
                runner.testmode_checks()
            stopped.assert_called_once_with(build, 4242, timeout=90)
        self.assertEqual({v["status"] for v in runner.verdicts.values()}, {"BLOCKED"})
        self.assertIn("runs firmware fw-other, not the checked fw-1", runner.verdicts["HW-48"]["reason"])

    def test_an_unreadable_library_is_an_environment_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, build, _ = self.runner_with_testmode_build(tmp)
            (build.parent / "lib" / "fw-1" / "manifest.json").write_text("not json")
            with self.assertRaises(hwsuite.EnvError) as caught:
                runner.check_firmware("the testmode build's", build, "testmode_manifest")
            self.assertIn("cannot be read from its library", str(caught.exception))
            (build.parent / "lib" / "fw-1" / "manifest.json").unlink()
            with self.assertRaises(hwsuite.EnvError):
                runner.check_firmware("the testmode build's", build, "testmode_manifest")

    def test_the_result_names_the_testmode_firmware_that_was_checked(self):
        import types
        with tempfile.TemporaryDirectory() as tmp:
            runner, build, profile = self.runner_with_testmode_build(tmp)
            runner.check_firmware("the testmode build's", build, "testmode_manifest")
            runner.run_id, runner.fixture_hashes = "r", {}
            args = types.SimpleNamespace(tier="extra", json_only=True)
            result, _ = hwsuite.write_result(Path(tmp) / "result", runner, [], "start", args, b"{}")
        run = result["run"]
        self.assertEqual(run["testmode_build"], str(build))
        self.assertEqual((run["testmode_firmware"]["id"], run["testmode_firmware"]["app_sha256"]),
                         ("fw-1", profile["firmware"]["app_sha256"]))
        self.assertEqual(len(run["testmode_firmware"]["manifest_sha256"]), 64)

    def test_only_extras_selected_means_no_main_session(self):
        self.assertTrue(all(hw in hwsuite.EXTRA_IDS for hw in hwsuite.select_checks("extra")))

    def library(self, root, app_sha):
        """A build with a one-firmware library whose manifest carries `app_sha`; returns the build folder."""
        (root / "lib" / "fw-1").mkdir(parents=True)
        (root / "lib" / "selected.json").write_text(json.dumps({"id": "fw-1"}))
        (root / "lib" / "fw-1" / "manifest.json").write_text(json.dumps({"id": "fw-1", "app_sha256": app_sha}))
        (root / "build").mkdir()
        (root / "build" / "assets.json").write_text(json.dumps({"library": str(root / "lib"), "research": True, "audio": "file"}))
        return root / "build"

    def test_an_extras_only_run_still_checks_the_selected_firmware_against_the_profile(self):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        profile = json.loads(profile_path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            build = self.library(Path(tmp), profile["firmware"]["app_sha256"])
            runner = hwsuite.Runner(profile, profile_path, build, None, tmp, tmp, "extra", ["HW-47"])
            with mock.patch.object(hwsuite.Runner, "hw47", return_value=hwsuite.verdict("HW-47", "PASS")):
                verdicts = runner.run()
            self.assertEqual((runner.firmware_id, verdicts[0]["status"]), ("fw-1", "PASS"))
            self.assertEqual(runner.notes["manifest"]["app_sha256"], profile["firmware"]["app_sha256"])
        with tempfile.TemporaryDirectory() as tmp:
            build = self.library(Path(tmp), "0" * 64)
            runner = hwsuite.Runner(profile, profile_path, build, None, tmp, tmp, "extra", ["HW-47"])
            with self.assertRaises(hwsuite.EnvError):
                runner.run()


class ShotRegressionTests(unittest.TestCase):
    """Runner.shot: the suite's own references, masks given as fractions, a heat map for a change."""

    def setUp(self):
        from PIL import Image
        self.Image = Image
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.profile = json.loads((TOOLS / "profiles" / "sp16-1.60.json").read_text())
        import shotdiff
        self.shotdiff = shotdiff
        self.make = lambda mode: hwsuite.Runner(
            self.profile, TOOLS / "profiles" / "sp16-1.60.json", root, None, root, root, "extra", ["HW-52"],
            shots=shotdiff.ShotStore(root / "refs", root / "out", mode))
        self.root = root

    def shot(self, runner, name, image, masks=(), check="HW-52"):
        runner.current_check = check
        return runner.shot(name, image, masks)

    def record(self, name, image, masks=(), status="PASS"):
        """A recording run whose check ended `status`: the reference exists afterwards only if it passed."""
        runner = self.make("record")
        self.shot(runner, name, image, masks)
        runner.finish_shots([hwsuite.verdict("HW-52", status, "x" if status != "PASS" else "")])
        return runner

    def test_off_means_no_result(self):
        runner = hwsuite.Runner(self.profile, TOOLS / "profiles" / "sp16-1.60.json", self.root, None, self.root, self.root)
        self.assertIsNone(runner.shot("home-lcd", self.Image.new("RGB", (10, 10))))

    def test_a_reference_comes_only_from_a_check_that_passed(self):
        image = self.Image.new("RGB", (200, 100), (30, 30, 30))
        self.record("home-lcd", image, status="FAIL")
        self.assertFalse((self.root / "refs" / "home-lcd.png").exists())
        runner = self.record("home-lcd", image)
        self.assertEqual(runner.notes["shots_promoted"], ["home-lcd"])
        self.assertTrue((self.root / "refs" / "home-lcd.png").exists())

    def test_comparing_without_a_reference_is_blocked_not_recorded(self):
        result = self.shot(self.make("compare"), "home-lcd", self.Image.new("RGB", (200, 100)))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("--shots record", result["observed"])
        self.assertFalse((self.root / "refs").exists())

    def test_a_change_fails_with_a_heat_map(self):
        image = self.Image.new("RGB", (200, 100), (30, 30, 30))
        self.record("home-lcd", image)
        runner = self.make("compare")
        self.assertEqual(self.shot(runner, "home-lcd", image)["status"], "PASS")
        changed = image.copy()
        changed.paste((255, 0, 0), (10, 10, 50, 30))
        result = self.shot(runner, "home-lcd", changed)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("800 of 20000 pixels differ", result["observed"])
        self.assertIn("heat map home-lcd-diff.png", result["observed"])
        self.assertTrue((self.root / "out" / "home-lcd-diff.png").exists())

    def test_a_mask_in_fractions_keeps_a_moving_region_out(self):
        image = self.Image.new("RGB", (200, 100), (30, 30, 30))
        masks = [hwsuite.PANEL_STATUS_MASK]
        self.record("home-panel", image, masks)
        changed = image.copy()
        changed.paste((255, 255, 255), (0, 97, 200, 100))              # the status line counts frames
        self.assertEqual(self.shot(self.make("compare"), "home-panel", changed, masks)["status"], "PASS")
        changed.paste((255, 0, 0), (0, 0, 20, 20))
        self.assertEqual(self.shot(self.make("compare"), "home-panel", changed, masks)["status"], "FAIL")

    def test_a_single_changed_pixel_fails_at_the_default_share(self):
        image = self.Image.new("RGB", (2560, 1600), (30, 30, 30))
        self.record("home-panel", image)
        changed = image.copy()
        changed.putpixel((10, 10), (255, 255, 255))
        result = self.shot(self.make("compare"), "home-panel", changed)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("1 of 4096000 pixels differ", result["observed"])

    def test_masks_that_leave_nothing_to_compare_fail_the_capture(self):
        image = self.Image.new("RGB", (200, 100), (30, 30, 30))
        self.record("home-lcd", image)
        result = self.shot(self.make("compare"), "home-lcd", image, masks=[(0.0, 0.0, 1.0, 1.0)])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("mask leaves nothing to compare", result["observed"])

    def test_the_result_file_and_a_report_carry_the_shots(self):
        import types
        image = self.Image.new("RGB", (200, 100), (30, 30, 30))
        self.record("home-lcd", image)
        runner = self.make("compare")
        changed = image.copy()
        changed.paste((255, 0, 0), (10, 10, 50, 30))
        verdicts = [hwsuite.from_subresults("HW-52", [self.shot(runner, "home-lcd", changed)])]
        args = types.SimpleNamespace(tier="extra", json_only=False)
        runner.run_id, runner.fixture_hashes = "r", {}
        result, _ = hwsuite.write_result(self.root / "result", runner, verdicts, "start", args, b"{}")
        self.assertEqual(result["shots"]["entries"][0]["status"], "diff")
        self.assertEqual(result["summary"]["counts"]["FAIL"], 1)
        report = (self.root / "result" / "shots-report.md").read_text()
        self.assertIn("| home-lcd | diff | 800 |", report)
        self.assertTrue((self.root / "out" / "home-lcd-diff.png").exists())

    def test_hw52_without_shots_is_n_a_with_a_precondition(self):
        runner = hwsuite.Runner(self.profile, TOOLS / "profiles" / "sp16-1.60.json", self.root, None, self.root, self.root,
                                "extra", ["HW-52"])
        result = runner.hw52()
        self.assertEqual(result["status"], "N/A")
        self.assertTrue(result["reason"].startswith("precondition:"))


class MarkVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        source = TOOLS / "profiles" / "sp16-1.50.json"
        self.path = Path(self.tmp.name) / "sp16-1.50.json"
        self.path.write_bytes(source.read_bytes())
        self.profile = json.loads(source.read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, verdicts=None, tier="full"):
        verdicts = verdicts or all_pass()
        return {"suite": {"version": hwsuite.SUITE_VERSION, "checks_hash": hwsuite.checks_hash(),
                          "hardware_fingerprint": hwsuite.hardware_fingerprint()},
                "run": {"id": "run1", "end": "2026-10-13T01:00:00", "firmware_version": "1.50",
                        "profile_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                        "app_sha256": self.profile["firmware"]["app_sha256"], "manifest_sha256": "a" * 64,
                        "firmware_image_sha256": "b" * 64},
                "verdicts": verdicts, "summary": hwsuite.summarize(verdicts, tier)}

    def test_marks_an_eligible_run(self):
        block = hwsuite.mark_verified(self.path, self.result(), "/x/hwsuite-result.json", "c" * 64)
        written = json.loads(self.path.read_text())
        self.assertEqual(written["status"], "verified")
        self.assertEqual(written["verification"], block)
        self.assertEqual(block["result_sha256"], "c" * 64)
        self.assertEqual(block["hardware_epoch"], hwsuite.HARDWARE_EPOCH)
        profiles.validate(written)

    def test_refuses_ineligible_or_stale_runs(self):
        blocked = all_pass()
        blocked[5] = hwsuite.verdict("HW-06", "BLOCKED", "x")
        with self.assertRaises(hwsuite.EnvError):
            hwsuite.mark_verified(self.path, self.result(blocked), "r", "c")
        stale = self.result()
        stale["run"]["profile_sha256"] = "0" * 64
        with self.assertRaises(hwsuite.EnvError):
            hwsuite.mark_verified(self.path, stale, "r", "c")
        other = self.result()
        other["run"]["app_sha256"] = "1" * 64
        with self.assertRaises(hwsuite.EnvError):
            hwsuite.mark_verified(self.path, other, "r", "c")
        na = all_pass()
        na[29] = hwsuite.verdict("HW-30", "N/A", "not declared anywhere")
        with self.assertRaises(hwsuite.EnvError):
            hwsuite.mark_verified(self.path, self.result(na), "r", "c")
        self.assertEqual(json.loads(self.path.read_text())["status"], self.profile["status"])

    def declare_known_failing(self):
        self.profile.setdefault("hardware", {})["known_failing"] = [
            {"check": "HW-35", "reason": "r", "owner": "owner", "date": "2026-10-28"}]
        self.path.write_text(json.dumps(self.profile, indent=2) + "\n")

    def test_marks_a_declared_known_fail(self):
        self.declare_known_failing()
        verdicts = all_pass()
        verdicts[34] = hwsuite.verdict("HW-35", "KNOWN-FAIL", "known-failing (owner, 2026-10-28): r")
        block = hwsuite.mark_verified(self.path, self.result(verdicts), "/x/hwsuite-result.json", "c" * 64)
        self.assertEqual(block["counts"]["KNOWN-FAIL"], 1)
        self.assertEqual(json.loads(self.path.read_text())["status"], "verified")

    def test_refuses_an_undeclared_known_fail(self):
        verdicts = all_pass()
        verdicts[34] = hwsuite.verdict("HW-35", "KNOWN-FAIL", "x")
        with self.assertRaises(hwsuite.EnvError):
            hwsuite.mark_verified(self.path, self.result(verdicts), "r", "c")
        self.assertEqual(json.loads(self.path.read_text())["status"], self.profile["status"])


class ProcessTests(unittest.TestCase):
    def test_qemu_found_under_the_standardized_tmp_path(self):
        """Foundation starts QEMU as /tmp/... for a build under /private/tmp/...; both spellings belong to the build."""
        build = Path("/private/tmp/x/build/dev")
        exe = "/private/tmp/x/build/dev/App.app/Contents/MacOS/App"
        listing = (f"10 1 {exe} --firmware a\n"
                   "11 10 /tmp/x/build/dev/qemu-system-aarch64 -machine virt\n"
                   "12 10 /private/tmp/x/build/dev/qemu-system-aarch64 -machine virt\n"
                   "13 1 /tmp/x/build/other/qemu-system-aarch64 -machine virt\n")
        with mock.patch.object(hwsuite.subprocess, "run", return_value=mock.Mock(stdout=listing)), \
                mock.patch.object(Path, "resolve", lambda self: self), \
                mock.patch.object(hwsuite, "app_executable", return_value=exe):
            apps, qemus = hwsuite.build_processes(build)
        self.assertEqual(apps, [10])
        self.assertEqual(qemus, [(11, 10), (12, 10)])


class CliTests(unittest.TestCase):
    def rejected(self, *argv):
        """(exit code, stderr) of a command line the parser refuses."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            hwsuite.main(list(argv))
        return caught.exception.code, stderr.getvalue()

    def test_screenshot_thresholds_outside_their_range_are_rejected_by_the_parser(self):
        for option, bad in (("--shots-tolerance", "256"), ("--shots-tolerance", "255"), ("--shots-tolerance", "-1"),
                            ("--shots-tolerance", "8.5"), ("--shots-tolerance", "abc"),
                            ("--shots-share", "1"), ("--shots-share", "1.0"), ("--shots-share", "-0.1"),
                            ("--shots-share", "nan"), ("--shots-share", "many")):
            code, message = self.rejected("--shots", "compare", option, bad)
            self.assertEqual(code, 2, (option, bad))
            self.assertIn(f"argument {option}:", message)

    def test_screenshot_thresholds_inside_their_range_get_past_the_parser(self):
        self.assertEqual((hwsuite.shots_tolerance_arg("0"), hwsuite.shots_tolerance_arg("254")), (0, 254))
        self.assertEqual((hwsuite.shots_share_arg("0"), hwsuite.shots_share_arg("0.999")), (0, 0.999))
        with mock.patch("sys.stderr"):   # accepted, so it goes on to the next usage error: no profile or build
            code = hwsuite.main(["--shots", "compare", "--shots-tolerance", "254", "--shots-share", "0.5"])
        self.assertEqual(code, hwsuite.EXIT_ENV)

    def test_an_unknown_control_to_skip_is_rejected_by_the_parser(self):
        code, message = self.rejected("--testmode-skip", "bar4,bar44")
        self.assertEqual(code, 2)
        self.assertIn("argument --testmode-skip: unknown control to skip: 'bar44' (did you mean 'bar4'?)", message)

    def test_the_skipped_controls_are_split_and_trimmed_by_the_parser(self):
        self.assertEqual(hwsuite.testmode_skip_arg(" bar4 , pad3,,"), ["bar4", "pad3"])
        self.assertEqual(hwsuite.testmode_skip_arg(""), [])
        with mock.patch("sys.stderr"):   # accepted, so it goes on to the next usage error: no profile or build
            self.assertEqual(hwsuite.main(["--testmode-skip", "bar4,pad3"]), hwsuite.EXIT_ENV)

    def test_usage_errors_exit_3(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(hwsuite.main(["--tier", "full"]), hwsuite.EXIT_ENV)
            with tempfile.TemporaryDirectory() as tmp:
                build = Path(tmp)
                (build / "assets.json").write_text(json.dumps({"research": True, "audio": "coreaudio"}))
                code = hwsuite.main(["--profile", str(TOOLS / "profiles" / "sp16-1.60.json"), "--build-dir", tmp])
                self.assertEqual(code, hwsuite.EXIT_ENV)


if __name__ == "__main__":
    unittest.main()
