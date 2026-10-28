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

## 2. The AK4458 transport words: what the app writes, per AUDIO TEST mode

`pt-i44.61` asked for the sixteen transport words against the eight DAC outputs. The transport half is measured here;
the DAC half is not decided by anything this emulator can show, and the reasons are below.

### How

The master stream is sixteen `S16_LE` channels per frame, and the host sink keeps only words 12..15 in its WAV, so the
only way to see all sixteen is the shim's research PCM tap. New tool:

```sh
./sp16 build --name ld-measure-tap --research --audio file --trace pcmtap --app-arg testmode \
    --library build/sp16/library --reuse-qemu-from build/qemu-source
python3 emulator/probes/ak4458_transport.py run --build ld-measure-tap
```

It steps the factory test to page 11 (AUDIO TEST), sets the shim's input tone to slot 700 (about -18 dBFS, under the
limiter, knobs left neutral), selects each mode in turn and prints one row per transport word. Run folder:
`build/ld-measure-tap/ak4458-transport/20261028-101625` (the table is that folder's `ak4458-transport.json`). Each
mode's capture is 176 400 frames, all usable, taken wholly inside the mode.

Stimulus, as measured in the capture rather than assumed: the words MODE1 drives read a dominant frequency of
**1000.01 Hz** with **0.9995** of their energy inside 1 kHz ± 150 Hz.

### The table (peak per word, grade O)

| word | pair | `none` (OFF) | MODE1 | MODE2 | MODE3 | driven by |
|---|---|---|---|---|---|---|
| 0 | 0 L | 0.0000 | 0.0473 | 0.0542 | 0.0000 | MODE1, MODE2 |
| 1 | 0 R | 0.0000 | 1.0000 | 1.0000 | 0.0000 | — (full-scale noise) |
| 2 | 1 L | 0.0000 | 0.0473 | 0.0542 | 0.0000 | MODE1, MODE2 |
| 3 | 1 R | 0.0000 | 1.0000 | 1.0000 | 0.0000 | — |
| 4 | 2 L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 5 | 2 R | 0.0000 | 1.0000 | 0.0000 | 0.0000 | — |
| 6 | 3 L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 7 | 3 R | 0.0000 | 1.0000 | 0.0000 | 0.0000 | — |
| 8 | 4 L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 9 | 4 R | 0.0000 | 1.0000 | 0.0000 | 0.0000 | — |
| 10 | 5 L | 0.0000 | 0.0473 | 0.0000 | 0.0000 | MODE1 |
| 11 | 5 R | 0.0000 | 1.0000 | 0.0000 | 0.0000 | — |
| 12 | 6 L | 0.0000 | 0.0473 | 0.0000 | **0.0473** | MODE1, MODE3 |
| 13 | 6 R | 0.0000 | 1.0000 | 0.0000 | 1.0000 | — |
| 14 | 7 L | 0.0000 | 0.0473 | 0.0000 | **0.0473** | MODE1, MODE3 |
| 15 | 7 R | 0.0000 | 1.0000 | 0.0000 | 1.0000 | — |

Read the even words only: **MODE1 puts the stimulus on 0, 2, 4, 6, 8, 10, 12 and 14; MODE2 on 0 and 2; MODE3 on 12
and 14.** The untouched page leaves all sixteen at zero, and the tap does not even arm on it, which is positive
evidence of silence rather than a missing capture.

An independent cross-check inside the same runs: the shim's own meter on the MAIN pair (words 12 and 14, after MAIN
OUT) reads 0.0473 in MODE1 and MODE3 and 0.0000 in MODE2, which is what the tap's words 12 and 14 read. The two
instruments agree word for word.

### The odd words are not silent

Every even word that carries audio has its odd partner reading **exactly 1.0000** with 0.016 to 0.022 of its energy at
the tone, against 0.9995 to 1.0000 for the tone words — i.e. full-scale noise across the band (the earlier note's
"full-scale pattern near the top of the band", reproduced). When the even word is zero the odd word is exactly zero
too, so the odd word is written only for the slots the app is using at that moment. That is a property of the app's
own frame, not of the codec.

### What this settles, and what stays U

- **O:** which transport words the app writes audio into, per test mode (the table). Words 0/2 are the only pair
  MODE2 isolates and words 12/14 the only pair MODE3 isolates, so the two pairs the emulator's jack map already
  grades O are corroborated from the transport side.
- **I, unchanged:** OUT3-4 on words 8/10 and OUT5-6 on words 4/6. MODE1 drives all four pairs and no mode separates
  the middle two, so the slot order cannot be read off a mode. This lane adds no evidence either way and does not
  promote them.
- **S:** the shipped kernel programs sixteen 16-bit SSI slots per frame with the codec in its 8-channel TDM mode
  (`docs/research/2026-10-28-sp16-firmware-gates/04-ak4458-pcm.md`, Control6 = `0x8d`), so one frame is eight channel
  words of two halfwords. Which two slots form which DAC channel, and which half is left, is **not** in the GPL codec
  driver (`sound/soc/codecs/ak4458.c` carries the register image and nothing about slot order), so it is **U**.
- **U:** which DAC pin each channel word leaves on, and which jack each pin feeds. Neither is decided by anything the
  firmware or this emulator contains. So a word-to-DAC-to-jack table cannot be completed from this side: it needs the
  codec's TDM-to-DAC assignment plus the board's wiring, or a measurement on a unit. **The mapping stays U**, and the
  emulator's monitor channels 12/14 stay an emulator observation (O for the transport word, U for the jack behind it).

### One new observation on the analog loop

MODE2 comes back through the send DAC and the return ADC, and its word 0 reads a dominant frequency of **1030.16 Hz**
where MODE1 and MODE3 read 1000.01 Hz: a 3 % rise through the modelled loop, with 0.5835 of the energy left inside
1 kHz ± 150 Hz and a level of 0.0542 instead of 0.0473. Grade O for the numbers, U for the cause — it is either the
shim's loop rate or the firmware's send path, and this lane did not separate them. It matters for two things already
recorded: HW-46's informational headphone levels, and the filter-send probe (section 4), whose return path is the same
one.

## 3. The residual panel-frame stall: it is no longer in the bridge

`pt-i44.33` asked for the probe's two figures over 60 s and for the panel-frame (TX) stalls to be found and fixed.
Section 6 of [l2-audio-timing.md](l2-audio-timing.md) is the history: 164 ms maximum before that lane, 76.5 ms after
its bridge and App Nap fixes. Two things were missing from the tool: it could not say *which hop* a stall sat on, and
so a residual stall could not be attributed to a file.

### What changed in `timing_probe.py`

`run` now also reports a `link` block and a `stall_hop`: the bridge counts each panel frame when it arrives from the
guest and the host app logs each one when its own reader has taken it, so comparing the two over the same window says
where the delay was. `link_window()` takes the counter differences and treats `tx_gap_max_ms` correctly as a running
maximum — a value that grew during the window *is* that window's maximum, and a value that did not grow is reported as
`null` rather than as a measurement of zero. `stall_hop()` returns `guest-to-bridge` when the bridge itself waited that
long between two arriving frames, `bridge-to-app` when it received them on time and the app's reader or log writer was
late, and `none` for a clean window.

### Two 60 s runs

Commands (load read at the start of each window, from the same worktree and build as section 1):

```sh
python3 emulator/timing_probe.py run --build ld-measure --seconds 60
```

| Run | load1 before / after | VMs | onset sigma | TX frames | period mean / sigma | TX stall max | stalls > 40 ms | bridge's own max gap | bridge gaps > 40 ms | `stall_hop` |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 7.8 / 7.3 | 1 | 0.006 ms | 4137 | 15.03 / 1.56 ms | **21.9 ms** | 0 | ≤ 34.6 ms (unchanged) | 0 | `none` |
| 2 | 8.3 / 10.2 | 2 | 0.006 ms | 4144 | 15.06 / 2.15 ms | **46.9 ms** | 3 | 36.5 ms (grew from 27.5) | 0 | **`bridge-to-app`** |

Both runs: tempo 120.000 BPM, `step_samples_fitted` 5512.5 against 5512.5 nominal, no missing steps, no short or long
intervals, `gap_frames_in_window` 0, `guest_drops_in_window` 0, every stream `speed=1.00x`. Run 1's report carries
`targets_met` true on all three; run 2 meets the audio target and misses the 40 ms stall ceiling on three frames.

Run 2 was taken with another lane's emulator running (two QEMU processes, load 8.3 rising to 10.2), which is what
makes it the interesting one: the audio path stayed sample accurate (0.006 ms sigma) while the panel log stalled three
times, so the two paths really are independent, as the probe's own note says.

### What the localisation means

In run 2 the bridge received every frame with a worst gap of **36.5 ms** and **no** gap over the 40 ms limit, while the
host app's own log shows three gaps over 40 ms with a worst of **46.9 ms**. The extra latency and the three stalls are
therefore not the guest's pacing, not the datagram path and not the bridge's reader; they happened between the bridge
handing the frame to the app and the app's `writeTxLog` line. That path is `emulator/native-window.swift`, which this
lane does not own, and the reason is visible in it: `writeTxLog` takes the shared `lock` and then writes the file
inside that lock, on the socket-reader thread, taking its timestamp only after it got in. That lane's own hardening
note (cross-lane request 1 of [l2-audio-timing.md](l2-audio-timing.md)) asks for exactly the fix; this run is the
evidence that it is still needed and now names the hop rather than guessing at it. The exact change is in this file's
cross-lane requests, item 1.

The bridge side needs nothing: its own worst gap stayed under the limit in both runs, `lock_wait_max_ms` was 0.9 and
4.5 ms, `frame_write_max_ms` 4.6 and 1.5 ms, and `outbox_dropped` was 0.

Before and after, for the record: 164 ms maximum (20 s run, before that lane) → 76.5 ms (one frame in 4098, after its
bridge and App Nap fixes) → 21.9 ms with no stall over 40 ms in 4137 frames on a quiet host, and 46.9 ms with three in
4144 when another emulator shares the machine.
