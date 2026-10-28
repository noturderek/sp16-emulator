#!/usr/bin/env python3
"""Guest-side firmware import and attach for the decoupled base image (docs/architecture.md, M2/M3/M6).

Runs only inside the offline VM (our init, never OEM scripts):
  import  The user's update is a read-only virtio disk (serial sp16upd); the host sends the 32-byte key over the
          sp16.import virtio port from memory. Verify trailer and CRC, decrypt with dm-crypt, check the ISO9660 volume, mount it, assemble images/ exactly as prepare.py does, drop keys and
          credentials, scan the application for the compatibility report, hash every file and write a read-only
          squashfs onto the output disk (serial sp16out). The manifest goes back over the port as JSON. The key only
          ever lives in this process and the kernel's dm table; it is never written to a file or printed.
  attach  At run time: verify the firmware image (serial sp16fw) against the manifest hash from the kernel command
          line, mount it read-only under a tmpfs overlay at /tmp/sp16-root, verify the application hash against the
          manifest (refused on mismatch), then pick the profile for the base application: a verified one, else a
          candidate or the product's reference measurements, marked unverified (D2 in docs/architecture.md).
"""
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import time
import zlib

sys.path.insert(0, "/opt/sp16")

SECTOR = 512
TRAILER = 20
MOD_MARKER = re.compile(rb"SP16MODLAB-([0-9]\.[0-9]{2})\x00")
KEY_MAGIC = b"SP16KEY1"
UPD_MOUNT = "/mnt/sp16-upd"
IMPORT_ROOT = "/tmp/sp16-import-root"
FW_LOWER = "/opt/sp16/fw"
ROOT = "/tmp/sp16-root"
OVERLAY = "/tmp/sp16-ov"
MAPPER = "sp16upd"
# Device nodes, ALSA PCMs and ioctl requests the hardware shim (hardware_stub.c) implements. The compatibility scan
# reports which of them the imported application references, and which similar-looking names it uses that the shim
# does not know. Kept equal to the C source by a unit test.
SHIM_DEVICES = ("/dev/subucom_spi0.1", "/dev/cdc_spi2.0", "/dev/dac_spi4.0", "/dev/backlight", "/proc/udev_usb1",
                "/proc/udev_usbctn1", "/dev/gpiodrv", "/dev/ttymxc1", "/dev/din_midi")
SHIM_PCMS = ("hw:ak4452audio,0", "hw:ak4452audio,1", "hw:ak4458audio,0")
SHIM_IOCTLS = (0x40047000, 0x40107000, 0x80017001, 0x40017001, 0x80047002, 0x40047002, 0x80027003, 0x40027003,
               0x80047004, 0x40047004, 0x40017005, 0x40047007, 0x80017006, 0x40017008, 0x40017009, 0x40017006)
DEVICE_PATTERN = re.compile(rb"/(?:dev|proc)/(?:subucom|cdc|dac|backlight|udev_usb|spi|panel|led|pdj)[A-Za-z0-9_.,-]*")
PCM_PATTERN = re.compile(rb"hw:[A-Za-z0-9_]+audio(?:,[0-9])?")


def profiles_module():
    import profiles
    return profiles


def log(message):
    print(message, flush=True)


def cmdline():
    try:
        words = Path("/proc/cmdline").read_text().split()
    except OSError:
        words = []
    return dict(word.split("=", 1) for word in words if word.startswith("sp16.") and "=" in word)


def run(*command, data=None):
    result = subprocess.run(list(command), input=data, capture_output=True)
    return result.returncode, (result.stdout + result.stderr).decode(errors="replace").strip()


def find_disk(serial, attempts=50):
    for _ in range(attempts):
        for block in sorted(glob.glob("/sys/block/vd*")):
            try:
                if Path(block, "serial").read_text().strip() == serial and os.path.exists("/dev/" + Path(block).name):
                    return "/dev/" + Path(block).name, int(Path(block, "size").read_text()) * SECTOR
            except OSError:
                pass
        time.sleep(.1)
    return None, 0


def find_port(name, attempts=50):
    for _ in range(attempts):
        for port in glob.glob("/sys/class/virtio-ports/*"):
            try:
                if Path(port, "name").read_text().strip() == name:
                    return "/dev/" + Path(port).name
            except OSError:
                pass
        time.sleep(.1)
    return None


def sha256_file(path, limit=None):
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as source:
        while remaining is None or remaining > 0:
            block = source.read(1 << 20 if remaining is None else min(1 << 20, remaining))
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    if remaining:
        raise ValueError("device shorter than expected")
    return digest.hexdigest()


def parse_trailer(trailer):
    """Return (kind, version) for a stock (LIVESAMPLER) or mod (SP16MODLAB-) trailer, else raise."""
    stock = profiles_module().product()["update"]["stock_marker"].encode()
    for kind, marker in (("stock", re.compile(re.escape(stock) + rb"([0-9]\.[0-9]{2})\x00")), ("mod", MOD_MARKER)):
        match = marker.fullmatch(trailer[:16])
        if match:
            return kind, match.group(1).decode()
    raise ValueError("not a sampler update: unknown trailer marker")


def verify_pvd(pvd, payload_bytes):
    if len(pvd) != 2048 or pvd[:7] != b"\x01CD001\x01":
        raise ValueError("the key does not decrypt this update (no ISO9660 volume after decryption)")
    blocks = int.from_bytes(pvd[80:84], "little")
    block_size = int.from_bytes(pvd[128:130], "little")
    if (blocks != int.from_bytes(pvd[84:88], "big") or block_size != int.from_bytes(pvd[130:132], "big")
            or block_size != 2048 or blocks == 0 or blocks * block_size > payload_bytes):
        raise ValueError("ISO9660 volume geometry is inconsistent")


def tree_manifest(root):
    """Per-entry listing: path -> {type, mode, size, sha256 | target}. Sorted, so its JSON is stable."""
    entries = {}
    root = Path(root)
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(parent) / name
            info = path.lstat()
            rel = "/" + str(path.relative_to(root))
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = {"type": "l", "target": os.readlink(path)}
            elif stat.S_ISDIR(info.st_mode):
                entries[rel] = {"type": "d", "mode": stat.S_IMODE(info.st_mode)}
            elif stat.S_ISREG(info.st_mode):
                entries[rel] = {"type": "f", "mode": stat.S_IMODE(info.st_mode), "size": info.st_size,
                                "sha256": sha256_file(path)}
            else:
                entries[rel] = {"type": "other"}
    return dict(sorted(entries.items()))


def ioctl_constants(data):
    """_IOC(dir, 'p' = 0x70, nr, size) request numbers the A32 code builds with a MOVW/MOVT pair into one register
    (how EP085 loads them; 9 of the shim's 16 requests appear this way in 1.60), plus any aligned literal word."""
    found = set()
    low = {}
    for index in range(0, len(data) - 3, 4):
        word = int.from_bytes(data[index:index + 4], "little")
        if word & 0x0ff00000 == 0x03000000:  # MOVW Rd, #imm16
            low[(word >> 12) & 15] = (index, (word >> 4) & 0xf000 | word & 0xfff)
        elif word & 0x0ff00000 == 0x03400000:  # MOVT Rd, #imm16
            register = (word >> 12) & 15
            if register in low and index - low[register][0] <= 32:
                found.add(((word >> 4) & 0xf000 | word & 0xfff) << 16 | low[register][1])
        if word in SHIM_IOCTLS:
            found.add(word)
    return {value for value in found
            if value >> 30 in (1, 2, 3) and (value >> 8) & 0xff == 0x70 and (value >> 16) & 0x3fff <= 0x100}


def scan_application(data, calibration_markers=()):
    """Compatibility facts from the application binary: shim devices/PCMs/ioctls it references and unknown ones, and
    which of the profile's calibration-format markers it contains."""
    words = ioctl_constants(data)
    devices = sorted({match.decode(errors="replace") for match in DEVICE_PATTERN.findall(data)})
    pcms = sorted({match.decode(errors="replace") for match in PCM_PATTERN.findall(data)})
    return {
        "devices_found": devices,
        "devices_unknown": [d for d in devices if d not in SHIM_DEVICES],
        "pcms_found": pcms,
        "pcms_unknown": [p for p in pcms if p not in SHIM_PCMS and p.count(",") == 1],
        "ioctls_known_found": sorted(f"0x{w:08x}" for w in words if w in SHIM_IOCTLS),
        "ioctls_unknown_candidates": sorted(f"0x{w:08x}" for w in words if w not in SHIM_IOCTLS),
        "calibration_markers": {marker: marker.encode() in data for marker in calibration_markers},
    }


def files_containing_key(root, key):
    needles = [n for n in (key, key.rstrip(b"\0"), key.hex().encode(), key.hex().upper().encode()) if len(n) >= 8]
    holders = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(parent) / name
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                holders.append(str(path.relative_to(root)))
    return holders


def read_key(port):
    fd = os.open(port, os.O_RDWR)
    buffer = b""
    deadline = time.time() + 60
    while len(buffer) < len(KEY_MAGIC) + 32 and time.time() < deadline:
        chunk = os.read(fd, len(KEY_MAGIC) + 32 - len(buffer))
        if not chunk:
            time.sleep(.05)
        buffer += chunk
    if len(buffer) != len(KEY_MAGIC) + 32 or not buffer.startswith(KEY_MAGIC):
        raise ValueError("the host did not send a key")
    return fd, buffer[len(KEY_MAGIC):]


def send(fd, message):
    data = memoryview((json.dumps(message, sort_keys=True) + "\n").encode())
    while data:
        written = os.write(fd, data)
        data = data[written:]


def map_update(device, payload_bytes, key):
    for module in ("dm_mod", "dm_crypt"):
        rc, out = run("modprobe", module)
        if rc:
            raise ValueError(f"cannot load {module}")
    # The table is passed on stdin, never as an argument, so the key is not in any process listing.
    table = f"0 {payload_bytes // SECTOR} crypt aes-cbc-plain {key.hex()} 0 {device} 0\n".encode()
    rc, out = run("dmsetup", "create", MAPPER, "--readonly", data=table)
    del table
    if rc:
        raise ValueError("dm-crypt mapping failed")
    run("dmsetup", "mknodes", MAPPER)
    return f"/dev/mapper/{MAPPER}"


def unmap_update():
    run("umount", UPD_MOUNT)
    run("dmsetup", "remove", MAPPER)


def open_update(params, port_fd, key):
    """Verify the trailer and CRC of the attached update, decrypt it and mount its ISO. Returns (metadata, images)."""
    device, device_bytes = find_disk("sp16upd")
    total = int(params.get("sp16.upd_bytes", "0"))
    if not device or total <= TRAILER or device_bytes < total:
        raise ValueError("update disk missing or shorter than announced")
    payload = total - TRAILER
    if payload % SECTOR:
        raise ValueError("encrypted payload is not sector-aligned")
    crc = 0
    with open(device, "rb") as source:
        remaining = payload
        while remaining:
            block = source.read(min(1 << 20, remaining))
            if not block:
                raise ValueError("truncated update")
            crc = zlib.crc32(block, crc)
            remaining -= len(block)
        trailer = source.read(TRAILER)
    kind, version = parse_trailer(trailer)
    stored = int.from_bytes(trailer[16:20], "little")
    if stored != crc:
        raise ValueError("update CRC does not match")
    mapped = map_update(device, payload, key)
    with open(mapped, "rb") as volume:
        volume.seek(32768)
        verify_pvd(volume.read(2048), payload)
    rc, _ = run("modprobe", "isofs")
    os.makedirs(UPD_MOUNT, exist_ok=True)
    rc, out = run("mount", "-t", "iso9660", "-o", "ro", mapped, UPD_MOUNT)
    if rc:
        raise ValueError("decrypted update volume does not mount as ISO9660")
    import profiles
    layout = profiles.product()["update"]
    images = Path(UPD_MOUNT, layout["images_dir"])
    layers = [layer["file"] for layer in layout["tar_layers"] + layout.get("squashfs_layers", [])]
    missing = [name for name in layers if not (images / name).is_file()]
    if missing:
        raise ValueError("update layout not recognised (missing " + ", ".join(missing) + ")")
    metadata = {"trailer_version": version, "kind": kind, "crc32": f"{crc:08x}", "bytes": total}
    revisions = {}
    for name in layout.get("revision_files", []):
        path = images / name
        if path.is_file() and path.stat().st_size <= 64:
            revisions[name] = path.read_text(errors="replace").strip()
    metadata["revisions"] = revisions
    send(port_fd, {"progress": "decrypted", "version": version, "kind": kind})
    return metadata, images


def mod_manifest():
    """A repacked (mod) update carries sp16-mod.json at the volume root (M6)."""
    path = Path(UPD_MOUNT, "sp16-mod.json")
    if not path.is_file() or path.stat().st_size > 1 << 20:
        return None
    return json.loads(path.read_text())


def build_image(root, device):
    """Read-only squashfs of the assembled tree, written straight onto the output disk; deterministic options."""
    rc, out = run("mksquashfs", root, device, "-noappend", "-all-root", "-no-xattrs", "-no-progress", "-quiet",
                  "-comp", "gzip", "-b", "131072", "-mkfs-time", "0", "-all-time", "0")
    if rc:
        raise ValueError("mksquashfs failed: " + out[-300:])
    with open(device, "rb") as image:
        superblock = image.read(96)
    if superblock[:4] != b"hsqs":
        raise ValueError("output is not a squashfs image")
    used = struct.unpack_from("<Q", superblock, 40)[0]
    size = (used + 4095) // 4096 * 4096
    return {"bytes": size, "sha256": sha256_file(device, size), "format": "squashfs-gzip"}


def import_update(params):
    port = find_port("sp16.import")
    if not port:
        raise SystemExit("SP16_IMPORT_ERROR no import port")
    fd, key = read_key(port)
    try:
        metadata, images = open_update(params, fd, key)
        mod = mod_manifest()
        import prepare
        import profiles
        product = profiles.product()
        root = Path(IMPORT_ROOT)
        root.mkdir(mode=0o700)
        extractor = "/usr/bin/unsquashfs"
        skipped = prepare.assemble(images, root, extractor, product["update"])
        # D1: the key must never end up in the firmware image. Credential and *.key files are already dropped; refuse
        # the import if any remaining file still carries the key (raw, unpadded or hex).
        holders = files_containing_key(root, key)
        key = None
        if holders:
            raise ValueError(f"the firmware tree contains the update key in {len(holders)} file(s); refusing to store it")
        send(fd, {"progress": "assembled", "skipped": skipped, "key_scan": "absent"})
        app_path = next((p for p in product["app_candidates"] if prepare.guest_file(root, p).is_file()), None)
        if app_path is None:
            raise ValueError(f"not an {product['name']} firmware: no application at "
                             + ", ".join(product["app_candidates"]))
        app_data = prepare.guest_file(root, app_path).read_bytes()
        if app_data[:4] != b"\x7fELF":
            raise ValueError("application is not an ELF executable")
        loader = next((p for p in product["loader_candidates"] if prepare.guest_file(root, p).is_file()
                       and prepare.guest_file(root, p).read_bytes()[:4] == b"\x7fELF"), None)
        if loader is None:
            raise ValueError("ARM ELF loader is missing (" + ", ".join(product["loader_candidates"]) + ")")
        files = tree_manifest(root)
        app_sha = hashlib.sha256(app_data).hexdigest()
        reference = profiles.for_app(app_sha) or profiles.load(product["reference_profile"])
        compat = scan_application(app_data, reference["calibration"].get("markers", []))
        compat["calibration_reference_profile"] = reference["id"]
        out_device, out_bytes = find_disk("sp16out")
        if not out_device:
            raise ValueError("output disk missing")
        image = build_image(str(root), out_device)
        if image["bytes"] > out_bytes:
            raise ValueError("output disk too small")
        manifest = {
            "update": metadata, "app_path": app_path, "loader_path": loader, "app_sha256": app_sha,
            "credential_or_special_entries_skipped": skipped, "key_scan": "absent", "image": image, "compat": compat,
            "files": files, "mod": mod,
            "tools": {"mksquashfs": run("mksquashfs", "-version")[1].splitlines()[0][:80]},
        }
        send(fd, {"result": "ok", "manifest": manifest})
        log(f"SP16_IMPORT_DONE version={metadata['trailer_version']} files={len(files)} image_bytes={image['bytes']}")
    except Exception as error:  # report a clean refusal; never include key material (ValueErrors are ours)
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        send(fd, {"result": "error", "error": message})
        log(f"SP16_IMPORT_REFUSED {message}")
    finally:
        unmap_update()
        os.sync()


def attach(params):
    """Mount the verified firmware image under an overlay at /tmp/sp16-root. Prints '<variant> <version>'."""
    import profiles
    device, device_bytes = find_disk("sp16fw")
    if not device:
        raise ValueError("firmware disk missing")
    expected = params.get("sp16.fw", "")
    size = int(params.get("sp16.fwbytes", "0"))
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or size <= 0 or size > device_bytes:
        raise ValueError("firmware identity missing from the command line")
    if sha256_file(device, size) != expected:
        raise ValueError("firmware image does not match its manifest")
    for module in ("squashfs", "overlay"):
        run("modprobe", module)
    os.makedirs(FW_LOWER, exist_ok=True)
    rc, out = run("mount", "-t", "squashfs", "-o", "ro", device, FW_LOWER)
    if rc:
        raise ValueError("firmware image does not mount")
    for path in (OVERLAY + "/upper", OVERLAY + "/work", ROOT):
        os.makedirs(path, mode=0o700, exist_ok=True)
    rc, out = run("mount", "-t", "overlay", "overlay", "-o",
                  f"lowerdir={FW_LOWER},upperdir={OVERLAY}/upper,workdir={OVERLAY}/work", ROOT)
    if rc:
        raise ValueError("overlay mount failed")
    import prepare
    app_path = params.get("sp16.app_path", "")
    if not profiles.guest_path(app_path):
        raise ValueError("application path missing from the command line")
    app_sha = hashlib.sha256(prepare.guest_file(Path(FW_LOWER), app_path).read_bytes()).hexdigest()
    if app_sha != params.get("sp16.fwapp"):
        raise ValueError("application does not match the firmware manifest")
    base = params.get("sp16.fwbase", app_sha)
    how, profile = profiles.active(base)
    variant = ("stock" if base == app_sha else "mod") + ("" if how == "verified" else "-unverified")
    version = params.get("sp16.fwver", "")
    if not re.fullmatch(r"[0-9]\.[0-9]{2}", version):
        raise ValueError("invalid firmware version")
    stage_calibration(params.get("sp16.padcal", "off"), profile)
    # One line of shell-safe fields (every value was validated as a plain token or guest path) for guest-init.sh.
    print(variant, version, how, *profiles.runtime_fields(profile, app_path))


def stage_calibration(setting, profile, root=ROOT):
    """Synthetic pad calibration (not factory data) in the profile's own file format, written into the overlay's
    upper layer: "default" uses the profile's default value, a number must be in the profile's range, and "off"
    stages nothing so the firmware reports uncalibrated pads."""
    import prepare
    if setting == "off":
        return None
    calibration = profile["calibration"]
    if setting == "default":
        setting = str(calibration["default"])
    if not re.fullmatch(r"[0-9]{1,5}", setting) or not calibration["range"][0] <= int(setting) <= calibration["range"][1]:
        raise ValueError(f"pad calibration value outside {calibration['range']} for profile {profile['id']}")
    attributes = " ".join(calibration["attribute"].format(n=n, value=int(setting))
                          for n in range(1, calibration["pads"] + 1))
    parent = prepare.guest_file(Path(root), os.path.dirname(calibration["path"]))
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / os.path.basename(calibration["path"])
    target.write_text(calibration["template"].format(attributes=attributes))
    os.chmod(target, 0o644)
    return target


PKG_ROOT = "/tmp/sp16-pkg"
MOD_EPOCH = 1569283200  # fixed timestamp (2019-09-24 00:00 UTC) for every file and volume date of a repacked update


def member_key(name, prefix=()):
    import prepare
    return "/" + "/".join(prefix + prepare.member_path(name))


def layer_prefix(layer):
    return tuple(Path(layer["root"]).parts[1:])


def find_layer(images, path, layout=None):
    """(layer descriptor, member name) of the tar.gz layer that holds a guest path; layers rooted deeper (e.g. at
    /root) are searched first. Squashfs layers are not repacked yet."""
    import profiles
    import tarfile
    layout = layout or profiles.product()["update"]
    for layer in layout.get("squashfs_layers", []):
        if path.startswith(layer["root"].rstrip("/") + "/"):
            raise ValueError(f"{path}: recipes on the squashfs layers ({layer['root']}) are not supported "
                             "in repacked updates yet")
    for layer in sorted(layout["tar_layers"], key=lambda item: -len(layer_prefix(item))):
        prefix = layer_prefix(layer)
        if prefix and not path.startswith("/" + "/".join(prefix) + "/"):
            continue
        with tarfile.open(Path(images) / layer["file"], "r:gz") as archive:
            for member in archive:
                if member.isfile() and member_key(member.name, prefix) == path:
                    return layer, member.name
    raise ValueError(f"{path} is not a regular file in any tar layer of the update")


def rewrite_tar_gz(source, target, replacements, prefix=()):
    """Same members, order and headers; replaced file contents; gzip with mtime 0, level 9, no name. Deterministic."""
    import gzip
    import io
    import tarfile
    pending = dict(replacements)
    buffer = io.BytesIO()
    with tarfile.open(source, "r:gz") as original, \
            tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as output:
        for member in original:
            data = original.extractfile(member).read() if member.isfile() else None
            key = member_key(member.name, prefix) if member.name not in (".", "./") else None
            if member.isfile() and key in pending:
                data = pending.pop(key)
                member.size = len(data)
            output.addfile(member, io.BytesIO(data) if data is not None else None)
    if pending:
        raise ValueError("layer rewrite missed " + ", ".join(sorted(pending)))
    with open(target, "wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0, compresslevel=9) as stream:
            stream.write(buffer.getvalue())


def read_request(fd):
    buffer = b""
    deadline = time.time() + 60
    while b"\n" not in buffer and time.time() < deadline:
        chunk = os.read(fd, 65536)
        if not chunk:
            time.sleep(.05)
        buffer += chunk
        if len(buffer) > 4 << 20:
            raise ValueError("package request too large")
    return json.loads(buffer.split(b"\n", 1)[0])


def package_update(params):
    """Repacked update (D3, emulator only): base update + recipes -> deterministic ISO -> encrypted onto the output disk.
    The host appends the SP16MODLAB- trailer and CRC; this never produces a stock LIVESAMPLER marker."""
    import recipes
    port = find_port("sp16.import")
    if not port:
        raise SystemExit("SP16_PACKAGE_ERROR no import port")
    fd, key = read_key(port)
    try:
        request = read_request(fd)
        mod_id = request["id"]
        recipe_list = [recipes.validate(r) for r in request["recipes"]]
        metadata, images = open_update(params, fd, key)
        if metadata["kind"] != "stock":
            raise ValueError("the base must be a stock update")
        iso = Path(PKG_ROOT) / "iso"
        iso.mkdir(parents=True, mode=0o700)
        rc, out = run("cp", "-a", UPD_MOUNT + "/.", str(iso))
        if rc:
            raise ValueError("copying the base volume failed")
        with open(f"/dev/mapper/{MAPPER}", "rb") as volume:
            volume.seek(32768 + 40)
            volume_id = volume.read(32).decode("ascii", "replace").strip() or "SP16"
        unmap_update()
        run("chmod", "-R", "u+w", str(iso))
        new_images = iso / profiles_module().product()["update"]["images_dir"]
        patched, results, layers = {}, {}, {}
        for recipe in recipe_list:
            for path, spec in recipe["files"].items():
                layer, member = find_layer(new_images, path)
                if path not in patched:
                    import tarfile
                    with tarfile.open(new_images / layer["file"], "r:gz") as archive:
                        patched[path] = (layer, archive.extractfile(member).read())
                layer, data = patched[path]
                patched[path] = (layer, recipes.apply_file(data, spec, f"{recipe['id']}:{path}"))
        by_layer, descriptors = {}, {}
        for path, (layer, data) in patched.items():
            by_layer.setdefault(layer["file"], {})[path] = data
            descriptors[layer["file"]] = layer
            results[path] = hashlib.sha256(data).hexdigest()
        for name, replacements in sorted(by_layer.items()):
            source = new_images / name
            before = sha256_file(source)
            target = Path(PKG_ROOT) / name
            rewrite_tar_gz(source, target, replacements, layer_prefix(descriptors[name]))
            os.replace(target, source)
            layers[name] = {"base_sha256": before, "sha256": sha256_file(source)}
        manifest = {"schema": 1, "kind": "sp16-mod-update", "id": mod_id,
                    "base": {"upd_sha256": request["base"]["upd_sha256"], "version": metadata["trailer_version"],
                             "app_sha256": request["base"]["app_sha256"]},
                    "recipes": recipe_list, "results": results, "layers": layers,
                    "builder": {"epoch": MOD_EPOCH, "python": sys.version.split()[0],
                                "xorriso": run("xorriso", "-version")[1].splitlines()[0][:80]}}
        (iso / "sp16-mod.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        run("find", str(iso), "-exec", "touch", "-h", "-d", f"@{MOD_EPOCH}", "{}", "+")
        out_iso = Path(PKG_ROOT) / "out.iso"
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(MOD_EPOCH), TZ="UTC")
        result = subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-o", str(out_iso), "-R", "-uid", "0",
                                 "-gid", "0", "-V", volume_id[:32], "--modification-date=2019092400000000", str(iso)],
                                capture_output=True, env=env)
        if result.returncode:
            raise ValueError("xorriso failed: " + result.stderr.decode(errors="replace")[-300:])
        size = out_iso.stat().st_size
        if size % SECTOR:
            raise ValueError("ISO size is not sector-aligned")
        with open(out_iso, "rb") as volume:
            volume.seek(32768)
            verify_pvd(volume.read(2048), size)
        out_device, out_bytes = find_disk("sp16out")
        if not out_device or out_bytes < size:
            raise ValueError("output disk missing or too small")
        table = f"0 {size // SECTOR} crypt aes-cbc-plain {key.hex()} 0 {out_device} 0\n".encode()
        key = None
        rc, out = run("dmsetup", "create", "sp16pkg", data=table)
        del table
        if rc:
            raise ValueError("dm-crypt mapping of the output failed")
        run("dmsetup", "mknodes", "sp16pkg")
        rc, out = run("dd", f"if={out_iso}", "of=/dev/mapper/sp16pkg", "bs=1M", "conv=fsync", "status=none")
        run("sync")
        run("dmsetup", "remove", "sp16pkg")
        if rc:
            raise ValueError("writing the encrypted payload failed")
        send(fd, {"result": "ok", "payload_bytes": size, "iso_sha256": sha256_file(out_iso),
                  "mod_manifest": manifest})
        log(f"SP16_PACKAGE_DONE id={mod_id} payload_bytes={size}")
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, KeyError)) else type(error).__name__
        send(fd, {"result": "error", "error": message})
        log(f"SP16_PACKAGE_REFUSED {message}")
    finally:
        unmap_update()
        os.sync()


DEV_SHARE = "/opt/sp16/dev-share"
# Where the record of the overlay's written paths lives. A record in /run is lost when the VM restarts, and then files an
# earlier session wrote through files/ into the persistent data directory are never put back. The eMMC image's own
# .sp16 directory survives a restart and is outside the app's data directory, so the record goes there; without a mounted
# eMMC nothing under it can persist, so /run is the honest fallback.
DEV_STATE = "/run/sp16-dev-applied.json"
DEV_STATE_IN_EMMC = (".sp16", "sp16-dev-applied.json")
DEV_PRELOAD_DIR = "/opt/sp16/dev"
MAX_DEV_FILE = 256 << 20


def dev_state_path(root, profile_id):
    """The file that records which guest paths the overlay wrote, next to the firmware's own .sp16 data directory on the
    eMMC image, or /run when this boot has no eMMC mounted."""
    import prepare
    try:
        mount = profiles_module().load(profile_id)["runtime"]["emmc_mount"]
        folder = prepare.guest_file(Path(root), mount) / DEV_STATE_IN_EMMC[0]
    except (OSError, ValueError, KeyError):
        return Path(DEV_STATE)
    return folder / DEV_STATE_IN_EMMC[1] if folder.is_dir() else Path(DEV_STATE)


def dev_plan(share, lower, profile_id):
    """What the developer overlay asks for: {guest path: (bytes, mode)} plus preload guest paths. Nothing is written.
    files/<guest path> replaces or adds a file; recipes/*.json patch the (possibly replaced) file; preload/*.so are
    ARM ELF shared objects added to LD_PRELOAD after the shim."""
    import prepare
    import recipes
    share, lower = Path(share), Path(lower)
    plan, applied = {}, []
    files = share / "files"
    if files.is_dir():
        for parent, dirs, names in os.walk(files, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                source = Path(parent) / name
                if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_DEV_FILE:
                    raise ValueError(f"overlay file {source.relative_to(share)} must be a regular file under 256 MiB")
                path = recipes.guest_path("/" + str(source.relative_to(files)))
                plan[path] = (source.read_bytes(), source.stat().st_mode & 0o755 or 0o644)
    for source in sorted((share / "recipes").glob("*.json")) if (share / "recipes").is_dir() else []:
        recipe = recipes.load_path(source)
        if recipe["base_profile"] != profile_id:
            raise ValueError(f"recipe {recipe['id']} targets {recipe['base_profile']}, not {profile_id}")
        for path, spec in recipe["files"].items():
            if path in plan:
                data, mode = plan[path]
            else:
                original = prepare.guest_file(lower, path)
                if not original.is_file():
                    raise ValueError(f"recipe {recipe['id']}: {path} is not in the firmware")
                data, mode = original.read_bytes(), original.stat().st_mode & 0o755
            plan[path] = (recipes.apply_file(data, spec, f"{recipe['id']}:{path}"), mode)
        applied.append(recipe["id"])
    preload = []
    for source in sorted((share / "preload").glob("*.so")) if (share / "preload").is_dir() else []:
        data = source.read_bytes()
        if source.is_symlink() or data[:4] != b"\x7fELF" or data[4] != 1 or int.from_bytes(data[18:20], "little") != 40:
            raise ValueError(f"preload {source.name} is not a 32-bit ARM ELF shared object")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}\.so", source.name):
            raise ValueError(f"preload name {source.name} is not allowed")
        path = f"{DEV_PRELOAD_DIR}/{source.name}"
        plan[path] = (data, 0o755)
        preload.append(path)
    return plan, applied, preload


def dev_restore(root, lower, paths):
    """Put every previously written path back to the firmware's own content (or remove an added file)."""
    import prepare
    for path in paths:
        target = prepare.guest_file(Path(root), path)
        original = prepare.guest_file(Path(lower), path)
        if original.is_file() and not original.is_symlink():
            target.write_bytes(original.read_bytes())
            os.chmod(target, original.stat().st_mode & 0o7777)
        elif target.is_file() and not target.is_symlink():
            target.unlink()


def dev_apply(params, share=DEV_SHARE, root=ROOT, lower=FW_LOWER, state=None, profile_id=None):
    """Apply the developer overlay into the overlay's upper layer, never the read-only image. Transactional: on any
    error every touched path is restored and nothing of the overlay stays. Prints the preload list for the shell."""
    import prepare
    profile_id = profile_id or os.environ.get("SP16_PROFILE", "")
    state = Path(state) if state is not None else dev_state_path(root, profile_id)
    try:
        previous = json.loads(state.read_text()).get("paths", [])
    except (OSError, ValueError, AttributeError):
        previous = []
    dev_restore(root, lower, previous)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"paths": []}))
    plan, applied, preload = dev_plan(share, lower, profile_id)
    owned = {path for path in plan if prepare.guest_file(Path(root), path) == state}
    if owned:
        raise ValueError(f"overlay file {sorted(owned)[0]} is the overlay's own record of written paths")
    written = []
    try:
        for path, (data, mode) in sorted(plan.items()):
            parent = prepare.guest_file(Path(root), os.path.dirname(path))
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / os.path.basename(path)
            if target.is_symlink():
                target = prepare.guest_file(Path(root), path)
            written.append(path)
            target.write_bytes(data)
            os.chmod(target, mode)
    except BaseException:
        dev_restore(root, lower, written)
        raise
    state.write_text(json.dumps({"paths": written}))
    app_path = params.get("sp16.app_path", "")
    app_sha = hashlib.sha256(prepare.guest_file(Path(root), app_path).read_bytes()).hexdigest() \
        if app_path else "unknown"
    log(f"SP16_DEV applied recipes={','.join(applied) or 'none'} files={len(plan) - len(preload)} "
        f"preload={len(preload)} app_sha256={app_sha}")
    print(":".join(preload))


APL_HOOK_MARKER = b"# SP16_APL_HOOK"
APL_HOOK_MAX = 240


def apl_hook_line(apl_start):
    """The one shell line the firmware's apl_start carries right after a `# SP16_APL_HOOK` line, or None.

    The emulator never runs the firmware's apl_start, but a firmware that loads a mod from the data volume does it with
    one line placed just before the app starts (for example `[ -f /root/data/mod.sh ] && (. /root/data/mod.sh)`). The
    init runs only that line, so such a mod is exercised the way the unit would. The line must be a single printable
    ASCII line of at most APL_HOOK_MAX bytes; anything else is refused."""
    try:
        lines = Path(apl_start).read_bytes().split(b"\n")
    except OSError:
        return None
    for index, line in enumerate(lines[:-1]):
        if line.strip() != APL_HOOK_MARKER:
            continue
        hook = lines[index + 1].rstrip(b"\r")
        if not hook.strip() or len(hook) > APL_HOOK_MAX or any(byte < 32 or byte > 126 for byte in hook if byte != 9):
            raise ValueError("the apl_start hook line is empty, longer than 240 bytes or not printable ASCII")
        return hook.decode("ascii")
    return None


def main():
    params = cmdline()
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "import":
        import_update(params)
    elif command == "package":
        package_update(params)
    elif command == "dev-apply":
        try:
            # stdout carries only the preload list; the status line goes to the console via stderr.
            global log
            log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
            dev_apply(params)
        except (ValueError, OSError) as error:
            print(f"SP16_DEV_ERROR {error}", file=sys.stderr, flush=True)
            sys.exit(3)
    elif command == "attach":
        try:
            attach(params)
        except ValueError as error:
            print(f"SP16_FIRMWARE_ERROR {error}", file=sys.stderr, flush=True)
            sys.exit(3)
    elif command == "apl-hook":
        try:
            hook = apl_hook_line(sys.argv[2]) if len(sys.argv) > 2 else None
        except ValueError as error:
            print(f"SP16_APL_HOOK_ERROR {error}", file=sys.stderr, flush=True)
            sys.exit(3)
        if hook:
            print(hook)
    else:
        raise SystemExit("usage: guest-firmware.py import|package|attach|dev-apply|apl-hook")


if __name__ == "__main__":
    main()
