---
title: "Lane lk10-downbeat hand-back: the true downbeat from the sequencer playhead"
description: "The probe measures the follower's bar position from the sequencer playhead, and the fake master reports the completed beat so the SP-16 locks its downbeat to ~16 ms instead of ~473 ms."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, prodjlink, downbeat]
author:
  harness: pi
  agent: lk10-downbeat
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane lk10-downbeat hand-back

Branch `lane/lk10-downbeat`, cut from `emu/2026-10-28` (`f7b8f0a`). Four themes, one commit each,
plus this file. One research build (`build/lk10-downbeat`, `--research --audio file`) and four
instrumented `--net mcast` follow runs; every session and segment master was stopped by its exact PID.

The headline result is the first **measured downbeat**. The probe now reads the sequencer position
(`hwsuite.playhead_steps`, step 0 = bar start) instead of the ambiguous click onsets, and reports
`downbeat_ms` (median / max / n). The follower's bar was **~473 ms late** when the master reported the
current beat; the beat handler `FUN_0051e6b0` reads the beat packet's `Bb` (`0x5c`) as the beat that
just **completed**, so reporting the completed beat aligns the bar to **~16 ms** residual.

## Per-task hand-back

1. **`pt-i44.93` pin the mcast segment to loopback — `11c3303`.**
   Files: `emulator/net/segment.py` (new shared `multicast_socket`), `net/segment_relay.py`,
   `net/segment_master.py`, `net/segment_watch.py`, `emulator/sync/bridge.py`,
   `tests/test_sp16_segment.py`, `tests/test_sp16_segment_relay.py`, `docs/pro-dj-link.md`.
   A `bridge100` default route appeared on this Mac, so the OS stopped picking the interface the relay
   and its peers joined, and multicast loopback delivery broke (`test_relays_both_ways_and_filters_own_echo`
   timed out, `test_relay_bounds_the_outbox_under_sustained_backpressure` received zero frames, on
   `emu/2026-10-28` too). The segment is now pinned to loopback everywhere: `IP_MULTICAST_IF` = 127.0.0.1,
   `IP_MULTICAST_LOOP` = 1, `IP_ADD_MEMBERSHIP` on 127.0.0.1. The relay keeps the same options inline (the
   build deploys that one file standalone); a loopback-delivery test pins the behaviour.
   **Check:** `python3 -m unittest emulator.tests.test_sp16_segment_relay emulator.tests.test_sp16_segment
   emulator.tests.test_sp16_net` → 55/55 OK; quick set → 459/459 OK.

2. **`pt-i44.92` part 2: measure the bar position — `d071fda`, `14a5635`.**
   Files: `emulator/timing_probe.py`, `tests/test_sp16_timing_probe.py`.
   `timing_probe.run` decodes the panel frames logged during the follow window
   (`session.lamps_since`), takes `hwsuite.playhead_steps` with the profile's playhead colour, and
   reports `downbeat_ms` = the playhead's step-0 entry time minus the nearest master downbeat
   (beat-in-bar 1). The tx-frame clock is the app's `systemUptime` (== `time.monotonic` on macOS), so the
   frames are re-based by the app's launch time read from metrics. Unit tests cover aligned, late, early,
   whole-bar-folded, non-step-0 and insufficient-data cases.
   `14a5635` fixed two live-run defects the first measurement exposed: the app-launch offset was computed
   from the window-start monotonic time against an end-of-run uptime, and the master's beat log ran out
   partway through the window on a slow boot (its duration is now `lead + seconds + 300`).
   **Check:** `python3 -m unittest emulator.tests.test_sp16_timing_probe` → 33/33 OK. Live run figure:
   `downbeat_ms` median 473.063 / max 480.621 / n 33 over 60 s (before task 2's fix).

3. **`pt-i44.92` part 1: the field that sets the bar phase — `55abdcf`.**
   Files: `emulator/net/segment_master.py` (`completed_beat`), `tests/test_sp16_segment.py`.
   Firmware reading (Ghidra pseudocode in the internal repo): `FUN_0051e6b0` byte-reverses and reads the
   beat packet's `Bb` (`0x5c`) and fourth-beat distance (`0x30`); it passes `Bb` to the sequencer
   (`FUN_005266c0`) to set the bar, and uses `0x30` in a tempo-correction ratio. Measured at 128 BPM +2 %
   (130.56), reporting the **current** beat leaves the bar one beat late; `Bb` is read as the beat just
   **completed**. `segment_master` now reports `completed_beat(beat_in_bar)` (the wrapped previous beat).
   **Check:** quick set plus `test_sp16_net`, `test_sp16_segment`, `test_sp16_segment_relay`,
   `test_sp16_pdl_sync`, `test_sp16_timing_probe` → 558/558 OK. Tries (all over 60 s, `n` 33):
   current beat 473 ms; +1 shift 299 ms; -1 shift (this fix) **-16 ms**; Bb-only patch with the
   distances left at the current beat 151 ms.

4. **`docs/pro-dj-link.md` — `02be6ba`.**
   The timing-probe entry gains `downbeat_ms`, and the downbeat state moves from "unverified" to
   "measured" (~473 ms current-beat, ~16 ms completed-beat).

## Side effects

- Built `build/lk10-downbeat` (`./sp16 build --name lk10-downbeat --research --audio file
  --library build/sp16/library --reuse-qemu-from build/qemu-source`).
- Ran four `timing_probe.py run --build lk10-downbeat --net mcast --master-bpm 128 --master-pitch 2`
  follow runs. Each launches and stops its own session and segment master by exact PID; none is left
  running. No firmware, key, device identity, data image or session capture was committed.

## Parked tasks

None. The downbeat is ~16 ms (not within the 5 ms target), but the controlling field is named with the
measurements of each try, which is the task's alternative acceptance.

## Cross-lane requests

`README.md` (lines 243 and 356) and `docs/architecture.md` (lines 19 and 178-179) still say "downbeat
alignment remains unverified". Now that the downbeat is measured (~473 ms current-beat, ~16 ms
completed-beat), those claims should be reworded. They are outside this lane's file list, so I did not
edit them.

## Open questions for the owner

1. The ~16 ms residual is the emulator's network latency (segment_master → relay → QEMU → guest), not a
   whole beat. Closing it to 5 ms would mean tuning the fake master's beat timing or the firmware's
   hardcoded latency compensation (`dVar13 = BPM * 0.07 - 3.167` in `FUN_0051e6b0`), which is beyond this
   lane.
2. The "completed beat" `Bb` convention is the opposite of the public Pro DJ Link write-up (which says
   `Bb` at `0x5c` is the current beat, 1-4). If the write-up is right, the SP-16's own downbeat would sit
   one beat late behind a real CDJ — worth confirming on hardware before treating this as purely a fake
   master quirk.
