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
        self.assertEqual([c.id for c in hwsuite.CHECKS], [f"HW-{n:02d}" for n in range(1, 48)])
        self.assertEqual(list(hwsuite.SMOKE_IDS), SPEC_SMOKE)
        self.assertEqual([c.id for c in hwsuite.CHECKS if c.tier == "L"], ["HW-44"])
        self.assertEqual([c.id for c in hwsuite.CHECKS if c.tier == "X"], ["HW-47"])

    def test_tiers_and_only(self):
        self.assertEqual(hwsuite.select_checks("smoke"), SPEC_SMOKE)
        self.assertEqual(len(hwsuite.select_checks("core")), 45)
        self.assertEqual(len(hwsuite.select_checks("full")), 46)
        self.assertEqual(hwsuite.select_checks("full", ["HW-18", "HW-05"]), ["HW-05", "HW-18"])
        self.assertEqual(hwsuite.select_checks("smoke", ["HW-02", "HW-05"]), ["HW-05"])
        # Extras stay out of full and core, and run by tier or by name.
        self.assertNotIn("HW-47", hwsuite.select_checks("full"))
        self.assertEqual(hwsuite.select_checks("extra"), ["HW-47"])
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
