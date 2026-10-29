#!/usr/bin/env python3
"""Firmware feature development loop for decoupled research builds (docs/architecture.md, M6).

A mod is a folder:
  mod.json     {"schema": 1, "id": "...", "base_profile": "sp16-1.60", "description": "..."}
  recipes/     *.json patch recipes (recipes.py format; base and result hashes, expected counts)
  files/       replacement or added files at their guest paths (your own content, never copied OEM files)
  src/         preload library sources (C), built by `fwdev.py sdk` into
  preload/     *.so ARM preload libraries, loaded after the hardware shim

Commands:
  new      scaffold an empty mod folder (mod.json, empty recipes/ and files/, an example preload source)
  check    preflight against the selected firmware's manifest, without booting
  run      start the decoupled build with the mod as a read-only 9p overlay, or restart only the OEM app in the
           running session so a changed recipe or file is visible in seconds
  test     check + run + verify the guest applied the overlay, then LCD/lamps/audio against a stock baseline
  sdk      build src/*.c into preload/*.so with the ARM cross toolchain (Docker is build tooling only)
  package  a mod archive (no OEM files), or with --upd a repacked, re-encrypted update for the emulator only (D3)
Nothing here flashes hardware or runs OEM update scripts.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import firmware  # noqa: E402
import profiles  # noqa: E402
import recipes  # noqa: E402

MOD_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
EXAMPLE_PRELOAD = r'''/* Example preload feature library for the SP-16 emulator (fwdev). It is loaded after the hardware shim
 * (LD_PRELOAD) inside the emulated firmware. Build: fwdev.py sdk --mod <this folder>. */
#include <stdio.h>

__attribute__((constructor)) static void sp16_mod_loaded(void) {
    fprintf(stderr, "[sp16-mod] %s loaded\n", "MOD_ID");
}
'''


class ModError(ValueError):
    pass


def load_mod(folder):
    folder = Path(folder).resolve()
    try:
        meta = json.loads((folder / "mod.json").read_text())
    except (OSError, ValueError):
        raise ModError(f"{folder} has no readable mod.json") from None
    if not isinstance(meta, dict) or meta.get("schema") != 1 or not MOD_ID.fullmatch(str(meta.get("id"))):
        raise ModError("mod.json needs schema 1 and an id of lowercase letters, digits and hyphens")
    profiles.load(meta.get("base_profile", ""))
    return folder, meta


def mod_recipes(folder):
    paths = sorted((Path(folder) / "recipes").glob("*.json"))
    return [(path, recipes.load_path(path)) for path in paths]


def mod_files(folder):
    """{guest path: host file} under files/ (regular files only)."""
    root = Path(folder) / "files"
    result = {}
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ModError(f"files/{path.relative_to(root)} is a symlink; use regular files")
            if path.is_file():
                result[recipes.guest_path("/" + str(path.relative_to(root)))] = path
    return result


# ------------------------------------------------------------------------------------------------------------- new

def new(folder, mod_id, base_profile=profiles.product()["reference_profile"]):
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise ModError(f"{folder} exists and is not empty")
    if not MOD_ID.fullmatch(mod_id):
        raise ModError("mod ids are lowercase letters, digits and hyphens")
    profiles.load(base_profile)
    for sub in ("recipes", "files", "src", "preload"):
        (folder / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    meta = {"schema": 1, "id": mod_id, "base_profile": base_profile, "description": ""}
    (folder / "mod.json").write_text(json.dumps(meta, indent=2) + "\n")
    (folder / "src" / f"{mod_id}.c").write_text(EXAMPLE_PRELOAD.replace("MOD_ID", mod_id))
    return {"status": "mod_created", "folder": str(folder.resolve()), "id": mod_id}


# ----------------------------------------------------------------------------------------------------------- check

def check(folder, library, firmware_id):
    """Preflight without booting: recipes against the firmware manifest's file hashes, overlay files, preloads."""
    folder, meta = load_mod(folder)
    manifest = library.manifest(firmware_id)
    status = firmware.boot_status(manifest, library)
    problems, notes = [], []
    if status["profile"] != meta["base_profile"]:
        problems.append(f"mod targets {meta['base_profile']}, firmware {firmware_id} has profile {status['profile']}")
    files = manifest["files"]
    oem_hashes = {item["sha256"] for item in files.values() if item.get("type") == "f"}
    overlay = mod_files(folder)
    planned = {}
    for path, host in overlay.items():
        digest = firmware.sha256_file(host)
        planned[path] = digest
        if digest in oem_hashes:
            problems.append(f"files{path} is an unmodified firmware file; overlays carry only your own content")
    for source, recipe in mod_recipes(folder):
        if recipe["base_profile"] != meta["base_profile"]:
            problems.append(f"{source.name}: base_profile {recipe['base_profile']} differs from mod.json")
        for path, spec in recipe["files"].items():
            current = planned.get(path) or (files.get(path) or {}).get("sha256")
            if current is None:
                problems.append(f"{source.name}: {path} is not a file of firmware {firmware_id}")
            elif current != spec["base_sha256"]:
                problems.append(f"{source.name}: {path} base SHA-256 does not match the firmware (wrong base)")
            elif spec.get("result_sha256"):
                planned[path] = spec["result_sha256"]
                notes.append(f"{source.name}: {path} -> {spec['result_sha256'][:12]}")
    for library_file in sorted((folder / "preload").glob("*.so")):
        head = library_file.read_bytes()[:20]
        if head[:4] != b"\x7fELF" or head[4] != 1 or int.from_bytes(head[18:20], "little") != 40:
            problems.append(f"preload/{library_file.name} is not a 32-bit ARM ELF shared object")
    return {"mod": meta["id"], "firmware": firmware_id, "ok": not problems, "problems": problems, "notes": notes,
            "recipes": [r["id"] for _, r in mod_recipes(folder)], "files": sorted(overlay),
            "preload": sorted(p.name for p in (folder / "preload").glob("*.so"))}


# ------------------------------------------------------------------------------------------------- running session

def native(*args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(TOOLS / "native.py"), *map(str, args)], capture_output=True, text=True,
                          env=env)


def app_executable(build):
    """The exact executable path of a build's app bundle (decoupled builds use the neutral name)."""
    import plistlib
    bundle = Path(json.loads((Path(build) / "assets.json").read_text())["app_bundle"])
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    return str(bundle / "Contents/MacOS" / info["CFBundleExecutable"])


def running_session(build, since=None):
    """(app pid, runtime dict) of the build's running session, or (None, None). Exact app path match only; with
    `since` (wall time) only a session created after it counts."""
    exe = app_executable(build)
    pids = [int(line.split(None, 1)[0]) for line in
            subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout.splitlines()
            if line.split(None, 1)[1:] and line.split(None, 1)[1].startswith(exe)]
    if not pids:
        return None, None
    return pids[0], latest_runtime(build, since)


def latest_runtime(build, since=None):
    """The newest session runtime.json of the build created after `since`, or None. The app writes that file as it
    starts, so a half-written or vanished one is skipped instead of raised: the session is simply not readable yet."""
    paths = sorted((path for path in (Path(build) / "sessions").glob("native-*/runtime.json")
                    if since is None or path.stat().st_mtime >= since - 1), key=lambda path: path.stat().st_mtime)
    for path in reversed(paths):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return None


class Socket:
    def __init__(self, runtime):
        self.runtime = runtime
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(40)
        deadline = time.monotonic() + 15
        while True:  # the app creates the socket shortly after the guest reports ready
            try:
                self.sock.connect(runtime["control_socket"])
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(.2)
        self.reader = self.sock.makefile("r")

    def call(self, **command):
        self.sock.sendall((json.dumps(command) + "\n").encode())
        reply = json.loads(self.reader.readline())
        if "error" in reply:
            raise ModError(reply["error"])
        return reply

    def snapshot(self, name):
        self.call(op="snapshot", name=name)
        return Path(self.runtime["session"]) / name

    def close(self):
        self.sock.close()


def lcd_digest(path, box=None):
    from PIL import Image
    image = Image.open(path).convert("RGB")
    return hashlib.sha256((image.crop(box) if box else image).tobytes()).hexdigest()


def wait_settled(sock, deadline, minimum_start=0.0, stable=1.0, name="fwdev-probe.png"):
    """Wait until the LCD has non-black content that stays unchanged for `stable` seconds; returns the settle time."""
    from PIL import Image
    last, since = None, None
    while time.monotonic() < deadline:
        path = sock.snapshot(name)
        image = Image.open(path).convert("RGB")
        lit = sum(1 for pixel in image.resize((80, 48)).getdata() if max(pixel) > 40)
        digest = hashlib.sha256(image.tobytes()).hexdigest()
        now = time.monotonic()
        if digest != last:
            last, since = digest, now
        elif lit > 200 and now - since >= stable and since >= minimum_start:
            return since
        time.sleep(.2)
    raise ModError("the LCD did not settle in time")


def console_since(runtime, offset, timeout=15.0):
    """The session's console.txt from `offset`, waiting up to `timeout` seconds for the file to appear: the session
    records one before the app has created its console, so a launch that just started can look for it too early. A
    console that shrank below `offset` was recreated by an app restart, so its whole text is new."""
    path = Path(runtime["session"]) / "console.txt"
    if offset < 0:
        raise ModError(f"console offset {offset} is negative")
    if not timeout > 0:
        raise ModError(f"console wait {timeout:g} s is not a positive timeout")
    deadline = time.monotonic() + timeout
    while True:
        try:
            data = path.read_text(errors="replace")
        except FileNotFoundError:
            if time.monotonic() > deadline:
                raise ModError(f"the app wrote no console.txt at {path} within {timeout:g} s: check that the session "
                               f"is still running and that the build launched the app") from None
            time.sleep(.1)
            continue
        return (data[offset:] if offset <= len(data) else data), len(data)


def same_overlay(runtime, folder):
    """The session's overlay is this mod folder. The app records a standardized path (/private/tmp/... becomes
    /tmp/...), so compare real paths."""
    recorded = runtime.get("dev_overlay")
    return bool(recorded) and os.path.realpath(recorded) == os.path.realpath(folder)


def run(build, folder, firmware_id=None, timeout=90):
    """Start the build with the mod overlay, or restart only the OEM app if that session is already running.
    Returns timings: restart_ms (socket round trip) and visible_s (command start -> LCD settled after restart)."""
    folder, meta = load_mod(folder)
    started = time.monotonic()
    pid, runtime = running_session(build)
    if pid and runtime and same_overlay(runtime, folder):
        sock = Socket(runtime)
        offset = console_since(runtime, 0)[1]
        reply = sock.call(op="app-restart")
        if not reply.get("ok"):
            raise ModError(f"app restart failed: {reply}")
        settled = wait_settled(sock, started + timeout, minimum_start=started + reply.get("ms", 0) / 1000)
        console, _ = console_since(runtime, offset)
        sock.close()
        return {"mode": "app-restart", "restart_ms": reply.get("ms"), "visible_s": round(settled - started, 2),
                "dev": [line for line in console.splitlines() if line.startswith("SP16_DEV")][-3:],
                "session": runtime["session"]}
    if pid:
        raise ModError("a session of this build is running without this mod overlay; stop it first (by its PID)")
    args = ["launch", "--output", build, "--dev-overlay", folder]
    if firmware_id:
        args += ["--firmware", firmware_id]
    wall = time.time()
    result = native(*args)
    if result.returncode:
        raise ModError("launch failed: " + (result.stderr or result.stdout)[-800:])
    deadline = started + timeout
    while time.monotonic() < deadline:
        pid, runtime = running_session(build, since=wall)
        if runtime and same_overlay(runtime, folder):
            try:
                console, _ = console_since(runtime, 0, timeout=2.0)
            except ModError:
                console = ""      # the app has not written its console yet; keep looking until the deadline
            if "SP16_NATIVE_READY" in console:
                break
            if "SP16_FIRMWARE_ERROR" in console:
                raise ModError("firmware refused: " + console[-500:])
        time.sleep(.2)
    else:
        raise ModError("session did not become ready")
    sock = Socket(runtime)
    settled = wait_settled(sock, deadline)
    console, _ = console_since(runtime, 0)
    sock.close()
    return {"mode": "launch", "visible_s": round(settled - started, 2), "pid": pid,
            "dev": [line for line in console.splitlines() if line.startswith("SP16_DEV")][-3:],
            "session": runtime["session"]}


def test(build, folder, library, firmware_id, baseline=None, expect_change=None, touch=None):
    """Preflight, run, then judge: the guest applied every recipe (SP16_DEV line, no SP16_DEV_ERROR), the declared
    result hash of the app if any, lamps live, and (with a stock baseline PNG) whether the LCD differs where expected."""
    report = {"check": check(folder, library, firmware_id)}
    if not report["check"]["ok"]:
        report["verdict"] = "FAIL (preflight)"
        return report
    report["run"] = run(build, folder, firmware_id)
    dev = report["run"]["dev"]
    errors = [line for line in dev if line.startswith("SP16_DEV_ERROR")]
    applied = [line for line in dev if line.startswith("SP16_DEV applied")]
    report["guest_applied"] = applied[-1] if applied else None
    expected = {}
    for _, recipe in mod_recipes(folder):
        for path, spec in recipe["files"].items():
            if spec.get("result_sha256"):
                expected[path] = spec["result_sha256"]
    app = expected.get(library.manifest(firmware_id)["app_path"])
    if app and applied and f"app_sha256={app}" not in applied[-1]:
        errors.append("application hash after the overlay differs from the recipe result")
    _, runtime = running_session(build)
    sock = Socket(runtime)
    metrics = sock.call(op="metrics")
    report["lamps_live"] = metrics.get("lamps_live")
    report["audio"] = {k: (metrics.get("audio") or {}).get(k) for k in ("guest_drops", "seq_gaps")} \
        if metrics.get("audio") else None
    if baseline and expect_change:
        x0, y0, x1, y1 = expect_change
        if touch:  # navigate to the screen the baseline shows (one LCD tap), then let it settle
            sock.call(op="touch", x=touch[0], y=touch[1], action=1)
            time.sleep(.1)
            sock.call(op="touch", x=touch[0], y=touch[1], action=2)
            wait_settled(sock, time.monotonic() + 20, minimum_start=time.monotonic() + .3, stable=.6)
        shot = sock.snapshot("fwdev-test.png")
        report["lcd_changed_in_region"] = lcd_digest(shot, (x0, y0, x1, y1)) != lcd_digest(baseline, (x0, y0, x1, y1))
        if not report["lcd_changed_in_region"]:
            errors.append("LCD region unchanged against the stock baseline")
    sock.close()
    report["errors"] = errors
    report["verdict"] = "PASS" if applied and not errors and report["lamps_live"] else "FAIL"
    return report


# ------------------------------------------------------------------------------------------------------------- sdk

SDK_DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc-arm-linux-gnueabi libc6-dev-armel-cross \\
 && rm -rf /var/lib/apt/lists/*
"""


def sdk(folder):
    """Build src/*.c into preload/*.so with the same ARM flags as the hardware shim (armv7-a, VFPv3, softfp: the
    firmware's EABI), linking only libdl and libgcc so the library binds to the firmware's own glibc at run time."""
    folder, meta = load_mod(folder)
    sources = sorted((folder / "src").glob("*.c"))
    if not sources:
        raise ModError("no src/*.c to build")
    work = Path(tempfile.mkdtemp(prefix="sp16-sdk-"))
    try:
        (work / "Dockerfile").write_text(SDK_DOCKERFILE)
        iid = work / "image.iid"
        result = subprocess.run(["docker", "build", "-q", "--iidfile", str(iid), str(work)], capture_output=True, text=True)
        if result.returncode:
            raise ModError("SDK image build failed: " + result.stderr[-500:])
        image = iid.read_text().strip()
        built = []
        try:
            for source in sources:
                name = source.stem + ".so"
                command = ["docker", "run", "--rm", "--network", "none",
                           "--mount", f"type=bind,src={folder / 'src'},dst=/src,readonly",
                           "--mount", f"type=bind,src={folder / 'preload'},dst=/out",
                           image, "arm-linux-gnueabi-gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-fPIC", "-shared",
                           "-nostdlib", "-march=armv7-a", "-mfpu=vfpv3", "-mfloat-abi=softfp", "-fno-stack-protector",
                           f"/src/{source.name}", "-Wl,--no-as-needed", "-l:libdl.so.2", "-lgcc",
                           "-o", f"/out/{name}"]
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode:
                    raise ModError(f"{source.name}: " + result.stderr[-800:])
                built.append(name)
        finally:
            subprocess.run(["docker", "image", "rm", image], capture_output=True)
        return {"status": "sdk_built", "preload": built}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------------------------------------- package

def deterministic_tar(entries, out_path):
    """entries: [(archive name, bytes, mode)] -> a gzip tar with fixed mtimes, owners and order."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data, mode in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    with open(out_path, "xb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0, compresslevel=9) as stream:
            stream.write(buffer.getvalue())


def package(folder, library, firmware_id, out):
    """Mod archive: mod.json, recipes, preload sources and binaries, own overlay files and a package manifest with
    the base identity. Unmodified firmware files are refused, so the archive carries no OEM file."""
    folder, meta = load_mod(folder)
    report = check(folder, library, firmware_id)
    if not report["ok"]:
        raise ModError("preflight failed: " + "; ".join(report["problems"]))
    manifest = library.manifest(firmware_id)
    entries, hashes = [], {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = str(path.relative_to(folder))
            if rel.split("/")[0] not in ("mod.json", "recipes", "files", "src", "preload"):
                continue
            data = path.read_bytes()
            entries.append((f"{meta['id']}/{rel}", data, 0o644 if not rel.startswith("preload/") else 0o755))
            hashes[rel] = hashlib.sha256(data).hexdigest()
    package_manifest = {"schema": 1, "mod": meta["id"], "base_profile": meta["base_profile"],
                        "base_app_sha256": manifest["app_sha256"], "base_update_sha256": manifest["update"]["sha256"],
                        "recipes": {r["id"]: recipes.recipe_sha256(r) for _, r in mod_recipes(folder)},
                        "files": hashes}
    entries.append((f"{meta['id']}/package.json", (json.dumps(package_manifest, indent=2, sort_keys=True) + "\n").encode(),
                    0o644))
    out = Path(out)
    deterministic_tar(entries, out)
    os.chmod(out, 0o600)
    return {"status": "mod_packaged", "archive": str(out), "sha256": firmware.sha256_file(out), "entries": len(entries)}


# --------------------------------------------------------------------------------------------------------- command

def build_library(build):
    sys.path.insert(0, str(TOOLS))
    import native as native_module
    assets, boot = native_module.base_boot(build)
    return assets, boot, firmware.Library(assets["library"])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("new")
    command.add_argument("--mod", required=True)
    command.add_argument("--id", required=True)
    for name in ("check", "run", "test", "package"):
        command = commands.add_parser(name)
        command.add_argument("--output", required=True, help="decoupled research build")
        command.add_argument("--mod", required=True)
        command.add_argument("--firmware", help="firmware id (default: the library's selected firmware)")
        if name == "test":
            command.add_argument("--baseline", help="stock LCD snapshot PNG to compare against")
            command.add_argument("--expect-change", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"))
            command.add_argument("--touch", nargs=2, type=int, metavar=("X", "Y"),
                                 help="tap the LCD here before the comparison snapshot")
        if name == "package":
            command.add_argument("--out", required=True)
            command.add_argument("--upd", action="store_true", help="repacked, re-encrypted update (emulator only)")
            command.add_argument("--key-file")
            command.add_argument("--gpl-archive", nargs="+")
    command = commands.add_parser("sdk")
    command.add_argument("--mod", required=True)
    args = parser.parse_args()
    try:
        if args.command == "new":
            result = new(args.mod, args.id)
        elif args.command == "sdk":
            result = sdk(args.mod)
        else:
            assets, boot, library = build_library(args.output)
            firmware_id = args.firmware or library.selected()
            if not firmware_id:
                raise ModError("no firmware selected; import one with native.py firmware import")
            if args.command == "check":
                result = check(args.mod, library, firmware_id)
            elif args.command == "run":
                result = run(Path(args.output).resolve(), args.mod, firmware_id)
            elif args.command == "test":
                result = test(Path(args.output).resolve(), args.mod, library, firmware_id, args.baseline,
                              args.expect_change, args.touch)
            elif args.upd:
                import fwpack
                key = firmware.load_key(args.key_file, args.gpl_archive)
                try:
                    result = fwpack.package_update(library, boot, firmware_id, args.mod, key, args.out)
                finally:
                    del key
            else:
                result = package(args.mod, library, firmware_id, args.out)
    except (ModError, firmware.FirmwareError, ValueError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) and result.get("verdict", "PASS") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
