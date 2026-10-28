"""Firmware profiles and patch recipes (docs/architecture.md M1/M6), with synthetic bytes only."""
import copy
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import os
import re
import shutil

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import firmware
import native
import panel
import panel_map
import profiles
import recipes

# The literal values the code carried before M1 moved them into profiles/sp16-1.60.json.
PRE_M1_LAMP_ROLES = {
    "track": ("mono", 0), "mute": ("mono", 1), "slice": ("mono", 2), "scale": ("mono", 3),
    "shift": ("mono", 4), "rec": ("mono", 5), "stop": ("mono", 6), "play": ("mono", 7),
    "scene": ("mono", 8), "pattern": ("mono", 9), "hold": ("mono", 10),
    "active": ("mono", 23), "usb_stop": ("mono", 24), "usb_led": ("mono", 25), "over_led": ("mono", 34),
    "bar1": ("mono", 26), "bar2": ("mono", 27), "bar3": ("mono", 28), "bar4": ("mono", 29),
    "lamp_pitch": ("mono", 12), "lamp_repeat": ("mono", 11), "lamp_user1": ("mono", 13), "lamp_user2": ("mono", 14),
    "bar_led1": ("mono", 30), "bar_led2": ("mono", 31), "bar_led3": ("mono", 32), "bar_led4": ("mono", 33),
    **{f"strip_led{i}": ("mono", 14 + i) for i in range(1, 9)},
    **{f"step{n}": ("rgb", n - 1) for n in range(1, 17)},
    **{f"pad{n}": ("rgb", 0x10 + n - 1) for n in range(1, 17)},
}


def write_profile(folder, profile):
    (Path(folder) / f"{profile['id']}.json").write_text(json.dumps(profile))


class LegacyProfileTests(unittest.TestCase):
    def test_1_60_profile_reproduces_the_pre_m1_constants(self):
        profile = profiles.legacy()
        self.assertEqual(profile["status"], "verified")
        # M1 moved these literal values into profiles/sp16-1.60.json; the lamp table then dropped the SHIFT role, because
        # the service manual's LED matrix shows mono 4 driving no LED (E15). Everything else is unchanged.
        self.assertNotIn("shift", panel_map.LAMP_ROLES)
        self.assertEqual(panel_map.LAMP_ROLES,
                         {role: spec for role, spec in PRE_M1_LAMP_ROLES.items() if role != "shift"})
        self.assertEqual((panel_map.PAD_A_MIN, panel_map.PAD_A_MAX, panel_map.PAD_B_CONTACT), (172, 874, 512))
        self.assertEqual(panel_map.PAD_CURVE(1), (172, 512))
        self.assertEqual(panel_map.PAD_CURVE(127), (874, 512))
        self.assertEqual(panel.PanelModel.SELECTOR_WINDOW_FRAMES, 8)
        self.assertEqual(profile["firmware"]["app_sha256"],
                         "3d1392a61323e73ea4f2fddef8fe9e0142add7a6f6fab79ab0eb0d5396555464")
        self.assertEqual(profile["firmware"]["version"], "1.60")
        self.assertEqual(profile["firmware"]["upd_sha256"],
                         "ff1cc60c4b6e2a3793196486826e78eb223e2f86cd95b5c968f6fca3ade537a4")

    def test_no_literal_1_60_hash_pins_remain_in_code(self):
        pin = "3d1392a61323e73ea4f2fddef8fe9e0142add7a6f6fab79ab0eb0d5396555464"
        upd = "ff1cc60c4b6e2a3793196486826e78eb223e2f86cd95b5c968f6fca3ade537a4"
        for path in [*TOOLS.glob("*.py"), *TOOLS.glob("*.sh")]:
            text = path.read_text()
            self.assertNotIn(pin, text, path.name)
            self.assertNotIn(upd, text, path.name)
        self.assertNotIn("firmware=1.60", (TOOLS / "guest-init.sh").read_text())

    def test_base_image_carries_the_profile_and_recipe(self):
        ignore = (TOOLS / ".dockerignore").read_text()
        for name in ("profiles.py", "recipes.py", "profiles/sp16-1.60.json"):
            self.assertIn(name, native.BASE_GUEST_FILES)
            self.assertTrue((TOOLS / name).is_file())
        for allowed in ("!profiles.py", "!recipes.py", "!profiles/*.json"):
            self.assertIn(allowed, ignore)
        base = (TOOLS / "Dockerfile.native-base").read_text()
        for line in ("COPY profiles.py", "COPY recipes.py", "COPY profiles /opt/sp16/profiles"):
            self.assertIn(line, base)
        # The decoupled base ships no firmware identity module and no mods, and strips the update hash (M7).
        self.assertNotIn("COPY mods", base)
        self.assertNotIn("app_identity", base)
        self.assertIn('fk != "upd_sha256"', base)


PRE_SCOPE_BUTTON_BITS = {
    "selector": (1, 1), "track": (1, 4), "mute": (1, 5), "slice": (1, 6), "scale": (1, 7),
    "shift": (2, 0), "rec": (2, 1), "stop": (2, 2), "play": (2, 3),
    "hold": (2, 4), "mode": (2, 5), "scene": (2, 6), "pattern": (2, 7),
    **{f"step{i + 1}": (3 if i < 8 else 4, i % 8) for i in range(16)},
    "bar1": (5, 0), "bar2": (5, 1), "bar3": (5, 2), "bar4": (5, 3), "active": (5, 5), "usb_stop": (5, 6),
    "back": (1, 2), "home": (1, 3), "power": (5, 4)}


class VersionDataTests(unittest.TestCase):
    """Scope addition 1: nothing version-specific stays in emulator code paths; it is profile or product data."""

    def test_panel_maps_come_from_the_profile_unchanged(self):
        self.assertEqual(panel_map.BUTTON_BITS, PRE_SCOPE_BUTTON_BITS)
        self.assertEqual(panel_map.COUNTERS, {"selector": (0x3c, 1), **{f"knob{i}": (0x30 + 2 * (i - 1), 1)
                                                                         for i in range(1, 7)}})
        self.assertEqual(panel_map.ANALOG_WORDS, {"main_out": (0x28, False), "drive": (0x26, True),
                                                  "hp_cutoff": (0x2a, True)})

    def test_runtime_layout_and_calibration_are_profile_data(self):
        self.assertEqual(profiles.runtime_fields(profiles.legacy()),
                         ["sp16-1.60", "/root/pdj/EP085_Main_R", "/root/pdj", "cortex-a9", "/lib:/usr/lib",
                          "/root/pdj/project,/root/pdj/browse,/root/pdj/scenes,/root/pdj/packages", "/root/data",
                          "/mnt/emmc", "/media/usb1/sda1", "testmode"])
        expected = ('<?xml version="1.0" encoding="UTF-8"?>\n\n<PadCalibrationData '
                    + " ".join(f'CALIB_VALUE_PAD{n}="242"' for n in range(1, 17)) + '/>\n')
        self.assertEqual(native.pad_calibration_xml(242), expected)

    def test_guest_init_has_no_version_literals_outside_comments(self):
        code = "\n".join(line for line in (TOOLS / "guest-init.sh").read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        for literal in ("EP085", "/root/pdj", "/root/data", "/mnt/emmc", "/media/usb1", "cortex-a9", "/lib:/usr/lib",
                        "testmode", "1.60"):
            self.assertFalse(literal in code, literal)
        for name in ("guest-firmware.py", "firmware.py", "fwpack.py", "fwdev.py", "enter.py"):
            text = (TOOLS / name).read_text()
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            self.assertNotIn("EP085_Main_R\"", code, name)
            self.assertNotIn("LIVESAMPLER(", code, name)

    def test_product_descriptor_and_profile_choice(self):
        product = profiles.product()
        self.assertEqual(product["reference_profile"], "sp16-1.60")
        self.assertEqual(product["update"]["stock_marker"], "LIVESAMPLER")
        legacy_app = profiles.legacy()["firmware"]["app_sha256"]
        self.assertEqual(profiles.active(legacy_app)[0], "verified")
        # Owner decision of 2026-10-13: an unknown application gets the reference profile, never a refusal; the old
        # allow_unverified argument is accepted and changes nothing.
        how, profile = profiles.active("e" * 64)
        self.assertEqual((how, profile["id"]), ("reference", "sp16-1.60"))
        self.assertEqual(profiles.active("e" * 64, allow_unverified=False), (how, profile))
        with tempfile.TemporaryDirectory() as folder:
            shutil.copytree(TOOLS / "profiles/products", Path(folder) / "products")
            candidate = copy.deepcopy(profiles.legacy())
            candidate.update(id="sp16-1.50", status="candidate")
            candidate["firmware"].update(version="1.50", app_sha256="e" * 64)
            write_profile(folder, candidate)
            write_profile(folder, profiles.legacy())
            how, chosen = profiles.active("e" * 64, directory=folder)
            self.assertEqual((how, chosen["id"]), ("candidate", "sp16-1.50"))
            self.assertEqual(profiles.active(legacy_app, directory=folder)[0], "verified")

    def test_candidate_profile_scaffold(self):
        manifest = {"update": {"trailer_version": "1.50", "sha256": "1" * 64,
                               "revisions": {"app.rev": "Rev.6000", "subucom1.rev": "Rev.5000"}},
                    "app_path": "/root/pdj/EP085_Main_R", "app_sha256": "2" * 64, "loader_path": "/lib/ld-linux.so.3"}
        profile = firmware.candidate_profile(manifest)
        self.assertEqual((profile["id"], profile["status"], profile["derived_from"]),
                         ("sp16-1.50", "candidate", "sp16-1.60"))
        self.assertEqual(profile["firmware"]["app_sha256"], "2" * 64)
        self.assertEqual(profile["firmware"]["panel_revisions"]["subucom1"], "Rev.5000")
        self.assertEqual(profile["panel"], profiles.legacy()["panel"])

    def test_shipped_profiles_keep_runtime_data_only(self):
        shipped = native.shipped_profile(profiles.legacy())
        self.assertEqual(set(shipped), set(profiles.RUNTIME_FIELDS))
        self.assertNotIn("upd_sha256", shipped["firmware"])
        for key in profiles.SUITE_FIELDS:
            self.assertNotIn(key, shipped)

    def test_hardware_suite_fields_present_for_1_60(self):
        profile = profiles.legacy()
        for key in profiles.SUITE_FIELDS:
            self.assertIn(key, profile)
        self.assertEqual(profile["device_nodes"], sorted(profile["device_nodes"], key=profile["device_nodes"].index))
        self.assertIn("0x40107000", profile["ioctl_set"])
        for key in ("selector_window_frames", "pad_curve", "button_roles", "lamp_roles", "analog_words"):
            self.assertIn(key, profile["panel"])
        self.assertIn("usb_led", profile["panel"]["lamp_roles"])
        self.assertIn("over_led", profile["panel"]["lamp_roles"])
        self.assertLessEqual({"sample_load", "velocity_enable", "gate_mode", "brightness", "save_project",
                              "persist_probe", "set_bpm"}, set(profile["recipes"]))

    def test_unknown_device_opens_are_logged_once_and_counted(self):
        stub = (TOOLS / "hardware_stub.c").read_text()
        self.assertIn('"[sp16-ui-stub] unknown device open path=%s\\n"', stub)
        bridge = (TOOLS / "guest-bridge.py").read_text()
        pattern = re.compile(re.search(r'UNKNOWN_DEVICE = re\.compile\(r"(.+)"\)', bridge).group(1))
        self.assertEqual(pattern.match("[sp16-ui-stub] unknown device open path=/dev/spi_new9").group(1),
                         "/dev/spi_new9")
        self.assertIn("counters.unknown_device(device.group(1))", bridge)

    def test_guest_stages_calibration_in_the_profile_format(self):
        spec = importlib.util.spec_from_file_location("guest_firmware_cal", TOOLS / "guest-firmware.py")
        guest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guest)
        with tempfile.TemporaryDirectory() as folder:
            target = guest.stage_calibration("default", profiles.legacy(), folder)
            self.assertEqual(target.read_text(), native.pad_calibration_xml(242))
            self.assertEqual(guest.stage_calibration("300", profiles.legacy(), folder).read_text(),
                             native.pad_calibration_xml(300))
            self.assertIsNone(guest.stage_calibration("off", profiles.legacy(), folder))
            for bad in ("47", "505", "x"):
                with self.assertRaises(ValueError):
                    guest.stage_calibration(bad, profiles.legacy(), folder)

    def test_enter_cwd_comes_from_the_profile(self):
        import enter
        with mock.patch.dict(os.environ, {"SP16_APP_CWD": "/root/pdj"}):
            self.assertEqual(enter.app_cwd(), "/root/pdj")
        for bad in ("relative", "/root/../etc", "/root/a b"):
            with mock.patch.dict(os.environ, {"SP16_APP_CWD": bad}):
                with self.assertRaises(SystemExit):
                    enter.app_cwd()


class ProfileValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = copy.deepcopy(profiles.legacy())

    def check_invalid(self, mutate):
        profile = copy.deepcopy(self.base)
        mutate(profile)
        with self.assertRaises(ValueError):
            profiles.validate(profile)

    def test_rejects_malformed_profiles(self):
        self.check_invalid(lambda p: p.update(status="trusted"))
        self.check_invalid(lambda p: p.update(schema=2))
        self.check_invalid(lambda p: p["firmware"].update(app_path="/root/../etc/passwd"))
        self.check_invalid(lambda p: p["firmware"].update(app_sha256="0" * 63))
        self.check_invalid(lambda p: p["firmware"].update(version="1.6"))
        self.check_invalid(lambda p: p["panel"]["lamp_roles"].update(track=["mono", 35]))
        self.check_invalid(lambda p: p["panel"]["lamp_roles"].update(track=["rgb", 32]))
        self.check_invalid(lambda p: p["panel"]["pad_curve"].update(a_min=900))
        self.check_invalid(lambda p: p["panel"]["button_roles"].update(track=[0, 9]))
        self.check_invalid(lambda p: p["panel"]["counters"].update(knob1=[60, 2]))
        self.check_invalid(lambda p: p["runtime"].update(cwd="/root/../tmp"))
        self.check_invalid(lambda p: p["runtime"].update(tmpfs_dirs=["/root/pdj/a b"]))
        self.check_invalid(lambda p: p["calibration"].update(template="<no attributes/>"))
        self.check_invalid(lambda p: p.update(verification={"result": "fail"}))
        self.check_invalid(lambda p: p.update(ioctl_set=["0x4010700"]))
        self.check_invalid(lambda p: p.update(hardware={"absent": [{"capability": "fx2"}]}))
        self.check_invalid(lambda p: p.update(recipes=["not", "a", "dict"]))
        self.check_invalid(lambda p: p["panel"].update(selector_window_frames=0))
        self.check_invalid(lambda p: p["calibration"].update(default=600))

    def test_ids_are_tokens_and_must_match_the_file(self):
        for bad in ("../sp16-1.60", "sp16/1.60", "", None, "UPPER"):
            with self.assertRaises(ValueError):
                profiles.load(bad)
        with tempfile.TemporaryDirectory() as folder:
            renamed = copy.deepcopy(self.base)
            renamed["id"] = "other"
            (Path(folder) / "sp16-9.99.json").write_text(json.dumps(renamed))
            with self.assertRaises(ValueError):
                profiles.load("sp16-9.99", folder)

    def test_only_verified_profiles_gate_an_application(self):
        with tempfile.TemporaryDirectory() as folder:
            candidate = copy.deepcopy(self.base)
            candidate.update(id="sp16-1.50", status="candidate")
            candidate["firmware"].update(version="1.50", app_sha256="a" * 64, upd_sha256=None)
            write_profile(folder, candidate)
            write_profile(folder, self.base)
            self.assertEqual(profiles.verified_for_app(self.base["firmware"]["app_sha256"], folder)["id"], "sp16-1.60")
            self.assertEqual(profiles.for_app("a" * 64, folder)["status"], "candidate")
            self.assertIsNone(profiles.verified_for_app("a" * 64, folder))
            self.assertIsNone(profiles.for_app("b" * 64, folder))


class RecipeTests(unittest.TestCase):
    def spec(self, data, patches, result=None):
        spec = {"base_sha256": hashlib.sha256(data).hexdigest(), "patches": patches}
        if result is not None:
            spec["result_sha256"] = hashlib.sha256(result).hexdigest()
        return spec

    def recipe(self, files):
        return {"schema": 1, "id": "test-mod", "base_profile": "sp16-1.60", "files": files}

    def test_offset_and_pattern_patches(self):
        data = b"head UTILITY\0 mid ABAB tail ABAB"
        expected = b"head MOD LAB\0 mid CDCD tail CDCD"
        spec = self.spec(data, [
            {"offset": 5, "original_hex": b"UTILITY\0".hex(), "replacement_hex": b"MOD LAB\0".hex(), "expected_count": 1},
            {"pattern_hex": b"ABAB".hex(), "replacement_hex": b"CDCD".hex(), "expected_count": 2}], expected)
        self.assertEqual(recipes.apply_file(data, spec), expected)
        written = {}
        result = recipes.apply(self.recipe({"/root/pdj/app": spec}), lambda p: data, written.__setitem__)
        self.assertEqual(written, {"/root/pdj/app": expected})
        self.assertEqual(result["/root/pdj/app"], hashlib.sha256(expected).hexdigest())

    def test_refusals(self):
        data = b"xxUTILITY\0UTILITY\0"
        offset = {"offset": 2, "original_hex": b"UTILITY\0".hex(), "replacement_hex": b"MOD LAB\0".hex()}
        with self.assertRaises(ValueError):  # the original occurs twice, one expected
            recipes.apply_file(data, self.spec(data, [dict(offset, expected_count=1)]))
        with self.assertRaises(ValueError):  # wrong base file
            recipes.apply_file(data + b"!", self.spec(data, [offset]))
        with self.assertRaises(ValueError):  # bytes at the offset differ
            recipes.apply_file(data, self.spec(data, [dict(offset, offset=0)]))
        with self.assertRaises(ValueError):  # declared result differs
            recipes.apply_file(data, self.spec(data, [offset], b"other"))
        with self.assertRaises(ValueError):  # pattern count differs
            recipes.apply_file(data, self.spec(data, [{"pattern_hex": b"UTILITY\0".hex(),
                                                       "replacement_hex": b"MOD LAB\0".hex(), "expected_count": 1}]))

    def test_a_failing_file_writes_nothing(self):
        good, bad = b"aaaa", b"bbbb"
        recipe = self.recipe({
            "/a": self.spec(good, [{"pattern_hex": b"aa".hex(), "replacement_hex": b"cc".hex(), "expected_count": 2}]),
            "/b": self.spec(bad, [{"pattern_hex": b"zz".hex(), "replacement_hex": b"cc".hex(), "expected_count": 1}])})
        written = {}
        with self.assertRaises(ValueError):
            recipes.apply(recipe, {"/a": good, "/b": bad}.__getitem__, written.__setitem__)
        self.assertEqual(written, {})

    def test_validation(self):
        spec = self.spec(b"ab", [{"pattern_hex": "61", "replacement_hex": "62", "expected_count": 1}])
        recipes.validate(self.recipe({"/root/x": spec}))
        for path in ("relative", "/root/../x", "/root//x", "/"):
            with self.assertRaises(ValueError):
                recipes.validate(self.recipe({path: spec}))
        for patch in ({"pattern_hex": "61", "replacement_hex": "6262", "expected_count": 1},  # length change
                      {"pattern_hex": "61", "replacement_hex": "62"},  # pattern without a count
                      {"offset": -1, "original_hex": "61", "replacement_hex": "62"},
                      {"offset": 0, "original_hex": "61", "replacement_hex": "62", "pattern_hex": "61"}):
            with self.assertRaises(ValueError):
                recipes.validate(self.recipe({"/root/x": dict(spec, patches=[patch])}))
        for bad_id in ("../label", "Label"):
            with self.assertRaises(ValueError):
                recipes.validate(dict(self.recipe({"/root/x": spec}), id=bad_id))


if __name__ == "__main__":
    unittest.main()
