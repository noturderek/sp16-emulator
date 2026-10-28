---
title: "Lane LK6-nits: the clock thread comes back, a late Carabiner outage warns again, and the OVER report stops printing None"
description: "The five non-blocking review nits from bucket 4: ClockEmitter.start guards on liveness, CarabinerLink clears its warning latch after a send, the OVER probe's docstrings and no-evidence labels tell the truth, and the -30 dBFS fixture is pinned to its name by a test plus a control label that names the volume it played at."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, sync, probe, fixtures]
author:
  harness: pi
  agent: lk6-nits
  model: deepseek-v4.1-flash
  thinking: high
  tokens: unknown
---

# Lane LK6-nits hand-back

Task `pt-i44.90` (bucket 6 of the 2026-10-28 wave), Muse review `agent-dispatch-0kgiund6`'s five non-blocking
findings. Branch `lane/lk6-nits`, cut from `emu/2026-10-28` = `a47d048`. Six commits: five code/test commits plus this
file. No build and no emulator run — every finding is unit-testable, and the brief said unit tests only.

Owned files only: `emulator/sync/*`, `emulator/probes/over_led.py`, `hwsuite.make_fixtures` (and `fixture_wavs`'s
docstring), and their test modules. Nothing from bucket 5 was touched.

## Per task

### 1. `803b8ab` — `ClockEmitter.start()` guards on liveness, not presence

`emulator/sync/bridge.py`, `emulator/tests/test_sp16_sync_bridge.py`.

`start()` returned early when `self._thread is not None`, but a `Thread` object outlives its worker: a sink that
raised (a CoreMIDI destination that went away, a recorder that errored) left the emitter dead with `_thread` set, so
`start()` was a permanent no-op and the MIDI clock never came back. The guard is now
`self._thread is not None and self._thread.is_alive()`.

Check: `python3 -m unittest emulator.tests.test_sp16_sync_bridge` → `Ran 7 tests` / `OK`. The new
`test_a_worker_that_died_lets_start_emit_again` kills the worker with a sink that raises once, joins it, then asserts
`start()` emits again; against the old guard it fails with `1 not greater than 10`, against the new one it passes.
7 of 7.

### 2. `b5db763` — a later Carabiner outage warns once more

`emulator/sync/pdl_sync.py`, `emulator/tests/test_sp16_pdl_sync.py`.

`CarabinerLink._warned` latched on the first failure and was never cleared, so a Carabiner that started, quit and quit
again lost Link tempo/phase sync with no diagnostic after the second outage. `_deliver` now clears the latch after a
`send` that succeeds; the diagnostic is still one per outage, not one per call.

Check: `python3 -m unittest emulator.tests.test_sp16_pdl_sync` → `Ran 17 tests` / `OK`. The new
`test_an_outage_after_a_recovery_warns_once_more` takes a real link through nothing-listening (one diagnostic), a
listening server (send succeeds, no diagnostic), then the server closed (second diagnostic) and asserts two lines;
against the old latch it gets one and fails. 17 of 17.

### 3. `7499944` — the OVER report says `n/a` and `silent`, not `None`

`emulator/probes/over_led.py`, `emulator/tests/test_sp16_over_led.py`.

* `emulator/probes/over_led.py:15` and `board_evidence`'s docstring named the singular `main_out_gain`; they now name
  both bounds the probe keeps, `main_out_gain_min` and `main_out_gain_max`.
* `gain_label(low, high)` returns `MAIN OUT gain n/a` when the window carried no gain reading at all, so a pass with no
  board evidence no longer prints `MAIN OUT gain None`.
* A new `level_label(dbfs)` prints `silent` for a window whose peak measured 0 (and `0.0 dBFS` for full scale, which
  is a value, not silence). It is used by the per-pass line and by the control's label, so a silent control reads
  `measured silent` instead of `measured None dBFS`.

Check: `python3 -m unittest emulator.tests.test_sp16_over_led` → `Ran 35 tests` / `OK`. Five new tests, one per label
(`gain_label` `n/a`, one value, a range; `level_label` with a unit, zero at full scale, `silent`) plus two `run()`-level
ones that capture the printed report; all five fail against the old probe. 35 of 35.

### 4. `7927bae` — every fixture's level is pinned to its name

`emulator/hwsuite.py` (`fixture_wavs` docstring), `emulator/tests/test_sp16_hwsuite.py`.

The review's premise does not hold at this commit: `hw-tone-m30.wav` is already
`tone(1.0, 440.0, 10 ** (-30 / 20))` and its own peak is 1036 of 32767 = **-30.00 dBFS** measured from the file's
samples. The -36.72 dBFS in `lk4-fixes.md` was that file played back at track volume 96: the sweep's full-scale pass
at 96 measured 15100/32767 = 0.4608 (-6.73 dB) and 1036 × 0.4608 = 477.4 → 478, exactly the figure in the report. So
the fixture was not mis-levelled and making it louder would have broken the control's meaning; what was missing is a
check that keeps the file honest if its amplitude ever drifts while the name stays.

The new `test_each_fixture_measures_the_level_its_name_states` decodes each named fixture (`hw-tone-full` 0,
`hw-tone440` -6, `hw-tone-m30` -30 dBFS) and demands every peak within 0.1 dB of its name; `fixture_wavs`'s docstring
now says the level is the file's peak, not what a pass records after the track volume.

Check: `python3 -m unittest emulator.tests.test_sp16_hwsuite` → `Ran 96 tests` / `OK`. 96 of 96; the fixture test
passes as the fixtures stand, which is the point (it is the pin, not a fix).

### 5. `d6ce535` — the control's label names the track volume it played at

`emulator/probes/over_led.py`, `emulator/tests/test_sp16_over_led.py`.

This is the cause of finding 5's report line, and it is in this lane's file. The control pass plays the -30 dBFS
fixture at whatever track volume the last sweep pass left (96 in the recorded run), so its measured level carries that
volume's attenuation and `-30 dBFS fixture control (measured -36.72 dBFS)` read as a mis-levelled fixture. The label now
reads `-30 dBFS fixture at track volume 96 (measured -36.72 dBFS)`, so the number explains itself. Behaviour is
unchanged: same window, same volume, same figures.

Check: the same `Ran 35 tests` / `OK`; the two control-label tests assert the volume in the label.

## What did not run

* `./sp16 test` (unfiltered) — ruled out by the brief; the quick set and each touched module ran after every commit.
* The end-to-end smoke test and the hardware suite — no app, guest, shim, profile or boundary change: two Python
  modules, a probe, a fixture check and unit tests.
* `bash emulator/arm-contracts.sh` — no stub, C or Swift change; nothing on the arm contract moved.
* No build, no emulator, no firmware — no finding here needed one.

## Side effects

* None on the machine: no build made, no process started, no emulator run. The only files written are the five source
  and test files plus this one.
* Quick set after each commit: `Ran 456 tests` / `OK` (7.9 s). The four touched modules together:
  `Ran 155 tests` / `OK`.

## Cross-lane requests

* Bucket 5 (`recipes/*`, `menu/*`): none — the fixture level and the probe's labels were this lane's files.
* `docs/changes/lk4-fixes.md` still records the control as measuring "-36.72 dBFS, not the -30 dBFS it is named for"
  and files it as a cross-lane request about `make_fixtures`. Common-brief rules keep existing `docs/*.md` untouched;
  the correction is above (finding 4) and the dispatcher may want to note it when merging.

## Open questions for the owner

* Should the control play at the loudest swept volume instead of inheriting the last one? Naming the volume makes the
  current number honest; playing at 127 would make the control's measured level the fixture's own (about -30 dBFS) and
  let a future check compare it to the name directly. That is a behaviour change to a research probe, so it stays
  unmade here.
