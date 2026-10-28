---
title: "Lane LK1 live hand-back: menu recipes, the hardened segment tools, the sync bridge runner and the follow verdict"
description: "A menu recipe drives SYNC SOURCE = PRO DJ LINK and USB note output, the segment tools are hardened with tests, a running sync bridge emits MIDI clock, and the follow measurement says the firmware does not yet follow a fake master."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, prodjlink, midi]
author:
  harness: pi
  agent: lk1-live
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane LK1 hand-back

Branch `lane/lk1-live`, cut from `emu/2026-10-28` (`7058eed`). Six commits, one theme each, plus this file. One
research build was made (`build/lk1-live`, `--research --audio file`, QEMU reused from `build/qemu-source`) and
booted several times, always stopped by the exact app PID.

The headline result is a **follow verdict**, not a success: with SYNC SOURCE = PRO DJ LINK set through a real menu
recipe, the HOME header gained `LINK` and the segment master logged the guest's `TORAIZ` keep-alives, but the
sequencer still ran at 120.0 BPM against the master's 130.56 (tempo error -10.56 BPM, phase unbounded). The
firmware's follower path does not simply work once packets arrive; it wants player/status fields it is not yet
getting. Everything else the brief asked for is delivered and tested.

The required quick set (14 modules) plus every new or changed module is
**544/544 OK** (`python3 -m unittest` with the quick-set list and
`test_sp16_net_tools_help test_sp16_net test_sp16_segment test_sp16_pdl_sync test_sp16_recipes
test_sp16_timing_probe test_sp16_sync_bridge`).

## Per-task hand-back

1. **`pt-i44.39` follow the fake master — code complete, acceptance parked — `a6eb983`, `80cb4ab`.**
   Files: `emulator/menu/sync_source.py` (new), `emulator/timing_probe.py`, `emulator/tests/test_sp16_recipes.py`
   (new). A menu recipe sets SYNC SOURCE SELECT = PRO DJ LINK by LCD OCR (UTILITY opens by tapping 748,58; the
   setting list is the left column, the value the right; the focus is remembered across visits so `focus_row`
   searches from wherever it starts). `timing_probe.py run --net mcast --master-bpm 128 --master-pitch 2` starts the
   segment master, launches the session itself (`hwsuite.launch` has no `--net` and `hwsuite.py` is lane 3's file),
   runs the recipe, plays, and measures; its report gained a `follow` block (master effective BPM, measured tempo and
   error, and a downbeat phase error — every 16th click onset vs the master's beat-in-bar 1, modulo the bar).
   **Live run:** the recipe set the source (header `LINK`), the master saw `TORAIZ` keep-alives, but the sequencer
   ran at 120.0 BPM (`tempo_error_bpm` -10.56, phase median 419 ms). Parked with that evidence; grade U is now
   "needs a player number / status field the fake master does not send".

2. **`pt-i44.85` harden the segment tools and force Link phase — complete — `8aaee30`.**
   Files: `emulator/sync/pdl_sync.py`, `emulator/net/segment.py`, `emulator/net/segment_master.py`,
   `emulator/net/segment_watch.py`, `emulator/net/qemu-net.sh`, and the `test_sp16_net`/`test_sp16_segment`/
   `test_sp16_pdl_sync` tests. `PdlClockBridge` now sends Carabiner's `force-beat-at-time` (beat 0 of the next bar,
   one bar-period ahead) on every downbeat and `CarabinerLink` grew `status()`/`parse_status()`; `parse_frame`
   refuses a UDP length below 8 or overrunning the frame; `segment_master` skips a malformed MAGIC frame
   (`keepalive_fields`) and drains a frame burst in one paced pass (`drain_frames`); the unused `struct` import is
   gone; the two tools resolve their sibling imports against their own directory; `qemu-net.sh` exits 2 when the
   relay socket never appears. Each fix is pinned by a test.

3. **`pt-i44.41` sync bridge end to end — MIDI clock half done, Link half parked — `9f330d4`.**
   Files: `emulator/sync/bridge.py` (new), `emulator/tests/test_sp16_sync_bridge.py` (new). A running bridge joins
   the mcast segment, feeds the master's beat/status packets to `PdlClockBridge`, and emits 24-PPQN 0xF8 clock on a
   `ClockEmitter` thread at the master's effective BPM (the CoreMIDI sink is a thin `Port` adapter, so the
   packet→tempo→clock pipeline is unit-tested without CoreMIDI); `--link` also drives a Carabiner on 127.0.0.1:17000.
   **Carabiner is not installed on this host** (no binary, nothing listening on 17000), so the Ableton Link half
   cannot be run live; the MIDI clock half is unit-tested (feed retunes the bridge, the emitter sends 24 PPQN and
   retunes without restarting, `run` feeds a beat packet and emits clock).

4. **`pt-i44.35` USB-B note output — recipe done, note output parked — `1eff387`, `40fa8c1`.**
   Files: `emulator/menu/sync_source.py`, `emulator/tests/test_sp16_recipes.py`. A `usb-note-out` recipe sets
   MIDI OUT MODE (USB) = OUT (0x2b = 0x120) and the project's MIDI OUT NOTE/CC = ENABLE (0x11000102 = 1) in one
   UTILITY visit; the list has two NOTE/CC rows, so the recipe focuses the unique MIDI OUT MODE (USB) row first and
   searches upward for the MIDI OUT NOTE/CC. **Live:** the recipe set both (OCR-confirmed) on a running `--midi`
   session, but pressing all 16 pads produced **no note** on the `SP-16 USB` CoreMIDI source — the two settings are
   not the whole gate; playable content and the channel map remain. The recipe is the deliverable; the note acceptance
   stays open.

5. **`pt-i44.40` link with an emulated CDJ-3000 — parked, no commit.** Depends on the follow (task 1), which the live
   measurement shows is not working, and on booting a second full emulator. The CDJ firmware
   (`firmware/CDJ-3000/3.22`, `CDJ3Kv322.zip`) and both emulators (`internal/external/nsaintot_cdj3k-emu`,
   `9c5s_cdj3k-emu`) are in place, but booting them would not change the follow verdict.

## Side effects

- Built `build/lk1-live` (`./sp16 build --name lk1-live --research --audio file --library build/sp16/library
  --reuse-qemu-from build/qemu-source`).
- Booted and stopped several sessions (one manual mcast session for menu discovery, one manual `--midi` session for
  the USB recipe, and two `timing_probe.py run --net mcast` follow runs); every emulator was stopped by its exact app
  PID and the follow runs cleaned up their own session and master. A parallel lane's `lk2-audio` session was left
  untouched.
- Pre-built the CoreMIDI helper (`build/midi-bridge/coremidi-io`, swiftc) because the two `--midi` bridges raced to
  build it and both failed with `ld: File exists`; the helper is a build artifact, not committed.
- No firmware, key, device identity, data image, PCAP or session capture was committed.

## Parked tasks

- `pt-i44.39` follow acceptance (130.56 BPM within 0.05, downbeat within 5 ms): the firmware runs at its own tempo.
- `pt-i44.35` note output acceptance: no note after the settings, needs playable content / channel map.
- `pt-i44.41` Link half: Carabiner is not installed.
- `pt-i44.40`: blocked by the follow verdict.

## Cross-lane requests

- The firmware follower's missing player/status fields are a firmware-RE question for the firmware lane, not a
  segment-tooling one: the packets arrive (keep-alives, beats, status all decode), but the sequencer does not follow.

## Open questions for the owner

- What player number / status field does EP085_Main_R require before it follows a Pro DJ Link master's tempo? The
  fake master assigns itself player 1 and sets play/master/sync flags; the header shows LINK and MASTER but the
  sequencer stays at 120.0 BPM.
- Is a virtual CoreMIDI note stream acceptable evidence for pt-i44.35, or must the firmware-generated note actually
  arrive (the recipe now enables the settings, but no note leaves on a pad hit)?
