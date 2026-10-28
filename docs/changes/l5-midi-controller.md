---
title: "L5: MIDI controller bridge"
description: "What the MIDI bridge adds (pt-i44.36), how to use it, what it assumes, its checks and the hand-back."
type: report
status: active
created: 2026-10-27
tags: [sp16, emulator, midi, lane-l5]
author:
  harness: claude-code
  agent: l5-midi-controller
  model: claude-sonnet-5-5
  thinking: unknown
  tokens: unknown
---

# L5: MIDI controller bridge (pt-i44.36)

New package `emulator/midi_bridge/`; no existing file was edited. Entry point: `python3 -m emulator.midi_bridge`
(from the repository root).

## Use

```sh
python3 -m emulator.midi_bridge ports                          # CoreMIDI sources and destinations
python3 -m emulator.midi_bridge learn --in "My Pads" --name mypads   # 8 pads, 4 knobs (default); --controls pad1,knob1,play,...
python3 -m emulator.midi_bridge run --in "My Pads" --profile mypads  # newest running research session
python3 -m emulator.midi_bridge run --in "TSP-16" --preset sp16 --channel 1   # mirror a real SP-16's MIDI OUT
```

`run` finds the newest session with a live control socket under `build/*/sessions/` (`--build NAME`, `--build-dir DIR`
or `--runtime FILE` pick another). `--in` and `--out` are case-insensitive parts of the CoreMIDI endpoint names; the
LED echo goes to `--out` (default: the same name as `--in`). `--no-leds` turns the echo off.

The first call builds `coremidi-io.swift` with `swiftc -O` into `build/midi-bridge/` (git-ignored). No pip package is
needed: the helper talks to Python in text lines over pipes. The same helper can create a virtual controller
(`coremidi-io virtual NAME`), which is how the tests and the recorded run below work without hardware.

## Files

- `profile.py`: **profile** (what the hardware sends: `inputs` of kind `button`, `absolute` or `relative` with encoding
  `twos`, `signmag` or `offset`; `outputs` for LEDs) and **mapping** (`bindings` input -> named emulator action, `lamps`
  lamp -> output), kept apart so a relearned controller keeps its mapping. Learned files go to `private/midi/`
  (git-ignored); a name is looked up there first, then in `emulator/midi_bridge/profiles/` and `mappings/`.
- `learn.py`: asks for a control, listens, classifies. A knob that stays ambiguous after one direction is asked to
  turn the other way (up to three rounds, then two's complement is assumed and said so).
- `actions.py`: message -> `sessionctl.Session.event` calls. Pads press with the note's velocity; knobs/selector get
  detents (relative encoders directly, absolute controls as the change in value); analog pots get value 0..1000;
  `strip` gets contact and position (released after 0.6 s idle, or at pitch-bend centre); START/STOP/CONTINUE are a
  60 ms tap on the bound button. A binding may be `{"action": ..., "velocity": N, "invert": true, "scale": X}`;
  `knob3+` / `knob3-` on a button steps a knob one detent. `scale` multiplies the step for every input kind: the
  fraction of an absolute pot (analog and strip targets), and the detents or analog units of a relative encoder, an
  absolute control bound to a knob, or a stepping button; the part of a detent a move leaves over is carried to the
  next event of that input, so `scale: 0.5` turns two single ticks into one detent. A burst of controller traffic collapses to one call per
  input (last absolute value, summed deltas).
- `leds.py`: switches the session's TX frame log on (`txlog`), follows `tx-frames.jsonl`, decodes frames with
  `panel_map.decode_lamps` and sends a lamp's brightness (brightest RGB channel, 0..127) as note or CC velocity when
  it changes; everything is resent every 5 s and the LEDs go dark when the bridge ends. The log is switched off again
  at the end (it grows by about 1 MB a minute).
- `presets.py`: the "mirror the real SP-16" profile (below).

## Mirror preset: what is manual and what is assumed

From the 1.60 addendum pp. 47-51 (S): a MIDI track sends on its own channel (TRACK n -> CH n by default); a pad plays
the track's NOTE (C0..B9); the MIDI CC1 page's six knobs are PITCH BEND, AFTER TOUCH, CC 7, CC 1, CC 2, CC 10 and the CC2
page CC 71..76; START/STOP/CONTINUE are sent when enabled. Not in the manual: which note each pad sends (a per-track
setting; the default `--note-base 60` is the screenshot's C5 with C0 = 0, grade I), any LED input over MIDI (U: the
preset has no lamps), and how the knob values behave on a turn (I: absolute, turned into detents: 127 for a sweep of the whole range, whether 7-bit or 14-bit pitch bend). Only the
unit's MIDI OUT is mirrored; nothing is sent to the unit.

## Checks

Quick, no emulator: `python3 -m unittest emulator.midi_bridge.tests.test_midi_bridge` (see the hand-back for the
result). It includes a round trip through a virtual CoreMIDI source and destination; it skips itself where `swiftc` or
CoreMIDI is missing.

## Cross-lane requests

- `./sp16` wiring, at merge (the dispatcher): a `midi` subcommand that runs
  `python3 -m emulator.midi_bridge ARGS` with the repository root as working directory
  (`ports | learn | preset | run`; `./sp16 ctl`'s `--build NAME` and `--runtime FILE` map to the bridge's options of the
  same name), listed under "Develop and test" next to `ctl`. `sp16emu.py` already has `find_runtime` and `pick_build`
  that could replace the bridge's own session lookup.
- `.gitignore` already covers `build/` and `private/` (where the helper and learned files go): nothing needed.
- Docs at merge: a README line under the control socket / `ctl` section pointing here.

## Hand-back

Task `pt-i44.36`, commit `3726cab` ("MIDI bridge: drive the emulator from any CoreMIDI controller"); this file is the
second commit. Files: `emulator/midi_bridge/` (`__init__`, `__main__`, `midi`, `profile`, `learn`, `actions`, `leds`,
`presets`, `bridge`, `coremidi-io.swift`, `tests/test_midi_bridge.py`) and this document. No existing file was edited.

Checks (grade O unless noted):
- `python3 -m unittest emulator.midi_bridge.tests.test_midi_bridge` -> `Ran 61 tests ... OK`, 61 of 61, including a
  round trip through a virtual CoreMIDI source and destination (note, CC and the reverse direction), four messages sent
  in one packet list, and a 300-message list run through the helper's receive path (`coremidi-io decode`).
  Review fixes since the first hand-back are covered by tests: a transport tap still pending at exit, `scale` for every
  input kind against every control kind, the packet-list walk, and pitch bend as 14 bits.
- Quick set slice run: `python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles` -> 88 OK.
  The rest of the quick set was not run: nothing outside `emulator/midi_bridge/` changed.
- Recorded run, research build `l5` (`./sp16 build --name l5 --research --audio file ...`, firmware 1.60, `run --ephemeral`),
  a virtual CoreMIDI controller "SP16 L5 Ctl" driven by the helper:
  - `learn --controls pad1,knob1` heard note 36 on channel 10 as a button and CC 16 sent as 1,2,1,127,126 as a relative
    two's-complement encoder, and wrote a profile and a mapping (LED output for pad1 = its own note).
  - `run`: a note-on 7f on note 36 -> `press pad1` and a note-off -> `release pad1`; three `b0 10 01` -> three
    `delta knob1`. The bridge reported `5 calls, 0 failed`; the app's `input_results` read `ok 5, failed 0, invalid 0,
    unsupported 0` and the guest counted `events: 5`.
  - knob1: the LCD's TRACK 1 VOLUME read 0 before and 3 after the three ticks (panelshots
    `l5b-pad-held.png` and `l5b-after-knob.png` in `build/l5/sessions/native-446064ae-.../`).
  - pad LED echo: the controller received `note_on 36` with velocity 7d at the start, 7f while pad 1 was held, 7e, then
    back to 7d (the lamp's brightness following the emulator's lamp frames), and `note_off` when the bridge ended.
    The pad 1 lamp is already lit on a fresh start (the selected track), so the echo shows as a change in brightness
    and the panelshots of the pad (`l5b-before.png`, `l5b-pad-held.png`) are pixel-identical at pad 1: the visible proof
    of the hit is the echo and the guest's event count, not a pad flash.
  - An earlier run (`l5-before.png`, `l5-pad-held.png`, `l5-after-knob.png`) was taken while the guest was still booting
    (LCD black); the host's load average was above 400 during this run, the guest ran at 0.22x and a control call took
    over a minute at times (`SP16_CONTROL_TIMEOUT=100` was used). Timings (the task's 10 ms acceptance) were therefore
    not measured; the translation itself is one control call per message batch.

Not run: the acceptance's physical controller (8 pads and 4 knobs learned in under a minute, then used); there is no USB
controller here, and the real SP-16 was not attached. The "mirror the real SP-16" preset is tested against the translator
only; its pad notes (a per-track setting) are an assumption (grade I, see above). Timing under 10 ms: not measured (U).

Side effects: built `build/l5` (research build) and `build/midi-bridge/coremidi-io` (git-ignored); started one emulator
(app pid 28340, asked to quit with SIGTERM) and virtual CoreMIDI endpoints, closed at the end of each run. The control
session's TX frame log was switched on for the LED echo and off again by the bridge. Parked tasks: none.

Open questions for the owner: (1) does a real SP-16 on USB MIDI send the same note per pad on every track, and which? Only
`./sp16 midi run --in TSP-16 --preset sp16` with the unit and a look at `--note-base` can settle it; (2) is an RGB
palette echo (Launchpad-style velocity colours) wanted? Today a lamp's brightness is sent as velocity.
