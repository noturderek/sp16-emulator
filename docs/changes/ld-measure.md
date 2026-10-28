---
title: "Lane D1: live audio and timing measurements"
description: "What the live-measurement lane observed in a running emulator: the HW-46 confirming run, the AK4458 transport-word to jack table, the residual panel-frame stall and the filter-send probe, with the commands and recordings behind each number."
type: report
status: active
created: 2026-10-28
tags: [sp16, emulator, audio, timing, lane-d1]
author:
  harness: pi
  agent: ld-measure
  model: deepseek-flash
  thinking: high
  tokens: unknown
---

# Lane D1: live measurements

Every number below was taken in a running emulator on this worktree, not on a unit. Grades: **O** observed in this
emulator, **S** static (code, manual), **I** inferred, **U** unknown.

Build under test everywhere unless a section says otherwise:

```sh
./sp16 build --name ld-measure --research --audio file \
    --library build/sp16/library --reuse-qemu-from build/qemu-source
```

firmware `sp16-1.60-ff1cc60c4b6e`, image SHA-256 `a2073e9d3b7dcdbe…`, app SHA-256 `3d1392a61323e73e…`, profile
`emulator/profiles/sp16-1.60.json` (`8684fbfcc89cd6e4…`).

## 1. HW-46 passes in a run that includes HW-35

`pt-wct33.28` asked only for the confirming run: the Goertzel fix (`phones_peak` reads the 1 kHz component only,
`d61624c`) is already on the branch.

```sh
./sp16 hwsuite --build ld-measure --only HW-35,HW-46
```

| Run | Order | HW-46 | HW-35 | Folder |
|---|---|---|---|---|
| A | HW-35, HW-46 | **PASS** (20.58 s) | FAIL (50.83 s) | `build/ld-measure/hwsuite/20261028-090002-0ebfff` |
| B | HW-46, HW-35 | **PASS** (20.69 s) | FAIL (52.41 s) | `build/ld-measure/hwsuite/20261028-090706-e39811` |
| C | HW-35 alone | — | FAIL (51.64 s) | `build/ld-measure/hwsuite/20261028-090249-e2b251` |

**HW-46 — O, passes; the acceptance is met.** Run A's five sub-checks, all PASS:

- ACTIVE drives GPIO5_IO05: `[(on, True, 1), (off, True, 0), (on, True, 1)]` — the select line follows the button
  three times over, so the fix is not masking anything.
- headphone 1 kHz level, ACTIVE on vs off (informational): `0.0971` / `0.0`.
- headphone 1 kHz level, CUTOFF open vs lowest (informational): `0.0971` / `3.8e-05`.
- the shim's loop returns the filter-input tone: `return_peak 0.5011`, `send_peak 0`, `underruns 0`, `drops 0`,
  3 283 968 frames.
- the track-insert send line, still informational: `send_peak 0` (see section 4).

The Goertzel change is what makes the first three lines mean something: run B measures the same 1 kHz component
(`0.0937` / `0.0`) with HW-46 run first, and the two informational levels differ by 3 % between the runs, which is the
fixture tone and the host sink's write position, not the check.

**HW-35 — a real, reproducible failure on this branch (O), not caused by this lane** (no commit of this lane changes
the audio path or the harness, and run C reproduces it on an otherwise untouched tree). Its first sub-check,
"OVER lit within 50 ms of the first near-full-scale sample", fails with `None` in three of three runs, in both
orders, while its other two sub-checks pass, including "full-scale tone reaches the output" (which the check only
reports when it fails, so that one passed silently).

A direct lamp probe settles the cause: with the same fixture loaded on track 3, VOLUME at 127 read back by OCR, and
the pad struck at velocity 127, the sink WAV for the window holds per-channel peaks `[32767, 0, 32767, 0]` on
`first_channel 12` — full scale on both master channels, grade O — and the only mono lamp channels that are ever lit
in the nine seconds sampled around the strike are `6, 12, 25, 26`. Channel **34, `over_led`, never lights**: not late,
never. (`panel_layout`/profile: `over_led` = mono 34; the service-manual notes grade it an M1 output driven by the
firmware's digital master peak, so the check's premise — the lamp follows the master level — is sound, grade S.)

So the firmware is not driving `LED_CLIP` for a full-scale sampler playback in this emulator, and the failure is in
the emulator or the fixture rather than in the check's reading of the lamp. Splitting "the app never raises its clip
detector" from "the clip detector's result never reaches TX bit 34" needs the meter that feeds it sampled at runtime;
that is the same class of probe as section 4, so it is written up there as an open question rather than guessed here.
The probe script is `build/over_probe.py` (git-ignored; the checks are the `--only` runs above plus that script's
`lit mono channel sets seen` line).
