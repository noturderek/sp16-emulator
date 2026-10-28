#!/usr/bin/env python3
"""Unpack an SP-16 update's layers into a guest tree without executing OEM programs (used by guest-firmware.py)."""

import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

REPO = Path(__file__).resolve().parents[1]


def private_path(value, *, fresh=False):
    path = Path(os.path.abspath(value))
    if not path.is_relative_to(REPO) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("workspace/output must be inside the repository without symlink parents")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(path)], cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if ignored.returncode:
        raise ValueError("workspace/output must be Git-ignored")
    if fresh and (path.exists() or not path.parent.is_dir()):
        raise ValueError("output must be absent with an existing private parent")
    permission_path = path.parent if fresh else path
    if permission_path.stat().st_mode & 0o077:
        raise ValueError("workspace/output parent must have owner-only permissions")
    return path


def member_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("archive member escapes its root")
    return tuple(p for p in path.parts if p != ".")


def secret_name(parts):
    return any(p.endswith(".key") or p in {"shadow", "shadow-", "passwd", ".ssh"} for p in parts)


def directory(root, parts):
    """Never follow an archive-created symlink while writing on the host."""
    path = root
    for part in parts:
        path /= part
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("archive directory conflicts with an existing non-directory")
        path.mkdir(exist_ok=True, mode=0o700)
    return path


def destination(root, parts):
    parent = directory(root, parts[:-1])
    path = parent / parts[-1]
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise ValueError("archive file conflicts with an existing directory")
    return path


def check_boot_read(archive, limit, name=None):
    """Refuse an archive longer than the length the unit's initramfs reads from flash at boot, and return its size.

    The initramfs reads a fixed length per layer (product descriptor: max_boot_read_bytes), so anything past it is
    silently dropped on the unit: a layer that boots in the emulator but would be truncated there is worse than a
    refusal. A `limit` of None means the length is not known for that layer and nothing is checked."""
    size = Path(archive).stat().st_size
    if limit is None:
        return size
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError(f"boot read limit {limit!r} for {name or Path(archive).name} is not a positive byte count")
    if size > limit:
        label = name or Path(archive).name
        over = size - limit
        raise ValueError(f"{label} is {size} bytes; the unit reads at most {limit} bytes of it at boot, so cut "
                         f"{over} byte{'' if over == 1 else 's'}")
    return size


def unpack_layer(archive, root, prefix=(), boot_read_limit=None):
    check_boot_read(archive, boot_read_limit)
    skipped = 0
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            parts = prefix + member_path(member.name)
            if not parts:
                continue
            if secret_name(parts) or parts[0] in {"dev", "proc", "sys"}:
                skipped += 1
                continue
            if member.isdir():
                directory(root, parts)
            elif member.issym():
                # Link strings belong to the eventual guest, never the host.
                if "\x00" in member.linkname:
                    raise ValueError("invalid guest symlink")
                os.symlink(member.linkname, destination(root, parts))
            elif member.isfile():
                with source.extractfile(member) as stream, destination(root, parts).open("xb") as target:
                    shutil.copyfileobj(stream, target)
                (root.joinpath(*parts)).chmod(member.mode & 0o755)
            else:
                # Special nodes and hardlinks are not silently materialized.
                if member.islnk():
                    raise ValueError("hardlinked archive member needs explicit handling")
                skipped += 1
    return skipped


def guest_file(root, guest_path):
    """Resolve absolute/relative guest symlinks without escaping to host files."""
    pending = list(PurePosixPath(guest_path).parts)
    current = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in {"/", "."}:
            continue
        if part == "..":
            if not current:
                raise ValueError("guest link escapes root")
            current.pop()
            continue
        path = root.joinpath(*current, part)
        if path.is_symlink():
            links += 1
            if links > 40:
                raise ValueError("guest symlink loop")
            target = PurePosixPath(os.readlink(path))
            if target.is_absolute():
                current = []
            pending = list(target.parts) + pending
        else:
            current.append(part)
    return root.joinpath(*current)


def assemble(images, root, extractor, layout):
    """Unpack an update's layers into the fresh directory `root`, drop credentials and keys, and create the mount
    points. `layout` is the product descriptor's update section (profiles/products/<product>.json). Returns the number
    of skipped credential or special entries."""
    images = Path(images)
    skipped = 0
    for layer in layout["tar_layers"]:
        limit = layer.get("max_boot_read_bytes") if layer.get("enforce_boot_read") else None
        skipped += unpack_layer(images / layer["file"], root, tuple(PurePosixPath(layer["root"]).parts[1:]), limit)
    for layer in layout.get("squashfs_layers", []):
        mount = PurePosixPath(layer["root"]).relative_to("/")
        directory(root, tuple(mount.parts))
        result = subprocess.run(
            [extractor, "-no-progress", "-f", "-d", str(root / mount), str(images / layer["file"])],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise ValueError(f"SquashFS extraction failed for {layer['file'].rsplit('.', 1)[0]}")
    # Remove credential/key files if a filesystem image also supplied them.
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + files:
            if secret_name((name,)):
                path = Path(parent) / name
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                if name in dirs:
                    dirs.remove(name)
                skipped += 1
    for name in layout.get("mount_points", []):
        directory(root, tuple(PurePosixPath(name).parts[1:]))
    return skipped
