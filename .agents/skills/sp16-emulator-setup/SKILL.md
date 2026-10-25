---
name: sp16-emulator-setup
description: Guide a user through setting up the SP16 Emulator on their Mac - check prerequisites, build, have the user download the firmware update and GPL source archive themselves, import, run, load samples onto the virtual USB stick, load a modified firmware (recipes, developer overlay, repacked update), run the hardware conformance suite, and troubleshoot, and collect diagnostics for a bug report. Use for "set up the emulator", "build it", "import my firmware", "it won't boot", "it crashed", "file a bug", "load my mod", "put samples on the USB stick", "run the hardware suite".
---

# SP16 Emulator setup

To take a user from a fresh clone to a running emulator with audio, firmware, samples and demo projects, use the
[sp16-bootstrap](../sp16-bootstrap/SKILL.md) skill (`./sp16 bootstrap`); this skill covers the details after that:
mods, the hardware suite, diagnostics and troubleshooting.

Work from the repository root. The user's README is the reference; this skill adds the order, the checks and the
rules. Everything goes through `./sp16` (`./sp16 help` lists every command; `./sp16 help <command>` shows its
options; `make help` lists the ready-made configurations). Commands assume the default build `build/sp16`; add
`--name <name>` for another. `./sp16 --dry-run <command>` prints the exact tool command without running it.

## Rules (always)

- **Firmware and the GPL archive are the user's to download.** Give them the two official links from the README and
  let them download. Download for them only after they explicitly confirm, naming the file and source first. The
  tool's own `./sp16 firmware download --version 1.60 --info` (then `--yes` after their yes) fetches only the
  official firmware update from the official server; the GPL archive and the key are never downloaded by it.
- **Never print, copy, log or commit key bytes.** Pass key files and archives by path only. Do not `cat`, `xxd`,
  `strings` or hash-print a key file. Do not describe how the update is encrypted.
- **Never commit** updates, keys, archives, imported libraries, data images, session folders or captures. Keep them
  in `build/` or `private/` (both git-ignored; create with `mkdir -m 700`).
- **Never flash hardware**, run a firmware's own scripts, or distribute a repacked update.
- **Audio:** when you launch to test, use builds made with `--audio file` (silent, records WAVs; `make build-record`).
  Use `--audio coreaudio` only when the user wants to hear it; `./sp16 audio devices` lists output devices.
- **Stopping:** stop an emulator only by its exact process id, found from its build path
  (`ps -axo pid,command | grep -F "<build dir>/"`), then confirm its `qemu-system-aarch64` child exits. Never quit by
  bundle id, never `pkill` or `killall`. Each running emulator uses about 3 CPU cores; keep at most three.

## 1. Check prerequisites

```sh
./sp16 doctor
```

It checks Apple silicon and macOS 13+, the Xcode Command Line Tools (`swiftc`, `codesign`, `/usr/bin/python3`), a
Python 3.12+ for the build tools, the QEMU build tools (`ninja`, `pkgconf`, `glib`, `pixman`), Docker running (builds
only), free disk space, and the git set-up (a clone whose `build/` is git-ignored). Report each `FAIL` with its `fix:`
line and stop until they are fixed. Typical fixes: `xcode-select --install`,
`brew install python@3.12 ninja pkgconf glib pixman`, start Docker Desktop.

## 2. Build

```sh
./sp16 build --audio file          # or: make build AUDIO=file  (build/sp16, silent, records WAVs)
```

`./sp16` creates `build/` owner-only. The first build takes a few minutes (QEMU from source, then the Docker guest
image). On failure read the log the error names in `build/<name>/` (`qemu-configure.log`, `qemu-build.log`,
`guest-build.log`, `swift-build.log`). Later builds reuse QEMU from an existing build automatically and share one
firmware library (`build/library`); `./sp16 builds` lists them. An existing build is never overwritten:
`--rebuild` rebuilds it in place and keeps the firmware library and data images.

## 3. Firmware: the user downloads it

Tell the user to download, from the links in the README:

1. the latest firmware update (unzip it; keep the `.UPD`);
2. the GPL source archive for this sampler (two zip parts), or use the key file they already have from it.

Ask where they saved them. Check the paths exist (`ls -l`), nothing more.

## 4. Import

```sh
./sp16 firmware import --update <path to .UPD> --gpl-archive <part 1 zip> <part 2 zip>   # or: --key-file <key file>
# or: make import UPD=<.UPD> GPL="<part 1 zip> <part 2 zip>"   (KEY=<key file> instead of GPL)
```

Read the JSON result: `firmware_imported` with an id and a report, or `firmware_refused` with the reason. Then
`./sp16 firmware list`. Typical refusals:

| Reason | Meaning |
|---|---|
| `update CRC does not match`, `not sector-aligned` | damaged or partial download: download again |
| `GPL archive is damaged or incomplete` | both parts are needed, part 1 first |
| `the key does not decrypt this update` | wrong key for this update |
| not an update for this sampler (application missing) | a different product's update |
| already imported | it is in the library; `firmware list` |

Any intact update that decrypts with the correct key imports. A version without a verified profile runs as
unverified (title "(unverified)", library status, a one-time notice); see section 7 to verify one.

## 5. Run

```sh
./sp16 run                     # or: make run;  --firmware <id>, --usb-image <file>, --ephemeral
```

Confirm it booted: the newest `build/sp16/sessions/native-*/console.txt` contains `SP16_NATIVE_READY`. The window
shows the panel; the LCD settles on the home screen after about 10 seconds.

For an unattended, scripted run of a research build (driven by `./sp16 ctl` or the control socket), add
`--no-ui-input`: the window then ignores clicks, scrolling, keys and accessibility actions and opens without taking
focus, so someone using the Mac cannot change what the script measures (details: `docs/architecture.md`, Bench mode).
If the run reads no audio, add `--no-audio-files` too: an audio build then writes no WAVs (about 64 MB a minute
otherwise) while its audio counters keep counting.

## 5a. Samples on the virtual USB stick

The sampler loads samples from a virtual USB stick: `build/stick.img`, a FAT32 image that `./sp16 run` attaches
when it exists (`--no-usb` skips it, `--usb-image <file>` uses another). Prepare it on the Mac, with the emulator
stopped:

```sh
./sp16 usb add <sample files>          # creates and formats build/stick.img the first time; --to <folder> for a subfolder
./sp16 usb ls                          # what is on the stick
# or: make usb-add FILES="<sample files>"
```

- Format: PCM 16- or 24-bit 44.1 kHz WAV (mono or stereo). Other WAV/AIFF files get one warning per kind, are copied, and are never converted;
  non-audio files are skipped unless `--any`. Samples go to `PIONEER DJ SAMPLER/Samples` on the stick; project files
  go with `./sp16 usb add <files> --dest Projects` (any file type; the path must stay under `PIONEER DJ SAMPLER`).
- **Never mount the stick while an emulator runs.** The commands refuse when a running emulator names the image (they
  print its process ids; stop an emulator only by those exact ids) or when the Mac already has the image attached
  (`./sp16 usb unmount`). Do not work around the refusal with `hdiutil` or Finder.
- `usb create --force` replaces an existing stick and deletes its content: ask the user first.
- Mount points live in `build/.usb-mnt` (removed again); `usb mount` prints the folder if the user wants Finder. When the clone is inside Downloads, Desktop, Documents or iCloud Drive, macOS refuses mounts there, so they go to `~/Library/Caches/sp16emu/usb-mnt` instead (the image is fine where it is).

## 6. Modified firmware

Follow `docs/modding.md`. The repository ships the mod tooling, not any mod: the user brings their own recipes,
files or preload sources (or a mod archive someone shared), always against firmware they imported themselves.
Short version, with a research build:

```sh
make build-dev                    # build/sp16-dev: research socket, WAV recording; shares build/library
./sp16 firmware select --id <id>  # a firmware matching the mod's base_profile (import first if the library is empty)
./sp16 mod new  --mod private/my-mod --id my-mod   # empty mod folder; add recipes/, files/ or src/ content
./sp16 mod check --mod private/my-mod              # preflight: wrong base, unknown paths, copied firmware files
./sp16 mod test --mod private/my-mod
```

Keep mod folders under the git-ignored `private/`. Base hashes for recipes are in
`build/library/<id>/manifest.json` (`files`); never copy firmware files into a mod's `files/` (`mod check` refuses
them). For a repacked update use `./sp16 mod package --upd ... --out private/<name>.UPD` and import it; a tampered copy
imports as unverified with the reason recorded. Never share a repacked update: it contains the vendor's firmware.

## 7. Hardware suite

Needs a `--research --audio file` build with the firmware imported, plus a control build made with `--uncalibrated`
sharing its library. Quit both apps first; the suite starts and stops its own sessions. Needs `pillow`, `numpy`
and `tesseract`.

```sh
make build-dev build-control      # build/sp16-dev and build/sp16-dev-uncal
./sp16 hwsuite --smoke         # --profile <version>, --firmware <library id>, --tier core|full
```

Exit 0 pass, 1 fail, 2 blocked (missing profile data), 3 environment, 4 session lost. Only `--tier full` with every
check passing can mark a profile verified (`--mark-verified`). Run it on an idle Mac. Details:
`docs/hardware-suite.md`; adding a version: `docs/architecture.md#adding-a-firmware-version`.

## 8. Collect diagnostics and file a bug

When the emulator crashes, will not boot or misbehaves, collect one zip for the bug report:

```sh
./sp16 diagnostics             # --sessions N, --include-audio, --out <zip>   (or: make diagnostics)
```

The app does the same from Help > Collect Diagnostics…, and offers it on the next start after an unclean exit. Read
the JSON result: `diagnostics_written` with the zip path (Desktop, or the build folder when the Desktop is not
writable). Then:

1. Tell the user what is inside: the report (versions, macOS, Mac model, firmware library summary), the last sessions'
   logs and screen picture, build logs, macOS crash reports. Text is redacted (home folder, account name, Mac name,
   e-mail addresses); firmware, updates, keys and data images are never included. Nothing is sent anywhere.
2. Suggest they look through the zip, then open an issue with the "Bug report" form and attach it. The form asks for
   the firmware version, kind and status from Firmware > Library and the macOS version.
3. To also prove the key is absent from the zip, add `--key-file <path>` (the key is not printed).

Do not attach, upload or post the zip yourself unless the user explicitly asks. Never attach or request firmware,
`.UPD` files, key files or data images; the tool refuses to include them.

## 9. Troubleshooting

See [references/troubleshooting.md](references/troubleshooting.md).
