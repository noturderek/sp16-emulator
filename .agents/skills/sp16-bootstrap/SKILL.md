---
name: sp16-bootstrap
description: Bootstrap the SP16 Emulator from a fresh clone to a running emulator with audio, the firmware version the user chooses, and a virtual USB stick filled with samples and demo projects. Use for "bootstrap the emulator", "set up the emulator", "install it", "get the emulator running", "get it running with sound", "put the factory samples and demo projects on it". Prompts the user for every manual step (firmware, key, samples). For troubleshooting, modded firmware and the hardware suite use sp16-emulator-setup.
---

# SP16 Emulator bootstrap

Goal: from this fresh clone to a running emulator that plays audio, runs the firmware the user picked, and has a USB
stick with their samples and the demo projects. Work from the repository root. Everything goes through `./sp16`;
one command, `./sp16 bootstrap`, does the deterministic work, and this skill gets the user's inputs first.

This repository ships no Pioneer-made firmware, keys or samples: **the user supplies them**. Tell the user so in your
first message.

## Rules (always)

- **Ask before every manual step and before installing anything** (Xcode Command Line Tools, Homebrew packages, Docker,
  Python). Never install silently.
- **Never print, paste, copy, log or commit key bytes.** Pass key files and archives by path only. Do not `cat`, `xxd`,
  `strings` or hash a key file. If the user asks how the key works or where it is inside the archive, say that you do not
  cover it here and that they can ask an agent or the community; never reveal or paste key bytes yourself.
- **Never commit** firmware, updates, keys, GPL archives, samples, projects, stick images or `build/`. They stay in
  `build/`, `private/` or the user's own folders (all git-ignored).
- **Downloads need the user's explicit yes, after you named the file, the source and the size.** The only thing the CLI
  downloads is the official firmware update from the official AlphaTheta server (section 3). Never download the key,
  the GPL archive, or sample packs for the user. Never create accounts or enter credentials for them.
- **Never flash hardware** or run a firmware's own update scripts.
- **Audio:** the default is `--audio coreaudio` (it plays through the Mac). If you are testing as an agent, and the user
  did not ask to hear sound, use `--audio file` (silent, records WAVs).
- **Never mount the stick while the emulator runs.** Stop an emulator only by its exact process id, found with
  `ps -axo pid,command | grep -F "build/sp16/"`, then confirm its `qemu-system-aarch64` child exits. Never quit by
  bundle id, never `pkill` or `killall`.

## 1. Check this Mac

```sh
./sp16 doctor
```

It prints `FAIL` lines with a `fix:` each. Walk the user through the ones you cannot do yourself, asking first:

| Missing | What to tell the user |
|---|---|
| Xcode Command Line Tools | run `xcode-select --install` (a macOS dialog appears); it takes a few minutes |
| Python 3.12 or later | `brew install python@3.12` (needs Homebrew, <https://brew.sh>) |
| QEMU build tools | `brew install ninja pkgconf glib pixman` |
| Docker running | start Docker Desktop or OrbStack and wait until it says it is running (every build needs it; the emulator itself runs without it) |
| Apple silicon, macOS 13+ | cannot be fixed; the emulator does not run on Intel Macs |

Run `./sp16 doctor` again until it prints `Ready.`.

## 2. Audio

Ask: "Should the emulator play through your Mac's default output?" Default yes (`coreaudio`). If they want a specific
device, run `./sp16 audio devices` and let them pick a UID (`--audio-device UID`). Mention that nothing is audible
until a track's VOLUME is raised in the emulator.

## 3. Firmware

Ask which version they want: **1.60** (latest, verified), **1.50** (also verified), or 1.40 / 1.31 (run, marked
"unverified"). Then ask this question, as written:

> Should I download the official firmware from AlphaTheta's official server for you, or will you download it yourself?

Make clear that the CLI fetches only the official update file, over https, from `downloads.support.alphatheta.com`,
and nothing else.

**If they want you to download it:**

```sh
./sp16 firmware download --version 1.60 --info      # prints file name, URL and size; downloads nothing
```

Show the user the file name, the URL and the size, and ask "Download it?". Only after a yes:

```sh
./sp16 firmware download --version 1.60 --yes       # saves and checks it in build/downloads/ver160/
```

(or pass `--download-firmware 1.60 --yes` to `bootstrap` in section 7). The tool checks the zip, that it holds exactly
one `.UPD`, the version, the CRC and, for 1.50 and 1.60, the SHA-256 the project pins. Note the `update` path in the
JSON it prints.

**If they download it themselves:** give them these official links.

- Firmware page: <https://support.alphatheta.com/en-us/articles/4405128836121>
- Direct downloads (same host the CLI uses):
  `https://downloads.support.alphatheta.com/firmwares/music-production/TORAIZ_SP-16/TORAIZSP16_ver160.zip`
  (also `ver150`, `ver140`, `ver131`).

They unzip it and keep the `.UPD` file. Ask where they saved it; check the path exists with `ls -l`, nothing more.

## 4. Key

The importer needs the key that the maker publishes inside its official GPL source archive for this sampler. It accepts
that archive directly, so nothing has to be extracted: give `--gpl-archive` the downloaded parts, in order. The
official page: <https://www.pioneerdj.com/en/support/open-source-code-distribution/gnu-open-source-license/>. Tell the
user to download the sampler's source archive (two zip parts) themselves, or to give you the key file they already
have. Ask where they saved the parts (in order) or the key. Check the paths exist; do not open them.

Say no more about the key than this.

## 5. Samples

Ask what they want on the virtual USB stick.

- **Official factory sample pack:** from Loopmasters at <https://www.loopmasters.com/register/TORAIZ>. It needs a free
  registration, so the user registers and downloads it themselves; you never create the account or enter anything.
  They give you the downloaded folder or `.zip`.
- **Their own samples:** folders or `.zip` files. Best format: 16- or 24-bit 44.1 kHz WAV.
- **Nothing:** the stick is optional; samples can be added later (section 8).

## 6. Demo projects (optional)

The official factory demo projects (five `.prj` files, 0.27 MB) come from AlphaTheta:
`https://downloads.support.alphatheta.com/other/TORAIZ_SP-16_Demo_Project_Data/TORAIZ_SP-16_Factory_Projects-B.zip`.
Offer to download that one file for them: state the file name, the source and the size (0.27 MB) and wait for a yes,
then `curl -fL --proto '=https' -o build/downloads/TORAIZ_SP-16_Factory_Projects-B.zip <url>`
(create the folder first with `(umask 077; mkdir -p build/downloads)`, so `build/` stays owner-only). Or they download it themselves. Their own project files also work.

Tell them: demo projects refer to samples on the sampler's internal storage, so after loading a demo they may need to
copy the sample folder from the USB stick to internal storage with the sampler's own copy function.

## 7. Confirm, then bootstrap

Read the collected paths back to the user and ask for a yes. Optionally show the plan first (it changes nothing):

```sh
./sp16 --dry-run bootstrap <options>
```

Then run it. Examples (adapt to what the user chose):

```sh
./sp16 bootstrap --update ~/Downloads/TORAIZSP16.UPD --gpl-archive ~/Downloads/gpl-part-1.zip ~/Downloads/gpl-part-2.zip \
  --samples "~/Downloads/LOOPMASTERS SAMPLE PACK" --projects ~/Downloads/TORAIZ_SP-16_Factory_Projects-B.zip
./sp16 bootstrap --download-firmware 1.60 --yes --key-file ~/keys/update.key --samples ~/Samples
# the same through make:  make bootstrap UPD=... GPL="part1 part2" SAMPLES=... PROJECTS=...   (or VERSION=1.60 YES=1 KEY=...)
```

It runs these steps in order, skips what is already done and says so, and can be run again after an interruption:
`doctor`, build `build/sp16` with the audio mode, import and select the firmware, create or top up `build/stick.img`
(samples under `PIONEER DJ SAMPLER/Samples/<folder name>`, projects under `PIONEER DJ SAMPLER/Projects`; zips are
unpacked safely into a temporary folder that is deleted again), launch. The first build takes a few minutes (QEMU is
compiled, then Docker builds the guest image). If it stops, read the message: it names the fix, and the same command
continues where it stopped. An existing build made with another audio mode is not touched unless you add `--rebuild`.
`--stick-size SIZE` sizes a new stick (64M to 32G; default: the smallest of 1G to 32G that holds the files),
`--firmware-name TEXT` names the imported firmware, and `--no-run` does everything except the launch.

If the user cannot hear anything: raise the track VOLUME in the emulator (the first launch of a firmware has no saved
project, and the mixer then starts at volume 0), check the Mac's output device, and see the
README's "Audio" section and the sp16-emulator-setup skill's troubleshooting table ("Slow tempo or crackling").

## 8. You are running: checklist for the user

Tell them:

- **First clicks:** raise a track's VOLUME; load samples from the USB stick with the sampler's own browser; load a demo
  project (copy its samples to internal storage if the sampler asks). The window title says "(unverified)" for firmware
  without a verified profile. **Firmware > Library** in the app menu lists and switches the imported firmware.
- **Where things live:** the build in `build/sp16/`, the stick in `build/stick.img`, the imported firmware and its data
  in the shared library `build/library`, downloads in `build/downloads/`. All git-ignored.
- **Stop:** close the emulator window (or quit the app). **Restart:** `./sp16 run`.
- **Add samples later:** quit the emulator first, then `./sp16 usb add <files or folders>`; project files with
  `./sp16 usb add <files> --dest Projects`; `./sp16 usb ls` lists the stick. Never mount the stick while the
  emulator runs; the commands refuse when it does.
- **Switch firmware:** `./sp16 firmware list`, `./sp16 firmware select --id <id>`, restart; or import another
  version with `./sp16 bootstrap --no-run ...`.
- **Problems:** `./sp16 diagnostics` collects a redacted zip for a bug report; see the sp16-emulator-setup skill.
- **Rules:** never commit firmware, keys, samples or projects; never flash a real device with anything from here.
