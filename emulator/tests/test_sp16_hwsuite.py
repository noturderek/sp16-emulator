"""Hardware conformance suite runner (hwsuite.py): pure logic only, no VM, no firmware, synthetic data."""
import hashlib
import importlib.util
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
        self.assertEqual([c.id for c in hwsuite.CHECKS], [f"HW-{n:02d}" for n in range(1, 53)])
        self.assertEqual(list(hwsuite.SMOKE_IDS), SPEC_SMOKE)
        self.assertEqual([c.id for c in hwsuite.CHECKS if c.tier == "L"], ["HW-44"])
        self.assertEqual([c.id for c in hwsuite.CHECKS if c.tier == "X"], [f"HW-{n}" for n in range(47, 53)])

    def test_tiers_and_only(self):
        self.assertEqual(hwsuite.select_checks("smoke"), SPEC_SMOKE)
        self.assertEqual(len(hwsuite.select_checks("core")), 45)
        self.assertEqual(len(hwsuite.select_checks("full")), 46)
        self.assertEqual(hwsuite.select_checks("full", ["HW-18", "HW-05"]), ["HW-05", "HW-18"])
        self.assertEqual(hwsuite.select_checks("smoke", ["HW-02", "HW-05"]), ["HW-05"])
        # Extras stay out of full and core, and run by tier or by name.
        self.assertNotIn("HW-47", hwsuite.select_checks("full"))
        self.assertEqual(hwsuite.select_checks("extra"), [f"HW-{n}" for n in range(47, 53)])
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


class VerdictTests(unittest.TestCase):
    def test_na_needs_a_reason(self):
        with self.assertRaises(ValueError):
            hwsuite.verdict("HW-30", "N/A")
        with self.assertRaises(ValueError):
            hwsuite.verdict("HW-30", "MAYBE")

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

    def test_extras_without_a_testmode_build_are_n_a_with_a_precondition(self):
        profile = json.loads((TOOLS / "profiles" / "sp16-1.60.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            runner = hwsuite.Runner(profile, TOOLS / "profiles" / "sp16-1.60.json", tmp, None, tmp, tmp,
                                    "extra", list(hwsuite.TESTMODE_IDS))
            runner.testmode_checks()
        self.assertEqual({hw: v["status"] for hw, v in runner.verdicts.items()}, {hw: "N/A" for hw in hwsuite.TESTMODE_IDS})
        self.assertTrue(all(v["reason"].startswith("precondition:") for v in runner.verdicts.values()))

    def test_a_testmode_session_that_cannot_start_blocks_the_checks_and_stops_nothing_else(self):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        profile = json.loads(profile_path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            runner = hwsuite.Runner(profile, profile_path, tmp, None, tmp, tmp, "extra", list(hwsuite.TESTMODE_IDS),
                                    testmode_build=tmp)
            with mock.patch.object(hwsuite, "launch", side_effect=hwsuite.EnvError("a session is already running")), \
                    mock.patch.object(hwsuite, "stop") as stopped:
                runner.testmode_checks()
            stopped.assert_not_called()
        self.assertEqual({v["status"] for v in runner.verdicts.values()}, {"BLOCKED"})
        self.assertIn("already running", runner.verdicts["HW-49"]["reason"])

    def test_a_testmode_session_that_started_is_always_stopped_by_its_pid(self):
        profile_path = TOOLS / "profiles" / "sp16-1.60.json"
        profile = json.loads(profile_path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            runner = hwsuite.Runner(profile, profile_path, tmp, None, tmp, tmp, "extra", list(hwsuite.TESTMODE_IDS),
                                    testmode_build=tmp)
            with mock.patch.object(hwsuite, "launch", return_value=(4242, Path(tmp) / "runtime.json", 1.0)), \
                    mock.patch.object(hwsuite, "Session", side_effect=hwsuite.SessionLost("control socket unavailable")), \
                    mock.patch.object(hwsuite, "stop") as stopped:
                runner.testmode_checks()
            stopped.assert_called_once_with(Path(tmp), 4242, timeout=90)
        self.assertEqual({v["status"] for v in runner.verdicts.values()}, {"BLOCKED"})

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
