---
title: "Lane LK4-fixes: the sync bridge survives a missing Carabiner, and the OVER sweep measures what it prints"
description: "Two review-follow-up tasks: CarabinerLink tempo/phase calls report one diagnostic instead of raising and ClockEmitter.start is guarded, then the OVER probe gained a warm-up strike, a 2-sample tolerance, both gain bounds, honest no-evidence wording and a gain-checked control labelled by its measured level - and the volume-127 pass now measures full scale live."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, sync, probe]
author:
  harness: pi
  agent: lk4-fixes
  model: deepseek-v4.1-flash
  thinking: high
  tokens: unknown
---

# Lane LK4-fixes hand-back

Branch `lane/lk4-fixes`, cut from `emu/2026-10-28` (`2270310`). Two code commits plus this file. No build was made
in this worktree: task 3 re-ran the probe against `../lk2-audio/build/lk2-audio` (`--research --audio file`), which
already existed.

Owned files only: `emulator/sync/bridge.py`, `emulator/sync/pdl_sync.py`, `emulator/probes/over_led.py` and their
test modules.

## Per task

### 1. `pt-i44.87` — a missing Carabiner no longer kills the clock: `118d9ac`

`emulator/sync/pdl_sync.py`, `emulator/sync/bridge.py`, `emulator/tests/test_sp16_pdl_sync.py`,
`emulator/tests/test_sp16_sync_bridge.py`.

`CarabinerLink.status()` already swallowed the `OSError` from a refused connect, but `set_tempo` and
`force_beat_at_time` sent through `send()` directly, so the first tempo change or downbeat of a `--link` run with no
Carabiner raised `ConnectionRefusedError` — and because `PdlClockBridge._on_tempo` is on the beat path, the run died
with the MIDI clock stream it had just started. Both now go through one `_deliver()` that:

* drops the refused socket (`_drop()`, which `close()` now shares) so a later send retries the connect instead of
  writing to a dead socket;
* prints **one** diagnostic for that link, on stderr, naming host and port:
  `sync: no Carabiner listening on 127.0.0.1:17000 (...); Link tempo/phase sync is off`;
* returns `False` instead of raising. A listening Carabiner prints nothing and returns `True`.

`ClockEmitter.start()` had no guard: a second call started a second thread, doubling the pulse rate to 48 PPQN. It is
now a no-op while the thread runs (documented in the docstring), and it clears the stop event, so `start()` after
`stop()` actually runs instead of returning immediately.

Check: `python3 -m unittest emulator.tests.test_sp16_sync_bridge emulator.tests.test_sp16_pdl_sync` →
`Ran 22 tests in 2.831s` / `OK`. New tests: one diagnostic and no raise for both calls against a loopback port with no
listener (asserting the socket is dropped afterwards); tempo delivered to a real listening socket with no diagnostic;
the second `start()` keeping the rate at ~29 pulses in 0.6 s (one thread; two would be ~58).

### 2. `pt-i44.89` — the OVER probe reports what it measured: `fdb52ff`

`emulator/probes/over_led.py`, `emulator/tests/test_sp16_over_led.py`.

* **Warm-up strike.** The first pad strike of a run is deterministically silent — every earlier sweep printed
  `full@127 peak 0` (2026-10-28T14:21, 15:02, 15:12 reports in `build/lk2-audio/over-led/`). `warm_up()` now loads,
  selects, strikes at velocity 127 and discards the window before the first measured pass.
* **Tolerance.** `shim_matches` allowed one S16 LSB, but `out_peak` is printed to four decimals: `1e-4 * 32768 =
  3.2768` samples, so a printed value spans `±1.6384` around the true peak. The tolerance is now `SHIM_TOLERANCE = 2.0`
  samples, compared before any rounding, with the derivation in the docstring.
* **Both gain bounds.** `board_evidence` returned `max()` of the gains, which hides a mid-window dip, and raised
  `TypeError` when the board block reported `main_out_gain: null`. It now returns `main_out_gain_min` and
  `main_out_gain_max`, skipping nulls; `divergent_passes` reports a dip as a range (`MAIN OUT gain 0.25..1.0`).
* **No evidence is not divergence.** A pass whose meter never reported a fresh value is worded in its own sentence —
  `no shim evidence for <pass> (no fresh output-meter reading in the window)` — instead of being called divergence.
  The verdict text moved into `shim_verdict()` so it is unit-tested.
* **The control.** The -30 dBFS fixture pass is now gain-checked like every other pass (a pass keyed `"control"`,
  flagged `control: true`), its peak match is left unjudged (it is there to stay dark), and it is labelled by the level
  it measured: `-30 dBFS fixture control (measured -36.72 dBFS)`. The hard-coded `"m30"` name is gone from the code.

Check: `python3 -m unittest emulator.tests.test_sp16_over_led` → `Ran 28 tests in 0.002s` / `OK`. New tests cover the
warm-up's strike order and full velocity (plus a loud failure when the track cannot be selected), a `run()`-level
sweep with stub `hwsuite`/`profiles` proving the warm-up happens before the first measured window and that the report's
control label carries the measured dBFS, the ±2-sample boundary (1.9 in, 2.0 in, 2.1 out), a gain dip read as a
range, a null gain not raising, the no-evidence wording, and the control's gain being judged.

### 3. `pt-i44.89` live — the volume-127 pass now measures full scale

`python3 emulator/probes/over_led.py run --build .../lk2-audio/build/lk2-audio`, firmware 1.60, track 3, load average
5.7 before the run. Report: `lk2-audio/build/lk2-audio/over-led/20261028-153728/over-led.json`.

| pass | vol read back | WAV peak | dBFS | near-full samples | mono 34 lit | MAIN OUT gain | shim `out_peak` | shim vs WAV |
|---|---|---|---|---|---|---|---|---|
| `full@127` | 127 | 32767 | 0.00 | 13280 | no | 1..1 | 1.0 | matches (32767) |
| `full@110` | 110 | 21600 | -3.62 | 0 | no | 1..1 | 0.6592 | matches (21600) |
| `full@102` | 102 | 17352 | -5.52 | 0 | no | 1..1 | 0.5295 | matches (17352) |
| `full@96` | 96 | 15100 | -6.73 | 0 | no | 1..1 | 0.4608 | matches (15100) |
| `control` | 96 | 478 | -36.72 | 0 | no | 1..1 | 0.0146 | peak match not judged |

Mono channels lit in every pass, OVER (34) never: 0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 25, 26.

Yes — the volume-127 pass now produces audio and matches its WAV peak: 32767 (0.00 dBFS) with 13280 near-full-scale
samples, against `peak 0` in the three sweeps before the warm-up, and the shim's own MAIN-pair peak (1.0) equals it
within the printing's tolerance with a neutral MAIN OUT gain. The verdict therefore still reads *the firmware does not
raise its clip flag here* on evidence rather than on a silent window: full scale reaches the master and mono 34 stays
dark.

Two observations for the owner, not changes: the control fixture plays **-36.72 dBFS**, not the -30 dBFS it is named
for (the earlier review run measured -35.5, i.e. 549/32767, so the fixture's level moves ~1 dB between runs —
`hwsuite.make_fixtures` is not this lane's file); and mono 26, which HW-35 also watches, is lit in every pass.

## What did not run

* `bash emulator/arm-contracts.sh` — no stub, C or Swift change; nothing on the arm contract moved.
* `./sp16 test` (unfiltered) and the full hardware suite — not needed and ruled out by the brief.
* A build in this worktree — `build/lk2-audio` existed and task 3 only needed a run.

## Side effects

* Emulator launched and stopped by its own pid inside `over_led.py`'s `finally` (`hwsuite.launch`/`stop`); the run
  wrote `over-led/20261028-153728/{fixtures,probe-stick.img,over-led.json}` and one sparse stick image under
  `../lk2-audio/build/lk2-audio/` (git-ignored build output). No process of another lane was touched.
* Temp dirs from the stub-`hwsuite` test are removed by `addCleanup`; the quick set and the two probe/sync modules were
  the only test runs.

## Cross-lane requests

* `emulator/hwsuite.py` (`make_fixtures`, not this lane's file): the `hw-tone-m30.wav` fixture is named -30 dBFS but
  measures -36.72 (and -35.5 in one earlier run); recording the fixture's true level, or pinning it, would make the
  control's expected level exact instead of measured-and-reported.

## Open questions for the owner

* OVER (mono 34) still never lights with a full-scale master and the shim proven transparent. That is now a solid
  measurement rather than a silent window; deciding whether that is a firmware-only clip flag, a lamp-role error or a
  check to drop belongs with the HW-35 work.
