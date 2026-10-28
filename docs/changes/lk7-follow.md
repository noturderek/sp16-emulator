---
title: "Lane LK7 follow hand-back: the P3 tempo-follow fix, the CDJ-3000 park and HW-35's real threshold"
description: "The status P3 play state makes the SP-16 follow a fake master's tempo (130.56 BPM exactly); the downbeat phase is parked ~495 ms off; the CDJ-3000 link stays parked behind it; and HW-35 is re-worded to the DSI drive's +5.1 dBFS ClipMeter threshold."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, prodjlink, hwsuite]
author:
  harness: pi
  agent: lk7-follow
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane LK7 hand-back

Branch `lane/lk7-follow`, cut from `emu/2026-10-28` (`b00e9b9`). Three commits, one theme each, plus this file. One
research build (`build/lk7-follow`, `--research --audio file`, QEMU reused from `build/qemu-source`) and one
instrumented `--net mcast` follow run; the emulator and the segment master were always stopped by their exact PID
and the follow run cleaned up its own session.

The headline result is the first **working tempo follow**: with SYNC SOURCE = PRO DJ LINK set, the SP-16 sequencer
now runs at **130.56 BPM exactly** against a master at 128 BPM +2 % (`tempo_error_bpm` 0.0). Every prior lane parked
this at "runs at 120.0 BPM". The downbeat phase is still ~495 ms off, which is the next gate (the beat packet's phase
semantics) that R3's note flagged and did not cover.

## Per-task hand-back

1. **`pt-i44.39` follow the fake master — tempo follows, downbeat parked — `34acf02`.**
   Files: `emulator/net/fake_master.py`, `emulator/tests/test_sp16_net.py`.
   Applied R3's fix: the status packet now sets `P3` (byte `0x9d`) to `0x0d` (track loaded, playing forward) while
   playing — passing the firmware's `(P3 & 1) || (P3 & 8)` gate — and sets the master-meaningful byte `0x9e` and the
   rekordbox `Mv` multiplier `0x90` (0x8000). The beat packet already ended 96 bytes with the redundant device number
   at `0x5f`; a comment names that trailing byte and a test pins it (`0x5d=0x00`, `0x5e=0x00`, `0x5f=player`,
   length 96). Tests also pin `P3` for playing (`0x0d`) and stopped (`0x00`), `Mm` and `Mv`.
   **Live run** (`python3 emulator/timing_probe.py run --build lk7-follow --net mcast --master-bpm 128 --master-pitch 2`):
   the sequencer runs at **130.56 BPM exactly** (`measured_tempo_bpm` 130.56, `tempo_error_bpm` 0.0) — the P3 gate was
   the sole blocker, as R3 read. The downbeat phase is **~495 ms off** (`phase_median_ms` 495.256,
   `phase_max_ms` 495.277 over 33 downbeats, 60 s), so the 5 ms acceptance is parked with this trace evidence; the
   remaining gate is the beat packet's phase semantics (`nextBeat` byte `0x30`, `Bb` `0x5c`), which R3's note named as
   follow-up #3 and did not cover. The window was clean (0 gap frames, 0 guest drops, max panel-frame stall 22.6 ms,
   host load ~5).

2. **`pt-i44.40` link an emulated CDJ-3000 — parked, no commit.** Task 1's downbeat does not align within 5 ms, so a
   CDJ-3000 emulator on the segment would not prove the phase within tolerance. The CDJ firmware
   (`firmware/CDJ-3000/3.22`) and both emulators (`internal/external/nsaintot_cdj3k-emu`, `9c5s_cdj3k-emu`) are in
   place but were not booted.

3. **HW-35 re-worded to the DSI drive's threshold — `7f0a2e7`.**
   Files: `emulator/hwsuite.py`, `emulator/profiles/sp16-1.60.json`.
   HW-35's title and its first expectation now read "OVER lights when the DSI drive input passes +5.1 dBFS" (R4: the
   OVER lamp is `ClipMeter` in the DSI drive, threshold 1.8 = +5.1 dBFS, 500 ms hold), and the profile's known-failing
   reason names the threshold and the missing drive-overload test (a 0 dBFS master stays 5.1 dB below the threshold,
   so the lamp correctly stays dark). The check stays known-failing.

## Side effects

- Built `build/lk7-follow` (`./sp16 build --name lk7-follow --research --audio file --library build/sp16/library
  --reuse-qemu-from build/qemu-source`).
- Booted and stopped one `--net mcast` session through `timing_probe.py run` (it launches and stops its own session
  and segment master by exact PID; both are gone now). No firmware, key, device identity, data image, PCAP or session
  capture was committed.

## Parked tasks

- `pt-i44.39` downbeat acceptance (within 5 ms over 60 s): measured ~495 ms constant offset.
- `pt-i44.40`: blocked by the follow's phase gap.

## Cross-lane requests

- The downbeat phase is the beat packet's `nextBeat`/`Bb` semantics, which R3's note (`docs/research/…/sp16-pdl-tempo-follow`)
  already queued as its follow-up #3 (a firmware-RE reading of the beat handler's use of byte `0x30` and `0x5c`). The
  beat/phase semantics is that lane's question, not a segment-tooling one.

## Open questions for the owner

- Is the ~495 ms downbeat offset a real firmware phase gap or an artefact of `timing_probe.follow_phase` taking every
  16th click onset (the click is on all 16 steps, so `onsets[::16]` starts from an arbitrary step, not necessarily
  step 1)? Confirming it wants a click fixture that marks step 1, or the sequencer position read from the panel.
