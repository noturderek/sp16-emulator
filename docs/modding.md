# Modding guide

You can change how a firmware behaves in the emulator without rebuilding the emulator, and without ever changing
the imported firmware image. Changes are applied in the guest, into a tmpfs overlay above the read-only image, each
time the firmware's application starts.

This repository ships the tooling to develop and run a mod, not any mod itself: you write your own recipes, files and
preload libraries against firmware you imported yourself.

Everything here runs on the emulator only. Nothing flashes hardware or runs a firmware's update scripts.

## Setup: a research build

The developer loop needs a build with the control socket:

```sh
mkdir -p -m 700 private           # git-ignored and owner-only (build/ is created for you)
./sp16 build --name sp16-dev --research --audio file --bundle-suffix dev     # or: make build-dev
./sp16 firmware import --update <your .UPD> --gpl-archive <part 1> <part 2>   # skip if already imported
```

`--bundle-suffix dev` gives this build its own bundle id, so it can run next to your normal build. All builds share
the firmware library `build/library`, so firmware you already imported is available here too.

## A mod folder

```text
my-mod/
  mod.json       {"schema": 1, "id": "my-mod", "base_profile": "sp16-1.60", "description": "..."}
  recipes/       *.json patch recipes
  files/         replaced or added files at their guest paths (your own content only)
  src/           C sources of preload libraries, built by `./sp16 mod sdk`
  preload/       ARM preload libraries (*.so), loaded after the hardware shim
```

Create an empty one (keep it under the git-ignored `private/` folder):

```sh
./sp16 mod new --mod private/my-mod --id my-mod
```

This writes `mod.json` with `base_profile` `sp16-1.60`, empty `recipes/` and `files/` folders and an example preload
source in `src/` that only logs a line when it loads. Change `base_profile` to `sp16-1.50` if your mod targets 1.50.

## Recipes

A recipe patches one or more files of the firmware by offset or by byte pattern. It names each file's SHA-256 before
(and optionally after) the patch, and how many matches it expects, so a recipe can never apply to the wrong firmware
or apply partially. Replacements keep the original length.

```json
{
  "schema": 1,
  "id": "my-label",
  "base_profile": "sp16-1.60",
  "files": {
    "<guest path of the file>": {
      "base_sha256": "<SHA-256 of the unmodified file>",
      "patches": [
        {"offset": 1234, "original_hex": "<bytes now at that offset>", "replacement_hex": "<same length>",
         "expected_count": 1}
      ],
      "result_sha256": "<optional: SHA-256 of the patched file>"
    }
  }
}
```

- `"offset"` patches need `"original_hex"`: the bytes that must be at that offset. With `"expected_count"`, those
  bytes must also occur exactly that many times in the whole file.
- Use `"pattern_hex"` instead of `"offset"` and `"original_hex"` to find bytes by content; every occurrence is
  replaced, and `"expected_count"` is required.
- The recipe `id` is lowercase letters, digits and hyphens; name the file after it (`recipes/my-label.json`).

A recipe with wrong original bytes or a wrong base hash is refused before anything changes; the firmware then starts
unchanged and the console shows `SP16_DEV_ERROR`.

### Finding what to patch

- The base hashes of every file of an imported firmware are in its manifest,
  `build/library/<firmware id>/manifest.json`, under `files` (`./sp16 firmware list` shows the ids). For example:
  `python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["files"][sys.argv[2]]["sha256"])' build/library/<id>/manifest.json /path/in/guest`.
- The firmware image itself is `build/library/<firmware id>/firmware.sqfs`, a read-only squashfs. To look at its
  files, extract a copy into `private/` (never into a tracked folder) with
  `unsquashfs -d private/fw-<id> build/library/<id>/firmware.sqfs` (`brew install squashfs`). Do not edit the
  library copy: a changed image is refused at boot.
- `./sp16 mod check` tells you when a path is not a file of the firmware or a base hash does not match.

## The loop

`./sp16 mod` (it runs `emulator/fwdev.py`) works on the library's selected firmware (`./sp16 firmware list` shows it),
or on the one you name with `--firmware <id>`. A recipe applies only to the firmware version it names, so select a
matching firmware first, for example `./sp16 firmware select --id <1.60 id>` for a mod based on `sp16-1.60`.

```sh
./sp16 mod check --mod private/my-mod   # preflight, no boot
./sp16 mod run   --mod private/my-mod   # launch with the overlay
# edit a recipe or a file, then run again: only the firmware's app restarts (about 0.2 s), and the screen settles
# a few seconds later
./sp16 mod test  --mod private/my-mod   # check + run + verify
```

`./sp16 mod test` confirms the guest applied the overlay and any declared result hash, that lamps are live and audio
counters move, and optionally that an LCD region changed against a stock baseline after a tap
(`--baseline stock.png --expect-change X0 Y0 X1 Y1 --touch X Y`).

## Replaced files and preload libraries

- `files/<guest path>` replaces or adds a file in the overlay. `./sp16 mod check` refuses unmodified copies of
  firmware files: put only your own content there.
- `src/*.c` are built by `./sp16 mod sdk --mod private/my-mod` into `preload/*.so` with the ARM cross
  toolchain in a throwaway Docker container, with the same flags as the hardware shim. The library binds to the
  firmware's own C library at run time and is loaded after the shim.

### Loading a library without restarting the app

The firmware app runs chrooted at `/tmp/sp16-root`, which used to hide the mod folder: only the guest saw
`/opt/sp16/dev-share`, and the app got copies of `preload/*.so` at each start. A developer-overlay build now also
bind-mounts the folder **read-only** at `/opt/sp16/dev-share` *inside* the app's root, so code running in the app, such as
a preload library, can read files that you change on the host while the app runs. The console shows
`SP16_DEV_SHARE app-visible=yes path=/opt/sp16/dev-share` at boot when it is there, and `SP16_DEV_ERROR developer overlay
share not visible to the app` when the bind failed. Without a developer overlay (`sp16.dev=1`) nothing is mounted.

- It needs a guest image from this version: rebuild an older build in place
  (`./sp16 build --name dev --rebuild --research --audio file --bundle-suffix dev`; options are not remembered).
  An older guest simply does not have the path, so a library must treat it as optional.
- The share is a 9p mount, which cannot be memory-mapped. To load a library from it, copy the file to `/tmp` first
  (the app's tmpfs) and `dlopen` the copy; reading it with `open`/`read` works as on any file. A new host file appears
  in the app within a moment (the mount does not cache), so a watcher thread can poll a small command file.
- The app can read everything in the mod folder, including `files/` and `recipes/`. Keep it to the mod itself.

### A launch hook: loading a mod from the data volume

The emulator does not run the firmware's `apl_start`. A firmware that loads a mod from the data volume (`/root/data`,
which lives on the persistent eMMC image, outside the size-limited boot archives) does it with one shell line that
`apl_start` runs just before it starts the app. To exercise that line, mark it: put a comment line
`# SP16_APL_HOOK` directly above it in `apl_start`, for example

```sh
# SP16_APL_HOOK
[ -f /root/data/mod.sh ] && (. /root/data/mod.sh)
```

Before each app start the guest runs only that one line, in the app's root, under the firmware's own `busybox sh`,
after the developer overlay is applied. It is limited to 10 seconds and can never keep the app from starting. The
console shows what happened:

```text
SP16_APL_HOOK run: [ -f /root/data/mod.sh ] && (. /root/data/mod.sh)
SP16_APL_HOOK <whatever the script printed>
SP16_APL_HOOK rc=0
```

- The line must be one printable-ASCII line of at most 240 bytes; anything else is refused (`SP16_APL_HOOK_ERROR`).
  Without the marker nothing runs and no `SP16_APL_HOOK` line appears.
- `rc=124` means the 10 second limit stopped it; a syntax error in the script gives `rc=2` and the app still starts
  (keep the `( … )` subshell so a broken script cannot stop the line itself).
- Child programs (`cp`, `rm`, `sleep`) work: the guest registers a `binfmt_misc` handler for ARM ELF files the first
  time a hook runs. It needs a guest image from this version (rebuild in place as above).
- A script that is written into `/root/data` through `files/` stays in the data image after the session ends, because
  the overlay's record of what it wrote is kept in the guest's RAM. Remove it from the script itself or rebuild the
  data image, or the next session finds it again.
- Put the marker and the hook line in your own `files/root/pdj/apl_start`, which is a modified copy of a firmware
  file: keep it in your private mod folder and never commit it.

## Package a mod

```sh
./sp16 mod package --mod private/my-mod --out private/my-mod.tar.gz
```

The archive holds your recipes, sources and libraries with a manifest; it refuses unmodified firmware files. This is
the form to share a mod in: it contains no firmware. Whoever receives it unpacks it into their own `private/`
folder and needs their own copy of the base firmware.

## Repacked updates (emulator only)

```sh
./sp16 mod package --upd --mod private/my-mod \
  --gpl-archive <part 1> <part 2> --out private/my-mod.UPD
./sp16 firmware import --update private/my-mod.UPD --gpl-archive <part 1> <part 2>
```

The repacked update is built from the verified base in your library and the mod's recipes, deterministically. It
carries an `SP16MODLAB-<base version>` marker and a manifest of its base and recipes. On import the emulator rebuilds
it from the base and the recipes. An identical rebuild is verified and inherits the base's profile. A modified update
made any other way (edited by hand, a changed byte, a different tool) can be imported and run too: it is accepted when
it is intact and was encrypted with the correct key, and runs as **unverified** with the reason recorded in the
library. Repacked updates take recipes only (`files/` and `preload/` stay developer overlays).

An update made by other means can carry its own preload library. The emulator starts the application directly and never
runs the firmware's `apl_start`, so it honours that launcher itself: when the application's folder holds
`sp16-preload.so` and the firmware's `apl_start` contains the marker line `# SP16_PRELOAD_LAUNCHER`, the library is
loaded after the hardware shim (and before any developer-overlay libraries), and the console shows `SP16_FW_LAUNCHER
preload=<path>`. Both come from the hash-checked firmware image. A stock update carries neither, so it boots unchanged.

A repacked update contains the vendor's firmware. Keep it in `private/`, never distribute it and never flash it to a
device.
