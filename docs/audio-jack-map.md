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

## Output monitor menu and API

- The host application provides an **Output** menu (MAIN, PHONES, OUT 3-4, OUT 5-6, OUT 7-8) and the research control
  `./sp16 ctl monitor MAIN|PHONES|OUT3-4|OUT5-6|OUT7-8` to select which jack pair CoreAudio plays (thread safe,
  flushes the output queue on change). The menu is enabled when an audio sink exists. `metrics.json` reports the
  selected jack in `audio.monitor`. The WAV files never depend on the monitor selection and always record the MAIN pair.
- The guest sends all 16 master channels by default. `AudioSink.OutputJack` (`audio-sink.swift`) defines `main`,
  `phones`, `out34`, `out56`, `out78`; `source` gives the stream id and channel pair. In file-only builds or when
  monitoring MAIN, the historical WAV recording retains channels 12..15.
