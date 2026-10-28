---
title: "Lane B2: routing preferences, HW-35, USB-B note output and the measurement follow-ups"
description: "MIDI routing preferences window with a port table and clock readout, HW-35 marked known-failing with the measured 5 dB gap, USB-B note output investigated, and the MIDI review follow-ups."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report]
author:
  harness: pi
  agent: lb2-features
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane B2 hand-back

Branch `lane/lb2-features`, cut from `emu/2026-10-28` (`d6058eb`). Three commits, one per completed task,
plus this file. One research build was made (`build/lb2`, `--research --audio file`, QEMU reused from
`build/qemu-source`) and booted once to investigate the live USB-B and measurement tasks.

## Per task

### 1. `pt-i44.77` — HW-35 known-failing, recording the measured gap: `6a8a348`

`emulator/hwsuite.py`, `emulator/profiles.py`, `emulator/profiles/sp16-1.60.json`, `emulator/tests/test_sp16_hwsuite.py`,
`emulator/tests/test_sp16_profiles.py`.

The owner asked to mark HW-35 known-failing so verification passes while the ~5 dB level gap is investigated. A profile
can now declare `hardware.known_failing` (check, reason, gap, owner, date). When a check fails and is declared, the
runner re-labels its FAIL verdict **KNOWN-FAIL**, keeps the measured figures (the sub-results still carry the live
peak in dBFS) and records the declaration in the reason and evidence. KNOWN-FAIL does not block verification
eligibility or the exit code, shows in the report, and `mark_verified` refuses a KNOWN-FAIL that is not declared. The
1.60 profile declares HW-35 with the measured gap: *the same 0 dBFS sample at track volume 102 measures -5.52 dBFS now
vs -0.65 dBFS on 2026-10-12 (E14)*.

The gap itself (find which stage lost 5 dB) is **not fixed and not in this lane's files**, so it is recorded, not
guessed at. The host WAV (audio-sink.swift) is a byte-for-byte copy of the transport channels with no gain, the
`over_led.py` probe and the `hw35()` check read the right channel (MAIN left, transport word 12), and the firmware is
the unchanged 1.60 binary — so the loss is in the firmware's own track-volume→master path or in the shim's audio loop
(`hardware_stub.c`, lane A owns; see cross-lane requests).

- `python3 -m unittest emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_profiles` — **116/116** (`OK`).
- Quick set (14 modules) — **452/452** (`OK`).

### 3. `pt-i44.37` — MIDI routing preferences window: `8938732`

`emulator/midi-routing.swift` (new), `emulator/native-window.swift`.

A new preferences window (menu **MIDI > Routing…**) with one row per CoreMIDI endpoint (sources then destinations,
remembered by name + unique ID so a re-plugged device keeps its settings) and In / Out / Send clock / Receive clock
toggles; a column is disabled for the endpoint kind it does not apply to. A clock source picker (Internal or any
source) and a BPM/lock/jitter readout from a second-order DLL (the same loop as `emulator/midi/clock.py`) sit under
the table. Toggling Send clock on a destination starts/stops a 24 PPQN 0xF8 stream to it; ports appear and disappear
live through a `MIDINotifyProc`. Every choice persists in UserDefaults and is also written as the bridge config
`~/Library/Application Support/SP-16 Emulator/midi-routing.json` (clock source, BPM, one entry per touched port) for
the Python DIN/USB bridges to read.

- `cd emulator && xcrun swiftc -typecheck -parse-as-library native-window.swift panel-view.swift data-image.swift audio-sink.swift audio-output.swift firmware-library.swift firmware-window.swift app-diagnostics.swift midi-routing.swift`
  — no errors (only the pre-existing macOS 27 deprecation warnings). `midi-routing.swift` is listed manually because
  `native.py`'s `SWIFT_SOURCES` is lane B1's file (see cross-lane requests).
- Quick set + `test_sp16_native`/`test_sp16_cli`/`test_sp16_smoke` — **606/606** (`OK`).

**Not run:** a panelshot (needs the app window on screen; a research build was made but a screenshot of the routing
window was not captured). The bridge-config *read* side (making the Python DIN/USB bridges act on the JSON) is a
follow-up; this lane wrote the config and the read side is noted below.

### 6. `pt-i44.83` — USB stop backoff, ENOENT free socket path, nonblocking rawmidi pacing, `--midi` doc: `7d8db41`

`emulator/midi/usb.py`, `emulator/midi/seq-bridge.c`, `emulator/hardware_stub.c` (rawmidi-write section),
`emulator/tests/sp16_din_contract.c`, `emulator/tests/test_sp16_usb_midi.py`, `docs/python-cli-and-setup.md`.

Four follow-ups from the Muse re-review, each pinned by a unit test or contract: `signal_stop` now backs off 5 ms per
failed attempt instead of busy-spinning and `bridge()` checks its return; `unlink_stale_socket` treats a connect that
raced an unlink (ENOENT) as a free socket path (matching the lstat case); a successful nonblocking rawmidi write
returns EAGAIN when the previous byte's 320 µs slot has not elapsed, so the UART never bursts past 31,250 baud; and the
`--midi` doc line names both the DIN and USB bridges.

- `python3 -m unittest emulator.tests.test_sp16_usb_midi emulator.tests.test_sp16_cli emulator.tests.test_sp16_native` — **154/154** (`OK`).
- `bash emulator/arm-contracts.sh` — all contracts pass, including the new nonblocking pacing case (the second byte of a
  back-to-back pair is refused, and the retry lands one 320 µs slot later).
- Quick set + the changed modules — **606/606** (`OK`).

### 2. `pt-i44.35` — USB-B note output: **parked (investigated, not shown)**

The virtual CoreMIDI device is present and live: with `--midi`, `./sp16 midi ports` lists `SP-16 USB [lb2]` as both a
source and a destination, and a research-boot of firmware 1.60 reaches `SP16_NATIVE_READY`. Lane Q2 (merged, `254852c`
+ `ea0ab14`) already showed the USB route's clock output (USB `FA`/`F8`/`FC`, 48.03 Hz at 120 BPM). Firmware-generated
**note** output over USB did not arrive on `SP-16 USB` when pads were pressed, which is expected: the firmware gates
generated channel messages behind its own settings — USB OUT mode `0x2b = 0x120` (OUT) and project MIDI OUT NOTE/CC
`0x11000102 = 1` (internal `docs/research/2026-10-28-sp16-midi-gates/01-din-midi.md`). Enabling those is a firmware
menu recipe (not authored here), so note output was not demonstrated in budget. The route itself is proven; the task
stays open for the settings recipe.

### 4. `pt-i44.61` — AK4458 transport-word probe re-run: **parked**

The probe is already committed (`emulator/probes/ak4458_transport.py`, lane D1). Re-running it with the fixed page
guard needs a second build with `--app-arg testmode --trace pcmtap`, a factory AUDIO TEST navigation, and a word-to-jack
write-up. Not attempted in budget; the probe and the page guard are in place.

### 5. `pt-i44.58` — filter send: **parked**

`emulator/probes/filter_send.py` exists (lane D1). The next step is a `--trace gdb` probe of the send enum, the mute
mode and the selected object's `+0x39`, plus a 1.60 profile recipe that assigns the filter to a track. That gdb +
recipe work was not attempted in budget.

## Side effects

- Built `build/lb2` (`./sp16 build --name lb2 --research --audio file --library build/sp16/library
  --reuse-qemu-from build/qemu-source`). Build and session artifacts stay git-ignored.
- Ran one firmware 1.60 session (`--midi --ephemeral`); pressed pads and monitored `SP16 USB`/`SP16 DIN` CoreMIDI
  ports. Stopped it by its exact app PID (21954); QEMU and the reaper exited with it. No emulator process is left running.

## Parked tasks

- `pt-i44.35`: USB-B note output needs the firmware's USB OUT mode + MIDI OUT NOTE/CC menu recipe.
- `pt-i44.61`: AK4458 probe re-run needs a `--app-arg testmode --trace pcmtap` build.
- `pt-i44.58`: filter-send gdb probe and profile recipe.

## Cross-lane requests

- **Lane B1** (`native.py`): add `midi-routing.swift` to `SWIFT_SOURCES` (the tuple after `app-diagnostics.swift`) so the
  new MIDI routing window is compiled into the app. `native.py` is lane B1's file, so this lane did not edit it; the
  exact change is:
  `SWIFT_SOURCES = ("native-window.swift", "panel-view.swift", "data-image.swift", "audio-sink.swift", "audio-output.swift", "firmware-library.swift", "firmware-window.swift", "app-diagnostics.swift", "midi-routing.swift")`
- **Lane A** (`hardware_stub.c` audio loop): the ~5 dB master-level gap behind HW-35 is not in lane B2's files (the
  host WAV is a faithful copy and the firmware is unchanged). The gap investigation should sample the shim's ALSA PCM
  serving / analog board model at runtime to find which stage lost the 5 dB since 2026-10-12.
- The bridge-config **read** side: `emulator/midi/din.py` / `usb.py` should read
  `~/Library/Application Support/SP-16 Emulator/midi-routing.json` (written by the new routing window) so the
  preferences actually drive the routes; this lane wrote the config format and the UI.

## Open questions for the owner

- Should `pt-i44.35` close on the virtual CoreMIDI route + clock output alone, or is the firmware settings recipe for
  note output required first?
