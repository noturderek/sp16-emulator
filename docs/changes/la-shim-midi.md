---
title: "Lane A: shim and MIDI"
description: "What the shim-and-MIDI lane changed in the SP-16 emulator (board-model sample formats, peak-equivalent dBu labels, the full 16-channel master transport, bridged networking, the shared MIDI clock engine) and the hand-back, with the MIDI clock recovered from a jittered clock and the page-8 MIDI check verified live."
type: report
status: active
created: 2026-10-28
tags: [sp16, emulator, shim, midi, clock, network, lane-la-shim-midi]
author:
  harness: pi
  agent: la-shim-midi
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane A: shim and MIDI

What this lane changed and how to use it, per task. Grades: **O** observed in this emulator, **S** static (code,
manual), **I** inferred, **U** unknown. Nothing below was measured on a unit.

## 1. S20_3LE is refused, not half-accepted

`codec_refuses` accepted S20_3LE (format 36) as a codec format, but the board model's sample path
(`bm_width`/`bm_get`/`bm_put`) carries only 2- and 4-byte layouts, so a negotiated S20_3LE stream got a width of 0 and
left the loop and input buffers untouched. The shim now refuses S20_3LE like the other unsupported layouts, so the app's
format search still settles on S24_LE as on the unit. `SP16_PCM_ANY_FORMAT=1` still restores the null plugin's answer.

## 2. S24_LE covered end to end in the contracts

The board and transport contracts only exercised FLOAT_LE (research-only) and S16_LE, leaving the actually-negotiated
S24_LE path untested. The board contract now round-trips 24-bit samples through the send/return loop (within one LSB
through the float queue) and meters an S24_LE input tone; the audio-transport contract gained an `s24` mode that writes
S24_LE on the 2-channel send/return playback and checks the packet carries the bit-exact FLOAT_LE conversion (`v/2^23`)
with `fmt=14`.

## 3. Filter loop wording

The loop queue (`loop_buf`) is a FIFO: it delivers a queued send frame whenever the return capture drains it, and enforces
no fixed one-period delay. Two stub comments said the capture "takes it one period later"; they now describe queued
delivery. The `docs/architecture.md` wording is a cross-lane request (phase-4 docs fold).

## 4. dBu readouts are named peak-equivalent

The shim derived `in_dbu`/`out_dbu` from the peak sample level but named them plain "dBu", which is an RMS unit. They are
now `in_dbu_peak`/`out_dbu_peak` everywhere they are shown (stub log line, the `guest.board.analog_levels` metric,
`service_stimulus.py`'s text, the level-contract and timing-probe tests) and the comments say explicitly they are
peak-equivalent dBFS + 16. The `docs/` line is a cross-lane request.

## 5. The master stream travels all 16 channels

The audio transport sent only master channels 12..15 so the host sink could hard-code the MAIN pair. The sink already
reads a full 16-channel stream (it keeps the historical WAV at ch12..15 when `first==0 && channels==16`), so the guest
now sends the whole stream by default and the host output monitor can pick any jack pair (lane B's menu). The transport
contract asserts all 16 channels arrive bit-exact.

## 6. Test page 8 verified live (no code change)

Built `./sp16 build --name lane-a --research --audio file --app-arg testmode`, ran it, stepped to page 8 (DEVICE TEST)
and confirmed: **MIDI CHECK : OK**, MAC `02:00:5E:10:00:01`, IP `192.0.2.16`, and page navigation continues. The stub's
loopback (`SP16_MIDI_LOOPBACK=1`) and fake `eth0` (`SP16_FAKE_ETH0=1`) were already in place from prior work; the run
confirmed they unwedge the page. Two findings for the wave-3 hwsuite check: the MIDI check is a **touch** button at about
(115, 112), not a physical key, and tapping it makes the app open `/dev/ttymxc1` twice (stub logs
`midi loopback /dev/ttymxc1 first bytes=4`).

## 7. Bridged `--net` gets a real-subnet address

`guest-init.sh` gave eth0 only a 169.254/16 link-local address in every `--net` mode, so LAN peers on a normal subnet
could not reach a bridged guest. The net mode (and an optional `--net-ip`) now travel in the guest's `/proc/cmdline`
(the QEMU wrapper appends `sp16.net=<mode> [sp16.netip=<addr>]` to `-append`); the init keeps the 169.254 alias for Pro
DJ Link and a bridged run also adds the static address on the interface's subnet (`native.py` validates and normalises it
to `a.b.c.d/prefix`, `/24` when bare). DHCP is tried if a client is present; the base guest ships none, so without
`--net-ip` the run stays on the alias and says so.

## 8. The shared MIDI clock engine

New `emulator/midi/clock.py` holds the timing both MIDI paths share, in one host time domain
(`time.monotonic_ns`): a second-order delay-locked loop (Fons Adriaensen) that recovers the period and BPM of a jittered
inbound 0xF8 stream, and a sample-position generator (gearmulator `midiClock`) that derives outbound clock from the audio
frame position rather than a timer. Inbound events are stamped at the edge and released a fixed latency later
(`RELEASE_LATENCY`). A synthetic 120 BPM clock with ±2 ms jitter recovers to 120.07 BPM and 20.820 ms (acceptance
0.1 BPM / 0.3 ms); the generator emits 24 pulses per beat at sample accuracy. The CoreMIDI-monitor sigma and SYNC SOURCE
acceptance belong to the DIN/USB paths (tasks 9/10).

## Hand-back

Checks are run from the repository root. "Quick set" is the brief's list of 14 modules (403 tests on this tree, plus the
lane's own modules where noted). The ARM contracts run under qemu-user in a throwaway Docker image through
`emulator/tests/arm_contract.py` (the helper the contract tests use); `emulator/arm-contracts.sh` itself was not run
end-to-end (it builds the large packaged guest image; the contracts it runs were exercised through the lighter helper).

| Task | Commit | Files | Checks and results |
|---|---|---|---|
| 1. S20_3LE | `7ad2779` | `hardware_stub.c`, `tests/sp16_hwparams_contract.c` | `sp16_hwparams_contract` (arm_contract helper): PASS, now also asserts format 36 is refused; quick set: Ran 403 tests, OK |
| 2. S24_LE contracts | `63feea3` | `tests/sp16_board_contract.c`, `tests/sp16_audio_transport_contract.c`, `arm-contracts.sh` | `sp16_board_contract` PASS and `sp16_audio_transport_contract` (file + new s24 modes) PASS; quick set: Ran 403, OK |
| 3. loop wording | `b7457d7` | `hardware_stub.c` (comments only) | `sp16_board_contract` PASS; quick set: Ran 403, OK |
| 4. dBu labels | `10d0b47` | `hardware_stub.c`, `guest-bridge.py`, `service_stimulus.py`, `tests/test_sp16_input_chain_contract.py`, `tests/test_sp16_timing_probe.py` | `test_sp16_input_chain_contract` + `test_sp16_timing_probe` + `test_sp16_analog_laws`: Ran 32, OK; quick set: Ran 403, OK |
| 5. 16 master channels | `5a9aa97` | `hardware_stub.c`, `tests/sp16_audio_transport_contract.c`, `arm-contracts.sh` | `sp16_audio_transport_contract` (file 16-channel + s24) PASS; quick set: Ran 403, OK |
| 6. page 8 E2E | — (no code change) | — | live: `--app-arg testmode` build booted, page 8 OCR "MIDI CHECK : OK", MAC/IP shown, navigation continued; console `midi loopback /dev/ttymxc1 first bytes=4` |
| 7. bridged net | `a2bbf57` | `native.py`, `guest-init.sh`, `net/qemu-net.sh`, `tests/test_sp16_net.py` | `test_sp16_net`: Ran 15, OK; `test_sp16_native` + quick set: Ran 458, OK; `sh -n` on both shell scripts OK |
| 8. MIDI clock | `2d80542` | `emulator/midi/__init__.py`, `emulator/midi/clock.py`, `tests/test_sp16_midi_clock.py` | `test_sp16_midi_clock`: Ran 8, OK (120 BPM ±2 ms → 120.074 BPM, 20.820 ms); quick set + `test_sp16_net`: Ran 426, OK |

**Did not run, and why.** `emulator/arm-contracts.sh` end-to-end (its contracts ran through the lighter `arm_contract.py`
helper; the full script builds the packaged guest image and was left to phase 4). The Swift typecheck
(`xcrun swiftc -typecheck` of `native.SWIFT_SOURCES`) was not needed: this lane touched no Swift. `./sp16 hwsuite` was not
run (lane C's file, and the wave-3 hwsuite check for page 8 is a cross-lane request). No CoreMIDI monitor or DAW run:
tasks 9/10 (DIN/USB paths) were not started, so the clock-out sigma and SYNC SOURCE acceptance stay with them.

**Side effects.** Builds under the git-ignored `build/`: `lane-a` (research, `--audio file --app-arg testmode`, then
rebuilt in place with `--trace strace` for the page-8 diagnosis). `build/qemu-source` was pre-existing and its QEMU was
reused (`--reuse-qemu-from`). Emulator sessions started by this lane were stopped by exact process id (13819, 27634); none
is running. Docker: a throwaway Debian image per contract run, removed afterwards. Nothing was pushed, rebased or merged;
no other worktree, the internal repository or `main` was touched. Scratch helpers live under `/tmp`.

**Parked tasks.** `pt-i44.17` (DIN MIDI path, L), `pt-i44.35` (USB-B MIDI device, L) and the optional `pt-i44.33`
(panel-frame stall) were not started: the budget ran to the clock engine (task 8, committed fully, as the brief directs
before starting 9). The clock engine they need is in place in `emulator/midi/clock.py`.

**Cross-lane requests.**

1. `docs/architecture.md` (~49): reword the filter-loop "one period later" to queued delivery (task 3).
2. `docs/` line for the level readouts: say the dBu fields are peak-equivalent (`in_dbu_peak`/`out_dbu_peak`), task 4.
3. Wave-3 hwsuite check for page 8 (lane D): the MIDI check is a touch button at about (115, 112); tap it, then expect
   "MIDI CHECK : OK" (task 6).

**Open questions for the owner.**

- The `--net-ip` path is complete for bridged mode; DHCP needs a client added to `Dockerfile.native-base`
  (`busybox-static`'s `udhcpc` is the small option) plus a guest rebuild. Is DHCP worth that now, or is `--net-ip` enough?
- For task 9, is `emulator/midi_bridge/coremidi-io.swift` (already a CoreMIDI virtual-port line bridge) the intended host
  side, or should the DIN path go straight to the signed MIDIServer driver plugin planned for the USB device?
