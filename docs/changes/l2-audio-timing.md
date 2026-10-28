---
title: "Lane L2: audio and timing"
description: "What the audio-and-timing lane changed in the SP-16 emulator (codec rate, analog controls, limiter, input injection and jack map, step-jitter probe, panel-frame stalls), how to use it, and the hand-back."
type: report
status: complete
created: 2026-10-27
tags: [sp16, emulator, audio, timing, lane-l2]
author:
  harness: claude-code
  agent: emu-l2-audio-timing
  model: claude-sonnet-5-5
  thinking: max
  tokens: unknown
---

# Lane L2: audio and timing

What changed in this lane and how to use it, per task. Grades: **O** observed in this emulator, **S** static (code,
manual), **I** inferred, **U** unknown. Nothing below was measured on a unit.

## 1. Only 44.1 kHz on the codec PCMs

The codec clock is fixed at 44.1 kHz, so the shim now answers 44100 on all three board PCMs (AK4452 playback and
capture, AK4458 master): `set_rate` refuses any other rate, `set_rate_near` returns 44100, and a PCM starts at 44100
instead of 48000. `SP16_PCM_ANY_RATE=1` restores the null plugin's answer for research.

Test: `tests/sp16_rate_contract.c`, run under qemu-user in a throwaway Docker image by
`python3 -m unittest emulator.tests.test_sp16_rate_contract` (helper: `tests/arm_contract.py`).

## 2. Why S24_LE on the AK4452 ports stopped the master level following VOLUME

Finding: the format restriction (S24_LE as on the unit) has been the default since the commit that also made a silent
capture write zeros. With both in place the level follows VOLUME again, and HW-34 passes on S24_LE (levels 66, 211, 473,
841 over four VOLUME settings, six of six sub-checks). The likely cause is the stale capture buffer (grade I): the null
plugin leaves a capture buffer untouched, and in S24_LE the app read those bytes as a loud constant signal that does not
follow the track volume; as FLOAT_LE the same bytes are tiny or non-finite. The output path does depend on the capture
buffer: with a 0x5a pattern in it HW-34 fails (silent capture). The exact old symptom (constant 19465) was not reproduced:
the control run for that experiment did not finish on a loaded host.

Research switch to repeat it: a file `capture-stale` in the folder given to `./sp16 run --dev-overlay` containing
`untouched` (leave the buffer as the app had it) or `pattern` (fill with 0x5a). Contract: `sp16_input_chain_contract`.

## 3. Host-side models of MAIN OUT, INPUT LEVEL, LP cutoff and resonance

The shim already applied these; they are now written down and tested. `emulator/analog_laws.py` holds the reference
curve of each control and its grade: MAIN OUT `pot^2`, INPUT LEVEL `4 * pot^2`, cutoff `20 Hz * 2^(pot * log2 1000)`,
damping `2 - 1.9 * pot` (Q 0.5 to 10). **Every curve shape is an assumption (U)**; only the endpoints are inferred (I).
`tests/sp16_analog_laws_contract.c` feeds the shim a master stream and the send/return loop and checks that a MAIN OUT
change alters the copy the host receives and not the app's buffer, and that CUTOFF and RESONANCE move the loop's
response to the reference value within 8 %. The firmware's words (the ADC word MAIN OUT also reports, the DAC word for
the high-pass) are untouched. MAIN OUT now scales only OUT1-2 (channels 12 and 14), not every channel sent.

## 4. Input limiter and dBu readout

GPIO5_IO06 (the AUDIO TEST LIMIT button, high = on, grade I from the line name) switches a soft limiter on the input
capture, after INPUT LEVEL: transparent up to half of full scale, soft saturation above (threshold and curve are
assumptions, U). The shim logs `analog levels ...` per window and on every limiter change; the bridge puts it in
`metrics.json` as `guest.board.analog_levels` (`limiter_active`, `limited_samples`, input and output peaks, `in_dbu`,
`out_dbu`, source). The input is metered after INPUT LEVEL and before the limiter, the output on the MAIN pair after MAIN OUT.
dBu = dBFS + 16: converter full scale is taken as the jack's rated maximum level, +16 dBu (I).
`emulator/service_stimulus.py --build NAME` sets the manual's service stimulus (-10 dBV in, INPUT LEVEL and MAIN OUT at
maximum) on a running research session.

Live check (factory test, AUDIO TEST page, injected 1 kHz / 2 kHz tones, INPUT LEVEL at maximum): after the LIMIT button
`limiter_active` went true with `limited_samples` in the tens of thousands, and the master WAV peak fell from 0.376 to
0.282 (the curve maps 1.0 to 0.75). The button also went false again on the second press.

## 5. Audio input injection and the channel-to-jack map

See [../audio-jack-map.md](../audio-jack-map.md) and `emulator/audio-jack-map.json`. Injection: put `input.wav` in the
`--dev-overlay` folder (up to 64 MB; replacing or removing it while the firmware plays is safe). Map (O unless marked): MAIN = OUT1-2 = MASTER is master channels 12/14; OUT7-8 is 0/2; OUT3-4 (8/10)
and OUT5-6 (4/6) are inferred (I) from the slot order; PHONES is the AK4452 playback stream. The odd master channels
are padding. For a later output monitor menu (not built here): `AudioSink.OutputJack` and `AudioSink.monitor`
(audio-sink.swift) select the jack the CoreAudio output plays; `SP16_PCM_PORT_CHANNELS=0:16` (or an overlay file
`pcm-port-channels`) makes the guest send all 16 master channels while the master WAV keeps channels 12..15.

## 6. Timing: step jitter probe and panel-frame (TX) stalls

`emulator/timing_probe.py run --build NAME` launches a session with the suite's fixture stick, puts the click sample on
all 16 steps, plays 60 s and prints the step onset jitter from the WAV (sample accurate), the frames the guest dropped
from the recording during the window, the panel frame period and stalls as the host received them, the host load, and the
bridge's own link counters (`metrics.guest.board.bridge_timing`). `wav FILE` and `tx FILE_OR_SESSION_DIR` analyse
recordings. A click already sounding when the window opens is read from a 0.3 s look-back, so it is not taken for a late
onset (the fixture click rings for about 80 ms; without this one run showed a 24 ms "outlier" that was only the window edge).

What it found:

- **Audio is sample accurate (O).** Onsets 124.99 to 125.01 ms apart, 120.000 BPM, jitter sigma 0.006 ms (0.25 sample),
  maximum 0.02 ms, no frame lost, every stream at `speed=1.00x`. The 120/120/135 ms step intervals seen on the lamps are the
  125 ms step observed through the firmware's 15 ms panel frames (8, 8 and 9 frames, 375 ms for three steps; I): not audio
  jitter.
- **Loaded hosts spoil the recording, not the emulated timing (O).** Runs taken while another session's calibration loaded
  the host showed 1 to 1.7 ms sigma with missing and early clicks: the guest's audio ring (about 0.7 s) overflowed because the
  host did not drain the audio port (up to 2.4 million frames dropped, receipt latency up to 46 s), so the WAV had holes.
  The probe reports `gap_frames_in_window` and `targets_met.audio_window_clean` so such a run cannot pass as a measurement.
  Those runs are not used for any number below.
- **Panel-frame stall cause 1, guest side (O).** The bridge's TX listener took the host-port lock to forward each frame, and
  the same lock is held while a 1.5 MB LCD frame is written. While it waited it stopped draining the shim's panel datagram
  queue (depth 10), so under load the shim dropped panel frames (2048 or more per run) and the receive gaps reached 3.9 s; on a
  quiet host it still waited up to 45 ms for the lock. Fix (`guest-bridge.py`): the listener only receives, a sender thread
  forwards the TX log and the lamp feedback, the datagram queue is raised (best effort) and the receive buffer enlarged, and
  the LCD frame loop leaves the port idle for as long as the last frame took to write.
- **Panel-frame stall cause 2, host side (O).** A session in the background (bench mode starts the app with `open -g`) is
  subject to App Nap, which delayed every thread of the app, the panel port reader and the audio sink alike, by tens to
  hundreds of milliseconds. Two checks: the audio port's late packets (over 20 ms) rose and fell with the panel stalls window
  by window (1092, 550, 165 late packets with 24, 19, 5 stalls), and disabling App Nap for the test bundle took the same three
  windows to 0, 21, 0 late packets and 0, 12, 0 stalls (the middle window had an outside load spike). Not the LCD frames:
  cutting the frame rate from about 29 to 4 per second changed nothing (5 against 4 stalls in about 6000 gaps). Fix
  (`audio-sink.swift`): `AudioSink.start()` holds a latency-critical activity assertion for the life of the sink, as the
  speaker output already did; file-only builds had none.

Quiet-host numbers (each run: PLAY at 120 BPM, 60 s window, two runs per build, runs alternated; load averages read at the
start of the window; panel frames as the host saw them; a stall is a gap over 40 ms; mean period 15.0 ms in every run):

| Build | load1 / load5 at start | sigma ms | p99 ms | max ms | stalls | guest receive gap max ms |
|---|---|---|---|---|---|---|
| old bridge, run 1 | 10.6 / 17.6 | 4.76 | 25.6 | 83.8 | 6 | 19 |
| old bridge, run 2 | 6.4 / 11.4 | 8.88 | 48.9 | 134.3 | 58 | 65 |
| fixed bridge, run 1 | 7.8 / 14.0 | 4.46 | 25.6 | 89.6 | 4 | 21 |
| fixed bridge, run 2 | 9.9 / 10.1 | 5.93 | 26.8 | 150.3 | 12 | 25 |
| fixed bridge + activity assertion, run 1 | 10.4 / 13.0 | 1.64 | 19.9 | 33.5 | 0 | 29 |
| fixed bridge + activity assertion, run 2 | 9.8 / 12.0 | 1.89 | 19.7 | 76.5 | 1 | 28 |

Targets: onset sigma under 1 ms is met by two orders of magnitude (0.006 ms). The 40 ms stall ceiling held in one of the two
final runs; the other had a single 76.5 ms gap in 4098 frames with the guest link clean (largest receive gap 28 ms, write 2 ms).
Before this lane the same measure gave mean 15.0 ms, sigma 5.7 ms, maximum 164 ms (a 20 s run); the "old bridge" rows
reproduce that (sigma 4.8 to 8.9 ms, maximum 84 to 134 ms).

## Cross-lane requests

Each needs a file this lane does not own.

1. **`emulator/native-window.swift`: take the same activity assertion at launch.** The host-side panel-frame stalls came
   from App Nap throttling the background app (section 6). This lane holds the assertion in `AudioSink.start()`, which
   covers every build with an audio sink (`--audio file`, `both` and the counters-only runs), but a build without a sink that
   runs in the background is still exposed. Suggested: `ProcessInfo.processInfo.beginActivity(options: [.userInitiated,
   .latencyCritical], reason: "SP16 panel transport")` once at launch, held for the life of the app. Untested hardening of the
   reader, only if stalls remain: take the TX-log timestamp when the message header is read (before any lock), write the log on
   its own queue (`writeTxLog` writes the file inside the shared `lock`), and format hex without a `String(format:)` per byte.
2. **`emulator/arm-contracts.sh`: run the four contracts this lane added in the packaged image** (they currently run in
   a lighter image from `tests/arm_contract.py`). After the `sp16_board_contract` expectations add:

   ```sh
   run sp16_rate_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t; $PRE -E LD_LIBRARY_PATH=/tmp/run /tmp/run/t; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_PCM_ANY_RATE=1 /tmp/run/t any"
   run sp16_input_chain_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t; rm -f /tmp/run/analog /tmp/run/in.wav; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_ANALOG=/tmp/run/analog -E SP16_INPUT_WAV=/tmp/run/in.wav /tmp/run/t"
   run sp16_analog_laws_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -lm -o /tmp/run/t; rm -f /tmp/run/analog; : > /tmp/run/port; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_ANALOG=/tmp/run/analog -E SP16_PCM_PORT=/tmp/run/port /tmp/run/t 0.25 0.285714 0.446934"
   run sp16_input_race_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -pthread -o /tmp/run/t; rm -f /tmp/run/analog /tmp/run/in.wav; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_ANALOG=/tmp/run/analog -E SP16_INPUT_WAV=/tmp/run/in.wav -E SP16_INPUT_WAV_POLL_MS=2 /tmp/run/t"
   ```

   (the last three numbers come from `analog_laws.py`: MAIN OUT gain at 500, the loop gain of a 1 kHz tone at CUTOFF 500 with
   RESONANCE 0 and 500). The `capture-stale` and `service` modes of `sp16_input_chain_contract` (a mode name as the first
   argument) need `/opt/sp16/dev-share/capture-stale` and a 2.6 s run, and are covered by the lighter image only.
3. **`emulator/profiles/sp16-1.50.json`, `sp16-1.60.json` (and `profiles.py` validation): the jack table.** The task asked
   for the channel-to-jack table in the profile; add `audio.jacks` as the `jacks` array of `emulator/audio-jack-map.json`
   (jack, stream, left, right, grade). `panel-layout.json` names no audio channel, so there is nothing to replace there.
4. **`README.md` line about what is not emulated and `docs/architecture.md` (MAIN OUT paragraph, audio codec row).** README:
   "audio input and live sampling (the research input tone stands in), and output routing beyond the main stereo pair" is now
   partly out of date (a research build plays an `input.wav` into the input, and the master channels map to jacks); say so
   and point to `docs/audio-jack-map.md`. architecture.md: MAIN OUT "scales the master channels the host receives" should read
   "scales OUT1-2 (master channels 12 and 14)"; the codec row should mention that every board PCM is fixed at 44.1 kHz
   (`SP16_PCM_ANY_RATE=1` for research).
5. **When the output monitor menu is built:** make the guest send all 16 master channels by default (`ax_init` first=0,
   count=16 in `hardware_stub.c`), then update `tests/sp16_audio_transport_contract.c` and the `arm-contracts.sh` expectation
   `audio transport hw:ak4458audio,0 -> /dev/sp16-pcm ... channels=12..15 of 16` (they assert channels 12..15). The sink
   already keeps the WAV at channels 12..15 either way.
6. **`emulator/hwsuite.py`: `MAX_VMS = 3`** refuses to start while other lanes' emulators run, so the suite cannot be used
   by parallel lanes; I ran HW-34 through a scratch wrapper that raises the limit in memory (not committed).

## Hand-back

Checks are run from the repository root. "Quick set" is the brief's list of 14 modules. No panelshots: nothing here draws
(live evidence was taken from LCD snapshots of the factory test, kept under the git-ignored `build/`).

| Task | Commit | Files | Checks and results |
|---|---|---|---|
| 1. 44.1 kHz | `fee3b8a` | `hardware_stub.c`, `tests/sp16_rate_contract.c`, `tests/test_sp16_rate_contract.py` | `python3 -m unittest emulator.tests.test_sp16_rate_contract`: Ran 2 tests, OK (2 of 2, Docker); quick set: Ran 358 tests, OK |
| 3. analog laws | `6e8048e` | `analog_laws.py`, `tests/arm_contract.py`, `tests/sp16_analog_laws_contract.c`, `tests/test_sp16_analog_laws.py`, `tests/test_sp16_analog_laws_contract.py`, `tests/test_sp16_rate_contract.py` | `test_sp16_analog_laws`: 6 of 6 OK; `test_sp16_analog_laws_contract`: 1 of 1 OK (Docker; also passes against the unchanged stub); `test_sp16_rate_contract`: 2 of 2 OK; quick set OK |
| 4. limiter, dBu | `f4e88f6` | `hardware_stub.c`, `guest-bridge.py`, `service_stimulus.py`, `tests/sp16_input_chain_contract.c`, `tests/test_sp16_input_chain_contract.py` | `test_sp16_input_chain_contract`: 4 of 4 OK; quick set OK; live: AUDIO TEST LIMIT toggled `limiter_active` and clipped the master WAV (0.376 to 0.282) |
| 5. injection, jack map | `a97e42c` | `docs/audio-jack-map.md`, `audio-jack-map.json`, `audio-sink.swift`, `audio-selftest.swift`, `guest-bridge.py`, `hardware_stub.c`, contract and test files | `test_sp16_jack_map`: 4 of 4 OK; `test_sp16_audio_sink`: 1 of 1 OK (3 scenarios); `test_sp16_input_chain_contract`: OK; `xcrun swiftc -typecheck` of the two sink files OK; live: factory test MODE1/2/3 measured |
| 2. S24_LE | `65a21b8` | `hardware_stub.c`, `tests/sp16_input_chain_contract.c`, `tests/test_sp16_input_chain_contract.py` | HW-34: PASS, 6 of 6 sub-checks (research build l2, firmware 1.60); `test_sp16_input_chain_contract`: 5 of 5 OK |
| 6. timing | `8026845` | `guest-bridge.py`, `hardware_stub.c`, `audio-sink.swift`, `timing_probe.py`, `tests/test_sp16_timing_probe.py` | `test_sp16_timing_probe`: 15 of 15 OK; `test_sp16_audio_sink` and `test_sp16_audio_output`: 2 of 2 OK; quick set OK; six quiet-host probe runs (table in section 6); `speed=1.00x` on 10 of 10 stream lines in each of the six sessions |

Order: task 2 was committed after task 5 because its live check needed a quiet host.

**Review fixes** (three findings of the code review, one commit each, after the notes were first committed):

| Finding | Commit | Files | Checks and results |
|---|---|---|---|
| The input WAV refresh ran unsynchronized on several PCM threads (task 5) | `246fbd4` | `hardware_stub.c`, `tests/sp16_input_race_contract.c`, `tests/arm_contract.py`, `tests/test_sp16_input_chain_contract.py`, `docs/audio-jack-map.md` | new race contract: replaces and removes the WAV 500 times in about four seconds while one thread reads and another paces; segfaults on the old code with the polling sped up, passes three runs in a row on the new (500 reloads, 10 removals, 0 bad frames each); `test_sp16_input_chain_contract`: 6 of 6 OK at that commit |
| A zero jitter or a zero stall was read as missing (task 6) | `8228fbf` | `timing_probe.py`, `tests/test_sp16_timing_probe.py` | `test_sp16_timing_probe`: 18 of 18 OK (the old code reports a perfect run as missing two targets) |
| The service-stimulus text put the input meter before INPUT LEVEL (task 4) | `801b81e` | `service_stimulus.py`, `hardware_stub.c` (comment), `docs/audio-jack-map.md`, `tests/sp16_input_chain_contract.c`, `tests/test_sp16_input_chain_contract.py` | `test_sp16_input_chain_contract`: 8 of 8 OK, including the shim's own in_dbu for the preset against `expected_in_dbu()` |

The race fix, in short: one lock covers every read of the source and the swap; the refresh does its file work outside the lock
and takes it only to swap; one thread at a time refreshes; the WAV is copied into private memory (no fault when the host
truncates or replaces the file; 64 MB at most); and the source keeps its mapping base. The old code passed a pointer into the
data chunk to `munmap`, which is not the base, so replaced files were never unmapped.

Slow modules: `test_sp16_audio_output` (touches `audio-sink.swift`) passes on a quiet host. Under heavy host load its
`device-unavailable` scenario (waits 0.5 s for a state change) fails the same way on the unchanged files.

**Final run on the lane's HEAD `801b81e` (before this notes commit; quiet host).** Quick set: `Ran 358 tests in 5.2s`, `OK`
(358 of 358). Lane modules, all OK: `test_sp16_rate_contract` 2 of 2, `test_sp16_analog_laws` 6 of 6,
`test_sp16_analog_laws_contract` 1 of 1, `test_sp16_input_chain_contract` 8 of 8, `test_sp16_jack_map` 4 of 4,
`test_sp16_audio_sink` 1 of 1, `test_sp16_timing_probe` 18 of 18, `test_sp16_audio_output` 1 of 1. The repository's own stub
contracts, `emulator/arm-contracts.sh` (ten contracts, in the packaged guest image): 34 PASS, 0 FAIL, run twice (after the race
fix and on the final tree). The lane's research build (`./sp16 build --name l2 --research --audio file`) plays firmware 1.60
with `speed=1.00x` on all five streams (three playback, two capture): 10 of 10 stream lines in each of the six measured
sessions (taken before the review fixes, which do not touch pacing).

**Did not run, and why.** The lane's four new contracts inside `emulator/arm-contracts.sh` (held; they run in a lighter image
from `tests/arm_contract.py`, and the script's own ten contracts ran as it is). `./sp16 hwsuite` itself (it refuses with more
than 3 VMs on the host; HW-34 ran through a scratch wrapper). For task 2, the control run and the `untouched` run of the
reproduction (host load at the time, then skipped to save time). A live MAIN OUT sweep in the factory test, skipped to save time
(the shim-level contract covers MAIN OUT; INPUT LEVEL and the limiter were checked live). No live run after the review fixes
(they change locking and reporting, not pacing). Probe runs on loaded hosts are not used for any number.

**Side effects.** Builds under the git-ignored `build/`: `l2` (rebuilt several times), `l2tm` (factory test mode), `l2base` and
`l2old` (the bridge before the fix, for the A/B). Docker: a throwaway Debian image per contract test, removed afterwards, two
scratch images used for manual runs (also removed), and the untagged guest image `arm-contracts.sh` builds and removes. One experiment set the preference `NSAppSleepDisabled` for the
test bundle `local.sp16-compatible.emulator.fullpanel.l2` and deleted it again (its domain no longer exists). Emulator sessions
started by this lane (`./sp16 run`, the probe, the suite) were stopped by exact process id; none is running. Nothing was
pushed, rebased or merged; no other worktree, the internal repository or `main` was touched; the calibration run of another
session was left alone. Scratch helpers (hwsuite wrapper, channel analysis, A/B scripts) are outside the repository.

**Parked tasks.** None. The output monitor menu was not built (by design).

**Open questions for the owner.**

- The limiter's threshold and curve, the knob laws and the +16 dBu full-scale mapping are assumptions; they need a
  measurement on the unit (a swept tone through the jack, LIMIT on and off).
- OUT3-4 and OUT5-6 (master channels 8/10 and 4/6) are inferred from slot order. A track routed to those outputs, or a
  unit measurement, would settle them.
- Should `native-window.swift` take the activity assertion itself (cross-lane request 1), and should `MAX_VMS` in the suite
  allow parallel lanes?
