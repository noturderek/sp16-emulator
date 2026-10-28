"""Repacked updates (docs/architecture.md, M6 and D3): layer rewrite, mod trailer, mod verification refusals.
Synthetic archives and mocked VMs only."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zlib

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import firmware
import fwpack
import profiles

spec = importlib.util.spec_from_file_location("guest_firmware_pack", TOOLS / "guest-firmware.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)

APP = "/root/pdj/EP085_Main_R"
LEGACY = profiles.legacy()


def tar_gz(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size, info.mtime, info.mode = len(data), 1234567, 0o755
                archive.addfile(info, io.BytesIO(data))


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.images = self.dir / "images"
        self.images.mkdir()
        tar_gz(self.images / "pdj.tar.gz", [("./", None), ("./pdj/", None), ("./pdj/EP085_Main_R", b"appUTILITY\0"),
                                            ("./pdj/other", b"x")])
        for n in range(1, 5):
            tar_gz(self.images / f"rootfs{n}.tar.gz", [(f"etc/file{n}", b"y" * n)])

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_find_layer(self):
        layer, member = guest.find_layer(self.images, APP)
        self.assertEqual((layer["file"], member), ("pdj.tar.gz", "./pdj/EP085_Main_R"))
        layer, member = guest.find_layer(self.images, "/etc/file3")
        self.assertEqual((layer["file"], member), ("rootfs3.tar.gz", "etc/file3"))
        with self.assertRaisesRegex(ValueError, "squashfs layers"):
            guest.find_layer(self.images, "/usr/lib/libx.so")
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            guest.find_layer(self.images, "/root/pdj/missing")

    def test_rewrite_is_deterministic_and_keeps_other_members(self):
        new = b"appMOD LAB\0"
        a, b = self.dir / "a.tar.gz", self.dir / "b.tar.gz"
        guest.rewrite_tar_gz(self.images / "pdj.tar.gz", a, {APP: new}, ("root",))
        guest.rewrite_tar_gz(self.images / "pdj.tar.gz", b, {APP: new}, ("root",))
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(a.read_bytes()[4:8], b"\0\0\0\0")  # gzip mtime 0
        with tarfile.open(a) as archive:
            names = archive.getnames()
            self.assertEqual(archive.extractfile("./pdj/EP085_Main_R").read(), new)
            self.assertEqual(archive.extractfile("./pdj/other").read(), b"x")
            self.assertEqual(archive.getmember("./pdj/EP085_Main_R").mtime, 1234567)
        self.assertEqual(names, [".", "./pdj", "./pdj/EP085_Main_R", "./pdj/other"])  # tarfile reads "./" as "."
        with self.assertRaisesRegex(ValueError, "missed"):
            guest.rewrite_tar_gz(self.images / "pdj.tar.gz", self.dir / "c.tar.gz", {"/root/pdj/nope": b"z"},
                                 ("root",))


class ModVerificationTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        os.chmod(self.dir, 0o700)
        self.library = firmware.Library(self.dir / "lib", create=True)
        self.base_id = "sp16-1.60-ff1cc60c4b6e"
        entry = self.library.entry_dir(self.base_id)
        entry.mkdir(mode=0o700)
        self.base = {"schema": 1, "product": "sp16", "id": self.base_id, "kind": "stock", "app_path": APP,
                     "app_sha256": LEGACY["firmware"]["app_sha256"], "mod": None,
                     "image": {"file": "firmware.sqfs", "bytes": 4096, "sha256": "b" * 64},
                     "update": {"trailer_version": "1.60", "sha256": LEGACY["firmware"]["upd_sha256"]}, "files": {}}
        (entry / "manifest.json").write_text(json.dumps(self.base))
        # A synthetic recipe: the verifier's rebuild is faked, so the patch itself never runs.
        self.recipe = {"schema": 1, "id": "label-mod", "base_profile": "sp16-1.60",
                       "files": {APP: {"base_sha256": LEGACY["firmware"]["app_sha256"], "result_sha256": "c" * 64,
                                      "patches": [{"offset": 0, "original_hex": "55", "replacement_hex": "4d",
                                                   "expected_count": 1}]}}}
        self.mod = {"schema": 1, "kind": "sp16-mod-update", "id": "label-mod",
                    "base": {"upd_sha256": LEGACY["firmware"]["upd_sha256"], "version": "1.60",
                             "app_sha256": LEGACY["firmware"]["app_sha256"]},
                    "recipes": [self.recipe],
                    "results": {APP: self.recipe["files"][APP]["result_sha256"]}}
        self.guest = {"mod": self.mod, "app_sha256": self.recipe["files"][APP]["result_sha256"]}
        self.meta = {"trailer_version": "1.60", "sha256": "e" * 64}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def verify(self, rebuilt_sha):
        def fake_build(library, boot, base_id, mod_id, recipe_list, key, target, work):
            Path(target).write_bytes(b"rebuilt")
            return {"sha256": rebuilt_sha}
        with mock.patch.object(fwpack, "build_update", fake_build):
            return fwpack.mod_verifier(self.library, {}, b"k" * 32)(self.guest, self.meta, self.dir)

    def test_identical_rebuild_is_accepted_and_inherits_the_base_profile(self):
        result = self.verify("e" * 64)
        self.assertEqual((result["verified"], result["base_id"], result["profile"]), (True, self.base_id, "sp16-1.60"))
        self.assertFalse((self.dir / "rebuild").exists())  # no work directory is left inside the new entry
        self.assertFalse((self.dir / "rebuilt.upd").exists())
        manifest = {**self.base, "id": "sp16-1.60-mod-label-mod-eeeeeeeeeeee", "kind": "mod",
                    "app_sha256": self.guest["app_sha256"], "mod": result}
        firmware.validate_manifest(manifest)
        self.assertEqual(firmware.boot_status(manifest, self.library)["status"], "verified")

    def test_unverifiable_mods_are_accepted_as_unverified_with_a_reason(self):
        mismatch = self.verify("f" * 64)
        self.assertFalse(mismatch["verified"])
        self.assertEqual(mismatch["id"], "label-mod")
        self.assertIn("content differs", mismatch["reason"])
        cases = [
            (lambda: self.mod.update(kind="other"), "sp16-mod.json"),
            (lambda: self.mod["base"].update(upd_sha256="0" * 64), "not in the library"),
            (lambda: self.mod["base"].update(app_sha256="0" * 64), "declared base"),
            (lambda: self.mod.update(recipes=[]), "no recipes"),
            (lambda: self.mod.update(recipes=[{"schema": 99}]), "invalid patch recipe"),
            (lambda: self.guest.update(app_sha256="0" * 64), "differs from its declared recipe result"),
        ]
        for mutate, reason in cases:
            saved_mod, saved_guest = json.loads(json.dumps(self.mod)), dict(self.guest)
            mutate()
            self.guest["mod"] = self.mod
            result = self.verify("e" * 64)
            self.assertFalse(result["verified"], reason)
            self.assertRegex(result["reason"], reason)
            self.mod, self.guest = saved_mod, saved_guest
            self.guest["mod"] = self.mod
        manifest = {**self.base, "id": "sp16-1.60-mod-label-mod-eeeeeeeeeeee", "kind": "mod",
                    "app_sha256": "9" * 64, "mod": mismatch}
        firmware.validate_manifest(manifest)
        status = firmware.boot_status(manifest, self.library)
        self.assertEqual((status["status"], status["profile"]), ("unverified", None))
        self.assertIn("content differs", status["reason"])

    def test_infrastructure_failures_are_still_refusals(self):
        def broken_build(*args):
            raise firmware.FirmwareError("package VM did not start")
        with mock.patch.object(fwpack, "build_update", broken_build):
            with self.assertRaisesRegex(firmware.FirmwareError, "did not start"):
                fwpack.mod_verifier(self.library, {}, b"k" * 32)(self.guest, self.meta, self.dir)

    def test_mod_trailer_is_never_a_stock_marker(self):
        self.assertFalse(fwpack.MOD_MARKER.startswith(b"LIVESAMPLER"))
        payload = bytes(1024)
        path = self.dir / "mod.UPD"
        path.write_bytes(payload + fwpack.MOD_MARKER + b"1.60\0" + zlib.crc32(payload).to_bytes(4, "little"))
        meta = firmware.inspect_update(path)
        self.assertEqual((meta["kind"], meta["trailer_version"]), ("mod", "1.60"))
        self.assertEqual(guest.parse_trailer((fwpack.MOD_MARKER + b"1.60\0").ljust(20, b"\0")), ("mod", "1.60"))

    def test_package_vm_uses_package_mode(self):
        args = fwpack.package_arguments({"qemu": "/q", "kernel": "/k", "initrd": "/i"}, "/u", "/o", "/s", 1000)
        append = args[args.index("-append") + 1]
        self.assertIn("sp16.mode=package", append)
        self.assertNotIn("sp16.mode=import", append)
        self.assertIn("file=/u,format=raw,if=none,id=sp16upd,readonly=on", args)


class AplHookTests(unittest.TestCase):
    """The one line a firmware's apl_start carries after `# SP16_APL_HOOK` (a mod loaded from the data volume)."""
    HOOK = b"[ -f /root/data/mod.sh ] && (. /root/data/mod.sh)"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def script(self, *lines):
        path = self.dir / "apl_start"
        path.write_bytes(b"\n".join(lines) + b"\n")
        return path

    def test_returns_only_the_line_after_the_marker(self):
        path = self.script(b"#!/bin/sh", b"echo before", b"# SP16_APL_HOOK", self.HOOK, b"echo after")
        self.assertEqual(guest.apl_hook_line(path), self.HOOK.decode())

    def test_no_marker_or_nothing_after_it_is_no_hook(self):
        self.assertIsNone(guest.apl_hook_line(self.script(b"#!/bin/sh", self.HOOK)))
        path = self.dir / "apl_start"
        path.write_bytes(b"echo x\n# SP16_APL_HOOK")
        self.assertIsNone(guest.apl_hook_line(path))
        self.assertIsNone(guest.apl_hook_line(self.dir / "missing"))

    def test_unusable_hook_lines_are_refused(self):
        for bad in (b"", b"   ", b"x" * 241, "echo é".encode(), b"echo \x01"):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(ValueError):
                    guest.apl_hook_line(self.script(b"# SP16_APL_HOOK", bad))

    def test_crlf_and_tabs_are_tolerated(self):
        path = self.dir / "apl_start"
        path.write_bytes(b"# SP16_APL_HOOK\r\n\t" + self.HOOK + b"\r\n")
        self.assertEqual(guest.apl_hook_line(path), "\t" + self.HOOK.decode())


if __name__ == "__main__":
    unittest.main()
