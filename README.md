# SP16 Emulator

A macOS emulator of the sampler hardware that is compatible with the TORAIZ SP-16. It emulates the machine (panel,
pads, encoders, lamps, LCD, touch strip, audio codec, USB stick and internal storage) and boots a firmware update
that **you** supply. The application, its libraries and everything else that runs inside the emulated machine come
from that update; this repository contains none of it.

> **Not affiliated.** This project is independent and is not affiliated with, endorsed by or supported by
> AlphaTheta Corporation, Pioneer DJ or the makers of the TORAIZ SP-16. It ships no firmware, no firmware-derived
> files, no key material and no vendor logos. The repository ships no vendor logos or artwork. For a personal build
> you may draw your own from `private/skin/overlay.png`; never commit it. You download the firmware update and the
> GPL source archive yourself, from the official pages linked below, and use them under their own terms.

## What you need

- A Mac with Apple silicon (the emulator uses Hypervisor.framework), macOS 13 or later.
- Xcode Command Line Tools (`xcode-select --install`): Swift, `codesign`, `make`, and `/usr/bin/python3` (Python 3.9),
  which is enough to start `./sp16` and which the app uses to import firmware.
- Python 3.12 or later for the build tools (for example `brew install python@3.12`). `./sp16` finds it by
  itself; `./sp16 doctor` tells you if it is missing.
- QEMU build dependencies: `brew install ninja pkgconf glib pixman`. QEMU itself is downloaded from
  download.qemu.org as source and built on your Mac during the first build.
- Docker Desktop (or another Docker engine, such as OrbStack), running during every `./sp16 build`: it builds the
  Linux guest base image from Debian packages and this project's code (later builds reuse Docker's cache). `./sp16 mod
  sdk` uses it too. A built emulator runs without Docker.
- About 3 GB of free disk space.
- `pillow` is needed by `emulator/layout_overlay.py` (the panel overlay) as well as the hardware suite:
  `python3 -m pip install pillow`. For the suite, `numpy` is also needed (`./sp16 doctor` prints the exact install
  command for it) and `brew install tesseract`.

## Quick start

Run the CLI from the repository root as `./sp16`; no global installation is needed.

### The fast way: bootstrap

Ask your agent (Claude Code, Codex or another) to **run the `sp16-bootstrap` skill** ([.agents/skills/sp16-bootstrap/SKILL.md](.agents/skills/sp16-bootstrap/SKILL.md)).
It checks your Mac, asks which firmware version you want (and offers to fetch the official update from the official
server, or points you to the download), tells you where to get the key source and the factory samples and demo
projects, then builds the emulator with audio, imports and selects your firmware, fills a virtual USB stick and starts
it. Or run the one command yourself, with files you downloaded:

```sh
make bootstrap UPD=~/Downloads/TORAIZSP16.UPD GPL="~/Downloads/gpl-part-1.zip ~/Downloads/gpl-part-2.zip" \
  SAMPLES="~/Downloads/LOOPMASTERS_SAMPLE_PACK" PROJECTS=~/Downloads/TORAIZ_SP-16_Factory_Projects-B.zip
# or have it download the official firmware (it shows the file, source and size first; YES=1 skips the question):
make bootstrap VERSION=1.60 KEY=~/keys/update.key SAMPLES=~/Samples
# the same without make:  ./sp16 bootstrap --update FILE.UPD --gpl-archive PART1 PART2 --samples DIR|ZIP --projects DIR|ZIP
```

`SAMPLES` and `PROJECTS` take folders or `.zip` files (a folder keeps its name under `PIONEER DJ SAMPLER/Samples`;
projects go to `PIONEER DJ SAMPLER/Projects`). Audio defaults to your Mac's output (`AUDIO=file` records silently).
`./sp16 --dry-run bootstrap ...` lists every step first. Every step skips what is already done, so the same command
can be run again after an interruption or to add more samples. For problems after that, the
[`sp16-emulator-setup`](.agents/skills/sp16-emulator-setup/SKILL.md) skill covers troubleshooting, modded firmware and the
hardware suite. The steps below are the same thing by hand.

### 1. Build

```sh
git clone https://github.com/noturderek/sp16-emulator.git
cd sp16-emulator
./sp16 doctor     # checks your Mac and prints the fix for anything missing
./sp16 build
```

(`make build` does the same; `make help` lists the ready-made configurations.)

The first build downloads and compiles QEMU and builds the guest base image with Docker: a few minutes on a recent
Mac (about 2 minutes measured), longer while Docker first downloads the Debian packages. The result
is `build/sp16/SP-16 Compatible Emulator.app`. Everything under `build/` is ignored by git and readable only by
you; the build tool refuses any other place, and `./sp16` creates `build/` for you.

Every later build (`--name` picks the folder, `./sp16 build --name second`) copies the verified QEMU from an
existing build instead of compiling it again, and all builds share one firmware library, `build/library`, so you
import firmware once. `./sp16 builds` lists your builds and how each was made.

The build is silent by default. Add `--audio file` to record each audio stream to a WAV file in the session folder
(still silent), or `--audio coreaudio` to hear it through your Mac (see [Audio](#audio)).

### 2. Get the firmware update and the GPL source archive

Download both yourself:

- **Firmware update.** The official firmware page:
  <https://support.alphatheta.com/en-us/articles/4405128836121>. Download the latest update (1.60), unzip it and
  keep the `.UPD` file. Version 1.50 is also supported if you have its update file. The direct official downloads are
  `https://downloads.support.alphatheta.com/firmwares/music-production/TORAIZ_SP-16/TORAIZSP16_ver160.zip` (also
  `ver150`, `ver140`, `ver131`). Or let the tool fetch it: `./sp16 firmware download --version 1.60` shows the
  file name, the URL and the size and asks first (`--info` only shows them, `--yes` agrees); it downloads only from
  that official host, over https, checks the zip, the version, the CRC and (for 1.50 and 1.60) the SHA-256 the
  profiles pin, and keeps the `.UPD` in `build/downloads/ver160/` (one folder per version; `--to DIR` saves it
  elsewhere). It never downloads the key or the GPL archive.
- **GPL source archive.** The official open source page:
  <https://www.pioneerdj.com/en/support/open-source-code-distribution/gnu-open-source-license/>. Download the
  TORAIZ SP-16 source archive (two zip parts). Keep both parts, in order.

The importer needs your update file plus the GPL source archive, or the key file from that archive if you already
have it. It reads the key in memory for the import only; the key is never written to the firmware library, the
manifest, logs or the app.

Keep the update, the archive and anything you import in private folders. Do not commit or share them.

### 3. Import

Either start the app (step 4) with an empty library and use its import screen (choose the `.UPD`, then the key file
or both GPL archive parts), or use the command line:

```sh
./sp16 firmware import \
  --update ~/Downloads/TORAIZSP16.UPD --gpl-archive ~/Downloads/gpl-part-1.zip ~/Downloads/gpl-part-2.zip
# or: --key-file <key file>
# or: make import UPD=~/Downloads/TORAIZSP16.UPD GPL="~/Downloads/gpl-part-1.zip ~/Downloads/gpl-part-2.zip"
```

The import takes about 10 seconds. It checks the update, unpacks it inside the emulator's own Linux guest, and
stores a read-only firmware image and a manifest in the library. It prints a compatibility report. Any update that
is intact and was encrypted with the correct key is accepted, whoever made it. Refused imports (damaged or truncated
file, wrong key, not an update for this sampler, already imported) leave nothing behind.

### 4. Run

```sh
./sp16 run          # or: make run
```

or open `build/sp16/SP-16 Compatible Emulator.app`. The first boot creates a 1 GB internal storage image for that
firmware; projects and settings persist there across launches. Close the window to stop the emulator. If you built
under another name, `./sp16 run --name <name>` launches that one; if the build does not exist, the error says
which `./sp16 build` command creates it.

**What a start looks like.** The panel starts with the filter wide open (DRIVE, RESONANCE and HIGH PASS CUTOFF at
minimum, LOW PASS CUTOFF at maximum, MAIN OUT centred) and ACTIVE off: the firmware lights ACTIVE at every start, so the
emulator presses it once after boot (see [the analog board model](docs/architecture.md#analog-board-model)). A normal
launch resumes the project saved in the firmware's data image, so the mixer comes back as you left it. `./sp16 run
--ephemeral` uses a disposable data image instead: nothing you do persists and there is no saved project, so the mixer
starts at volume 0 (track 1 reads VOLUME 0). Use it for scripted runs and for a second app, since two apps cannot share
one firmware's data image.

`./sp16 firmware list` lists imports; `select`, `rename`, `remove`, `show` and `check --id <id>` manage them. In
the app, Firmware > Library does the same.

### 5. Load your own samples (optional)

The emulated sampler reads samples from a virtual USB stick: a FAT32 disk image on your Mac, `build/stick.img`. Put
your samples on it before you start the emulator, then run as usual:

```sh
./sp16 usb add ~/samples/*.wav     # creates build/stick.img the first time, then copies the files
./sp16 run                         # attaches build/stick.img automatically
# or: make usb-add FILES="~/samples/*.wav" && make run
```

- **Format:** PCM 16- or 24-bit 44.1 kHz WAV, mono or stereo (AIFF is accepted too). WAV and AIFF files in another
  sample rate or codec (float, compressed) are copied with one warning per kind, never converted; convert them first if the sampler will not load them. Files that are not WAV or AIFF are
  skipped with a warning unless you add `--any`.
- **Where they go:** the firmware's own `PIONEER DJ SAMPLER/Samples` folder on the stick (`--to kicks` copies into a
  folder `kicks` under it; `--dest Projects` copies any file type into another folder under `PIONEER DJ SAMPLER`, for
  example demo projects, and cannot leave that folder). `./sp16 usb ls` lists what is on the stick, including projects the sampler saved to
  it. macOS's `._*` and `.DS_Store` files are removed again after each copy.
- **Whole folders and big packs:** a folder passed to `usb add` is copied with its name and its structure;
  `./sp16 usb add --tree ~/SamplePack --to Pack` copies what is inside the folder, at any depth, into
  `Samples/Pack`. A new stick is sized for the files (at least 1G). The stick can be up to 32G (`usb create --size 8G`;
  the image is sparse, so it takes disk space only for what you copy), and a stick above 4 GB works as long as each
  file is under 4 GB (a FAT32 limit). The free space is checked before anything is copied; if the files do not fit,
  nothing is copied and the message suggests `usb create --size NG --force` (which replaces the stick) or a new
  `--image`.
- **Never mount the stick while the emulator runs.** The emulated sampler and your Mac would both write to one file
  system and corrupt it. `usb add`, `ls`, `create` and `mount` refuse while a running emulator has the image, or while
  your Mac has it mounted elsewhere. Quit the emulator first (close its window), or release the stick with
  `./sp16 usb unmount`.
- `./sp16 usb create [--size 4G] [--force]` makes an empty stick without adding anything (it never overwrites an
  existing image unless you add `--force`); `./sp16 usb mount` mounts it in `build/.usb-mnt` (in
  `~/Library/Caches/sp16emu/usb-mnt` when the clone is inside Downloads, Desktop, Documents or iCloud Drive) and prints
  the folder if you would rather copy with Finder, and `usb unmount` releases it. `--image PATH` works on another image.
- `./sp16 run` attaches `build/stick.img` when it exists. `--no-usb` starts without it and `--usb-image PATH`
  attaches another image. If another running emulator already uses the stick, `run` says so and starts without it.

## Audio

A build chooses its audio at build time:

| Build option | What happens |
|---|---|
| (none) | Silent, nothing recorded. |
| `--audio file` | Each audio stream is recorded to a WAV file in the session folder (`build/<name>/sessions/`). Silent. |
| `--audio coreaudio` | Plays through your Mac's default output. **Audible.** |
| `--audio both` | Plays and records. |
| `--audio-device UID` | With `coreaudio` or `both`: play through this output device instead of the default. |

List your Mac's output devices and their UIDs with `./sp16 audio devices` (the first call compiles a small tool
into `build/.cache`, a few seconds), then for example:

```sh
./sp16 build --name loud --audio coreaudio --audio-device <UID>
./sp16 run --name loud
```

`make build-audio` and `make build-record` build the two variants as `sp16-audio` and `sp16-record`.

With `coreaudio` or `both`, the app's **Output** menu (and `./sp16 ctl monitor MAIN|PHONES|OUT3-4|OUT5-6|OUT7-8`) selects
which jack pair plays through your Mac. The master stream sends all 16 channels by default; MAIN OUT scales OUT1-2
(channels 12 and 14). Research builds can inject an input WAV (`input.wav`, up to 64 MB) from a `--dev-overlay` folder,
metered with peak-equivalent dBu readouts (`in_dbu_peak`, `out_dbu_peak`); see [docs/audio-jack-map.md](docs/audio-jack-map.md).

## Commands and Makefile

`./sp16 help` lists every command; `./sp16 help <command>` (or `<command> --help`) shows its options and
examples. `./sp16 --dry-run <command> ...` prints the exact tool commands instead of running them, and `--verbose`
prints each one as it runs. `SP16EMU_BUILD_DIR` moves the build folder (default `build/`) and `SP16EMU_PYTHON` names
the Python 3.12+ for the build tools.

| Command | What it does |
|---|---|
| `doctor` | Check your Mac (Apple silicon, macOS, Command Line Tools, Python, Docker, disk space, git) and print the fixes |
| `build` | Build the app into `build/<name>`: `--audio`, `--audio-device`, `--research`, `--trace` (strace, gdb, pcmtap, exec), `--app-arg` (research: start the app with an argument such as `testmode`), `--uncalibrated`, `--pad-calibration-value`, `--bundle-suffix`, `--library`, `--reuse-qemu-from`, `--no-reuse-qemu`, `--rebuild` |
| `run` | Launch a build: `--firmware`, `--usb-image` (default `build/stick.img` when it exists), `--no-usb`, `--ephemeral`, `--dev-overlay`, `--no-ui-input` (bench mode: the window ignores input, for scripted runs), `--no-audio-files` (audio builds: count the audio, write no WAV), `--net` (none, host, bridged:IF, mcast), `--net-ip`, `--midi` |
| `bootstrap` | Everything in one go: `doctor`, build with audio, import and select firmware (`--update` or `--download-firmware`), fill the stick (`--samples`, `--projects`, folders or zips), launch; skips what is done, `--dry-run` lists the steps |
| `usb create\|add\|ls\|mount\|unmount` | Prepare the virtual USB stick on the Mac without booting: load samples (`add --dest Projects` for project files), list, mount (see [Load your own samples](#5-load-your-own-samples-optional)) |
| `builds` | List builds and the options each was built with |
| `audio devices` | List audio output devices (UIDs for `--audio-device`) |
| `firmware import\|list\|show\|check\|select\|rename\|remove\|profile\|audit` | Manage the shared firmware library |
| `firmware download --version 1.60` | Fetch the official firmware update from the official server (asks first; `--info`, `--yes`) |
| `mod new\|check\|run\|test\|sdk\|package` | Develop and test firmware mods |
| `ctl` | Drive a running research session (buttons, encoders, LCD, power, standby, pressure, calibrate, fault, monitor, usb-oc) |
| `midi` | Drive the emulator from a MIDI controller: list ports, learn a controller profile, mirror an SP-16, run the bridge (`ports`, `learn`, `preset`, `run`) |
| `hwsuite` | Run the hardware conformance suite (supports `--tier extra`, `--testmode-build`, `--shots record\|compare`) |
| `smoke` | Run the end-to-end smoke test and record a sped-up screencast |
| `diagnostics` | Collect a redacted bug-report zip |
| `test` | Run the firmware-free unit tests (`--coverage` measures coverage) |
| `clean` | Delete build outputs; the firmware library and data images are kept unless you ask for `--everything` |

`make help` lists the ready-made configurations (the Makefile only calls `./sp16`; `make -n <target>` shows the
exact command). `make install` links `sp16` into `~/.local/bin` (`PREFIX=` changes it; `make uninstall` removes
it); no shell files are touched.

| Target | Builds or runs |
|---|---|
| `make bootstrap UPD=... GPL="p1 p2" SAMPLES=... PROJECTS=...` | Everything: build with audio (`AUDIO=file` for silent), import firmware (`VERSION=1.60 YES=1 KEY=...` downloads it), fill the stick, run |
| `make build` | `build/sp16`, silent (`AUDIO=file` to record; `REBUILD=1` rebuilds in place) |
| `make build-audio` / `make build-record` | `build/sp16-audio` (audible) / `build/sp16-record` (WAV files) |
| `make build-dev` / `make build-control` | `build/sp16-dev` (research socket, WAV files) and `build/sp16-dev-uncal` for the hardware suite |
| `make run`, `run-audio`, `run-dev` | Launch the matching build (`FIRMWARE=`, `USB_IMAGE=`) |
| `make usb-add FILES="a.wav b.wav"` | Copy samples onto the stick (creating `build/stick.img` if needed); `usb-create`, `usb-ls`, `usb-mount`, `usb-unmount`; `USB_IMAGE=` picks another image |
| `make import UPD=... KEY=...` (or `GPL="part1 part2"`) | Import firmware |
| `make list`, `doctor`, `diagnostics`, `test`, `coverage`, `smoke`, `hwsuite-smoke`, `audio-devices` | Information, checks and tests |
| `make clean` / `make clean-all CONFIRM=1` | Delete build outputs / everything in `build/` including firmware |

The root `sp16` Python launcher dispatches to `emulator/sp16emu.py`. That CLI calls
`emulator/native.py` (`build`, `launch`, `firmware`, `diagnostics`), `emulator/fwdev.py`, `emulator/hwsuite.py` and
`emulator/sessionctl.py`; any option those tools have that is not listed in `./sp16 help` can be passed after `--`.

## Supported firmware versions

A version is **verified** when it has a verified compatibility profile in `emulator/profiles/` (one that passed the
full hardware conformance suite). Verified today: **1.50 and 1.60**. Any other update that was encrypted with the
correct key also runs, as **unverified**: it uses the measurements of the reference profile (1.60), the window title
says "(unverified)", the library lists it as unverified, and the app shows its compatibility report once. The
integrity checks are the same for every version (a changed library image is refused at boot).

| Version | Profile | Status |
|---|---|---|
| 1.60 | `sp16-1.60.json` | verified |
| 1.50 | `sp16-1.50.json` | verified |

To turn an unverified version into a verified one, see [docs/architecture.md](docs/architecture.md#adding-a-firmware-version).

## Current state

**Works** (checked on firmware 1.60):

- Boot to a settled home screen with live lamps; buttons, pads, encoders, the selector, the touch strip; LCD with
  calibrated, debounced touch (delivered as RX frame words); Utility LCD brightness dimming the panel; internal storage
  that persists across launches.
- App shell and menus: View > Lamp Glow (off by default), View > Rear I/O Panel (Cmd-R), View > Private Skin Overlay
  (`private/skin/overlay.png`), Output monitor menu (MAIN, PHONES, OUT 3-4, OUT 5-6, OUT 7-8), Firmware > Power On
  With Keys Held (Normal, Test Mode, Update Mode).
- Power visual states: BOOT overlay, RUN, OFF on standby (dark panel, WAKE UP lit steady red), FAULT (`ctl fault on`:
  panel dark, WAKE UP blinking red at ~1 Hz); power/standby key (`./sp16 ctl press power` or `ctl standby yes`).
- Audio: three playback streams, silent, recorded to WAV files or played through CoreAudio (`--audio`); all 16 master
  channels streamed; host Output monitor menu; CoreAudio power-on mute held until playback starts.
- Virtual USB stick: mounts and hot-attaches at runtime (`virtio-blk-pci`, `ctl usb attach PATH` and USB > Attach USB
  Image...); over-current testing with `./sp16 ctl usb-oc on|off`.
- Analog board: MAIN OUT scales OUT1-2 (channels 12 and 14); INPUT LEVEL and soft input limiter on GPIO5_IO06;
  peak-equivalent dBu readouts (`in_dbu_peak`, `out_dbu_peak`); input injection from `input.wav` in `--dev-overlay`
  (up to 64 MB); HP CUTOFF DAC code; filter loop passes a research test tone through low-pass and high-pass.
- MIDI controller bridge: `./sp16 midi` maps CoreMIDI controllers to emulator actions with lamp LED echo; built-in
  SP-16 mirror preset.
- Factory test mode (`--research --app-arg testmode`): PAGE 8 DEVICE TEST MIDI check (touch button at 115, 112)
  verified live against DIN loopback; hardware suite extras tier checks HW-47..HW-54.
- Research tools: the control socket (`./sp16 ctl` including `power`, `standby`, `pressure`, `calibrate`, `fault`,
  `monitor`, `usb-oc`), `--trace` (strace, gdb, pcmtap, exec), the hardware suite with screenshot regression layer
  (`--shots`), smoke test and mod tooling.

**Not there yet:**

- LOW PASS CUTOFF, RESONANCE, HIGH PASS CUTOFF and ACTIVE do not change the sound of normal sampler playback: in the
  sessions tried nothing the firmware plays reaches the filter loop, which so far only carries the research
  `filter_tone`. The cutoff and resonance laws of the model are assumptions, not measurements. DRIVE has no emulator
  model: the firmware reads the pot as an ADC word and applies it itself.
- Audio input and live sampling: a research test tone or injected `input.wav` stands in for the input ADC; master
  channels map to output jacks (see [docs/audio-jack-map.md](docs/audio-jack-map.md)); output routing beyond the
  monitored pairs is not emulated.
- DIN MIDI and USB-B: DIN MIDI transport (`--midi`) provides host virtual CoreMIDI endpoints, but firmware-side MIDI
  discovery, sequencer routing and audio-position clock output remain parked. USB-B MIDI device emulation is not
  implemented.
- Pro DJ Link: guest NIC (`--net host|bridged:IF|mcast`) and GLINK/ARP compatibility are present, but the firmware does
  not emit port 50000 UDP keep-alives; tempo following and Ableton Link sync remain parked.
- A version is only `verified` by a full-tier hardware suite run ([docs/hardware-suite.md](docs/hardware-suite.md)).

## Load a modified firmware

The repository ships the tooling to develop and run mods, not any mod. You write your own (or use a mod archive
someone shared) against firmware you imported. There are three ways, described in [docs/modding.md](docs/modding.md):

1. **Recipes**: small JSON patch files (base hash, offset or byte pattern, replacement, expected result hash),
   applied when the app starts.
2. **Developer overlay**: a folder of replaced files, recipes and preload libraries shared into a running emulator;
   edit, then restart only the firmware's app (about 0.2 s; the screen settles a few seconds later).
3. **Repacked update**: `./sp16 mod package --upd` builds a modified update from a verified base and your recipes.
   It carries an `SP16MODLAB-<version>` marker, so it can never pass for an official update. The importer rebuilds it
   from your verified base: an identical rebuild is verified, anything else runs as unverified with the reason
   recorded. Repacked updates are for this emulator only:
   never flash them to hardware and never distribute them (they contain the vendor's firmware).

## Smoke test and screencast

`./sp16 smoke` (or `make smoke`) boots a research build for about a minute and checks that the emulator runs and
responds: a settled home screen, live lamps, a selector detent that changes the LCD, pad presses and PLAY/STOP
reaching the firmware, the mounted USB stick and a clean stop. It records the whole panel through the control socket
and writes a sped-up screencast (`screencast.mp4` and `screencast.gif`, 4x by default; `--speed N`) with a report to
`build/sp16-dev/smoke/<run id>/`. It needs a `--research` build (`make build-dev`), imported firmware and, for the
screencast, ffmpeg (`brew install ffmpeg`); it never plays sound.

## Hardware conformance suite

`emulator/hwsuite.py` checks that the emulated hardware behaves correctly for a given firmware (panel wire protocol,
lamps, encoders, pads, strip, audio clock, USB, storage, power, driver coverage) without testing that firmware's
features. It runs against a build made with `--research --audio file` plus an `--uncalibrated` control build that
shares its library, and starts its own sessions:

```sh
make build-dev build-control      # build/sp16-dev and build/sp16-dev-uncal, sharing build/library
./sp16 hwsuite --smoke         # profile = the version of the selected firmware; --profile 1.60 to choose
```

The smoke tier takes about 8 minutes. Without the control build, check HW-24 (uncalibrated pads) is `BLOCKED` and the
run exits 2, which is not a failure. See [docs/hardware-suite.md](docs/hardware-suite.md) for the full steps.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Anything fails before or during a build | Run `./sp16 doctor`: it checks the Mac, Command Line Tools, Python, QEMU tools, Docker, disk space and the git set-up, and prints the fix for each. |
| `is not a git work tree` or `must be Git-ignored` or `owner-only permissions` | Use a git clone of this repository (the build tool only writes into the git-ignored `build/`); `./sp16` creates `build/` owner-only, or fix it with `chmod 700 build`. |
| `without symlink parents` | The clone's path must not pass through a symlink (on macOS `/tmp` is one; clone into your home folder). |
| `this native HVF build currently requires an Apple Silicon Mac` | Intel Macs are not supported. |
| `the build tools need Python 3.12 or later` | `brew install python@3.12`; or name one with `./sp16 --python /path/to/python3.12 ...` (or `SP16EMU_PYTHON`, `make TOOL_PYTHON=...`). |
| `build/sp16 already exists` | `./sp16 run` launches it; `./sp16 build --rebuild` rebuilds it in place and keeps the firmware library and data; `--name` makes another build. |
| QEMU build fails | Read `build/<name>/qemu-configure.log` or `qemu-build.log`; usually a missing `ninja`, `pkgconf`, `glib` or `pixman` (`./sp16 doctor` checks them). |
| Guest image build fails | Start Docker Desktop and retry; read `build/sp16/guest-build.log`. |
| Window title says `(unverified)` | The version has no verified profile (see the table above); it runs on the reference measurements. `./sp16 firmware check --id <id>` prints the report. |
| Import: `update CRC does not match` or `not sector-aligned` | The update file is damaged or incomplete; download it again. |
| Import: `GPL archive is damaged or incomplete` | Give both zip parts, in order (part 1, then part 2). |
| Import: `the key does not decrypt this update` | The key does not belong to this update. |
| The app's import screen says Python is missing | Install the Xcode Command Line Tools (`xcode-select --install`). |
| Pads blink red and ignore hits | The firmware reports missing pad calibration; rebuild without `--uncalibrated`. |
| `usb ...: is in use by a running emulator` | The emulator (or a leftover QEMU) has the stick. Quit it, stopping only the process ids the message names, then repeat the command. Mounting a stick the emulator writes to would corrupt it. |
| `usb ...: already attached to this Mac` | Release it with `./sp16 usb unmount`, then repeat. `./sp16 run` also starts without the stick (and says why) while the Mac holds it. |
| `usb ...: cannot attach` | The image has no FAT32 file system (for example a zero-filled file). Recreate it with `./sp16 usb create --force`, or `usb add` formats a blank image by itself. |
| `usb ...: cannot mount ... privacy protection` (clone in Downloads, Desktop, Documents or iCloud Drive) | macOS privacy protection refuses some disk operations in those folders, even though the image itself is fine (`./sp16 doctor` warns about the location). Move the clone (for example to `~/src/sp16-emulator`) or give your terminal app Full Disk Access (System Settings > Privacy & Security), then repeat. |
| The window opens but the emulator never boots, and `qemu-stderr.txt` says `Failed to get "consistent read" lock` | Another emulator already uses this firmware's data image, or the Mac has the image attached (for example after an `hdiutil attach`). Quit the other emulator, stopping only its process ids, or start the second one with `--ephemeral`. `hdiutil info \| grep -F emmc.img` shows an attachment; `diskutil eject <device>` releases it. |
| The mixer shows volume 0 | You started with `--ephemeral` or on a brand-new data image, which has no saved project yet. A normal launch resumes the saved one. |
| The window is black | Read `console.txt` in the newest folder under `build/sp16/sessions/`. |
| The app quit or crashed | Start it again: it offers to collect diagnostics. Then see [Reporting a bug](#reporting-a-bug). The newest folder under `build/sp16/sessions/` holds `app.log` (the app's log) and `qemu-stderr.txt`. |

## Reporting a bug

If the emulator crashes, will not boot, or behaves differently from the real hardware, collect diagnostics and attach
them to an issue (the bug report form asks for them).

1. In the app choose **Help > Collect Diagnostics…**. If the app did not shut down cleanly last time, it offers this
   itself the next time it starts. Without the app (for example when it will not start), run
   `./sp16 diagnostics` (or `make diagnostics`). Options: `--sessions N` (how many recent sessions,
   default 3), `--include-audio` (adds the sessions' WAV captures, which can be large), `--out file.zip`.
2. The result is one zip, `sp16-diagnostics-<time>.zip`, written to your Desktop (macOS may ask whether the app may
   access the Desktop; if you decline, or there is no Desktop, it is written to the build folder instead). The app
   shows it in Finder.
3. Open an issue with the "Bug report" form and attach the zip. Look through it first: it is an ordinary zip.

**Nothing is sent automatically.** The tool only writes the zip on your Mac.

What is in the zip: `report.json` (emulator version, build options, macOS and Mac model, CPU, free disk, Python,
whether Docker is installed, QEMU version, and the firmware library: name, version, kind, verified or unverified with
the reason, the update, application and image hashes and the compatibility findings); for the last sessions the guest
console, metrics, runtime description, the app's own log, QEMU's error output and the last screen picture; the app logs
and build logs (the last 500 lines of each); and macOS crash reports of the last 14 days for the app and QEMU.

What is never in it: firmware, updates (`.UPD`), key files or GPL archives, the firmware image, or your data image
(projects and settings). The tool refuses any file named `*.key`, `*.upd`, `*.sqfs`, `*.img`, `*.iso`, `*.sfs` or
`*.mot`. Text files are redacted: your home folder becomes `~`, your account name `<user>`, your Mac's name
`<host>`, your full name `<name>` and e-mail addresses `<email>`; the machine identifiers macOS writes into crash
reports are blanked, and an audio output device you chose is reported only as `<set>` (its UID can contain a serial
number). Add `--key-file <your key file>` to the command to
also prove that the key does not occur anywhere in the zip (the key is never printed). Paths outside your home folder,
such as an external drive's name, are kept.

**Never attach firmware, update files, key files or data images to an issue**, whatever a maintainer asks.

## Repository layout

```text
sp16, Makefile    the command line (`./sp16 help`) and the ready-made build configurations (`make help`)
emulator/            sp16emu.py (the command), build tool, firmware importer, Linux guest base, hardware shim, panel model, macOS app,
                     compatibility profiles, developer tools, hardware suite, unit tests
docs/                Python CLI/setup, architecture, modding guide, hardware suite, profile reference
.agents/skills/      agent skills: sp16-bootstrap (fresh clone to a running emulator) and sp16-emulator-setup (setup details, mods, troubleshooting)
```

See [Python CLI and Setup](docs/python-cli-and-setup.md) for the launcher layout, PATH installation and prerequisites.

Run the firmware-free unit tests with `./sp16 test` (or `make test`); it runs
`python3 -m unittest discover -s emulator/tests -p 'test_sp16_*.py'` with a Python 3.12 or later. `./sp16 test
--coverage` (or `make coverage`) also measures line and branch coverage with coverage.py and writes
`build/coverage/coverage.xml`; CI shows the coverage table in each run's summary.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): no firmware or firmware-derived material, no mods, and `./sp16 test` before a
pull request.

## License

The code in this repository is released into the public domain under the [Unlicense](LICENSE). Third-party components that the
build downloads and builds on your Mac (QEMU, Debian packages in the guest base image) keep their own licences; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Firmware you import is not covered by either and remains subject
to its owner's terms.
