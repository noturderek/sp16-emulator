---
title: "Lane LK2-audio: HW-35's missing 5 dB, the AK4458 word-to-jack table, and the filter send"
description: "HW-35's ~5 dB localised to the firmware's track-volume->master path with the shim proven transparent, the AK4458 transport-word table re-run with the fixed page guard, and the filter-send first silent boundary reproduced with the gdb probe and recipe parked."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, audio]
author:
  harness: pi
  agent: lk2-audio
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane LK2-audio hand-back

Branch `lane/lk2-audio`, cut from `emu/2026-10-28` (`7058eed`). Two code commits plus this file. Two builds were
made in this worktree: `build/lk2-audio` (`--research --audio file`) and `build/lk2-audio-tap` (`--research
--audio file --trace pcmtap --app-arg testmode`); the gdb build was not needed and was not made.

## Per task

### 1. `pt-i44.77` — the ~5 dB is upstream of the shim: `2cc93cf`, verdict fixed by `4282cd1`

`emulator/probes/over_led.py`, `emulator/tests/test_sp16_over_led.py`.

The four named candidates — MAIN OUT law, input limiter, 16-channel send, S24 conversion — are all **transparent**
for the master stream, so the ~5 dB is the firmware's own output, not the emulator's level path. The probe samples
the shim's board model (`guest.board.analog_levels`) and reports `main_out_gain` and the shim's own MAIN-pair
output peak alongside the sink WAV peak, so each pass can say where a level shortfall would be.

The first version's verdict claimed "the measured level is the firmware's" from `main_out_gain == 1.0` alone, and
its `shim_out_peak` was the maximum over the whole window, so the volume-102 pass read the 127 pass's carried-over
1.0. `4282cd1` fixes both: the verdict now requires, per pass, a neutral gain **and** a shim peak equal to that
pass's own WAV peak within one S16 LSB (the shim prints `out_peak` to four decimals, so it is rounded back to the
nearest sample before the one-LSB comparison), and the shim peak is bracketed to the pass — the probe records the
peak carried over from before the strike and `board_evidence` discards readings equal to it, so no value carries
from one pass to the next.

A live sweep on build `lk2-audio` (firmware 1.60, `app_sha256_verified=yes`, SHA `3d1392a61323e73e…`, run folder
`build/lk2-audio/over-led/20261028-151205`):

| pass | WAV peak | dBFS | `main_out_gain` | shim `out_peak` | mono 34 |
|---|---|---|---|---|---|
| volume 127 | 0 (no audio: first-pass settle) | — | — | — | dark |
| volume 110 | 21600 | -3.62 | 1.0 | 0.6592 | dark |
| volume 102 | 17352 | -5.52 | 1.0 | 0.5295 | dark |
| volume 96 | 15100 | -6.73 | 1.0 | 0.4608 | dark |
| -30 dBFS control | 478 | -36.72 | 1.0 | 0.0146 | dark |

The carried-over peak is gone: volume 102 now reads `shim_out_peak` 0.5295, matching its WAV peak 17352 within one
LSB (and 110 and 96 match theirs). The volume-127 pass produced no audio on its first strike — the same first-pass
settle the first sweep worked around — so that pass has no shim evidence in this run and the full-scale pass was not
re-measured. Every pass that produced audio meets the rule (neutral gain, shim peak equal to the WAV peak within one
LSB), so for those passes the measured level is the firmware's own track-volume→master gain law; the shim stays
proven transparent at full scale by the first sweep's settled 127 pass (WAV 32767, shim 1.0). The 2026-10-12 E14
figure (-0.65 dBFS at "volume 102") was taken before the hwsuite, the `volume_knob` recipe and the encoder-window
fixes existed, with a sweep method whose "volume" did not correspond to the firmware's OCR-confirmed track volume
the way this probe's does; the firmware binary and the knob model (counters, pad_curve) are unchanged since
`c215d32` (2026-10-12), so the two figures cannot both be the firmware's law.

The stage is named with evidence and HW-35 stays known-failing. Note for the owner: the deeper observation is that
mono 34 stays **dark even at 0.0 dBFS** (volume 127, full scale), so restoring the 5 dB would not by itself light
the lamp — the firmware does not raise `LED_CLIP` for a full-scale sampler playback in this emulator at all.

- `python3 -m unittest emulator.tests.test_sp16_over_led` — **18/18** (`OK`, nine new in the fix round).
- Quick set (14 modules) — **452/452** (`OK`).

### 2. `pt-i44.61` — AK4458 transport words re-run with the fixed page guard

No code change; the probe (`emulator/probes/ak4458_transport.py`, lane D1) and its page guard were already fixed.
Re-ran on `build/lk2-audio-tap`; the guard held (all four modes were tapped on the AUDIO TEST page, none exited with
"not on test page"). Run folder `build/lk2-audio-tap/ak4458-transport/20261028-143646`, every capture 176 400 of
176 400 frames usable.

Word → DAC slot → half → driven-by table (peak per word, grade O for the transport side):

| word | slot | half | none | MODE1 | MODE2 | MODE3 | driven by |
|---|---|---|---|---|---|---|---|
| 0 | 0 | L | 0.0000 | 0.0473 | 0.0562 | 0.0000 | MODE1, MODE2 |
| 2 | 1 | L | 0.0000 | 0.0473 | 0.0562 | 0.0000 | MODE1, MODE2 |
| 4 | 2 | L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 6 | 3 | L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 8 | 4 | L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 10 | 5 | L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 12 | 6 | L | 0.0000 | 0.0473 | 0.0000 | 0.0473 | MODE1, MODE3 |
| 14 | 7 | L | 0.0000 | 0.0473 | 0.0000 | 0.0473 | MODE1, MODE3 |

Every odd word reads 1.0000 (full-scale noise) whenever its even partner carries audio, and 0.0000 otherwise — a
property of the app's frame, not the codec. MODE2 isolates words 0/2 (→ OUT7-8, observed) and MODE3 isolates words
12/14 (→ MAIN, observed); MODE1 drives all four pairs, so the middle two (OUT3-4 on 8/10, OUT5-6 on 4/6) stay
inferred from the slot order.

**The word→DAC→jack mapping stays U.** The measured table says which transport words the app writes; the codec's
TDM-to-DAC assignment (which two halfwords form which DAC channel, and which half is left) and the board's
DAC-pin→jack wiring are not decided by anything the firmware or this emulator contains (the GPL `ak4458.c` driver
carries the register image and nothing about slot order). The emulator's monitor channels 12/14 stay an emulator
observation (O for the transport word, U for the jack behind it); they are not promoted to a physical mapping.

- `python3 emulator/probes/ak4458_transport.py run --build lk2-audio-tap` — four modes, 176 400/176 400 usable
  frames each, the fixed guard held on every mode.

### 3. `pt-i44.58` — filter send: first silent boundary reproduced; gdb probe and recipe parked

No code change; `emulator/probes/filter_send.py` re-run on `build/lk2-audio` (run folder
`build/lk2-audio/filter-send/20261028-144038`):

| pass | send peak | nonzero send frames | loop send peak | master peak |
|---|---|---|---|---|
| ACTIVE off | 0.0 | 0 | (none reported) | 1.0 |
| ACTIVE on | 0.0 | 0 | (none reported) | 1.0 |

The first silent boundary holds and is **at or above the send bus**: the master peaks at 1.0 while the send stream
carries not a single frame, with ACTIVE on or off. Lane D1 already showed the whole analog path beyond the app works
(factory MODE2 drives the same send DAC and return ADC), so what is silent is the app's own writing of the send pair.

The remaining half of the acceptance — the `--trace gdb` probe of the send enum (`TrackUnit+0x61c`), the mute mode
(`*(track+0x130)+0x84`), the selected object's `+0x39`/`+0x24`, `TrackUnit+0x60c` and the manager's pending/applied
config — and the 1.60 recipe that assigns the filter to a track are **parked**. Two things are missing and neither
was established in budget:

- **The recipe.** The filter must be assigned to the track in the app's UI (Pre FX / Post FX). That path goes through
  the FX/filter popup (`MIXER > FX SETTING`), the same area where lane R recorded a selector-popup reproduction
  problem (`docs/research/2026-10-12-toraiz-sp16-select-fx-popup.md`: SELECT FX selector not reproducible as working).
  Deriving the recipe needs a boot plus OCR-driven menu exploration, not a guess.
- **The gdb probe.** The offsets are static (lane R,
  `internal/docs/research/2026-10-28-sp16-firmware-gates/02-filter-send.md`), but the app is symbol-free, so the
  runtime probe has to find the live manager/TrackUnit/selected-object pointers first (the selected object is behind
  `track+0xc4`'s switchable-object indirection) before it can read any of them. That pointer-chasing is a debugger
  task of its own, not a bounded one-shot measurement.

No forced callback or unmute patch was made (the task forbids them).

- `python3 emulator/probes/filter_send.py run --build lk2-audio` — send stream silent with ACTIVE on and off,
  master peak 1.0.

## Side effects

- Builds made, all under the git-ignored `build/`: `build/lk2-audio` (`--research --audio file`),
  `build/lk2-audio-tap` (`--research --audio file --trace pcmtap --app-arg testmode`). QEMU was reused from
  `build/qemu-source` for the first and from `build/lk2-audio` for the second; the firmware library is shared.
- Processes: one over-led run, one ak4458 run, one filter-send run (two over-led runs total across the sweep and the
  re-run). Every session was stopped by its own probe's `finally` block by exact app PID; no emulator process from
  this lane is left running. Another lane's `lk1-live` timing probe was running throughout and was left untouched.

## Parked tasks

- `pt-i44.58` (second half): the filter-assignment recipe and the `--trace gdb` probe of the send enum / mute mode /
  `+0x39`. The first silent boundary is established (this lane and D1 agree); the recipe needs menu exploration and
  the gdb probe needs symbol-free pointer-chasing.

## Cross-lane requests

None. (`hwsuite.py`'s HW-35 check and `docs/audio-jack-map.md` are not this lane's files, and neither needed a change.)

## Open questions for the owner

- HW-35: mono 34 (`LED_CLIP`) stays dark even at 0.0 dBFS (volume 127, full scale). Since the shim is proven
  transparent, either the firmware does not raise its clip flag in this emulator, or the lamp role (mono 34 =
  `LED_CLIP`, service manual E15) is not what the firmware drives here. That is a firmware-reading question (the
  app's clip detector and its TX-bit wiring), not an emulator level-path question.
- Whether the E14 (2026-10-12) "-0.65 dB at volume 102" should be treated as a stale baseline: the firmware binary
  and the knob model are unchanged since then, so it cannot be reproduced against the current probe's OCR-confirmed
  volume readout.
