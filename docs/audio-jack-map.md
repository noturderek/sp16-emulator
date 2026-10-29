---
title: Audio input injection and the output jack map
description: How to play a WAV into the emulated input jack, which master channel feeds which output jack, and the API an output monitor needs.
type: reference
status: active
created: 2026-10-27
tags: [sp16, emulator, audio]
author:
  harness: claude-code
  agent: emu-l2-audio-timing
  model: claude-sonnet-5-5
  thinking: max
  tokens: unknown
---

# Audio input injection and the output jack map

What reaches which jack, how to feed audio into the input, and the API an output monitor needs. Grades: **O**
observed in this emulator, **I** inferred, **U** unknown. Nothing here was measured on a unit.

## Injecting audio into the input jack

Research builds can play a WAV into the input capture (`hw:ak4452audio,0`, the AK5386 input ADC):

```sh
./sp16 run --name NAME --dev-overlay DIR      # DIR/input.wav
```

`input.wav` is 16/24/32-bit PCM or 32-bit float, mono or stereo (mono feeds both channels), any rate (it is resampled
linearly to 44.1 kHz), up to 64 MB, and loops. The shim looks at the file once a second, re-reads it when its size
changes (a file that is still being written is picked up on a later look) and goes back to silence (or the research
test tone) when it is removed; replacing or removing it while the firmware plays is safe. Set `SP16_INPUT_WAV` to read
another path. The chain is the board's: INPUT LEVEL gain, then
the input limiter while the firmware holds the LIMIT line high, then the converter. `metrics.json` reports
`guest.board.analog_levels`: peaks and peak-equivalent dBu readouts (`in_dbu_peak` and `out_dbu_peak`,
where dBu = dBFS + 16 and converter full scale is taken as +16 dBu peak-equivalent, grade I) of the input
(metered after INPUT LEVEL, before the limiter) and of the MAIN output pair, `limiter_active`,
`limited_samples`, and the source (`wav`, `tone` or `off`).
`emulator/service_stimulus.py` sets the service manual's stimulus (-10 dBV in, INPUT LEVEL and MAIN OUT at maximum)
on a running session.

## Which channel is which jack

The master stream (`hw:ak4458audio,0`) is 16 S16 channels: eight DAC slots of two channels, and only the even
channel of each slot carries audio. The odd channels are padding (O).

| Jack | Stream | Left | Right | Grade | Evidence |
|---|---|---|---|---|---|
| MAIN (OUT1-2, MASTER) | ak4458audio-0 | 12 | 14 | O | AUDIO TEST MODE3 lights channels 12 and 14 only |
| OUT3-4 | ak4458audio-0 | 8 | 10 | I | regular slot order; no mode isolates this pair |
| OUT5-6 | ak4458audio-0 | 4 | 6 | I | as OUT3-4 |
| OUT7-8 | ak4458audio-0 | 0 | 2 | O | AUDIO TEST MODE2 lights channels 0 and 2 only |
| PHONES | ak4452audio-0 | 0 | 1 | O | carries the input in MODE1 and MODE2, silent in MODE3 |

The machine-readable table is [`emulator/audio-jack-map.json`](../emulator/audio-jack-map.json). The slot order is the
reverse of the jack numbers: the pair the firmware uses for the master mix is channels 12/14, so "OUT1-2" is the last
slot, not the first.

MAIN OUT (the analog volume) scales only the MAIN pair of the copy the host hears; the other jacks are not behind
that knob.

### How it was measured

Build with `--research --audio file --app-arg testmode`, run with a dev overlay holding `input.wav` (1 kHz left, 2 kHz
right) and `pcm-port-channels` containing `0:16`, and press HOME until PAGE 11 : AUDIO TEST shows. Touch MODE1, MODE2
or MODE3 (800x480 panel, buttons at y 145: x 272, 400, 528; LIMIT at x 656) and read the per-channel level and
dominant frequency of `audio-ak4458audio-0.wav`. Page 8 (DEVICE TEST) now renders; pages step one HOME press at a time,
so wait for the title before the next press.

### Which transport words the app writes

The host WAV keeps only words 12..15, so all sixteen words are visible only through the shim's research PCM tap. The
probe does the whole run:

```sh
./sp16 build --name NAME-tap --research --audio file --trace pcmtap --app-arg testmode
python3 emulator/probes/ak4458_transport.py run --build NAME-tap [--mode MODE1 ...]
python3 emulator/probes/ak4458_transport.py show build/NAME-tap/ak4458-transport/<run>/ak4458-transport.json
```

It steps the factory test to page 11 (AUDIO TEST), sets the shim's input tone to about -18 dBFS, selects each mode in
turn and prints one row per word (peak, dominant frequency, how many of the capture's frames were usable). It counts a
word as carrying the stimulus when its peak is at least `--floor` (0.01, -40 dBFS) and at least `--purity` (0.5) of its
energy lies within `--band` Hz of `--tone`; on the captures it takes, a tone word scores 0.90 and full-scale noise
scores 0.005. The untouched page is captured first as a silence baseline, and every mode tap is confirmed on page 11
by OCR before it is used.

Measured (firmware 1.60, grade O for the transport side; two runs, MAIN pair readings quoted from the second):

| Even words | Pair | `none` | MODE1 | MODE2 | MODE3 |
|---|---|---|---|---|---|
| 0, 2 | 0/1 | 0 | 0.0473 | 0.0562 | 0 |
| 4, 6, 8, 10 | 2..5 | 0 | 0.0473 | 0 | 0 |
| 12, 14 | 6/7 | 0 | 0.0473 | 0 | 0.0473 |

The two runs' levels agree to the printed digit except MODE2's first pair, which measured 0.0542 in the first run.

MODE1 drives words 0, 2, 4, 6, 8, 10, 12 and 14; MODE2 isolates 0 and 2; MODE3 isolates 12 and 14. Every odd word reads
exactly 1.0000 (full-scale noise) whenever its even partner carries audio and 0.0000 otherwise, so the odd words are
written only for the slots the app is using at that moment. The shim's own meter on the MAIN pair agrees with words
12 and 14 word for word.

What this settles and what it does not:

- **O:** which transport words the app writes per mode. MODE2 and MODE3 corroborate the two pairs the jack table above
already grades O.
- **I, unchanged:** OUT3-4 on words 8/10 and OUT5-6 on words 4/6. MODE1 drives all four pairs and no mode separates
the middle two, so the slot order cannot be read off a mode.
- **U:** which DAC pin each channel word leaves on and which jack each pin feeds. The codec's TDM-to-DAC assignment is
not in the GPL driver, so a word-to-DAC-to-jack table cannot be completed from the emulator side. The monitor channels
12/14 stay an emulator observation: O for the transport word, U for the jack behind it.
- One measurement to keep in mind: MODE2 comes back through the send DAC and the return ADC at 1030.16 Hz where MODE1
and MODE3 read 1000.01 Hz, a 3 % rise through the modelled loop. It is either the shim's loop rate or the firmware's
send path; the two were not separated.

## The filter send

The analog filter receives whatever the app writes to its send stream (`hw:ak4452audio,1`). The probe plays a tone on a
track, samples the send stream, the master stream and the shim's loop counters while the pad sounds, then presses
ACTIVE and repeats:

```sh
python3 emulator/probes/filter_send.py run --build NAME [--track 3] [--sample hw-tone440.wav] [--seconds 2.5]
#   --setup-recipe NAME runs a profile recipe first, for a filter that has to be assigned to the track
```

Measured (firmware 1.60, two runs, grade O): with a full-scale tone on the master, the send stream carries a 0.0 peak
and **0 nonzero frames**, with ACTIVE on and with ACTIVE off, and the shim's loop reports `send_peak 0` and
`return_peak 0`. So the first silent boundary is at or above the send bus, not in the send DAC, the loop or the PCM
transport: the factory test's MODE2 drives the same send DAC and return ADC and comes back with the tone.

What is silent is the app's own writing of the send pair. Why is not known: the send enum, the mute mode and the
selected object's flags live in the app's heap and need `--trace gdb` plus a symbol-free pointer chase, and the filter
also has to be assigned to the track in the UI before the send can be expected to carry anything. That recipe does not
exist yet, so "never assigned" and "assigned and still silent" cannot be told apart. No forced callback or unmute patch
was made.

## The OVER lamp

HW-35 watches the OVER lamp (mono channel 34, `LED_CLIP`). The probe sweeps the track volume, strikes a pad loaded with
a 0 dBFS tone at velocity 127 and records, over the same window, the master stream's peak from the sink WAV, the shim's
board-model gain and output peak, and every TX frame's mono lamp set:

```sh
python3 emulator/probes/over_led.py run --build NAME [--track 3] [--volumes 127,110,102,96] [--seconds 3]
```

It runs a discarded warm-up strike first (the first strike of a run is silently dropped), and its last pass plays the
-30 dBFS fixture as a control that has to stay dark, labelled with the track volume and level it actually played at.
Each pass says whether mono 34 lit and which mono channels did.

Measured (firmware 1.60, grade O): the master reaches 32767 (0.0 dBFS, 13280 near-full-scale samples) at track volume
127 with a neutral MAIN OUT gain, and the shim's own MAIN-pair peak equals the WAV peak within printing tolerance in
every pass that produced audio (21600 at volume 110, 17352 at 102, 15100 at 96). Mono 34 stays **dark in every pass**.
So the shim's level path is transparent and the lamp is not driven by sampler playback at all: HW-35 is declared
known-failing against the OVER lamp's real +5.1 dBFS threshold (see
[hardware-suite.md](hardware-suite.md#hw-35-and-the-over-lamp)), and the firmware's clip detector is the open question.

A 0 dBFS sample at track volume 102 measured -5.52 dBFS here, against -0.65 dBFS on 2026-10-12 (E14). The firmware
binary and the knob model are unchanged since then, and the earlier figure came from a sweep whose "volume" did not
match the OCR-confirmed track volume, so the two cannot both be the firmware's gain law; the current probe's figures
are the ones to use.

## Output monitor menu and API

- The host application provides an **Output** menu (MAIN, PHONES, OUT 3-4, OUT 5-6, OUT 7-8) and the research control
  `./sp16 ctl monitor MAIN|PHONES|OUT3-4|OUT5-6|OUT7-8` to select which jack pair CoreAudio plays (thread safe,
  flushes the output queue on change). The menu is enabled when an audio sink exists. `metrics.json` reports the
  selected jack in `audio.monitor`. The WAV files never depend on the monitor selection and always record the MAIN pair.
- The guest sends all 16 master channels by default. `AudioSink.OutputJack` (`audio-sink.swift`) defines `main`,
  `phones`, `out34`, `out56`, `out78`; `source` gives the stream id and channel pair. In file-only builds or when
  monitoring MAIN, the historical WAV recording retains channels 12..15.
