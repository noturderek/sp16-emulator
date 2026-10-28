#!/usr/bin/env python3
"""Build once, then launch the native macOS SP-16 app without Docker/browser."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

from prepare import private_path
import diagnostics
import firmware
import profiles

TOOLS = Path(__file__).resolve().parent
QEMU_VERSION = "10.2.1"
QEMU_ARCHIVE_SHA256 = "a3717477d8e2c84d630bfffbc20f6cd3293eb45aa1e6dac6d0cc27689991c9e1"
APP_NAME = "SP16 Emulator.app"
BASE_GUEST_FILES = ("Dockerfile.native-base", "hardware_stub.c", "guest-init.sh", "guest-bridge.py", "pack-guest.sh",
                    "protocol.py", "panel.py", "panel_map.py", "input.py", "enter.py", "profiles.py", "recipes.py",
                    "prepare.py", "guest-firmware.py", "midi/seq-bridge.c", "profiles/sp16-1.60.json",
                    "profiles/products/sp16.json")
SWIFT_SOURCES = ("native-window.swift", "panel-view.swift", "data-image.swift", "audio-sink.swift",
                "audio-output.swift", "firmware-library.swift", "firmware-window.swift", "app-diagnostics.swift")
ICON_TOOL = "make-icon.swift"
# Vendor logo files and the old icon name. The public copy never ships them: the distribution scan (native.py
# firmware audit) refuses their names and, if any is present, their content.
VENDOR_LOGO_FILES = ("assets/legacy-logo.svg", "assets/legacy-logo-2.svg", "assets/legacy-logo-3.svg")
OLD_ICON_NAME = "sp16-emulator"
PROFILES = ("fullpanel",)
# Decoupled builds (docs/architecture.md M7, design decision D4): neutral product name and bundle id, no vendor
# logos, a project-made icon, the firmware library tool and third-party notices.
DECOUPLED_APP_NAME = "SP-16 Compatible Emulator.app"
DECOUPLED_PRODUCT = "SP-16 Compatible Emulator"
DECOUPLED_EXECUTABLE = "SP16Emulator"
DECOUPLED_BUNDLE_ID = "local.sp16-compatible.emulator"
DECOUPLED_RESOURCES = ("panel-layout.json", "qemu_reaper.py")
NEUTRAL_ICON = "assets/neutral-pads.svg"
DECOUPLED_ICON_FILE = "sp16-compatible-emulator"
FWLIB_FILES = ("firmware.py", "profiles.py", "recipes.py", "fwpack.py", "diagnostics.py")

# Optional per-task suffix (e.g. a task id) so a dev build gets its own bundle ID and quitting one app by ID
# never quits another. Without it the bundle IDs are unchanged.
BUNDLE_SUFFIX_PATTERN = re.compile(r"[a-z0-9-]{1,32}")
# The pad calibration file (path, format, accepted range, default) is firmware data from the profile. The 1.60
# profile's (JUCE ValueTree XML /root/data/padcalibration.xml, 48..504, default 242: the firmware's own default knee
# level, at which its pressure mapping is the identity). Decoupled builds pass the choice on the kernel command line
# and the guest stages it in the active profile's format. The synthetic (not factory) value keeps the firmware out of
# its uncalibrated pad-blink state; --uncalibrated reproduces the stock behaviour for research.
_REFERENCE_CALIBRATION = profiles.legacy()["calibration"]
DEFAULT_PAD_CALIBRATION = _REFERENCE_CALIBRATION["default"]


def resolve_pad_calibration(profile, value=None, uncalibrated=False):
    """Calibration value to stage for a build, or None. Only the full-panel profile carries one."""
    if profile != "fullpanel":
        if value is not None or uncalibrated:
            raise ValueError("--pad-calibration-value and --uncalibrated apply to the fullpanel profile only")
        return None
    if uncalibrated and value is not None:
        raise ValueError("--uncalibrated and --pad-calibration-value are mutually exclusive")
    if uncalibrated:
        return None
    return DEFAULT_PAD_CALIBRATION if value is None else value


def validate_bundle_suffix(token):
    """Return the suffix unchanged when it is 1-32 lowercase letters, digits or hyphens; None passes through."""
    if token is None:
        return None
    if not isinstance(token, str) or not BUNDLE_SUFFIX_PATTERN.fullmatch(token):
        raise ValueError("bundle suffix must be 1-32 lowercase letters, digits or hyphens")
    return token


def pad_calibration_xml(value, calibration=None):
    """The calibration file in a profile's own format (default: the 1.60 reference profile)."""
    calibration = calibration or _REFERENCE_CALIBRATION
    low, high = calibration["range"]
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"pad calibration value must be an integer in {low}..{high}")
    attributes = " ".join(calibration["attribute"].format(n=n, value=value) for n in range(1, calibration["pads"] + 1))
    return calibration["template"].format(attributes=attributes)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command, *, cwd=None, log=None, env=None):
    if log:
        with Path(log).open("wb") as output:
            result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, env=env)
        if result.returncode:
            raise RuntimeError(f"build step failed ({result.returncode}); inspect {log}")
    else:
        subprocess.run(command, cwd=cwd, check=True, env=env)


def apple_binutils_env():
    """Build environment that pins Apple's archiver tools. GNU binutils on PATH (Homebrew `binutils`) write archives
    Apple's linker rejects ("64-bit mach-o not 8-byte aligned"), which breaks the QEMU link."""
    env = dict(os.environ)
    for variable, tool in (("AR", "ar"), ("NM", "nm"), ("RANLIB", "ranlib"), ("STRIP", "strip")):
        if Path("/usr/bin", tool).exists():
            env[variable] = f"/usr/bin/{tool}"
    return env


def host_check():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("this native HVF build currently requires an Apple Silicon Mac")


def qemu_source_filter(member, path):
    """tarfile's "data" filter, except that a member it refuses as an absolute symlink is skipped instead of failing
    the whole extraction (the QEMU archive has one, under roms/edk2, which this build never uses)."""
    try:
        return tarfile.data_filter(member, path)
    except tarfile.AbsoluteLinkError:
        return None


def build_qemu(output):
    archive = output / f"qemu-{QEMU_VERSION}.tar.xz"
    if not archive.exists():
        print("Downloading pinned official QEMU source…", flush=True)
        with urllib.request.urlopen(f"https://download.qemu.org/{archive.name}", timeout=60) as source:
            with archive.open("xb") as target:
                shutil.copyfileobj(source, target)
    if digest(archive) != QEMU_ARCHIVE_SHA256:
        raise ValueError("QEMU source archive SHA-256 mismatch")
    source = output / f"qemu-{QEMU_VERSION}"
    if not source.exists():
        # Extract next to the target and rename, so an interrupted extraction is never mistaken for a source tree.
        partial = output / f".qemu-{QEMU_VERSION}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir(mode=0o700)
        with tarfile.open(archive) as package:
            package.extractall(partial, filter=qemu_source_filter)
        (partial / source.name).rename(source)
        # Not rmdir: Finder can drop a .DS_Store into the folder meanwhile, and the source tree is already in place.
        shutil.rmtree(partial, ignore_errors=True)
    build = output / "qemu-build"
    build.mkdir(mode=0o700, exist_ok=True)
    if not (build / "build.ninja").exists():
        flags = ["--target-list=aarch64-softmmu", "--enable-hvf", "--disable-pvg",
                 "--disable-docs", "--disable-tools", "--disable-guest-agent", "--disable-cocoa",
                 "--disable-sdl", "--disable-gtk", "--disable-opengl", "--disable-vnc",
                 "--disable-slirp", "--disable-libssh", "--disable-gnutls", "--disable-nettle",
                 "--disable-gcrypt", "--disable-linux-user", "--disable-bsd-user", "--disable-werror"]
        run([str(source / "configure"), *flags], cwd=build, log=output / "qemu-configure.log", env=apple_binutils_env())
    else:
        run([str(build / "pyvenv/bin/meson"), "configure", "-Dpvg=disabled"], cwd=build,
            log=output / "qemu-configure-refresh.log")
    print("Building native QEMU with Hypervisor.framework…", flush=True)
    run(["ninja", "-j", str(min(12, os.cpu_count() or 4)), "qemu-system-aarch64"],
        cwd=build, log=output / "qemu-build.log")
    return build / "qemu-system-aarch64"


def remove_guest_image(image):
    # Untagged per-build image: removing it by ID never touches another build's tag.
    # A failure (for example, the image is still used by another container) is logged, not fatal.
    result = subprocess.run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        print(f"warning: could not remove guest image {image[:19]}: {result.stderr.strip()}",
              file=sys.stderr, flush=True)


def build_guest(output):
    print("Packaging offline Linux guest (Docker is build tooling only)…", flush=True)
    # No shared tag: --iidfile records this build's image ID, so parallel builds cannot
    # package each other's guest code.
    iid_dir = Path(tempfile.mkdtemp(prefix="iid-", dir=output))
    iidfile = iid_dir / "image.iid"
    image = None
    try:
        run(["docker", "build", "-f", str(TOOLS / "Dockerfile.native-base"),
             "--iidfile", str(iidfile), str(TOOLS)], log=output / "guest-build.log")
        built = iidfile.read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", built):
            raise ValueError("docker build returned an invalid image identity")
        image = built
        return package_guest(output, image)
    finally:
        if image:
            remove_guest_image(image)
        shutil.rmtree(iid_dir, ignore_errors=True)


def package_guest(output, image):
    guest = Path(tempfile.mkdtemp(prefix="guest-", dir=output))
    container = None
    try:
        container = subprocess.check_output(["docker", "create", "--network", "none",
            "--mount", f"type=bind,src={guest},dst=/output", image], text=True).strip()
        if len(container) != 64 or any(c not in "0123456789abcdef" for c in container):
            raise ValueError("invalid packaging container identity")
        run(["docker", "start", "-a", container], log=output / "guest-pack.log")
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, check=False)
    if not (guest / "guest.cpio.gz").is_file() or not (guest / "kernel").is_file():
        raise ValueError("guest packer did not produce both boot assets")
    return guest


TRACE_MODES = ("strace", "gdb", "pcmtap", "exec")
APP_ARG_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")


# Persistent per-profile eMMC image (fullpanel): <output>/data/emmc.img, sparse raw, formatted once by the guest.
DATA_DIRECTORY = "data"
DATA_IMAGE_SIZE = 1 << 30
DATA_PLACEHOLDER = "@DATA@"


AUDIO_MODES = ("file", "coreaudio", "both")
# Decoupled builds (docs/architecture.md, M3): the app fills these from the selected firmware's manifest after
# checking its profile and image hash; values are validated hex hashes, a d.dd version and a profile token.
FIRMWARE_PLACEHOLDERS = ("@FWIMAGE@", "@FWSHA@", "@FWBYTES@", "@FWAPP@", "@FWBASE@", "@FWVER@", "@FWAPPPATH@")
PADCAL_PATTERN = re.compile(r"default|off|[0-9]{1,5}")


def arguments(qemu, guest, socket, trace=None, data=False, audio=None, firmware=False, padcal=None, app_arg=None):
    if audio is not None and audio not in AUDIO_MODES:
        raise ValueError("unsupported audio mode")
    if trace is not None and trace not in TRACE_MODES:
        raise ValueError("unsupported trace mode")
    persistent = ["-drive", f"file={DATA_PLACEHOLDER},format=raw,if=none,id=sp16emmc",
                  "-device", "virtio-blk-device,drive=sp16emmc,serial=sp16emmc",
                  "-chardev", f"socket,id=sp16ctl,path={socket}.ctl,server=on,wait=off",
                  "-device", "virtserialport,chardev=sp16ctl,name=sp16.ctl"] if data else []
    # Audio transport: a dedicated virtio-serial port carrying playback frames to the host file sink.
    pcm = ["-chardev", f"socket,id=sp16pcm,path={socket}.pcm,server=on,wait=off",
           "-device", "virtserialport,chardev=sp16pcm,name=sp16.pcm"] if audio else []
    # Full-duplex DIN MIDI UART, separate from the panel and factory-test loopback.
    din = ["-chardev", f"socket,id=sp16din,path={socket}.midi,server=on,wait=off",
           "-device", "virtserialport,chardev=sp16din,name=sp16.din"]
    usb_midi = ["-chardev", f"socket,id=sp16usbmidi,path={socket}.usb-midi,server=on,wait=off",
                "-device", "virtserialport,chardev=sp16usbmidi,name=sp16.usb-midi"]
    # Research gdb bridge (--trace gdb): a dedicated virtio-serial port so the guest can tunnel the qemu-user
    # gdbstub (127.0.0.1:1234 in the guest) out to the host, since the guest has no NIC beyond lo. The host end
    # is the unix socket {socket}.gdb; the guest-init forwarder bridges the vport to the in-guest stub, and a tiny
    # host bridge (emulator/gdb-bridge.py) re-exposes {socket}.gdb as TCP for lldb's `gdb-remote`.
    gdb = ["-chardev", f"socket,id=sp16gdb,path={socket}.gdb,server=on,wait=off",
           "-device", "virtserialport,chardev=sp16gdb,name=sp16.gdb"] if trace == "gdb" else []
    if app_arg is not None and not APP_ARG_PATTERN.fullmatch(app_arg):
        raise ValueError("app argument must be 1 to 32 characters of a-z, 0-9, _ and -")
    if padcal is not None and (not firmware or not PADCAL_PATTERN.fullmatch(str(padcal))):
        raise ValueError("padcal is default, off or a number, for decoupled builds only")
    fw = ["-drive", "file=@FWIMAGE@,format=raw,if=none,id=sp16fw,readonly=on",
          "-device", "virtio-blk-device,drive=sp16fw,serial=sp16fw"] if firmware else []
    return ["-machine", "virt,gic-version=3", "-cpu", "host", "-accel", "hvf",
            "-smp", "4", "-m", "4096", "-nodefaults", "-display", "none",
            "-monitor", "none", "-serial", "stdio", "-nic", "none", "-no-reboot",
            "-kernel", str(guest / "kernel"), "-initrd", str(guest / "guest.cpio.gz"),
            "-append", "console=ttyAMA0 rdinit=/init panic=-1 oops=panic random.trust_cpu=on"
                       + (f" sp16.trace={trace}" if trace else "")
                       + (" sp16.data=1" if data else "")
                       + (" sp16.audio=file" if audio else "")
                       + (" sp16.fw=@FWSHA@ sp16.fwbytes=@FWBYTES@ sp16.fwapp=@FWAPP@ sp16.fwbase=@FWBASE@"
                          " sp16.fwver=@FWVER@ sp16.app_path=@FWAPPPATH@" if firmware else "")
                       + (f" sp16.padcal={padcal}" if firmware and padcal is not None else "")
                       + (f" sp16.app={app_arg}" if app_arg else ""),
            "-device", "virtio-serial-device",
            "-chardev", f"socket,id=sp16,path={socket},server=on,wait=off",
            "-device", "virtserialport,chardev=sp16,name=sp16.ui", *fw, *persistent, *pcm, *din, *usb_midi, *gdb]


def shipped_profile(profile):
    """A copy of a profile for distribution: only what the D2 check, the guest runtime and the panel need (no update
    hash, no verification notes, no hardware-suite probes)."""
    copy = {key: json.loads(json.dumps(profile[key])) for key in profiles.RUNTIME_FIELDS if key in profile}
    copy["firmware"].pop("upd_sha256", None)
    return profiles.validate(copy)


def third_party_notices(guest):
    """THIRD-PARTY-NOTICES.md for a decoupled bundle: QEMU from pinned source, the Debian guest packages (list and
    copyright files collected by pack-guest.sh) and how to obtain their sources."""
    packages = (guest / "third-party-packages.txt").read_text() if (guest / "third-party-packages.txt").exists() else ""
    kernel = next((line.split()[1] for line in packages.splitlines() if line.startswith("linux-image-")), "unknown")
    return f"""# Third-party notices

{DECOUPLED_PRODUCT} contains no firmware, firmware-derived files or key material. The user imports their own update.

## QEMU {QEMU_VERSION}

`qemu-system-aarch64`, built from the official source archive `https://download.qemu.org/qemu-{QEMU_VERSION}.tar.xz`
(SHA-256 `{QEMU_ARCHIVE_SHA256}`) with Hypervisor.framework support. QEMU is licensed under the GNU General Public
License version 2; see `COPYING` in that archive. The configure options are recorded in `native.py` (`build_qemu`).

## Guest base image (Debian 12 "bookworm", arm64)

The guest kernel (Debian `linux-image-arm64`, kernel package {kernel}) and initramfs are built from Debian packages
plus this project's own scripts and hardware shim. `ThirdParty/packages.txt` lists every package with its version and
source package; `ThirdParty/copyright.txt` holds each package's copyright and licence file. The corresponding sources
are the Debian source packages of those exact versions (`https://snapshot.debian.org/`, `apt-get source`).

## Redistributing this app

This app was built on your own Mac from the project's source code. If you give a copy of it to anyone else, you
distribute QEMU and the GPL-licensed Debian packages listed above and take on their licence obligations, for example by
supplying their complete corresponding source code (the QEMU archive and the Debian source packages named above)
together with the app.
"""


def build_icon(output, resources, logo=NEUTRAL_ICON, name=DECOUPLED_ICON_FILE):
    """Render the app icon (make-icon.swift draws every size) and package it as Resources/<name>.icns."""
    work = output / "icon-build"
    iconset = work / f"{name}.iconset"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700)
    tool = work / "make-icon"
    run(["swiftc", "-O", "-framework", "AppKit", str(TOOLS / ICON_TOOL), "-o", str(tool)], log=output / "icon-build.log")
    run([str(tool), str(TOOLS / logo), str(iconset)], log=output / "icon-render.log")
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / f"{name}.icns")], log=output / "icon-pack.log")


def bundle(output, qemu, guest, variant="stock", profile=None, research=False, trace=None, bundle_suffix=None,
           persistent=False, audio=None, audio_device=None, library=None, padcal=None, app_arg=None):
    app = output / DECOUPLED_APP_NAME
    if (output / APP_NAME).exists():
        shutil.rmtree(output / APP_NAME)  # a bundle an earlier version of this tool left in the same build folder
    executable = app / "Contents/MacOS" / DECOUPLED_EXECUTABLE
    executable.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resources = app / "Contents/Resources"
    shutil.rmtree(resources, ignore_errors=True)  # start clean: nothing from an earlier build survives a rebuild
    resources.mkdir(mode=0o700, exist_ok=True)
    run(["swiftc", "-O", "-parse-as-library", "-framework", "AppKit", "-framework", "AVFoundation", "-framework", "CoreAudio",
         *(str(TOOLS / name) for name in SWIFT_SOURCES), "-o", str(executable)],
        log=output / "swift-build.log")
    for name in DECOUPLED_RESOURCES:
        shutil.copy2(TOOLS / name, resources / Path(name).name)
    # The app checks the selected firmware against these profiles before boot (D2); the firmware library tool
    # (import screen, library window) is the same code as `native.py firmware`.
    fwlib = resources / "fwlib"
    shutil.rmtree(fwlib, ignore_errors=True)
    for folder in (resources / "profiles", fwlib, fwlib / "profiles"):
        shutil.rmtree(folder, ignore_errors=True) if folder == resources / "profiles" else None
        folder.mkdir(mode=0o700, exist_ok=True)
    for source in sorted((TOOLS / "profiles").glob("*.json")):
        text = json.dumps(shipped_profile(profiles.load(source.stem)), indent=2, sort_keys=True) + "\n"
        (resources / "profiles" / source.name).write_text(text)
        (fwlib / "profiles" / source.name).write_text(text)
    for name in FWLIB_FILES:
        shutil.copy2(TOOLS / name, fwlib / name)
    (fwlib / "profiles/products").mkdir(mode=0o700)
    for source in sorted((TOOLS / "profiles/products").glob("*.json")):
        profiles.product(source.stem)
        shutil.copy2(source, fwlib / "profiles/products" / source.name)
    # Where this app came from, for bug reports (Help > Collect Diagnostics reads it).
    (resources / "build-info.json").write_text(json.dumps(
        {"git_commit": diagnostics.git_commit(TOOLS),
         "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=2) + "\n")
    notices = resources / "ThirdParty"
    shutil.rmtree(notices, ignore_errors=True)
    notices.mkdir(mode=0o700)
    (resources / "THIRD-PARTY-NOTICES.md").write_text(third_party_notices(guest))
    for source, name in (("third-party-packages.txt", "packages.txt"), ("third-party-copyright.txt", "copyright.txt")):
        if (guest / source).exists():
            shutil.copy2(guest / source, notices / name)
    build_icon(output, resources)
    info = {"CFBundleIdentifier": DECOUPLED_BUNDLE_ID, "CFBundleName": DECOUPLED_PRODUCT,
            "CFBundleExecutable": DECOUPLED_EXECUTABLE, "CFBundlePackageType": "APPL",
            "CFBundleIconFile": DECOUPLED_ICON_FILE, "CFBundleShortVersionString": "0.1",
            "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "13.0"}
    if profile:
        info["CFBundleIdentifier"] += "." + profile
    if bundle_suffix:
        info["CFBundleIdentifier"] += "." + bundle_suffix
        info["CFBundleName"] += " " + bundle_suffix
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    sessions = output / "sessions"
    sessions.mkdir(mode=0o700, exist_ok=True)
    # QEMU starts through a small wrapper that adds the NIC `./sp16 run --net` asks for (no-op without it).
    wrapper = output / NET_WRAPPER
    shutil.copy2(TOOLS / "net" / NET_WRAPPER, wrapper)
    wrapper.chmod(0o700)
    # The mcast wrapper spawns this relay next to itself (see net/qemu-net.sh).
    relay = output / NET_RELAY
    shutil.copy2(TOOLS / "net" / NET_RELAY, relay)
    relay.chmod(0o700)
    defaults = {"qemu": str(wrapper),
                "arguments": arguments(qemu, guest, "@SOCKET@", trace, persistent, audio, True, padcal, app_arg),
                "session": str(sessions), "socket": "@SOCKET@", "variant": variant,
                "profile": profile or "", "research": research}
    if audio:
        defaults["audio"] = audio + (":" + audio_device if audio_device else "")
    if persistent:
        defaults["data"] = {"dir": str(output / DATA_DIRECTORY), "size": DATA_IMAGE_SIZE}
    # Data images are per firmware identity: <library>/<id>/data (the app replaces data.dir at launch).
    defaults["firmware"] = {"library": str(library)}
    (resources / "default-config.json").write_text(json.dumps(defaults, indent=2) + "\n")
    run(["codesign", "--force", "--sign", "-", str(app)], log=output / "app-sign.log")
    return app


def validated_assets(output):
    assets = json.loads((output / "assets.json").read_text())
    if assets.get("decoupled") is not True:
        raise ValueError("unsupported build folder (made by an older build); rebuild it with native.py build")
    return validated_base_assets(output, assets)


def validated_base_assets(output, assets):
    """A decoupled build: QEMU, base kernel and base initramfs hashes; no OEM application is part of the build."""
    if "application" in assets or assets.get("firmware") is not None:
        raise ValueError("decoupled build assets must not name a firmware application")
    required = (Path(assets["qemu"]), Path(assets["guest"]) / "kernel", Path(assets["guest"]) / "guest.cpio.gz")
    if not all(str(path) in assets["hashes"] for path in required):
        raise ValueError("native boot asset hashes are incomplete")
    for name, expected in assets["hashes"].items():
        path = Path(name)
        if not path.resolve().is_relative_to(output.resolve()) or digest(path) != expected:
            raise ValueError("native boot asset identity changed; rebuild")
    return assets


def base_boot(output):
    """QEMU and base guest paths of a validated decoupled build, for the import VM."""
    assets = validated_assets(private_path(output))
    guest = Path(assets["guest"])
    return assets, {"qemu": assets["qemu"], "kernel": str(guest / "kernel"), "initrd": str(guest / "guest.cpio.gz"),
                    "initrd_sha256": assets["hashes"][str(guest / "guest.cpio.gz")]}


def build(output, *, reuse_qemu_from=None, profile="fullpanel", research=False, pad_calibration=None,
          uncalibrated=False, trace=None, bundle_suffix=None, audio=None, audio_device=None, library=None,
          allow_unverified=False, app_arg=None):
    """Decoupled build (docs/architecture.md, M3): the app bundle, QEMU and a guest base image with no OEM files.
    Firmware comes from the per-build library (native.py firmware import) and is attached at launch read-only."""
    bundle_suffix = validate_bundle_suffix(bundle_suffix)
    if profile != "fullpanel":
        raise ValueError("the build supports --profile fullpanel only")
    if trace and not research:
        raise ValueError("--trace is research-only; add --research")
    if app_arg and not research:
        raise ValueError("--app-arg is research-only; add --research")
    if app_arg and not APP_ARG_PATTERN.fullmatch(app_arg):
        raise ValueError("--app-arg must be 1 to 32 characters of a-z, 0-9, _ and -")
    # allow_unverified is accepted and ignored: every correctly keyed firmware boots, marked unverified when no
    # verified profile matches (D2 in docs/architecture.md).
    if audio_device is not None and audio not in ("coreaudio", "both"):
        raise ValueError("--audio-device needs --audio coreaudio or both")
    if audio_device is not None and (not audio_device or len(audio_device) > 512
                                     or any(ord(c) < 32 for c in audio_device)):
        raise ValueError("--audio-device must be a device UID (or offline:<wav>[:rate[:ppm[:block]]])")
    if uncalibrated and pad_calibration is not None:
        raise ValueError("--uncalibrated and --pad-calibration-value are mutually exclusive")
    # The range and format are the active firmware profile's; the guest validates the value at boot.
    padcal = "off" if uncalibrated else ("default" if pad_calibration is None else str(int(pad_calibration)))
    host_check()
    output = private_path(output, fresh=not Path(output).exists())
    if (output / "assets.json").exists():
        previous = json.loads((output / "assets.json").read_text())
        if previous.get("decoupled") is not True or previous.get("profile") != profile:
            raise ValueError("existing output belongs to a different build")
    output.mkdir(mode=0o700, exist_ok=True)
    if "," in str(output):
        raise ValueError("build path must not contain commas")
    # Absolute: the app is started by LaunchServices with / as its working directory, so a relative path breaks.
    library = Path(os.path.abspath(library)) if library else output / "library"
    firmware.Library(library, create=True)
    if reuse_qemu_from:
        previous = validated_assets(private_path(reuse_qemu_from))
        if previous.get("qemu_version") != QEMU_VERSION:
            raise ValueError("reused QEMU must match the pinned version")
        qemu = output / "qemu-system-aarch64"
        if Path(previous["qemu"]).resolve() != qemu.resolve():
            shutil.copy2(previous["qemu"], qemu)
        if digest(qemu) != previous["hashes"][previous["qemu"]]:
            raise ValueError("reused QEMU copy differs")
    else:
        qemu = build_qemu(output)
    guest = build_guest(output)
    app = bundle(output, qemu, guest, "stock", profile, research, trace, bundle_suffix, True, audio, audio_device,
                 library=library, padcal=padcal, app_arg=app_arg)
    assets = {"decoupled": True, "firmware": None, "qemu_version": QEMU_VERSION,
              "app_bundle": str(app), "qemu": str(qemu), "guest": str(guest), "library": str(library),
              "profile": profile, "research": research, "trace": trace or "", "app_arg": app_arg or "", "audio": audio or "",
              "audio_device": audio_device or "", "pad_calibration": padcal, "persistent_data": True,
              "hashes": {str(p): digest(p) for p in (qemu, guest / "kernel", guest / "guest.cpio.gz")},
              "guest_source_hashes": {name: digest(TOOLS / name) for name in BASE_GUEST_FILES}}
    (output / "assets.json").write_text(json.dumps(assets, indent=2) + "\n")
    for stale in output.glob("guest-*"):
        if stale.is_dir() and stale.resolve() != guest.resolve():
            shutil.rmtree(stale)
    print(json.dumps({"status": "native_app_built", "decoupled": True, "app": str(app), "library": str(library),
                      "runtime_requires_docker": False}))


USB_IMAGE_SIZE = 1 << 30


def prepare_usb_image(path):
    """Resolve the virtual USB stick image for `launch --usb-image`: an absolute regular file owned by the user.
    A missing file is created sparse (1 GiB, 0600); it stays blank and the guest's trusted init formats it as FAT32
    on first attach (the OEM never formats anything)."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if "," in str(path):
        raise ValueError("USB image path must not contain a comma")
    if path.is_symlink():
        raise ValueError("USB image must not be a symlink")
    if not path.exists():
        if not path.parent.is_dir():
            raise ValueError(f"USB image directory {path.parent} does not exist")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.truncate(USB_IMAGE_SIZE)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("USB image must be a regular file owned by you")
    return str(path)


def dev_overlay_path(folder):
    """A developer overlay folder for --dev-overlay: an existing directory you own, no symlinks, no commas."""
    path = Path(os.path.abspath(folder))
    if "," in str(path) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("--dev-overlay must not contain commas or symlinks")
    info = path.lstat() if path.exists() else None
    if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("--dev-overlay must be an existing folder owned by you")
    return str(path)


NET_WRAPPER = "qemu-net.sh"
NET_RELAY = "segment_relay.py"
NET_IFACE = re.compile(r"[a-z]{1,8}[0-9]{1,3}")
NET_MCAST_GROUP = "230.0.0.1"
NET_MCAST_PORT = 34567
VMNET_DIRECTORIES = ("/opt/homebrew/var/run", "/usr/local/var/run", "/var/run")
NET_MODES = "none, host, bridged:IF (for example bridged:en0) or mcast[:PORT]"
NET_IP = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?")


def vmnet_socket_path(name):
    """The socket_vmnet daemon socket called `name` (Homebrew's service: <prefix>/var/run/socket_vmnet), or None."""
    for directory in VMNET_DIRECTORIES:
        candidate = Path(directory) / name
        if candidate.is_socket():
            return str(candidate)
    return None


def validate_net_ip(net_ip):
    """--net-ip: a.b.c.d or a.b.c.d/prefix, octets 0..255 and prefix 0..32; returned normalised with a /24 when bare."""
    if net_ip is None:
        return None
    if not NET_IP.fullmatch(net_ip):
        raise ValueError("--net-ip must be a.b.c.d[/prefix], e.g. 192.168.1.50/24")
    addr, _, prefix = net_ip.partition("/")
    if any(not octet.isdigit() or int(octet) > 255 for octet in addr.split(".")):
        raise ValueError("--net-ip has an octet above 255")
    if prefix and (not prefix.isdigit() or int(prefix) > 32):
        raise ValueError("--net-ip prefix must be 0..32")
    return addr + "/" + (prefix or "24")


def net_environment(mode, net_ip=None):
    """`launch --net MODE [--net-ip ADDR]` -> the environment the QEMU wrapper reads (net/qemu-net.sh), {} for no
    network. host and bridged:IF need the root socket_vmnet daemon to be running (the user starts it, see
    docs/changes/l4-network.md); mcast needs nothing and links instances on this Mac only. SP16_NET_MODE carries the
    mode to the guest init (via /proc/cmdline) so a bridged run can add a real-subnet address; SP16_NET_IP is the
    optional static address for that."""
    if mode in (None, "none"):
        if net_ip is not None:
            raise ValueError("--net-ip needs --net bridged:IF")
        return {}
    net_ip = validate_net_ip(net_ip)
    if mode == "host" or mode.startswith("bridged:"):
        iface = mode.partition(":")[2]
        if mode != "host" and not NET_IFACE.fullmatch(iface):
            raise ValueError("--net bridged:IF needs an interface name such as bridged:en0")
        if net_ip is not None and mode == "host":
            raise ValueError("--net-ip needs --net bridged:IF (host mode has no real subnet)")
        name = "socket_vmnet" if mode == "host" else f"socket_vmnet.bridged.{iface}"
        path = vmnet_socket_path(name)
        if path is None:
            raise ValueError(f"--net {mode}: the socket_vmnet daemon is not running (no {name} socket in "
                             f"{', '.join(VMNET_DIRECTORIES)}). Start it first (see docs/changes/l4-network.md), or use "
                             "--net mcast, which needs no root and links emulators on this Mac only")
        if "," in path:
            raise ValueError("socket_vmnet path must not contain commas")
        env = {"SP16_NET_KIND": "unix", "SP16_NET_TARGET": path, "SP16_NET_MODE": mode}
        if net_ip is not None:
            env["SP16_NET_IP"] = net_ip
        return env
    if mode == "mcast" or mode.startswith("mcast:"):
        port = mode.partition(":")[2] or str(NET_MCAST_PORT)
        if not port.isdigit() or not 1024 <= int(port) <= 65535:
            raise ValueError("--net mcast:PORT needs a port from 1024 to 65535")
        if net_ip is not None:
            raise ValueError("--net-ip needs --net bridged:IF")
        return {"SP16_NET_KIND": "mcast", "SP16_NET_TARGET": f"{NET_MCAST_GROUP}:{port}", "SP16_NET_MODE": mode,
                "SP16_PYTHON": sys.executable}
    raise ValueError(f"--net must be {NET_MODES}")


def launch(output, ephemeral=False, usb_image=None, firmware_id=None, dev_overlay=None, no_ui_input=False,
           no_audio_files=False, net=None, net_ip=None, midi=False):
    host_check()
    output = private_path(output)
    assets = validated_assets(output)
    net_env = net_environment(net, net_ip)
    if net_env and not (output / NET_WRAPPER).is_file():
        raise ValueError("--net needs a build made by this version; rebuild it with ./sp16 build --rebuild")
    if dev_overlay is not None and not (assets.get("decoupled") is True and assets.get("research")):
        raise ValueError("--dev-overlay needs a decoupled research build")
    if firmware_id is not None:
        if assets.get("decoupled") is not True:
            raise ValueError("--firmware applies to decoupled builds only")
        library = firmware.Library(assets["library"])
        manifest = firmware.verify_image(library, firmware_id)
        status = firmware.boot_status(manifest, library)
        if status["status"] != "verified":
            # Not refused (D2 in docs/architecture.md): it boots on the reference measurements, marked unverified.
            report = firmware.check_report(manifest, library)
            print("firmware is unverified (" + status["reason"] + "); it boots on the product's reference "
                  "measurements.\n" + json.dumps(report, indent=2), file=sys.stderr)
    # LaunchServices supervises the app. Closing its window stops its own QEMU.
    # --ephemeral: disposable per-session data image and no profile lock (tests); persistent builds default to the profile image.
    # --no-ui-input (bench mode): the window ignores pointer, scroll, keyboard and accessibility input, and `open -g` starts
    # it without taking focus. macOS still sends scroll to the window under the pointer; only the app's gate stops it.
    # --no-audio-files: an audio build's sink keeps reading and counting the guest's PCM but writes no WAV.
    app_args = (["--ephemeral"] if ephemeral else []) + (["--usb-image", prepare_usb_image(usb_image)] if usb_image else []) \
        + (["--firmware", firmware_id] if firmware_id else []) \
        + (["--dev-overlay", dev_overlay_path(dev_overlay)] if dev_overlay else []) \
        + (["--no-ui-input"] if no_ui_input else []) + (["--no-audio-files"] if no_audio_files else [])
    env_args = [arg for key, value in net_env.items() for arg in ("--env", f"{key}={value}")]
    midi_since = time.time() if midi else None
    run(["open", "-n", *(["-g"] if no_ui_input else []), *env_args, assets["app_bundle"],
         *(["--args", *app_args] if app_args else [])])
    # The app owns QEMU's lifetime; this bridge joins its DIN socket and exits when that socket closes.
    # Keep it separate from the panel bridge's virtio port.
    if midi and output.is_dir():
        with (output / "din-midi.log").open("a") as midi_log:
            subprocess.Popen([sys.executable, "-m", "midi.din", "--wait-for-session", str(output),
                              "--since", str(midi_since), "--build-dir", str(output.parent),
                              "--name", f"SP-16 DIN [{output.name}]"],
                             cwd=TOOLS, stdin=subprocess.DEVNULL, stdout=midi_log, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
        with (output / "usb-midi.log").open("a") as midi_log:
            subprocess.Popen([sys.executable, "-m", "midi.usb", "--wait-for-session", str(output),
                              "--since", str(midi_since), "--build-dir", str(output.parent),
                              "--name", f"SP-16 USB [{output.name}]"],
                             cwd=TOOLS, stdin=subprocess.DEVNULL, stdout=midi_log, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
    print(json.dumps({"status": "native_app_opened", "app": assets["app_bundle"], **({"net": net} if net_env else {}),
                      "diagnostics": str(output / "sessions"), "runtime_requires_docker": False}))


def firmware_command(args):
    """native.py firmware ...: prints one JSON document; a refused import prints its compatibility report."""
    assets, boot = base_boot(args.output)
    library = firmware.Library(assets["library"])
    if args.action == "import":
        if not args.update:
            raise ValueError("firmware import needs --update")
        key = firmware.load_key(args.key_file, args.gpl_archive)
        try:
            import fwpack
            entry_id, report = firmware.import_update(library, boot, args.update, key, name=args.name,
                                                      verify_mod=fwpack.mod_verifier(library, boot, key),
                                                      progress=lambda m: print(json.dumps(m), file=sys.stderr))
        except firmware.FirmwareError as error:
            del key
            print(json.dumps({"status": "firmware_refused",
                              "report": getattr(error, "report", firmware.refusal_report(None, str(error)))}, indent=2))
            return 1
        del key
        if library.selected() in (None, entry_id):  # the first import becomes the explicit selection
            library.select(entry_id)
        print(json.dumps({"status": "firmware_imported", "id": entry_id, "report": report}, indent=2))
        return 0
    if args.action == "audit":
        # Distribution scan (M3 gate, M7): bundle, QEMU, kernel and every file inside the base initramfs.
        logos = {digest(TOOLS / name): f"vendor logo {name}" for name in VENDOR_LOGO_FILES if (TOOLS / name).is_file()}
        upd_pins = {p["firmware"]["upd_sha256"]: f"update SHA-256 pin of {p['id']} (not needed for verification)"
                    for p in profiles.all_profiles() if p["firmware"].get("upd_sha256")}
        names = {Path(name).name for name in VENDOR_LOGO_FILES} | {f"{OLD_ICON_NAME}.icns"}
        report = firmware.audit_build([assets["app_bundle"], assets["qemu"], boot["kernel"]], [boot["initrd"]], library,
                                      forbidden_files=logos, forbidden_strings=upd_pins, forbidden_names=names)
        print(json.dumps(report, indent=2))
        return 0 if report["clean"] else 1
    if args.action == "list":
        print(json.dumps({"library": str(library.path), "selected": library.selected(),
                          "firmware": [library.summary(i) for i in library.ids()]}, indent=2))
        return 0
    if not args.id:
        raise ValueError(f"firmware {args.action} needs --id")
    if args.action == "show":
        manifest = library.manifest(args.id)
        shown = {k: v for k, v in manifest.items() if k != "files"}
        shown["files"] = len(manifest["files"])
        print(json.dumps({**library.summary(args.id), "manifest": shown}, indent=2))
    elif args.action == "check":
        print(json.dumps(firmware.check_report(firmware.verify_image(library, args.id), library), indent=2))
    elif args.action == "rename":
        library.rename(args.id, args.name)
        print(json.dumps(library.summary(args.id), indent=2))
    elif args.action == "remove":
        library.remove(args.id)
        print(json.dumps({"status": "firmware_removed", "id": args.id}))
    elif args.action == "select":
        library.select(args.id)
        print(json.dumps({"status": "firmware_selected", "id": args.id}))
    elif args.action == "profile":
        # A candidate profile for an imported version: identity from its manifest, measurements copied from a
        # reference profile as the starting assumption. Save it as profiles/<id>.json, verify, then mark it verified.
        print(json.dumps(firmware.candidate_profile(library.manifest(args.id), args.from_profile), indent=2))
    return 0


def diagnostics_command(args):
    """native.py diagnostics: one redacted zip for a bug report (nothing is uploaded); prints one JSON document."""
    try:
        result = diagnostics.collect(args.output, args.sessions, args.include_audio, args.out, args.key_file)
    except diagnostics.KeyFound as error:
        print(json.dumps({"status": "diagnostics_refused", "error": str(error)}))
        return 1
    print(json.dumps({"status": "diagnostics_written", **result}, indent=2))
    return 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="compile QEMU/window and package guest once")
    builder.add_argument("--output", required=True)
    builder.add_argument("--reuse-qemu-from", help="copy hash-verified QEMU from an existing private build")
    builder.add_argument("--profile", choices=PROFILES, default="fullpanel",
                         help="app profile (fullpanel is the only one)")
    builder.add_argument("--app-arg", metavar="TOKEN",
                         help="research only: start the firmware app with this argument; the guest passes it on only "
                              "if the firmware profile's runtime.app_args lists it (1.60: testmode)")
    builder.add_argument("--pad-calibration-value", type=int, metavar="N",
                         help="fullpanel only: stage a synthetic padcalibration.xml with N (48..504) for all pads "
                              f"(default {DEFAULT_PAD_CALIBRATION}, not factory data)")
    builder.add_argument("--uncalibrated", action="store_true",
                         help="fullpanel only: stage no calibration file, so the OEM reports uncalibrated pads (blinking)")
    builder.add_argument("--research", action="store_true",
                         help="enable the owner-only session control socket (events, raw research frames, snapshots)")
    builder.add_argument("--trace", choices=TRACE_MODES,
                         help="research only: strace streams qemu-arm-static -strace to console.txt as SP16_STRACE lines; "
                              "gdb opens the qemu-user stub (-g 1234, guest loopback, blocks the app until attached); "
                              "pcmtap records 4 s of each playback PCM from the first non-zero frame and prints it to "
                              "console.txt as SP16_PCM_DUMP gzip+base64 lines (no host audio output); "
                              "exec logs the qemu-user translator's executed translation-block addresses inside the app's "
                              "text range, deduped per app instance and guest thread (cpu) in the guest, to console.txt as SP16_TB lines (read them with "
                              "emulator/exectrace.py; slows the app a lot)")
    builder.add_argument("--audio", choices=AUDIO_MODES,
                         help="research or fullpanel only: carry the OEM playback PCM over a dedicated virtio-serial port "
                              "(sp16.pcm). file: one WAV per stream plus counters in the session directory (no sound reaches a "
                              "host device); coreaudio: play ak4458 ch12/ch14 as stereo on the host output (AUDIBLE); both. "
                              "Off (silent) by default")
    builder.add_argument("--audio-device", metavar="UID",
                         help="with --audio coreaudio|both: output device by stable CoreAudio UID (list them with "
                              "`./sp16 audio devices`, which compiles and runs the audio tool); default is the system "
                              "default output. "
                              "offline:<wav>[:rate[:ppm[:block]]] renders the output graph to a file on a software clock "
                              "instead (verification; never opens a device)")
    builder.add_argument("--decoupled", action="store_true", help="always on; kept for older commands")
    builder.add_argument("--library", metavar="DIR",
                         help="firmware library directory (default <output>/library)")
    builder.add_argument("--allow-unverified", action="store_true",
                         help="no longer needed and has no effect: any correctly keyed firmware boots, marked unverified when "
                              "no verified profile matches (accepted so existing commands keep working)")
    builder.add_argument("--bundle-suffix", metavar="TOKEN",
                         help="append .TOKEN to the bundle ID (lowercase letters, digits, hyphens; max 32), for dev builds "
                              "that must be quit separately; default bundle IDs are unchanged")
    launcher = commands.add_parser("launch", help="open the built native app; no Docker needed")
    launcher.add_argument("--output", required=True)
    launcher.add_argument("--ephemeral", action="store_true",
                          help="use a disposable data image for this session (no persistence, no profile lock)")
    launcher.add_argument("--usb-image", metavar="PATH",
                          help="fullpanel only: attach this raw image as a virtual USB stick (created sparse if missing; "
                               "formatted FAT32 by the guest init when blank). Cold attach: chosen at launch")
    launcher.add_argument("--firmware", metavar="ID", help="decoupled builds: firmware id from the build's library")
    launcher.add_argument("--dev-overlay", metavar="DIR",
                          help="decoupled research builds: share this mod folder read-only over 9p; its files/, "
                               "recipes/ and preload/ are applied in the guest before each app start (fwdev.py)")
    launcher.add_argument("--no-ui-input", action="store_true",
                          help="bench mode: the window ignores clicks, drags, scrolling, keys and accessibility actions "
                               "and opens without taking focus (open -g); the research control socket still works")
    launcher.add_argument("--net", metavar="MODE", default="none",
                          help="guest network (Pro DJ Link): " + NET_MODES + ". host and bridged need the socket_vmnet "
                               "daemon; mcast links emulators on this Mac without root. Default: none")
    launcher.add_argument("--net-ip", metavar="ADDR[/PREFIX]",
                          help="with --net bridged:IF: a static address on that interface's subnet for the guest "
                               "(a.b.c.d[/prefix], /24 when the prefix is omitted), added beside the 169.254 link-local "
                               "alias kept for Pro DJ Link")
    launcher.add_argument("--no-audio-files", action="store_true",
                          help="audio builds: the sink keeps reading and counting the guest's PCM (metrics) but "
                               "writes no WAV into the session folder")
    launcher.add_argument("--midi", action="store_true",
                          help="start the CoreMIDI bridges for this session: MIDI DIN in and out, and the separate USB "
                               "MIDI byte stream the firmware's g_midi peer uses (two virtual endpoints, no physical USB "
                               "device)")
    fw = commands.add_parser("firmware", help="decoupled builds: import, list, check and manage user-supplied firmware")
    fw.add_argument("action", choices=("import", "list", "show", "check", "rename", "remove", "select", "audit",
                                       "profile"))
    fw.add_argument("--output", required=True, help="decoupled build directory")
    fw.add_argument("--update", help="import: the SP-16 update (.UPD)")
    fw.add_argument("--key-file", help="import: the update key file")
    fw.add_argument("--gpl-archive", nargs="+", metavar="PART",
                    help="import: the official GPL source archive (the .tar.gz, its split parts, or the two official "
                         "zip downloads, in order); the key is read from it in memory")
    fw.add_argument("--id", help="firmware id (show, check, rename, remove, select)")
    fw.add_argument("--name", help="import/rename: display name")
    fw.add_argument("--from-profile", help="profile: measurements to start from (default: the product's reference)")
    diag = commands.add_parser("diagnostics", help="collect logs, session files and system facts into one redacted zip "
                                                    "for a bug report (nothing is uploaded)")
    diag.add_argument("--output", required=True, help="the build folder")
    diag.add_argument("--sessions", type=int, default=diagnostics.DEFAULT_SESSIONS, metavar="N",
                      help="how many recent sessions to include (default %(default)s)")
    diag.add_argument("--include-audio", action="store_true", help="also include the sessions' WAV captures (can be large)")
    diag.add_argument("--out", metavar="ZIP", help="zip to write (default ~/Desktop/sp16-diagnostics-<time>.zip, else the build folder)")
    diag.add_argument("--key-file", help="also prove this key file's key is absent from the zip (never printed)")
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(args.output, reuse_qemu_from=args.reuse_qemu_from,
                  profile=args.profile, research=args.research, pad_calibration=args.pad_calibration_value,
                  uncalibrated=args.uncalibrated, trace=args.trace, bundle_suffix=args.bundle_suffix,
                  audio=args.audio, audio_device=args.audio_device, library=args.library,
                  allow_unverified=args.allow_unverified, app_arg=args.app_arg)
        elif args.command == "firmware":
            return firmware_command(args)
        elif args.command == "diagnostics":
            return diagnostics_command(args)
        else:
            launch(args.output, args.ephemeral, args.usb_image, args.firmware, args.dev_overlay, args.no_ui_input,
                   args.no_audio_files, args.net, args.net_ip, args.midi)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"native emulator failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
