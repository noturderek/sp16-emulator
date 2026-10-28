---
title: "Lane 3 finish-ups: the routing preferences reach the bridges, the clock readout measures a deviation, lane B2's review notes"
description: "The DIN and USB bridges read midi-routing.json, the jitter readout reports a standard deviation instead of a mean interval, and lane B2's four review notes on known-failing and the clock packet list are closed."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, midi]
author:
  harness: pi
  agent: lk3-finish
  model: deepseek-v4.1-flash
  thinking: high
  tokens: unknown
---

# Lane 3 hand-back

Branch `lane/lk3-finish`, cut from `emu/2026-10-28` (`7058eed`). Three commits, one per task, plus this file.
Nothing was built or run: every task's acceptance is a unit test or the Swift type-check, and both ran.

## Per task

### 1. `pt-i44.37` — the DIN and USB bridges read the routing preferences: `9958150`

`emulator/midi/routing.py` (new), `emulator/midi/din.py`, `emulator/midi/usb.py`, `emulator/midi-routing.swift`,
`emulator/tests/test_sp16_midi_routing.py` (new).

Lane B2 wrote `~/Library/Application Support/SP-16 Emulator/midi-routing.json` from the MIDI > Routing… window and
noted the read side as a follow-up, so every In / Out / Send clock / Receive clock toggle was inert. `midi/routing.py`
now parses that document and answers one question per direction. The mapping follows CoreMIDI's split, which is what
the window's table shows: our own **source** row (In, Receive clock) gates what the guest sends to the host, our own
**destination** row (Out, Send clock) gates what the host sends to the guest. As in a DAW's MIDI preferences (Track
vs Sync) the direction toggle gates the whole stream and the clock toggle gates only the 0xF8 inside it.

Three properties the review will want:

- A port **absent** from the document carries everything, which is exactly what a bridge did before the window
  existed. Only a present entry can close a direction, and a toggle that is not literally a JSON `true` closes it, so
  a damaged entry silences rather than opens.
- The file is re-read whenever its mtime/size/inode changed, on a 0.25 s throttle, so a toggle takes effect on the
  next message without restarting the bridge. An unreadable file keeps the routes already in force (`Routing.problem`
  records why) rather than reopening everything.
- The window now writes only ports the user has toggled. `reload()` gives every visible endpoint a blank entry, so
  writing all of `settings` (B2's version) would have told the bridges that every port is switched off the first time
  the window was opened. The window also remembers the touched set in UserDefaults.

Both bridges gained `--config PATH` for an alternate preferences file (refused if it names a directory); native.py
needs no change because the default path is the app's own.

- `python3 -m unittest emulator.tests.test_sp16_midi_routing` — **15/15** (`OK`). Per toggle: one route test
  (closed carries nothing, open carries bytes) plus the same toggle through a **running** DIN bridge and a **running**
  USB bridge, against a socket pair and a fake CoreMIDI helper (no CoreMIDI, no device), plus the absent-port,
  malformed-document, not-a-JSON-true, keep-the-old-routes and live-toggle cases.
- `python3 -m unittest emulator.tests.test_sp16_din_midi emulator.tests.test_sp16_usb_midi
  emulator.tests.test_sp16_midi_clock` — **19/19** (`OK`), including the live CoreMIDI round trips.
- Quick set (14 modules) — **452/452** (`OK`); Swift type-check — no errors.

### 2. `pt-i44.37` — the jitter readout measures a deviation, the DLL gains use rad/s: `482cc1b`

`emulator/midi-routing.swift`.

The readout printed the *mean* of the recent clock intervals, which at 120 BPM is 20.8 ms however steady or wobbly
the clock is, so it never moved. It now prints the sample standard deviation `sqrt(sum((x - mean)^2) / (n - 1))` of
the last 96 intervals: 0 µs for a perfectly steady clock, growing with the wobble.

The loop's gains were also wrong in the way the brief suspected: `loopHz` (a frequency in Hz) was used where
`clock.py` builds its loop from `wn = 2*pi*loop_hz` in rad/s, and both gains were normalised to the *recovered*
period instead of the nominal one. `onClockByte` now computes `wn = 2*pi*loopHz`, `t0 = clockPeriodSeconds(nominalBPM)`,
`b = 2*zeta*wn*t0`, `c = (wn*t0)^2` and applies `centre += period + b*e`, `period += c*e`, with that formula in the
comment. For the same loop (`zeta = 0.707`, `loop_hz = 2`, 120 BPM) the phase gain goes `0.058925565 -> 0.370240245`
and the period gain `0.001736111 -> 0.068538919`, which is what `ClockDLL(120.0, 2.0)` in `emulator/midi/clock.py`
carries — the Swift loop now settles at the Python one's rate.

- `cd emulator && xcrun swiftc -typecheck -parse-as-library $(python3 -c 'import native; print(" ".join(native.SWIFT_SOURCES))')`
  — no errors (only the pre-existing macOS 27 deprecation warnings), with `midi-routing.swift` already in the real
  `SWIFT_SOURCES` list from B2's review round.
- Gain values cross-checked against `ClockDLL(120.0, 2.0)` (golden values from the reference, not the Swift code).

**Not run:** a live readout capture. Seeing the number move needs a build with `--midi`, an external clock source
and a device knocking on the network port; the type-check is the brief's acceptance and the arithmetic is the
reference's.

### 3. Lane B2's review notes: `9b8c615`

`emulator/hwsuite.py`, `emulator/midi-routing.swift`, `emulator/tests/test_sp16_hwsuite.py`.

- **`mark_verified`'s eligibility message names KNOWN-FAIL.** It said "every check PASS or N/A", but `summarize`
  leaves KNOWN-FAIL out of the failing counts, so a KNOWN-FAIL run *is* eligible; the message now reads "every check
  PASS, KNOWN-FAIL or N/A". The new test also asserts that a declared KNOWN-FAIL run is really eligible, so the
  message cannot drift back.
- **`known_failing()` tolerates a malformed profile.** It used `item.get(...)` on every entry and `(profile.get("hardware") or {}).get(...)`,
  so a hand-edited or half-written profile (a string in the list, `hardware: []`, `hardware: "known"`, an entry with
  no `check`) raised `AttributeError` out of a check and out of `mark_verified`. The new `declared_known_failing()`
  returns only entries that are objects with a string `check`; `known_failing` and `mark_verified` both use it, and
  `mark_verified` still refuses an undeclared KNOWN-FAIL when the list has junk in it.
- **The bridge-config docstring matches what reads it** — closed in `9958150`, alongside the writer it describes.
  `MidiRoutingConfig` and `writeBridgeConfig` now document the document `emulator/midi/routing.py` reads: the keys,
  that an untouched port is absent and absent means "carry everything", and that the bridges match by `name` (their
  own display label) and never by `uniqueID` (the system assigns it after their port exists).
- **The `MIDIPacketList` scratch buffer fits the struct.** `sendClockByte()` built its 0xF8 list in a fixed 256-byte
  `[UInt8]`. `sizeof(MIDIPacket)` is 268 (a 256-byte data window plus timestamp and length) and
  `sizeof(MIDIPacketList)` is 272, so copying the list back out of the buffer (`listPtr.pointee`) read 16 bytes past
  its end. The buffer is now `MemoryLayout<MIDIPacketList>.stride` bytes and the list is sent straight out of it,
  once per destination; a list that somehow does not fit is logged instead of being sent empty (`MIDIPacketListAdd`'s
  result was ignored before). Sizes measured with a throwaway `swiftc` snippet: MIDIPacket 268 / data at offset 10,
  MIDIPacketList 272.

- `python3 -m unittest emulator.tests.test_sp16_hwsuite` — **95/95** (`OK`), with three new tests (malformed profile,
  malformed list through `mark_verified`, eligibility message).
- Quick set (14 modules) — **455/455** (`OK`); Swift type-check — no errors.

**Not run:** `bash emulator/arm-contracts.sh`. No C contract, stub or stub-backed behaviour was touched (Docker would
only re-run the unchanged contracts).

## Side effects

None: no build, no boot, no QEMU, no CoreMIDI helper process and no emulator process was started. Every bridge test
ran in-process against a socket pair with the CoreMIDI helper replaced by a fake, and the existing live CoreMIDI
tests cleaned up their own helper processes.

## Parked tasks

None.

## Cross-lane requests

- **Lane B1 / owner (`docs/python-cli-and-setup.md`)**: the bridges' new `--config PATH` option (the preferences file
  to read instead of `~/Library/Application Support/SP-16 Emulator/midi-routing.json`) is documented only in
  `--help` and the module docstrings, because existing `docs/*.md` are not this lane's to edit. The intended sentence
  for the `--midi` paragraph: *"Both bridges read the MIDI > Routing… preferences; `--config PATH` points them at a
  different `midi-routing.json` (a port with no entry carries everything)."*

## Open questions for the owner

- The bridges match a preferences entry by the endpoint's display name. That is the label native.py gives them
  (`SP-16 DIN [<build>]`, `SP-16 USB [<build>]`), and it is what the window shows, so it works today; a name the user
  edits in Audio MIDI Setup would silently drop the routing. If you would rather key on identity, the helper
  (`emulator/midi_bridge/coremidi-io.swift`, not this lane's file) would have to print its endpoint's unique ID on
  the `ready` line, and the bridges would match on that.
- A preferences file written by a build of this branch *before* `9958150` lists every visible port with every toggle
  off (that was `writeBridgeConfig` writing all of `settings`), and the bridges now read that as "everything off".
  Nothing shipped in that state — the window is unreleased and the read side did not exist — but a developer's local
  `midi-routing.json` may need deleting once.
