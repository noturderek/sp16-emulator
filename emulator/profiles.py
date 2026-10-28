"""Firmware compatibility profiles and product descriptors (docs/architecture.md, M1). Host and guest; stdlib only.

A profile holds everything the emulator does differently per firmware version, as data: identity (version,
application path and SHA-256, panel MCU revisions), runtime layout (working directory, emulated CPU, library path,
data/eMMC/USB mount points, tmpfs directories, allowed app arguments), the panel maps measured on that version
(buttons, counters, analog words, lamp roles, pad curve limits, selector window) and the pad calibration file format.
It never holds firmware content. A version is "verified" when a profile whose status is "verified" matches its exact
application; that requires a passing verification record (the hardware conformance suite is the gate). Since D2 in
docs/architecture.md (which replaced the earlier refusal) every correctly keyed import also boots without one: a
"candidate" profile, or the product's reference profile for an unprofiled import, supplies the measurements and the
run is marked unverified.

A product descriptor (profiles/products/<product>.json) holds what is common to a product's updates: layer layout,
application and loader candidates, the stock trailer marker and the reference profile used before a version has its
own. The schema is documented in docs/profiles.md.
"""
import json
from pathlib import Path
import re

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
SCHEMA = 1
STATUSES = ("verified", "candidate")
PRODUCT = "sp16"
# The 1.60 profile: the default when no profile is named.
LEGACY_PROFILE = "sp16-1.60"
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
VERSION_PATTERN = re.compile(r"[0-9]\.[0-9]{2}")
GUEST_PATH = re.compile(r"/(?:[A-Za-z0-9._-]+/?)*")
TOKEN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
LAMP_KINDS = {"mono": range(0, 35), "rgb": range(0, 32)}
FRAME_BYTES = 128

# Hardware-suite fields at the top level of a profile and their JSON container types (the names the suite uses;
# selector_window_frames, pad_curve, button_roles, lamp_roles and analog_words live under "panel").
SUITE_FIELDS = {"identity": dict, "home": dict, "touch_probe": dict, "touch_dead": dict, "screen_titles": list,
                "nav_probe": dict, "selector_probe": dict, "knob_columns": list, "knob_probe": dict,
                "knob_bounded_param": dict, "rgb_known": dict, "shift_probe": dict, "recipes": dict, "audio": dict,
                "strip": dict, "usb": dict, "device_nodes": list, "device_nodes_unserved": list, "ioctl_set": list,
                "hardware": dict}
# What code needs at run time; shipped copies (app bundle, guest base image) keep only these, without the update hash.
RUNTIME_FIELDS = ("schema", "id", "product", "status", "firmware", "runtime", "panel", "calibration")

_cache = {}


def _require(condition, message):
    if not condition:
        raise ValueError(f"invalid firmware profile: {message}")


def guest_path(value):
    """True for an absolute guest path of plain components (no spaces, no '..')."""
    return isinstance(value, str) and bool(GUEST_PATH.fullmatch(value)) and ".." not in value.split("/")


def validate(profile):
    """Check the structure a profile must have before any code reads it. Returns the profile unchanged."""
    _require(isinstance(profile, dict) and profile.get("schema") == SCHEMA, "schema")
    _require(isinstance(profile.get("id"), str) and ID_PATTERN.fullmatch(profile["id"]), "id")
    _require(profile.get("status") in STATUSES, "status")
    if profile["status"] == "verified":
        # Shipped copies drop the verification notes; the repository copy must carry a passing record.
        verification = profile.get("verification")
        _require(verification is None or (isinstance(verification, dict) and verification.get("result") == "pass"),
                 "a verified profile needs a passing verification record")
    firmware = profile.get("firmware")
    _require(isinstance(firmware, dict), "firmware")
    _require(isinstance(firmware.get("version"), str) and VERSION_PATTERN.fullmatch(firmware["version"]), "version")
    _require(isinstance(firmware.get("app_sha256"), str) and SHA256_PATTERN.fullmatch(firmware["app_sha256"]),
             "app_sha256")
    _require(firmware.get("upd_sha256") is None or SHA256_PATTERN.fullmatch(str(firmware["upd_sha256"])), "upd_sha256")
    _require(guest_path(firmware.get("app_path")), "app_path")
    runtime = profile.get("runtime")
    _require(isinstance(runtime, dict), "runtime")
    for key in ("cwd", "data_dir", "emmc_mount", "usb_mount"):
        _require(guest_path(runtime.get(key)), f"runtime.{key}")
    _require(isinstance(runtime.get("qemu_cpu"), str) and TOKEN.fullmatch(runtime["qemu_cpu"]), "runtime.qemu_cpu")
    _require(isinstance(runtime.get("ld_library_path"), str)
             and all(guest_path(p) for p in runtime["ld_library_path"].split(":")), "runtime.ld_library_path")
    _require(isinstance(runtime.get("tmpfs_dirs"), list) and all(guest_path(p) for p in runtime["tmpfs_dirs"]),
             "runtime.tmpfs_dirs")
    _require(isinstance(runtime.get("app_args", []), list) and all(isinstance(a, str) and TOKEN.fullmatch(a)
                                                                 for a in runtime.get("app_args", [])), "runtime.app_args")
    panel = profile.get("panel")
    _require(isinstance(panel, dict), "panel")
    _require(isinstance(panel.get("selector_window_frames"), int) and 1 <= panel["selector_window_frames"] <= 64,
             "selector_window_frames")
    pad = panel.get("pad_curve")
    _require(isinstance(pad, dict) and all(isinstance(pad.get(k), int) for k in ("a_min", "a_max", "b_contact")),
             "pad_curve")
    _require(0 < pad["a_min"] < pad["a_max"] <= 0xffff and 0 < pad["b_contact"] <= 0xffff, "pad limits")
    buttons = panel.get("button_roles")
    _require(isinstance(buttons, dict) and buttons, "button_roles")
    for name, value in buttons.items():
        _require(isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value)
                 and 1 <= value[0] < FRAME_BYTES and 0 <= value[1] <= 7, f"button {name!r}")
    counters = panel.get("counters")
    _require(isinstance(counters, dict) and counters, "counters")
    for name, value in counters.items():
        _require(isinstance(value, list) and len(value) == 2 and isinstance(value[0], int)
                 and 0 < value[0] < FRAME_BYTES - 1 and value[1] in (1, -1), f"counter {name!r}")
    analog = panel.get("analog_words")
    _require(isinstance(analog, dict), "analog_words")
    for name, value in analog.items():
        _require(isinstance(value, list) and len(value) == 2 and isinstance(value[0], int)
                 and 0 < value[0] < FRAME_BYTES - 1 and isinstance(value[1], bool), f"analog {name!r}")
    roles = panel.get("lamp_roles")
    _require(isinstance(roles, dict) and roles, "lamp_roles")
    for role, value in roles.items():
        _require(isinstance(role, str) and isinstance(value, list) and len(value) == 2
                 and value[0] in LAMP_KINDS and isinstance(value[1], int) and value[1] in LAMP_KINDS[value[0]],
                 f"lamp role {role!r}")
    calibration = profile.get("calibration")
    _require(isinstance(calibration, dict) and isinstance(calibration.get("range"), list)
             and len(calibration["range"]) == 2 and isinstance(calibration.get("default"), int)
             and calibration["range"][0] <= calibration["default"] <= calibration["range"][1], "calibration")
    _require(guest_path(calibration.get("path")), "calibration.path")
    _require(isinstance(calibration.get("template"), str) and "{attributes}" in calibration["template"]
             and isinstance(calibration.get("attribute"), str) and "{n}" in calibration["attribute"]
             and "{value}" in calibration["attribute"] and isinstance(calibration.get("pads"), int)
             and 1 <= calibration["pads"] <= 64, "calibration template")
    _require(isinstance(calibration.get("markers", []), list), "calibration.markers")
    # Hardware conformance suite fields (docs/hardware-suite.md): optional
    # here; null means not yet authored. Only their container types are checked; the suite runner reads them.
    for key, kinds in SUITE_FIELDS.items():
        _require(profile.get(key) is None or isinstance(profile[key], kinds), f"suite field {key}")
    _require(all(isinstance(path, str) and guest_path(path) for path in profile.get("device_nodes") or []), "device_nodes")
    _require(all(isinstance(value, str) and re.fullmatch(r"0x[0-9a-f]{8}", value) for value in profile.get("ioctl_set") or []),
             "ioctl_set")
    absent = (profile.get("hardware") or {}).get("absent", [])
    _require(isinstance(absent, list) and all(isinstance(item, dict) and isinstance(item.get("capability"), str)
                                              and isinstance(item.get("reason"), str) for item in absent),
             "hardware.absent entries need capability and reason")
    return profile


def load(profile_id=LEGACY_PROFILE, directory=None):
    """Load and validate profiles/<id>.json; ids are plain tokens, never paths."""
    if not isinstance(profile_id, str) or not ID_PATTERN.fullmatch(profile_id):
        raise ValueError("unknown firmware profile")
    folder = Path(directory) if directory else PROFILE_DIR
    key = (str(folder), profile_id)
    if key not in _cache:
        path = folder / f"{profile_id}.json"
        if not path.is_file():
            raise ValueError(f"unknown firmware profile {profile_id}")
        profile = validate(json.loads(path.read_text()))
        if profile["id"] != profile_id:
            raise ValueError("firmware profile id does not match its file name")
        _cache[key] = profile
    return _cache[key]


def all_profiles(directory=None):
    folder = Path(directory) if directory else PROFILE_DIR
    return [load(path.stem, folder) for path in sorted(folder.glob("*.json"))]


def for_app(app_sha256, directory=None):
    """The profile whose application SHA-256 matches, or None. Identity is by the application binary hash."""
    for profile in all_profiles(directory):
        if profile["firmware"]["app_sha256"] == app_sha256:
            return profile
    return None


def verified_for_app(app_sha256, directory=None):
    profile = for_app(app_sha256, directory)
    return profile if profile and profile["status"] == "verified" else None


def _boot_read_ok(layer):
    """A tar layer's boot read limit: max_boot_read_bytes is the length the unit's initramfs reads (a positive byte
    count, or absent when the length is not known), and enforce_boot_read may only be set for a layer that has one."""
    limit = layer.get("max_boot_read_bytes")
    enforce = layer.get("enforce_boot_read", False)
    if not isinstance(enforce, bool):
        return False
    if limit is None:
        return not enforce
    return isinstance(limit, int) and not isinstance(limit, bool) and limit > 0


def product(name=PRODUCT, directory=None):
    """The product descriptor (layout, application candidates, reference profile)."""
    if not isinstance(name, str) or not ID_PATTERN.fullmatch(name):
        raise ValueError("unknown product")
    path = (Path(directory) if directory else PROFILE_DIR) / "products" / f"{name}.json"
    data = json.loads(path.read_text())
    update = data.get("update") or {}
    ok = (data.get("schema") == SCHEMA and data.get("product") == name
          and isinstance(update.get("stock_marker"), str) and re.fullmatch(r"[A-Z0-9-]{4,15}", update["stock_marker"])
          and all(guest_path(layer.get("root")) and re.fullmatch(r"[A-Za-z0-9._-]+", str(layer.get("file")))
                  for layer in update.get("tar_layers", []) + update.get("squashfs_layers", []))
          and update.get("tar_layers") and all(guest_path(p) for p in data.get("app_candidates") or [None])
          and all(_boot_read_ok(layer) for layer in update.get("tar_layers", []))
          and all(guest_path(p) for p in data.get("loader_candidates") or [None])
          and all(guest_path(p) for p in update.get("mount_points", []))
          and ID_PATTERN.fullmatch(str(data.get("reference_profile"))))
    if not ok:
        raise ValueError("invalid product descriptor")
    return data


def active(app_sha256, allow_unverified=True, directory=None):
    """The profile a boot uses and how it was chosen: ("verified"|"candidate"|"reference", profile). Every firmware
    gets a profile (D2 in docs/architecture.md); `allow_unverified` is accepted for old callers and has no effect."""
    profile = for_app(app_sha256, directory)
    if profile and profile["status"] == "verified":
        return "verified", profile
    if profile:
        return "candidate", profile
    return "reference", load(product(directory=directory)["reference_profile"], directory)


def lamp_roles(profile):
    return {role: (kind, channel) for role, (kind, channel) in profile["panel"]["lamp_roles"].items()}


def runtime_fields(profile, app_path=None):
    """Shell-safe runtime layout of a profile for guest-init.sh, in a fixed order (every value is a validated plain
    token or guest path): id app_path cwd qemu_cpu ld_library_path tmpfs_dirs data_dir emmc_mount usb_mount app_args."""
    runtime = profile["runtime"]
    return [profile["id"], app_path or profile["firmware"]["app_path"], runtime["cwd"], runtime["qemu_cpu"],
            runtime["ld_library_path"], ",".join(runtime["tmpfs_dirs"]) or "-", runtime["data_dir"],
            runtime["emmc_mount"], runtime["usb_mount"], ",".join(runtime.get("app_args", [])) or "-"]


def legacy():
    """The verified 1.60 profile, the reference the tools and tests use; it must stay verified."""
    profile = load(LEGACY_PROFILE)
    if profile["status"] != "verified":
        raise ValueError("the 1.60 reference profile must be verified")
    return profile


if __name__ == "__main__":
    import sys
    # `python3 profiles.py runtime <id>` prints the runtime fields of a profile.
    if len(sys.argv) == 3 and sys.argv[1] == "runtime":
        print(" ".join(runtime_fields(load(sys.argv[2]))))
    else:
        raise SystemExit("usage: profiles.py runtime <profile id>")
