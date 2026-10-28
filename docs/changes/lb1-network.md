---
title: "Lane B1 hand-back: Pro DJ Link end to end and the CDJ-3000 link"
description: "A host-side mcast frame relay delivers the guest's keep-alive, a segment peer puts a fake master on the segment, and the sync bridge drives MIDI clock and Link."
type: report
status: partial
created: 2026-10-28
tags: [sp16, emulator, prodjlink, network, sync]
author:
  harness: pi
  agent: lb1-network
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane B1 hand-back

The Pro DJ Link network path is repaired end to end at the segment level. The root blocker was QEMU's
`socket,mcast=` backend: it binds its UDP socket to the multicast group address, and macOS never delivers a datagram
sent that way to another local listener, so the guest's keep-alives never reached `segment_watch.py`. A host-side relay
replaces that backend, and the 54-byte TORAIZ keep-alive now decodes on the segment about every 2 s. A host peer joins
the same segment, so a fake tempo master reaches the guest and sees its keep-alive. The follower bridge (MIDI clock and
Ableton Link) is implemented and unit-tested. The live follow measurement and the CDJ-3000 link remain parked behind
UI navigation the lane could not drive blind, and the firmware's follower path is ungraded (U).

`Q` is the required quick set:

```sh
python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter
```

## Per-task hand-back

1. **`pt-i44.84`, complete — `002f392`, `580696f`.** New `emulator/net/segment_relay.py`; changed
   `emulator/net/qemu-net.sh`, `emulator/native.py` (`bundle()` copies the relay next to the wrapper;
   `net_environment("mcast")` adds `SP16_PYTHON`), `.gitignore` (`.pi/`), and
   `emulator/tests/test_sp16_net.py`; new `emulator/tests/test_sp16_segment_relay.py`. The `mcast` wrapper now spawns a
   relay that QEMU attaches to over a unix `stream` (the same framing socket_vmnet uses) and that joins the multicast
   group on a wildcard-bound socket, forwarding guest frames to the group and group frames to the guest, dropping its
   own multicast echo by source MAC. `host`, `bridged:IF` and `none` modes are unchanged. Checks: `Q` **445/445 OK**;
   `python3 -m unittest emulator.tests.test_sp16_net emulator.tests.test_sp16_segment_relay` **23/23 OK**;
   `git diff --check` exit 0. Live `--net mcast` acceptance: `python3 emulator/net/segment_watch.py --seconds 65`
   decoded the 54-byte keep-alive every ~2 s (`type 0x06 name 'TORAIZ'`), **1/1**.

2. **`pt-i44.38` / `pt-i44.57`, keep-alive acceptance — `42c6f7c`.** Changed `emulator/net/segment_watch.py` (name
   field read at its true offset 0x0c, so `TORAIZ` decodes instead of an empty string) and
   `emulator/tests/test_sp16_segment.py`. That unconditional 0x0c was one byte early for every type on 50001/50002;
   the fix round below replaces it with the packet-family helper `fake_master.packet_name`. The live mcast run shows the
   guest binds its NIC, takes a link-local address, and emits the 54-byte `TORAIZ` keep-alive every ~2 s; the host
   watcher decodes it. No device identity is recorded
   (the MAC is a random per-start `52:54:00` address and the IP is the guest's Autoip address; both are synthetic).
   Checks: `Q` **484/484 OK** (with the new net/sync modules); `test_sp16_segment` **7/7 OK**. Host/bridged socket_vmnet
   modes were not re-run (they reuse the unchanged `unix` stream path and need the root daemon plus BPF for tcpdump).

3. **`pt-i44.39`, partial — `e4f8d95`.** New `emulator/net/segment.py` (IPv4/UDP/Ethernet framing, checksummed IP
   header, zero UDP checksum) and `emulator/net/segment_master.py` (fake master on the segment); new
   `emulator/tests/test_sp16_segment.py`. The master's keep-alive/beat/status packets now reach the guest over the
   mcast segment, and the master reads the guest's keep-alive back: a live 40 s run at 128 BPM pitch +2 % saw the master
   send its frames and log **20+ guest keep-alives**, whose type byte read `0x06` but whose **name field read `''`**: the
   master decoded it at `payload[11:31]`, one byte early (the 50000 layout puts a reserved 0x00 at 0x0b and the name at
   0x0c), so no live `TORAIZ` name was ever logged by this tool. The fix round below corrects that and unit-tests the
   decode (keep-alive `'TORAIZ'`, beat and status `'CDJ-3000'`) in both tools instead of re-measuring it live. The
   follow measurement (130.56 BPM within 0.05, downbeat within 5 ms over 60 s) is **0/1 not run**: it needs SYNC SOURCE
   = PRO DJ LINK and a playing pattern, which require blind menu navigation this lane cannot verify (the session model
   has no image support), and the firmware follower path is grade U. Parked with the segment-peer evidence above.

4. **`pt-i44.41`, code complete, end-to-end parked — `8c6f927`.** New `emulator/sync/pdl_sync.py` and
   `emulator/tests/test_sp16_pdl_sync.py`. `TempoTracker` holds the master tempo/phase;
   `effective_bpm = bpm x (1 + pitch)`; `PdlClockBridge` retunes `midi/clock.py`'s `ClockGenerator` (CoreMIDI clock)
   and drives a minimal `CarabinerLink` (TCP 127.0.0.1:17000, `set-tempo` on change, `force-beat-at-time` for the
   downbeat). Ableton Link itself is design reference only (GPL), so no Link code is bundled. Checks: `Q` plus the new
   modules **484/484 OK**; `test_sp16_pdl_sync` **10/10 OK**. The acceptance (a master tempo change reaching a real Link
   peer and a CoreMIDI clock monitor) is **0/1 not run**: it needs a live master, a running Carabiner and a CoreMIDI
   monitor, and follows from task 3's parked follow.

5. **`pt-i44.40`, parked — no commit.** Depends on task 3's follow and on booting a second emulator
   (`nsaintot_cdj3k-emu` / `9c5s_cdj3k-emu`) with `firmware/CDJ-3000/3.22` on the same segment. Not attempted; no
   CDJ-3000 firmware, keys or device identity were read or committed.

## Fix round (blocking review findings)

1. **`pt-i44.38`, blocking — `emulator/net/segment_master.py`.** The guest keep-alive name was decoded at
   `payload[11:31]`, one byte early, so the master printed `name ''` for the SP-16's `TORAIZ` keep-alive.
2. **`pt-i44.38`, blocking — `emulator/net/segment_watch.py`.** `42c6f7c` read the name at `[12:32]` for every type,
   which is only the port-50000 layout; a beat or status packet on 50001/50002 keeps its name at 0x0b, so `CDJ-3000`
   decoded as `DJ-3000`.

Both now call one helper, `fake_master.packet_name(payload, port)` (with `name_offset` beside it): the name is at 0x0c
for the port-50000 family (54-byte keep-alive, join stages, other 50000 types) and at 0x0b for the port-50001/50002
family (beat 0x28, status 0x0a); a payload on an unknown port is placed by shape (a 54-byte type-0x06 packet is the
50000 layout) and a payload too short for the field raises `ValueError` rather than decoding garbage. `fake_master.py`
also grew `NAME_LENGTH` and the two offsets as named constants; `segment_watch.py` now takes `MAGIC` from it too, and
`segment_master.py` prints through the new `keepalive_line()`/`frame_name()` seam so the decode is testable without the
send loop.

Checks: `python3 -m unittest emulator.tests.test_sp16_segment emulator.tests.test_sp16_net` **31/31 OK** (was 25
cases; new: the helper's three families, its unknown-port fallback and its short-payload refusal in `test_sp16_net`; a
hand-built 54-byte SP-16 keep-alive plus `fake_master`'s beat and status packets fed to both `segment_watch.describe()`
and the master's `keepalive_line()`/`frame_name()` in `test_sp16_segment`); `Q` **445/445 OK**; `git diff --check`
exit 0. Not run: the live `--net mcast` re-capture (the decode is now covered by golden-layout unit tests, and the
segment path itself was unchanged by this round). Note: `python3 emulator/net/segment_master.py --help` raises
`TypeError: must be real number, not dict` from argparse on this branch and on `0d4628a` before it; pre-existing and
outside this round's two findings, left untouched.

## Side effects

Built the research `--audio file` build `build/lb1`, reusing `build/sp16/library` and `build/qemu-source`. Started and
stopped three bounded `--net mcast` emulator boots and two 40-65 s segment watches; each app was stopped by its exact
PID and `ps` confirmed no emulator, QEMU, relay or master process remained. Logs, PCAPs, screenshots and WAV settings
stay under the git-ignored `build/lb1`. No firmware, key, device identity, data image or capture was committed. No
download, hardware flash, push, PR, merge or rebase occurred; the task queue was not mutated.

## Parked tasks and cross-lane requests

- `pt-i44.39` follow measurement needs a lane with screen visibility (or the dispatcher) to set SYNC SOURCE = PRO DJ
  LINK and start a pattern while the segment master runs, then run `emulator/timing_probe.py` against a networked
  session. The lane also found `net/timing_probe.py run` launches its own session without `--net`, so following cannot
  be measured through it as-is.
- `pt-i44.41` end-to-end needs Carabiner built and running (design reference only; not bundled) plus a CoreMIDI clock
  monitor, after task 3's follow.
- `pt-i44.40` is the integration tail of `.39`; the segment is ready for a second emulator.
- No `README.md` or existing `docs/*.md` were edited; `./sp16 run --net mcast` behavior changed (relay instead of
  `socket,mcast`) and the dispatcher owns any user-facing docs.

## Open questions for the owner

- Does the SP-16 firmware follow a master once its packets arrive, or does it need a player number / status packet it
  does not yet receive? Grade U until the SYNC SOURCE selection is attempted with the master present.
- Whether to switch the segment from the multicast group to loopback multicast (more robust against the host's
  interface churn; the relay already tolerates transient `No route to host` on en0).
