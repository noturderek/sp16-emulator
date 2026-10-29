---
title: "Review fix B: native launcher, app window, MIDI routing, profiles doc"
description: "Codex Sol review findings 2, 7, 8, 9 and 13 on the lkx-native lane: serialise the CoreMIDI helper build, drive standby on ordinary launches, remount after a failed USB swap, preserve MIDI routes on a stat permission error, and correct the profiles guide's audio.jacks claim. Each finding reproduced with a failing test, fixed, and kept."
type: report
status: active
created: 2026-10-28
tags: [sp16, emulator, lkx-native, review-fix]
author:
  harness: pi
  agent: lkx-native
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Review fix B (`lkx-native`)

Fixes for the blocking findings Codex Sol raised on the `lkx-native` lane in
`/Users/shwanton/Projects/pioneer/internal/.gw/runs/agent-dispatch-eatqen1f/final.txt`. Worktree branch
`lane/lkx-native`, cut from `emu/2026-10-28` (`5b0dded`).

## Findings

### FINDING 9 [blocking] — `emulator/midi/routing.py`: a stat permission error cleared existing routes

`Routing.reload()` caught every `OSError` from `stat()` and reset the routes to the "everything carries" default, so
an unreadable preferences file silently reopened MIDI directions the user had closed. Fixed by distinguishing
`FileNotFoundError` (no preferences: everything carries) from any other `OSError` (keep the current routes and their
stamp, record the problem so the next refresh retries).

- Commit: `43b03b0`
- Files: `emulator/midi/routing.py`, `emulator/tests/test_sp16_midi_routing.py`
- Check: `python3 -m unittest emulator.tests.test_sp16_midi_routing emulator.tests.test_sp16_usb_midi emulator.tests.test_sp16_din_midi` — OK (26 tests)

### FINDING 2 [blocking] — `emulator/midi_bridge/midi.py`: DIN and USB bridges raced the CoreMIDI helper build

`native.py launch` starts the DIN and USB bridges back to back, and each compiled
`build/midi-bridge/coremidi-io` on first use. Without a lock the two `swiftc` runs raced on the same output path,
failing with "ld: File exists" and leaving first-use `--midi` broken. Fixed by serialising the build under a lock file
(`fcntl.flock`) and re-checking the binary once the lock is held, so the second bridge finds it up to date. Reproduced
by running the USB and DIN MIDI modules together (they failed before, pass after).

- Commit: `a8eb2a6`
- Files: `emulator/midi_bridge/midi.py`, `emulator/tests/test_sp16_usb_midi.py`
- Check: `python3 -m unittest emulator.tests.test_sp16_usb_midi emulator.tests.test_sp16_din_midi` — OK (12 tests, run
  three times after deleting the built helper, to confirm the race is gone); the concurrent-build test asserts swiftc
  runs exactly once.

### FINDING 8 [blocking] — `emulator/native-window.swift`: a failed USB swap never re-mounted the restored stick

After a failed `QMP.swapUSB`, `hotAttachUSB` re-added the previous QEMU device via `QMP.restoreUSB` but never
re-sent `usb-attach`, so the guest left the restored stick unmounted and unannounced while the reply claimed it was
"re-attached". Fixed by requesting `usb-attach` after the restore and reporting whether the guest remounted it.

- Commit: `32fe004`
- Files: `emulator/native-window.swift`
- Check: Swift type-check of the real `SWIFT_SOURCES` list — exit 0

### FINDING 7 [blocking] — `emulator/native-window.swift`: standby depended on research logging

The standby overlay read the TX standby flag (byte 0x67 bit 0) only from `TX_FRAME` messages, which the guest sends
solely with research logging enabled, so an ordinary launch never applied the overlay. Fixed by forwarding the flag
as its own `STANDBY` message on the always-on port, on transitions only (`guest-bridge.py`), defining the kind in
`protocol.py`, and handling it in the window. The window ignores a true flag until the home screen has settled, so the
firmware's start-up frames (which also set the flag) never darken a boot.

- Commit: `96fa783`
- Files: `emulator/protocol.py`, `emulator/guest-bridge.py`, `emulator/native-window.swift`,
  `emulator/tests/test_sp16_panel.py`
- Check: `python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_native` — OK (143 tests); Swift
  type-check — exit 0

### FINDING 13 [non-blocking] — `docs/profiles.md`: shipped profiles do not retain audio.jacks

The guide claimed `audio.jacks` is "retained in shipped profiles for the Output monitor menu", but
`native.shipped_profile()` keeps runtime fields only (it drops `audio`), and the monitor menu reads the jack mapping
compiled into the app (`AudioSink.OutputJack`), validated against `audio-jack-map.json`. Corrected the table row.

- Commit: `344d8d0`
- Files: `docs/profiles.md`

## Verification

- Quick set plus the MIDI and native modules:
  `python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev
  emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack
  emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze
  emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap
  emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter emulator.tests.test_sp16_midi_routing
  emulator.tests.test_sp16_usb_midi emulator.tests.test_sp16_din_midi emulator.tests.test_sp16_native`
  — OK, 544 tests.
- Swift type-check: `cd emulator && xcrun swiftc -typecheck -parse-as-library $(python3 -c 'import native; print(" ".join(native.SWIFT_SOURCES))')`
  — exit 0 (deprecation warnings only).

## Standby on a launch (FINDING 7)

The finding's note asks for "one ordinary (non-research) launch to show standby". An ordinary build has no way to
trigger standby from outside: the power/standby key has no place on the panel layout and is driven only through the
research control socket (`sessionctl` refuses non-research sessions), so a literal non-research launch cannot press
POWER. The actual gate the finding targets is research *logging* (`txlog`), not the research build itself, so the fix
was verified the way the note suggests ("a dedicated message ... forwarded without the log"):

- `./sp16 build --name lkx-native --research --audio file --library build/sp16/library --reuse-qemu-from build/qemu-source`
  (built, exit 0).
- `./sp16 run --name lkx-native`; waited for `SP16_NATIVE_READY` and `panel: running (first LCD frame)` in
  `build/lkx-native/sessions/<id>/app.log`.
- `./sp16 ctl --build lkx-native event power press` then `event power release` — a single control event each, which
  never enables the TX log.
- Observed `panel: standby — dark with WAKE UP lit` in `app.log` about 1 s after the release, proving the standby
  overlay applies over the new always-on STANDBY message with the research TX log never enabled.
- Stopped the app by its exact process id (`app.pid`); QEMU was reaped with it.

## Side effects

- Built `build/lkx-native` (research, `--audio file`), reusing QEMU from `build/qemu-source`.
- Compiled the CoreMIDI helper `build/midi-bridge/coremidi-io` (first use of the MIDI tests).

## Parked tasks

None.

## Cross-lane requests

None.

## Open questions for the owner

- Whether a future lane should add a physical power/standby button to the panel layout so an ordinary build can enter
  standby without the research socket (out of scope for this lane).
