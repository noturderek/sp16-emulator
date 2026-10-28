"""The ./sp16 command line and the Makefile: argument mapping, defaults, help coverage and drift against the tools."""
import ast
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sp16emu_cli", TOOLS / "sp16emu.py")
cli = importlib.util.module_from_spec(SPEC)
sys.modules["sp16emu_cli"] = cli
SPEC.loader.exec_module(cli)
PYTHON = "/py/python3"


def public_files():
    """(Makefile, root shim): in the public layout next to emulator/, in the private one under public/."""
    for folder in (TOOLS.parent, TOOLS / "public"):
        if (folder / "Makefile").is_file() and (folder / "sp16").is_file():
            return folder / "Makefile", folder / "sp16"
    return None, None


MAKEFILE, SHIM = public_files()


class CliCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name)) / "repo"
        self.root.mkdir()
        self.build = self.root / "build"
        self.build.mkdir(mode=0o700)
        self.calls = []
        self.ps = ""
        self.hdiutil = plistlib.dumps({"images": []}).decode()
        self.ctx = self.make_ctx()

    def make_ctx(self, **kwargs):
        ctx = cli.Context(tools=TOOLS, root=self.root, build=self.build, env={"PATH": os.environ.get("PATH", "")},
                          out=io.StringIO(), err=io.StringIO(), interactive=False, **kwargs)
        ctx.python = lambda strict=True: PYTHON
        ctx.call = lambda argv, env=None: self.calls.append((list(argv), dict(env or {}))) or 0

        def capture(argv, timeout=30):
            if argv[0] == "ps":
                return 0, self.ps
            if argv[0] == "hdiutil":
                return 0, self.hdiutil
            return 0, ""
        ctx.capture = capture
        ctx.preflight = False
        return ctx

    def make_build(self, name, research=False, audio="", library=None, qemu=True, complete=True, **extra):
        folder = self.build / name
        folder.mkdir(mode=0o700, exist_ok=True)
        if not complete:
            return folder
        assets = {"decoupled": True, "profile": "fullpanel", "research": research, "audio": audio,
                  "audio_device": "", "trace": "", "pad_calibration": "default",
                  "library": str(library or self.build / "library"), "qemu_version": cli.qemu_version(self.ctx),
                  "app_bundle": str(folder / "app.app"), "hashes": {}}
        if qemu:
            path = folder / "qemu-system-aarch64"
            path.write_bytes(b"qemu " + name.encode())
            assets["qemu"] = str(path)
            assets["hashes"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        assets.update(extra)
        (folder / "assets.json").write_text(json.dumps(assets))
        return folder

    def make_library(self, path, ids=("20261013-1.60",), version="1.60", selected=True):
        path = Path(path)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        for entry in ids:
            (path / entry).mkdir(exist_ok=True)
            (path / entry / "manifest.json").write_text(json.dumps({"update": {"trailer_version": version}}))
        if selected and ids:
            (path / "selected.json").write_text(json.dumps({"id": ids[0]}))
        return path

    def argv(self, command, *args):
        """The tool command line ./sp16 would run (after the interpreter) and the notes it prints."""
        run = cli.BY_NAME[command]
        head, tail = cli.split_tail(list(args))
        plan = run.handler(self.ctx, run.parser().parse_args(head), tail)
        self.assertEqual(len(plan.steps), 1)
        return plan.steps[0].argv, plan.notes

    def tool(self, name):
        return str(TOOLS / name)

    def main(self, *args):
        self.ctx.out, self.ctx.err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stderr", self.ctx.err):
            code = cli.main(list(args), ctx=self.ctx)
        return code, self.ctx.out.getvalue(), self.ctx.err.getvalue()


class BuildTests(CliCase):
    def test_default_build_command(self):
        argv, notes = self.argv("build")
        self.assertEqual(argv, [PYTHON, self.tool("native.py"), "build", "--decoupled", "--profile", "fullpanel",
                                "--output", str(self.build / "sp16"), "--library", str(self.build / "library")])
        self.assertTrue(any("compiled" in n for n in notes))

    def test_every_build_option_is_forwarded_in_order(self):
        argv, _ = self.argv("build", "--name", "all", "--audio", "coreaudio", "--audio-device", "UID1", "--research",
                            "--trace", "pcmtap", "--pad-calibration-value", "100", "--bundle-suffix", "tag",
                            "--allow-unverified", "--library", "libdir", "--reuse-qemu-from", "other",
                            "--", "--extra", "x")
        self.assertEqual(argv[2:], ["build", "--decoupled", "--profile", "fullpanel", "--output",
                                    str(self.build / "all"), "--audio", "coreaudio", "--audio-device", "UID1",
                                    "--research", "--trace", "pcmtap", "--pad-calibration-value", "100",
                                    "--bundle-suffix", "tag", "--allow-unverified", "--library",
                                    os.path.abspath("libdir"), "--reuse-qemu-from", "other", "--extra", "x"])
        argv, _ = self.argv("build", "--uncalibrated")
        self.assertIn("--uncalibrated", argv)

    def test_bundle_suffix_defaults_to_the_name_for_other_builds(self):
        self.assertNotIn("--bundle-suffix", self.argv("build")[0])
        argv, _ = self.argv("build", "--name", "dev2")
        self.assertEqual(argv[argv.index("--bundle-suffix") + 1], "dev2")
        argv, _ = self.argv("build", "--name", "Has.Dots_1")
        self.assertNotIn("--bundle-suffix", argv)

    def test_names_are_validated(self):
        for bad in ("../x", "a b", ".hidden", "x/y", "a,b", "library", "downloads", "coverage"):
            with self.assertRaises(cli.CliError, msg=bad):
                self.argv("build", "--name", bad)

    def test_qemu_is_reused_from_an_intact_build(self):
        donor = self.make_build("first")
        argv, notes = self.argv("build", "--name", "second")
        self.assertEqual(argv[argv.index("--reuse-qemu-from") + 1], str(donor))
        self.assertTrue(any("reusing the verified QEMU" in n for n in notes))

    def test_a_changed_qemu_is_not_reused(self):
        donor = self.make_build("first")
        (donor / "qemu-system-aarch64").write_bytes(b"tampered")
        argv, _ = self.argv("build", "--name", "second")
        self.assertNotIn("--reuse-qemu-from", argv)

    def test_qemu_reuse_can_be_forced_or_disabled(self):
        self.make_build("first")
        argv, _ = self.argv("build", "--name", "second", "--no-reuse-qemu")
        self.assertNotIn("--reuse-qemu-from", argv)
        argv, _ = self.argv("build", "--name", "second", "--reuse-qemu-from", "somewhere")
        self.assertEqual(argv[argv.index("--reuse-qemu-from") + 1], "somewhere")
        with self.assertRaises(cli.UsageError):
            self.argv("build", "--name", "second", "--reuse-qemu-from", "x", "--no-reuse-qemu")

    def test_the_shared_library_is_used_when_it_has_imports(self):
        library = self.make_library(self.build / "library")
        argv, notes = self.argv("build")
        self.assertEqual(argv[argv.index("--library") + 1], str(library))
        self.assertEqual([n for n in notes if "library" in n], [])

    def test_an_older_per_build_library_is_reused_in_place(self):
        old = self.make_library(self.build / "first" / "library", ids=("a-1.60", "b-1.50"))
        self.make_build("first", library=old)
        argv, notes = self.argv("build", "--name", "second")
        self.assertEqual(argv[argv.index("--library") + 1], str(old))
        self.assertTrue(any("nothing was moved or deleted" in n for n in notes), notes)
        self.assertTrue((old / "a-1.60" / "manifest.json").is_file())

    def test_several_old_libraries_are_named_and_left_alone(self):
        one = self.make_library(self.build / "one" / "library", ids=("a-1.60", "b-1.50"))
        two = self.make_library(self.build / "two" / "library", ids=("c-1.60",))
        self.make_build("one", library=one)
        self.make_build("two", library=two)
        argv, notes = self.argv("build", "--name", "third")
        self.assertEqual(argv[argv.index("--library") + 1], str(one))
        self.assertTrue(any("left untouched" in n and "two" in n for n in notes), notes)

    def test_an_explicit_library_wins(self):
        self.make_library(self.build / "library")
        argv, _ = self.argv("build", "--library", "elsewhere")
        self.assertEqual(argv[argv.index("--library") + 1], os.path.abspath("elsewhere"))

    def test_an_existing_build_needs_rebuild(self):
        self.make_build("sp16")
        with self.assertRaisesRegex(cli.CliError, "keeps firmware.*--rebuild"):
            self.argv("build")

    def test_rebuild_is_in_place_and_keeps_its_library(self):
        old = self.make_library(self.build / "sp16" / "library")
        folder = self.make_build("sp16", library=old, audio="file")
        shared = self.make_library(self.build / "library", ids=("z-1.60",))
        data = old / "20261013-1.60" / "data"
        data.mkdir()
        (data / "emmc.img").write_bytes(b"user data")
        before = sorted(str(p) for p in self.build.rglob("*"))
        argv, notes = self.argv("build", "--rebuild", "--audio", "coreaudio")
        self.assertEqual(argv[argv.index("--output") + 1], str(folder))
        self.assertEqual(argv[argv.index("--library") + 1], str(old))
        self.assertEqual(argv[argv.index("--reuse-qemu-from") + 1], str(folder))
        self.assertTrue(any("kept" in n for n in notes))
        plan = cli.BY_NAME["build"].handler(self.ctx, cli.BY_NAME["build"].parser().parse_args(["--rebuild"]), [])
        cli.execute(self.ctx, plan)
        self.assertEqual(sorted(str(p) for p in self.build.rglob("*")), before)  # nothing deleted by the wrapper
        self.assertEqual((data / "emmc.img").read_bytes(), b"user data")
        self.assertTrue(shared.is_dir())

    def test_rebuild_refuses_a_running_build(self):
        folder = self.make_build("sp16")
        self.ps = f"4242 {folder}/SP-16 Compatible Emulator.app/Contents/MacOS/SP16Emulator\n"
        with self.assertRaisesRegex(cli.CliError, "is running"):
            self.argv("build", "--rebuild")

    def test_an_incomplete_build_can_be_resumed(self):
        self.make_build("sp16", complete=False)
        argv, _ = self.argv("build")
        self.assertIn("--output", argv)

    def test_paths_with_a_tilde_are_expanded(self):
        home = os.path.expanduser("~")
        self.make_build("sp16")
        argv, _ = self.argv("run", "--usb-image", "~/stick.img")
        self.assertEqual(argv[-1], os.path.join(home, "stick.img"))
        argv, _ = self.argv("build", "--name", "t", "--library", "~/lib")
        self.assertEqual(argv[argv.index("--library") + 1], os.path.join(home, "lib"))

    def test_the_end_of_a_build_says_what_to_do_next(self):
        plan = cli.BY_NAME["build"].handler(self.ctx, cli.BY_NAME["build"].parser().parse_args([]), [])
        self.assertIn("firmware import", plan.after[0])
        self.make_library(self.build / "library")
        plan = cli.BY_NAME["build"].handler(self.ctx, cli.BY_NAME["build"].parser().parse_args(["--name", "x"]), [])
        self.assertIn("Next: ./sp16 run --name x", plan.after[0])
        cli.execute(self.ctx, plan)
        self.assertIn("Next:", self.ctx.err.getvalue())

    def test_dry_run_prints_the_command_and_changes_nothing(self):
        shutil.rmtree(self.build)
        code, out, _ = self.main("--dry-run", "build", "--audio", "file")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("+ "))
        self.assertIn("native.py build --decoupled --profile fullpanel", out)
        self.assertFalse(self.build.exists())
        self.assertEqual(self.calls, [])

    def test_build_root_is_created_owner_only_and_loose_permissions_are_refused(self):
        shutil.rmtree(self.build)
        cli.ensure_build_root(self.ctx)
        self.assertEqual(self.build.stat().st_mode & 0o777, 0o700)
        os.chmod(str(self.build), 0o755)
        with self.assertRaisesRegex(cli.CliError, "chmod 700"):
            cli.ensure_build_root(self.ctx)

    def test_preflight_names_each_missing_prerequisite(self):
        ctx = self.make_ctx()
        ctx.which = lambda name: None
        ctx.capture = lambda argv, timeout=30: (1, "")
        with mock.patch.object(cli.platform, "system", return_value="Darwin"):
            with self.assertRaises(cli.CliError) as caught:
                cli.preflight_build(ctx, "sp16", True)
        message = str(caught.exception)
        for text in ("swiftc", "Docker is not installed", "brew install ninja", "not a git work tree"):
            self.assertIn(text, message)


class RunTests(CliCase):
    def test_run_command(self):
        folder = self.make_build("sp16")
        self.make_library(self.build / "library")
        argv, notes = self.argv("run")
        self.assertEqual(argv, [PYTHON, self.tool("native.py"), "launch", "--output", str(folder)])
        self.assertEqual(notes, [])

    def test_run_options(self):
        folder = self.make_build("dev", research=True)
        argv, _ = self.argv("run", "--build", "dev", "--firmware", "abc", "--usb-image", "stick.img", "--ephemeral",
                            "--dev-overlay", "mod", "--no-ui-input")
        self.assertEqual(argv, [PYTHON, self.tool("native.py"), "launch", "--output", str(folder), "--firmware", "abc",
                                "--usb-image", "stick.img", "--ephemeral", "--dev-overlay", "mod", "--no-ui-input"])

    def test_run_passes_no_audio_files(self):
        folder = self.make_build("dev", research=True)
        argv, _ = self.argv("run", "--build", "dev", "--ephemeral", "--no-ui-input", "--no-audio-files")
        self.assertEqual(argv, [PYTHON, self.tool("native.py"), "launch", "--output", str(folder), "--ephemeral",
                                "--no-ui-input", "--no-audio-files"])

    def test_a_missing_build_names_the_command_that_creates_it(self):
        self.make_build("other")
        with self.assertRaisesRegex(cli.CliError, r"./sp16 build --name nope.*existing builds: other"):
            self.argv("run", "--name", "nope")
        with self.assertRaisesRegex(cli.CliError, r"there is no build.*./sp16 build --name sp16"):
            shutil.rmtree(self.build / "other")
            self.argv("run")

    def test_the_only_build_is_the_default(self):
        folder = self.make_build("solo")
        argv, _ = self.argv("run")
        self.assertEqual(argv[argv.index("--output") + 1], str(folder))

    def test_several_builds_need_a_choice(self):
        self.make_build("a")
        self.make_build("b")
        with self.assertRaisesRegex(cli.CliError, "several builds exist"):
            self.argv("run")

    def test_an_empty_library_is_pointed_out(self):
        self.make_build("sp16")
        self.make_library(self.build / "library", ids=())
        _, notes = self.argv("run")
        self.assertTrue(any("library is empty" in n for n in notes))


class FirmwareTests(CliCase):
    def test_import_with_a_key_file(self):
        folder = self.make_build("sp16")
        update, key = self.root / "SP16.UPD", self.root / "the.key"
        update.write_bytes(b"x")
        key.write_bytes(b"y")
        argv, _ = self.argv("firmware", "import", "--update", str(update), "--key-file", str(key), "--name", "My 1.60")
        self.assertEqual(argv, [PYTHON, self.tool("native.py"), "firmware", "import", "--output", str(folder),
                                "--update", str(update), "--key-file", str(key), "--name", "My 1.60"])

    def test_import_with_gpl_parts(self):
        folder = self.make_build("sp16")
        files = []
        for name in ("SP16.UPD", "a.zip", "b.zip"):
            (self.root / name).write_bytes(b"x")
            files.append(str(self.root / name))
        argv, _ = self.argv("firmware", "import", "--update", files[0], "--gpl-archive", files[1], files[2])
        self.assertEqual(argv[2:], ["firmware", "import", "--output", str(folder), "--update", files[0],
                                    "--gpl-archive", files[1], files[2]])

    def test_import_checks_its_inputs_before_starting(self):
        self.make_build("sp16")
        with self.assertRaisesRegex(cli.UsageError, "--update"):
            self.argv("firmware", "import")
        with self.assertRaisesRegex(cli.UsageError, "--key-file KEY or --gpl-archive"):
            self.argv("firmware", "import", "--update", "x")
        with self.assertRaisesRegex(cli.CliError, "does not exist"):
            self.argv("firmware", "import", "--update", str(self.root / "missing.UPD"), "--key-file", "k")

    def test_actions_that_need_an_id(self):
        folder = self.make_build("sp16")
        for action in ("show", "check", "select", "remove", "profile"):
            with self.assertRaisesRegex(cli.UsageError, "--id"):
                self.argv("firmware", action)
            argv, _ = self.argv("firmware", action, "--id", "abc")
            self.assertEqual(argv[2:], ["firmware", action, "--output", str(folder), "--id", "abc"])
        with self.assertRaisesRegex(cli.UsageError, "--name"):
            self.argv("firmware", "rename", "--id", "abc")
        argv, _ = self.argv("firmware", "profile", "--id", "abc", "--from-profile", "sp16-1.60")
        self.assertEqual(argv[-2:], ["--from-profile", "sp16-1.60"])

    def test_list_and_audit_take_no_id(self):
        folder = self.make_build("sp16")
        for action in ("list", "audit"):
            argv, _ = self.argv("firmware", action)
            self.assertEqual(argv, [PYTHON, self.tool("native.py"), "firmware", action, "--output", str(folder)])

    def test_unknown_action_and_missing_build(self):
        with self.assertRaises(cli.UsageError):
            cli.BY_NAME["firmware"].parser().parse_args(["frobnicate"])
        with self.assertRaisesRegex(cli.CliError, "./sp16 build --name sp16"):
            self.argv("firmware", "list")


class OtherCommandTests(CliCase):
    def test_diagnostics(self):
        folder = self.make_build("sp16")
        argv, _ = self.argv("diagnostics", "--sessions", "5", "--include-audio", "--out", "z.zip", "--key-file", "k")
        self.assertEqual(argv, [PYTHON, self.tool("native.py"), "diagnostics", "--output", str(folder),
                                "--sessions", "5", "--include-audio", "--out", "z.zip", "--key-file", "k"])

    def test_diagnostics_works_on_an_incomplete_build(self):
        folder = self.make_build("sp16", complete=False)
        argv, _ = self.argv("diagnostics")
        self.assertEqual(argv[argv.index("--output") + 1], str(folder))

    def test_mod_actions(self):
        dev = self.make_build("sp16-dev", research=True, audio="file")
        argv, _ = self.argv("mod", "check", "--mod", "m")
        self.assertEqual(argv, [PYTHON, self.tool("fwdev.py"), "check", "--output", str(dev), "--mod", "m"])
        argv, _ = self.argv("mod", "test", "--mod", "m", "--baseline", "b.png", "--expect-change", "1", "2", "3", "4",
                            "--touch", "5", "6", "--firmware", "abc")
        self.assertEqual(argv[2:], ["test", "--output", str(dev), "--mod", "m", "--firmware", "abc", "--baseline",
                                    "b.png", "--expect-change", "1", "2", "3", "4", "--touch", "5", "6"])
        argv, _ = self.argv("mod", "package", "--mod", "m", "--out", "o", "--upd", "--key-file", "k")
        self.assertEqual(argv[2:], ["package", "--output", str(dev), "--mod", "m", "--out", "o", "--upd",
                                    "--key-file", "k"])
        argv, _ = self.argv("mod", "new", "--mod", "m", "--id", "x")
        self.assertEqual(argv, [PYTHON, self.tool("fwdev.py"), "new", "--mod", "m", "--id", "x"])
        argv, _ = self.argv("mod", "sdk", "--mod", "m")
        self.assertEqual(argv, [PYTHON, self.tool("fwdev.py"), "sdk", "--mod", "m"])
        with self.assertRaises(cli.UsageError):
            self.argv("mod", "sdk", "--mod", "m", "--build", "x")

    def test_mod_run_needs_a_research_build(self):
        self.make_build("sp16")
        with self.assertRaisesRegex(cli.CliError, "no research build.*--research --audio file"):
            self.argv("mod", "run", "--mod", "m")

    def test_ctl_finds_the_newest_live_session(self):
        dev = self.make_build("sp16-dev", research=True)
        live = self.root / "live.sock"
        live.write_text("")
        for index, socket_path in enumerate((live, self.root / "gone.sock")):
            session = dev / "sessions" / f"native-{index}"
            session.mkdir(parents=True)
            runtime = session / "runtime.json"
            runtime.write_text(json.dumps({"control_socket": str(socket_path)}))
            os.utime(str(runtime), (1000 + index * 10 ** (1 - index), 1000 + index * 10 ** (1 - index)))
        os.utime(str(dev / "sessions" / "native-0" / "runtime.json"), (2000, 2000))
        os.utime(str(dev / "sessions" / "native-1" / "runtime.json"), (1000, 1000))
        argv, _ = self.argv("ctl", "turn", "knob1", "-3")
        self.assertEqual(argv, [PYTHON, self.tool("sessionctl.py"), "--runtime",
                                str(dev / "sessions" / "native-0" / "runtime.json"), "turn", "knob1", "-3"])

    def test_ctl_without_a_session(self):
        self.make_build("sp16-dev", research=True)
        with self.assertRaisesRegex(cli.CliError, "no running research session.*./sp16 run --name sp16-dev"):
            self.argv("ctl", "metrics")
        argv, _ = self.argv("ctl", "--runtime", "r.json", "press", "play", "0.2")
        self.assertEqual(argv[2:], ["--runtime", "r.json", "press", "play", "0.2"])

    def test_hwsuite_defaults(self):
        library = self.make_library(self.build / "library")
        dev = self.make_build("sp16-dev", research=True, audio="file", library=library)
        control = self.make_build("sp16-dev-uncal", research=True, audio="file", library=library)
        argv, notes = self.argv("hwsuite", "--smoke", "--only", "HW-24")
        profile = str(TOOLS / "profiles" / "sp16-1.60.json")
        self.assertEqual(argv[1:], [self.tool("hwsuite.py"), "--profile", profile, "--build-dir", str(dev),
                                    "--control-build", str(control), "--smoke", "--only", "HW-24"])
        self.assertEqual(notes, [])

    def test_hwsuite_options_and_requirements(self):
        library = self.make_library(self.build / "library")
        self.make_build("sp16-dev", research=True, audio="file", library=library)
        argv, notes = self.argv("hwsuite", "--profile", "1.50", "--tier", "core", "--firmware", "f")
        self.assertIn(str(TOOLS / "profiles" / "sp16-1.50.json"), argv)
        self.assertTrue(any("no control build" in n for n in notes))
        self.assertEqual(argv[-4:], ["--firmware", "f", "--tier", "core"])
        with self.assertRaisesRegex(cli.CliError, "no profile for version 9.99"):
            self.argv("hwsuite", "--profile", "9.99")
        shutil.rmtree(self.build / "sp16-dev")
        self.make_build("sp16-dev", research=True, audio="", library=library)
        with self.assertRaisesRegex(cli.CliError, "--audio file"):
            self.argv("hwsuite")

    def test_hwsuite_tools_without_a_build(self):
        argv, _ = self.argv("hwsuite", "--make-fixtures", "fx")
        self.assertEqual(argv, [PYTHON, self.tool("hwsuite.py"), "--make-fixtures", "fx"])

    def test_smoke_defaults_and_refusals(self):
        library = self.make_library(self.build / "library")
        dev = self.make_build("sp16-dev", research=True, audio="file", library=library)
        argv, _ = self.argv("smoke", "--speed", "6", "--no-video")
        self.assertEqual(argv[1:], [self.tool("smoke.py"), "--build-dir", str(dev), "--speed", "6", "--no-video"])
        loud = self.make_build("loud", research=True, audio="coreaudio", library=library)
        with self.assertRaisesRegex(cli.CliError, "plays sound"):
            self.argv("smoke", "--build", loud.name)
        self.make_build("plain", research=False, library=library)
        with self.assertRaises(cli.CliError):
            self.argv("smoke", "--build", "plain")

    def test_ctl_power_standby_pressure_and_calibrate(self):
        """The session commands the mod and calibration lanes added are reachable from ./sp16 ctl."""
        for command in (("power",), ("power", "0.5"), ("standby", "yes"), ("pressure", "3", "300"),
                        ("calibrate", "3", "300")):
            with self.subTest(command=command):
                argv, _ = self.argv("ctl", "--runtime", "r.json", *command)
                self.assertEqual(argv[2:], ["--runtime", "r.json", *command])
        with self.assertRaisesRegex(cli.UsageError, "./sp16 help ctl"):
            self.argv("ctl", "--runtime", "r.json", "powered")

    def test_midi_maps_each_subcommands_options(self):
        argv, _ = self.argv("midi", "ports")
        self.assertEqual(argv, [PYTHON, "-m", "emulator.midi_bridge", "ports", "--build-dir", str(self.build)])
        argv, _ = self.argv("midi", "preset", "--name", "p", "--channel", "3", "--note-base", "48",
                            "--cc-page", "2", "--out-dir", "d")
        self.assertEqual(argv[3:], ["preset", "--name", "p", "--channel", "3", "--note-base", "48",
                                    "--cc-page", "2", "--out-dir", "d"])
        argv, _ = self.argv("midi", "learn", "--in", "nano", "--name", "n", "--controls", "pad1,knob1",
                            "--timeout", "5", "--no-leds")
        self.assertEqual(argv[3:], ["learn", "--build-dir", str(self.build), "--in", "nano", "--name", "n",
                                    "--controls", "pad1,knob1", "--timeout", "5", "--no-leds"])
        argv, _ = self.argv("midi", "run", "--in", "nano", "--preset", "sp16", "--seconds", "30", "--no-leds")
        self.assertEqual(argv[3:], ["run", "--build-dir", str(self.build), "--in", "nano", "--preset", "sp16",
                                    "--no-leds", "--seconds", "30"])
        argv, _ = self.argv("midi", "run", "--in", "nano", "--runtime", "r.json", "--build-dir", "elsewhere")
        self.assertEqual(argv[3:], ["run", "--runtime", "r.json", "--build-dir", "elsewhere", "--in", "nano"])

    def test_midi_runs_the_bridge_from_the_repository_root(self):
        """`python3 -m emulator.midi_bridge` needs the repository on PYTHONPATH whatever the caller's directory."""
        run = cli.BY_NAME["midi"]
        plan = run.handler(self.ctx, run.parser().parse_args(["ports"]), [])
        self.assertEqual(plan.steps[0].env["PYTHONPATH"].split(os.pathsep)[0], str(self.root))

    def test_midi_refuses_options_a_subcommand_does_not_have(self):
        with self.assertRaisesRegex(cli.UsageError, "ports: --in is not an option"):
            self.argv("midi", "ports", "--in", "nano")
        with self.assertRaisesRegex(cli.UsageError, "preset: --build-dir is not an option"):
            self.argv("midi", "preset", "--name", "p", "--build-dir", "x")
        with self.assertRaisesRegex(cli.UsageError, "midi learn: --in is required"):
            self.argv("midi", "learn", "--name", "n")
        with self.assertRaisesRegex(cli.UsageError, "midi run: --in is required"):
            self.argv("midi", "run")
        with self.assertRaisesRegex(cli.UsageError, "./sp16 help midi"):
            self.argv("midi", "play")

    def test_hwsuite_extra_tier_and_testmode_build(self):
        library = self.make_library(self.build / "library")
        self.make_build("sp16-dev", research=True, audio="file", library=library)
        testmode = self.make_build("sp16-dev-testmode", research=True, audio="file", library=library)
        argv, notes = self.argv("hwsuite", "--tier", "extra")
        self.assertEqual(argv[argv.index("--testmode-build") + 1], str(testmode))     # the build next to it
        self.assertEqual(argv[-2:], ["--tier", "extra"])
        self.assertFalse(any("testmode" in note for note in notes))
        shutil.rmtree(testmode)
        other = self.make_build("other-testmode", research=True, audio="file", library=library)
        argv, notes = self.argv("hwsuite", "--tier", "extra", "--testmode-build", other.name)
        self.assertEqual(argv[argv.index("--testmode-build") + 1], str(other))
        shutil.rmtree(other)
        argv, notes = self.argv("hwsuite", "--tier", "extra")
        self.assertNotIn("--testmode-build", argv)
        self.assertTrue(any("factory-test checks are N/A" in note for note in notes))
        self.assertTrue(any("--app-arg testmode" in note for note in notes))
        self.assertNotIn("--testmode-build", self.argv("hwsuite")[0])   # a normal run does not need one

    def test_coverage_measures_then_reports(self):
        run = cli.BY_NAME["test"]
        plan = run.handler(self.ctx, run.parser().parse_args(["--coverage"]), ["-k", "panel"])
        measure, report, xml = (step.argv for step in plan.steps)
        self.assertEqual(measure[1:4], ["-m", "coverage", "run"])
        self.assertIn("--branch", measure)
        self.assertEqual(measure[-2:], ["-k", "panel"])
        self.assertEqual(report[1:4], ["-m", "coverage", "report"])
        self.assertIn(str(self.build / "coverage" / "coverage.xml"), xml)
        self.assertTrue((self.build / "coverage").is_dir())
        self.ctx.capture = lambda argv, timeout=30: (1, "")  # coverage.py is not installed
        with self.assertRaisesRegex(cli.CliError, "pip install coverage"):
            run.handler(self.ctx, run.parser().parse_args(["--coverage"]), [])

    def test_test_command_uses_the_right_folder(self):
        argv, _ = self.argv("test", "--", "-k", "panel")
        self.assertEqual(argv[1:4], ["-m", "unittest", "discover"])
        self.assertEqual(argv[-2:], ["-k", "panel"])
        pattern = argv[argv.index("-p") + 1]
        self.assertEqual(pattern, "test_sp16_*.py" if (TOOLS / "tests").is_dir() else "test_sp16_*.py")

    def test_unknown_options_are_refused_but_pass_after_double_dash(self):
        self.make_build("sp16")
        with self.assertRaises(cli.UsageError):
            self.argv("run", "--frobnicate")
        argv, _ = self.argv("run", "--", "--frobnicate")
        self.assertEqual(argv[-1], "--frobnicate")

    def test_builds_listing(self):
        library = self.make_library(self.build / "library", ids=("a-1.60", "b-1.50"))
        self.make_build("sp16", audio="file", library=library, pad_calibration="off")
        self.make_build("sp16-dev", research=True, trace="pcmtap", library=library)
        self.make_build("half", complete=False)
        for shared in ("coverage", "downloads/ver160"):  # written by test --coverage and firmware download
            (self.build / shared).mkdir(parents=True)
        code, out, _ = self.main("builds")
        self.assertEqual(code, 0)
        lines = {line.split()[0]: line for line in out.splitlines() if line.strip() and not line.startswith("Shared")}
        self.assertIn("file", lines["sp16"])
        self.assertIn("uncalibrated", lines["sp16"])
        self.assertIn("yes+pcmtap", lines["sp16-dev"])
        self.assertIn("incomplete", lines["half"])
        self.assertIn("Shared firmware library build/library: 2 import(s)", out)
        code, out, _ = self.main("builds", "--json")
        rows = json.loads(out)
        self.assertEqual([r["name"] for r in rows], ["half", "sp16", "sp16-dev"])
        self.assertTrue({cli.DOWNLOAD_DIR, cli.COVERAGE_DIR} <= set(cli.SHARED_FOLDERS))
        self.assertEqual(rows[1]["firmware"], 2)


class CleanTests(CliCase):
    def populate(self):
        old = self.make_library(self.build / "sp16" / "library")
        folder = self.make_build("sp16", library=old)
        (folder / "app.app").mkdir()
        (folder / "app.app" / "exe").write_bytes(b"x")
        (folder / "qemu-build.log").write_bytes(b"x")
        (folder / "data").mkdir()
        (folder / "data" / "emmc.img").write_bytes(b"data")
        (folder / "sessions").mkdir()
        shared = self.make_library(self.build / "library", ids=("s-1.60",))
        (self.build / ".cache").mkdir()
        return folder, old, shared

    def test_clean_all_keeps_libraries_and_data(self):
        folder, old, shared = self.populate()
        code, out, _ = self.main("clean", "--all")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["data", "library"])
        self.assertTrue((old / "20261013-1.60" / "manifest.json").is_file())
        self.assertTrue((shared / "s-1.60" / "manifest.json").is_file())
        self.assertEqual((folder / "data" / "emmc.img").read_bytes(), b"data")
        self.assertFalse((self.build / ".cache").exists())
        self.assertIn("Kept the firmware library and data", out)

    def test_a_build_with_nothing_to_keep_disappears(self):
        folder = self.make_build("plain")
        (folder / "x.log").write_text("x")
        self.main("clean", "--all")
        self.assertFalse(folder.exists())

    def test_clean_one_build_only(self):
        folder, _, _ = self.populate()
        other = self.make_build("other")
        (other / "log").write_text("x")
        self.main("clean", "--name", "other")
        self.assertFalse((other / "assets.json").exists())
        self.assertTrue((folder / "assets.json").exists())

    def test_clean_retries_when_finder_refills_a_folder(self):
        folder, _, _ = self.populate()
        other = self.make_build("other")
        for build in (folder, other):  # a QEMU source tree: removed with rmtree, where the race happens
            (build / "qemu-10.2.1").mkdir()
            (build / "qemu-10.2.1/configure").write_text("x")
        real_rmtree = cli.shutil.rmtree
        calls = []

        def finder_race(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:  # the first attempt finds a .DS_Store that appeared meanwhile
                raise OSError(errno.ENOTEMPTY, "Directory not empty", path)
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(cli.shutil, "rmtree", side_effect=finder_race), mock.patch.object(cli.time, "sleep"):
            code, _, _ = self.main("clean", "--name", "other")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse((other / "assets.json").exists())
        with mock.patch.object(cli.shutil, "rmtree", side_effect=OSError(errno.ENOTEMPTY, "Directory not empty")), \
                mock.patch.object(cli.time, "sleep"):
            code, _, err = self.main("clean", "--name", "sp16")
        self.assertEqual(code, 1)
        self.assertIn("close any Finder window", err)

    def test_everything_needs_confirmation_and_stays_inside_build(self):
        self.populate()
        outside = self.root / "outside.txt"
        outside.write_text("keep")
        (self.build / "link").symlink_to(outside)
        code, _, err = self.main("clean", "--everything")
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertTrue((self.build / "library").is_dir())
        code, out, _ = self.main("clean", "--everything", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(list(self.build.iterdir()), [])
        self.assertEqual(outside.read_text(), "keep")

    def test_dry_run_deletes_nothing(self):
        self.populate()
        before = sorted(str(p) for p in self.build.rglob("*"))
        code, out, _ = self.main("--dry-run", "clean", "--everything")
        self.assertEqual(code, 0)
        self.assertIn("would remove", out)
        self.assertEqual(sorted(str(p) for p in self.build.rglob("*")), before)

    def test_a_running_build_is_refused_and_one_choice_is_required(self):
        folder, _, _ = self.populate()
        self.ps = f"77 {folder}/x/SP16Emulator\n"
        code, _, err = self.main("clean", "--all")
        self.assertEqual(code, 1)
        self.assertIn("is running", err)
        self.assertEqual(self.main("clean")[0], 2)
        self.assertEqual(self.main("clean", "--all", "--everything")[0], 2)

    def test_nothing_outside_build_can_be_removed(self):
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(cli.CliError, "outside"):
            cli.remove_path(self.ctx, outside)


class DoctorAndAudioTests(CliCase):
    def run_doctor(self, results, which=None, root=None, home=None):
        ctx = self.make_ctx()
        ctx.which = which or (lambda name: "/usr/bin/" + name)
        if root is not None:
            ctx.root = root
        if home is not None:
            ctx.home = home

        def capture(argv, timeout=30):
            for key, value in results.items():
                if key in " ".join(argv):
                    return value
            return 0, ""
        ctx.capture = capture
        with mock.patch.object(cli.platform, "system", return_value="Darwin"), \
                mock.patch.object(cli, "is_apple_silicon", return_value=True), \
                mock.patch.object(cli.platform, "mac_ver", return_value=("14.5", ("", "", ""), "arm64")):
            return cli.run_doctor(ctx, None, []), ctx.out.getvalue()

    def test_doctor_reports_fixes_and_fails(self):
        code, out = self.run_doctor({"docker info": (1, ""), "rev-parse": (128, "")})
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] Docker running", out)
        self.assertIn("fix: start Docker Desktop", out)
        self.assertIn("fix: clone the repository with git", out)

    def test_doctor_passes_when_everything_is_there(self):
        code, out = self.run_doctor({"docker info": (0, "27.0\n"), "rev-parse": (0, str(self.root) + "\n"),
                                     "check-ignore": (0, "")})
        # check-ignore returning 0 means ignored, which is what the doctor wants to see
        self.assertNotIn("[FAIL] Docker", out)
        self.assertIn("Xcode Command Line Tools", out)

    def test_doctor_warns_when_the_repository_is_in_a_protected_folder(self):
        home = self.root / "home"
        project = home / "Downloads" / "sp16-emulator"
        code, out = self.run_doctor({"check-ignore": (0, "")}, root=project, home=home)
        self.assertIn("[warn] repository location (macOS privacy): inside", out)
        self.assertIn("Full Disk Access", out)
        code, out = self.run_doctor({"check-ignore": (0, "")}, root=home / "Projects" / "sp16-emulator", home=home)
        self.assertIn("[ok  ] repository location (macOS privacy)", out)

    def test_doctor_names_missing_tools(self):
        code, out = self.run_doctor({}, which=lambda name: None)
        self.assertEqual(code, 1)
        for text in ("swiftc", "xcode-select --install", "brew install ninja", "install Docker Desktop"):
            self.assertIn(text, out)

    def test_audio_tool_compile_command_and_listing(self):
        argv = cli.audio_compile_argv(self.ctx)
        self.assertEqual(argv[0], "swiftc")
        self.assertIn("-parse-as-library", argv)
        for source in cli.AUDIO_SOURCES:
            self.assertIn(str(TOOLS / source), argv)
            self.assertTrue((TOOLS / source).is_file())
        self.assertEqual(argv[-2:], ["-o", str(self.build / ".cache" / "audio-selftest")])
        self.assertEqual(cli.parse_devices("BuiltInSpeakerDevice\tMacBook Speakers\nnoise\nAB-1\tUSB DAC\n"),
                         [("BuiltInSpeakerDevice", "MacBook Speakers"), ("AB-1", "USB DAC")])
        code, out, _ = self.main("--dry-run", "audio", "devices")
        self.assertEqual(code, 0)
        self.assertIn("audio-selftest devices", out)

    def test_audio_tool_is_cached_by_source_hash(self):
        cache, binary, stamp = cli.audio_tool_paths(self.ctx)
        cache.mkdir()
        binary.write_text("fake")
        stamp.write_text(cli.audio_stamp(self.ctx) + "\n")
        self.assertEqual(cli.ensure_audio_tool(self.ctx), binary)
        stamp.write_text("stale\n")
        self.ctx.which = lambda name: None
        with self.assertRaisesRegex(cli.CliError, "swiftc is missing"):
            cli.ensure_audio_tool(self.ctx)


class HelpTests(CliCase):
    def test_overview_lists_every_command(self):
        code, out, _ = self.main("help")
        self.assertEqual(code, 0)
        for command in cli.COMMANDS:
            self.assertRegex(out, rf"(?m)^  {command.name}\s+\S")
        self.assertEqual(cli.main([], ctx=self.make_ctx()), 0)
        wanted = {"help", "doctor", "build", "run", "firmware", "diagnostics", "hwsuite", "mod", "ctl", "midi",
                  "audio", "builds", "test", "clean", "usb", "bootstrap", "smoke"}
        self.assertEqual(wanted, set(cli.BY_NAME))

    def test_every_command_documents_every_option_and_an_example(self):
        for command in cli.COMMANDS:
            for form in (["help", command.name], [command.name, "--help"]):
                code, out, _ = self.main(*form)
                self.assertEqual(code, 0)
                self.assertIn(f"./sp16 {command.name}:", out)
                self.assertIn("Examples:", out)
                for opt in command.opts:
                    for flag in opt.flags:
                        self.assertIn(flag, out, f"{command.name} help lacks {flag}")
                for pos in command.positionals:
                    for choice in pos.choices or ():
                        self.assertIn(choice, out)

    def test_unknown_command_suggests_a_fix(self):
        code, _, err = self.main("biuld")
        self.assertEqual(code, 2)
        self.assertIn("did you mean 'build'", err)
        self.assertEqual(self.main("help", "nope")[0], 2)

    def test_errors_are_one_line_with_a_nonzero_exit(self):
        code, _, err = self.main("run", "--name", "nope")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("sp16: "))

    def test_source_is_python_39_syntax(self):
        ast.parse((TOOLS / "sp16emu.py").read_text(), feature_version=(3, 9))
        if SHIM:
            ast.parse(SHIM.read_text(), feature_version=(3, 9))

    @unittest.skipUnless(os.access("/usr/bin/python3", os.X_OK), "no system python")
    def test_help_runs_on_the_system_python(self):
        result = subprocess.run(["/usr/bin/python3", str(TOOLS / "sp16emu.py"), "help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("firmware", result.stdout)

    def test_root_shim_runs_through_a_symlink(self):
        if SHIM is None or TOOLS.name != "emulator":
            self.skipTest("the root shim is checked in the public layout")
        link = self.root / "installed"
        link.symlink_to(SHIM)
        result = subprocess.run([sys.executable, str(link), "help", "build"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--rebuild", result.stdout)


TOOL_FLAG = re.compile(r"(?m)^ {2}(?:-\w, )?(--[a-z][a-z0-9-]*)")


class DriftTests(unittest.TestCase):
    """Every option of the underlying tools stays reachable: a flag a tool gains must be added to sp16emu."""

    @staticmethod
    def flags_of(*command):
        result = subprocess.run([sys.executable, *command, "--help"], capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        assert result.returncode == 0, result.stderr
        return set(TOOL_FLAG.findall(result.stdout)) - {"--help"}

    @staticmethod
    def reachable(command, extra=()):
        return {flag for opt in cli.BY_NAME[command].opts for flag in opt.flags} | set(extra)

    def test_native_build_flags(self):
        tool = self.flags_of(str(TOOLS / "native.py"), "build")
        handled = self.reachable("build") | {"--output", "--decoupled", "--profile"}
        self.assertEqual(tool - handled, set(), "native.py build has options ./sp16 does not know")

    def test_native_build_choices(self):
        text = subprocess.run([sys.executable, str(TOOLS / "native.py"), "build", "--help"], capture_output=True,
                              text=True).stdout.replace("\n", " ")
        for opt in cli.BUILD_OPTS:
            for choice in opt.choices or ():
                self.assertIn(choice, text)

    def test_native_launch_flags(self):
        tool = self.flags_of(str(TOOLS / "native.py"), "launch")
        self.assertEqual(tool - self.reachable("run") - {"--output"}, set())

    def test_native_firmware_flags_and_actions(self):
        tool = self.flags_of(str(TOOLS / "native.py"), "firmware")
        self.assertEqual(tool - self.reachable("firmware") - {"--output"}, set())
        usage = subprocess.run([sys.executable, str(TOOLS / "native.py"), "firmware", "--help"], capture_output=True,
                               text=True).stdout
        actions = set(re.search(r"\{([a-z,]+)\}", usage).group(1).split(","))
        self.assertEqual(actions, set(cli.FIRMWARE_ACTIONS))

    def test_native_diagnostics_flags(self):
        tool = self.flags_of(str(TOOLS / "native.py"), "diagnostics")
        self.assertEqual(tool - self.reachable("diagnostics") - {"--output"}, set())

    def test_fwdev_flags_and_actions(self):
        usage = subprocess.run([sys.executable, str(TOOLS / "fwdev.py"), "--help"], capture_output=True, text=True).stdout
        actions = set(re.search(r"\{([a-z,]+)\}", usage).group(1).split(","))
        self.assertEqual(actions, set(cli.MOD_ACTIONS))
        seen = set()
        for action in actions:
            seen |= self.flags_of(str(TOOLS / "fwdev.py"), action)
        self.assertEqual(seen - self.reachable("mod") - {"--output"}, set())

    def test_hwsuite_flags(self):
        tool = self.flags_of(str(TOOLS / "hwsuite.py"))
        self.assertEqual(tool - self.reachable("hwsuite") - {"--build-dir", "--control-build"}, set())

    def test_smoke_flags(self):
        tool = self.flags_of(str(TOOLS / "smoke.py"))
        self.assertEqual(tool - self.reachable("smoke") - {"--build-dir"}, set())

    def test_sessionctl_commands(self):
        usage = subprocess.run([sys.executable, str(TOOLS / "sessionctl.py"), "--help"], capture_output=True,
                               text=True).stdout.replace("\n", " ")
        listed = re.search(r"\{([a-z,-]+)\}", usage).group(1).split(",")
        self.assertEqual(set(listed), set(cli.CTL_COMMANDS))
        text = " ".join(cli.help_ctl_lines())
        for name in listed:
            self.assertIn(name, text)


@unittest.skipUnless(MAKEFILE and shutil.which("make"), "no make or no Makefile in this layout")
class MakefileTests(unittest.TestCase):
    def make(self, *args):
        result = subprocess.run(["make", "-C", str(MAKEFILE.parent), *args], capture_output=True, text=True,
                                env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"})
        return result.returncode, result.stdout, result.stderr

    def targets(self):
        text = MAKEFILE.read_text()
        phony = re.search(r"(?m)^\.PHONY:((?:.*\\\n)*.*)$", text).group(1).replace("\\\n", " ").split()
        return text, phony

    def test_best_practice_header(self):
        text, _ = self.targets()
        for line in ("SHELL := /bin/bash", ".SHELLFLAGS := -eu -o pipefail -c", ".DELETE_ON_ERROR:",
                     "MAKEFLAGS += --warn-undefined-variables --no-builtin-rules", ".SUFFIXES:",
                     ".DEFAULT_GOAL := help"):
            self.assertIn(line, text)
        self.assertNotIn("$(MAKE)", text)  # no recursive make

    def test_every_target_is_phony_documented_and_in_help(self):
        text, phony = self.targets()
        rules = re.findall(r"(?m)^([a-z][a-z-]*):", text)
        self.assertEqual(sorted(rules), sorted(phony))
        code, out, err = self.make("help")
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err.lower())
        for target in phony:
            self.assertRegex(text, rf"(?m)^{target}:.*## \S", f"{target} has no help comment")
            self.assertRegex(out, rf"(?m)^  {target}\s+\S")
        for variable in ("NAME", "AUDIO", "AUDIO_DEVICE", "FIRMWARE", "USB_IMAGE", "FILES", "DEST", "UPD", "KEY", "GPL",
                         "VERSION", "YES", "SAMPLES", "PROJECTS", "PYTHON", "PREFIX"):
            self.assertRegex(out, rf"(?m)^  {variable}\s+\S")
            self.assertRegex(text, rf"(?m)^{variable} \?=")
        self.assertEqual(self.make()[1], out)  # the default goal is help

    def test_targets_expand_to_the_expected_commands(self):
        expect = {
            "doctor": ["doctor"],
            "build": ["build", "--name", "sp16"],
            "build-audio": ["build", "--name", "sp16-audio", "--audio", "coreaudio"],
            "build-record": ["build", "--name", "sp16-record", "--audio", "file"],
            "build-dev": ["build", "--name", "sp16-dev", "--research", "--audio", "file", "--bundle-suffix", "dev"],
            "build-control": ["build", "--name", "sp16-dev-uncal", "--research", "--audio", "file", "--uncalibrated",
                              "--bundle-suffix", "dev-uncal"],
            "run": ["run", "--name", "sp16"], "run-audio": ["run", "--name", "sp16-audio"],
            "run-dev": ["run", "--name", "sp16-dev"], "audio-devices": ["audio", "devices"],
            "diagnostics": ["diagnostics"], "hwsuite-smoke": ["hwsuite", "--smoke", "--build", "sp16-dev"],
            "test": ["test"], "clean": ["clean", "--all"], "list": ["builds"],
            "usb-create": ["usb", "create"], "usb-ls": ["usb", "ls"], "usb-mount": ["usb", "mount"],
            "usb-unmount": ["usb", "unmount"],
        }
        for target, words in expect.items():
            code, out, err = self.make("-n", target)
            self.assertEqual(code, 0, err)
            first = shlex.split(out.splitlines()[0])
            self.assertEqual(first[:2], ["/usr/bin/python3", "./sp16"], target)
            self.assertEqual(first[2:], words, target)

    def test_variables_flow_into_the_commands(self):
        out = self.make("-n", "build", "NAME=mine", "AUDIO=both", "AUDIO_DEVICE=UID 1", "REBUILD=1", "ARGS=--research")[1]
        self.assertEqual(shlex.split(out)[2:], ["build", "--name", "mine", "--audio", "both", "--audio-device",
                                                "UID 1", "--rebuild", "--research"])
        out = self.make("-n", "run", "NAME=a b", "FIRMWARE=f", "USB_IMAGE=my stick.img", "PYTHON=/opt/py thon")[1]
        self.assertEqual(shlex.split(out), ["/opt/py thon", "./sp16", "run", "--name", "a b", "--firmware", "f",
                                            "--usb-image", "my stick.img"])
        out = self.make("-n", "import", "UPD=/p a/SP16.UPD", "GPL=one.zip two.zip")[1]
        self.assertEqual(shlex.split(out)[2:], ["firmware", "import", "--update", "/p a/SP16.UPD", "--gpl-archive",
                                                "one.zip", "two.zip"])
        out = self.make("-n", "import", "UPD=u.UPD", "KEY=k", "NAME=other")[1]
        self.assertEqual(shlex.split(out)[2:], ["firmware", "import", "--build", "other", "--update", "u.UPD",
                                                "--key-file", "k"])
        self.assertEqual(shlex.split(self.make("-n", "list", "NAME=x")[1].splitlines()[1])[2:],
                         ["firmware", "list", "--build", "x"])

    def test_usb_targets_pass_the_files_and_the_image(self):
        out = self.make("-n", "usb-add", "FILES=a.wav b/c d.wav")[1]
        self.assertEqual(shlex.split(out)[2:], ["usb", "add", "a.wav", "b/c", "d.wav"])
        out = self.make("-n", "usb-add", "FILES=a.wav", "USB_IMAGE=my stick.img", "ARGS=--to kicks")[1]
        self.assertEqual(shlex.split(out)[2:], ["usb", "add", "a.wav", "--image", "my stick.img", "--to", "kicks"])
        out = self.make("-n", "usb-add", "FILES=Demo 1.prj", "DEST=Projects")[1]
        self.assertEqual(shlex.split(out)[2:], ["usb", "add", "Demo", "1.prj", "--dest", "Projects"])
        for target in ("usb-create", "usb-ls", "usb-mount", "usb-unmount"):
            out = self.make("-n", target, "USB_IMAGE=/s/x.img")[1]
            self.assertEqual(shlex.split(out)[2:], ["usb", target[4:], "--image", "/s/x.img"], target)
        self.assertEqual(shlex.split(self.make("-n", "usb-create", "ARGS=--size 2G --force")[1])[2:],
                         ["usb", "create", "--size", "2G", "--force"])

    def test_missing_variables_fail_early_with_a_readable_message(self):
        code, _, err = self.make("-n", "import")
        self.assertNotEqual(code, 0)
        self.assertIn("make import needs UPD=", err)
        code, _, err = self.make("-n", "import", "UPD=x")
        self.assertIn("KEY=", err)
        code, _, err = self.make("-n", "usb-add")
        self.assertNotEqual(code, 0)
        self.assertIn("make usb-add needs FILES=", err)
        code, _, err = self.make("-n", "clean-all")
        self.assertNotEqual(code, 0)
        self.assertIn("CONFIRM=1", err)
        code, out, _ = self.make("-n", "clean-all", "CONFIRM=1")
        self.assertEqual(code, 0)
        self.assertEqual(shlex.split(out)[2:], ["clean", "--everything", "--yes"])

    def test_install_only_links_and_uninstall_checks_the_link(self):
        code, out, _ = self.make("-n", "install", "PREFIX=/opt/x")
        self.assertEqual(code, 0)
        self.assertIn('ln -sfn "', out)
        self.assertIn('/opt/x/bin/sp16"', out)
        self.assertNotRegex(out, r"\.zshrc|\.bash_profile|\.profile")
        self.assertIn("readlink", self.make("-n", "uninstall")[1])

    def test_every_command_in_the_makefile_parses(self):
        for target in self.targets()[1]:
            if target in ("help", "install", "uninstall"):
                continue
            extra = {"import": ["UPD=a", "KEY=k"], "clean-all": ["CONFIRM=1"], "usb-add": ["FILES=a.wav"]}.get(target, [])
            code, out, err = self.make("-n", target, *extra)
            self.assertEqual(code, 0, err)
            for line in out.splitlines():
                words = shlex.split(line)
                self.assertEqual(words[1], "./sp16")
                name, rest = words[2], words[3:]
                self.assertIn(name, cli.BY_NAME)
                head, _ = cli.split_tail(rest)
                cli.BY_NAME[name].parser().parse_args(head)

    def test_install_and_uninstall_really_work(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = os.path.join(os.path.realpath(folder), "pre fix")
            self.assertEqual(self.make("install", f"PREFIX={prefix}")[0], 0)
            link = Path(prefix) / "bin" / "sp16"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.path.realpath(str(link)), os.path.realpath(str(MAKEFILE.parent / "sp16")))
            self.assertEqual(self.make("uninstall", f"PREFIX={prefix}")[0], 0)
            self.assertFalse(link.exists() or link.is_symlink())
            link.write_text("not mine")
            self.assertNotEqual(self.make("install", f"PREFIX={prefix}")[0], 0)
            self.assertEqual(link.read_text(), "not mine")
            self.assertIn("Nothing to remove", self.make("uninstall", f"PREFIX={prefix}")[1])
            self.assertEqual(link.read_text(), "not mine")


if __name__ == "__main__":
    unittest.main()
