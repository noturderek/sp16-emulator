---
title: "Review fix A (lkx-sync): sync bridge, segment relay, timing probe, Pro DJ Link docs"
description: "Fixes for Codex Sol's review findings in the sync bridge, the segment relay, the timing probe and docs/pro-dj-link.md."
type: report
status: active
created: 2026-10-28
tags: [sp16, emulator, lkx-sync, review-fix]
author:
  harness: pi
  agent: lkx-sync
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Review fix A (`lkx-sync`)

This lane fixed the review findings Codex Sol raised on the `lane/lkx-sync` branch (run
`agent-dispatch-eatqen1f/final.txt`). Files owned and changed: `emulator/sync/bridge.py`,
`emulator/sync/pdl_sync.py`, `emulator/net/fake_master.py`, `emulator/net/segment_relay.py`,
`emulator/timing_probe.py`, `docs/pro-dj-link.md`, and the tests under `emulator/tests/` for those modules.
Every finding is fixed with a unit test that reproduces it first; none was refuted as wrong.

## Findings

- **FINDING 1 [blocking]** `bridge.py:147` — `clock_sink` sent `Message(kind)`, but `Message` is the four-field
  namedtuple `(type, channel, number, value)`, so the first 0xF8 raised `TypeError` and killed the clock worker.
  Fixed to `Message("clock", None, 0, 0)` (the realtime shape `midi/midi.py`'s decoder itself uses).
  Commit `85d9883`.
- **FINDING 3 [blocking]** `bridge.py:102` — every player's status packet retuned the clock; a stopped non-master's
  status overwrote the master's tempo. `feed_status` now ignores a status whose master flag is clear (returns no
  packet). `fake_master.status_packet` gained a `master=True` flag so the case is testable. Commit `963e37f`.
- **FINDING 4 [blocking]** `bridge.py:130` — malformed beat/status payloads raised `ValueError`/`struct.error`/
  `IndexError` straight through the receive loop and ended the bridge; the status parser's length gate stopped at
  `0x93` although it reads through offset `0xa6`. The gate now requires `0xa7` bytes and the loop skips a frame that
  raises `ValueError`. Commit `34722cf`.
- **FINDING 5 [blocking]** `pdl_sync.py:223` — `_on_tempo` recorded `_sent_bpm` even when `set_tempo` returned False,
  so a failed Carabiner delivery was never retried at the same BPM. It now records only on a `True` return.
  Commit `d64a510`.
- **FINDING 6 [blocking]** `segment_relay.py:127` — `sendall` on the nonblocking QEMU stream raised `BlockingIOError`
  under backpressure and closed the relay. Frames to the guest are now buffered in a pending queue and flushed only
  when select reports the socket writable (`flush_stream` keeps the unsent tail of a partial send). Commit `fe033c4`.
- **FINDING 10 [blocking]** `timing_probe.py:248` — `follow_phase` took every 16th onset and assumed the first was a
  downbeat, so a window opened at step 3 reported a false ~250 ms phase. Each onset now reads its step number (1..16)
  from the master beat nearest it (beat-in-bar plus sub-beat offset) and only step-1 onsets measure phase against the
  nearest master downbeat. Commit `5d27502`.
- **FINDING 11 [blocking]** `docs/pro-dj-link.md:130` — the Link-only example omitted `--link`; it now passes it.
  Commit `168f7cf`.
- **FINDING 12 [blocking]** `docs/pro-dj-link.md:143` — the claim that the bridge works against a real master on
  `host`/`bridged` networks was wrong; the bridge reads encapsulated Ethernet on the mcast segment only. The doc now
  says native Pro DJ Link UDP (50001/50002) must first be re-encapsulated onto the segment (not automated).
  Commit `168f7cf`.
- **FINDING 14 [non-blocking]** `timing_probe.py:315` — `stall_hop`'s "mixed" label is ambiguous evidence, not proof
  of stalls on both hops (one upstream stall also opens a gap in the app's own log). Docstrings clarified.
  Commit `80443b9`.

## Hand-back

| Finding | Commit | Files | Check | Result |
|---|---|---|---|---|
| 1 | `85d9883` | sync/bridge.py, tests/test_sp16_sync_bridge.py | `python3 -m unittest emulator.tests.test_sp16_sync_bridge` | 8/8 OK |
| 3 | `963e37f` | sync/pdl_sync.py, net/fake_master.py, tests/test_sp16_pdl_sync.py | `python3 -m unittest emulator.tests.test_sp16_pdl_sync emulator.tests.test_sp16_net emulator.tests.test_sp16_sync_bridge` | 52/52 OK |
| 4 | `34722cf` | sync/pdl_sync.py, sync/bridge.py, tests/test_sp16_pdl_sync.py, tests/test_sp16_sync_bridge.py | `python3 -m unittest emulator.tests.test_sp16_pdl_sync emulator.tests.test_sp16_sync_bridge` | 30/30 OK |
| 5 | `d64a510` | sync/pdl_sync.py, tests/test_sp16_pdl_sync.py | `python3 -m unittest emulator.tests.test_sp16_pdl_sync` | 22/22 OK |
| 6 | `fe033c4` | net/segment_relay.py, tests/test_sp16_segment_relay.py | `python3 -m unittest emulator.tests.test_sp16_segment_relay emulator.tests.test_sp16_segment emulator.tests.test_sp16_net emulator.tests.test_sp16_net_tools_help` | 50/50 OK |
| 10 | `5d27502` | timing_probe.py, tests/test_sp16_timing_probe.py | `python3 -m unittest emulator.tests.test_sp16_timing_probe` | 25/25 OK |
| 11, 12 | `168f7cf` | docs/pro-dj-link.md | doc-only (no test) | — |
| 14 | `80443b9` | timing_probe.py | `python3 -m unittest emulator.tests.test_sp16_timing_probe` | 25/25 OK |

Full quick set plus every changed module:
`python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev
emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack
emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze
emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap
emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter emulator.tests.test_sp16_sync_bridge
emulator.tests.test_sp16_pdl_sync emulator.tests.test_sp16_net emulator.tests.test_sp16_segment
emulator.tests.test_sp16_segment_relay emulator.tests.test_sp16_net_tools_help emulator.tests.test_sp16_timing_probe`
→ 562 tests, OK. `git diff --check` clean before every commit.

**What did not run and why.** Finding 10's note asked for a live re-measurement
(`timing_probe.py run --build lkx-sync --net mcast --master-bpm 128 --master-pitch 2` after
`./sp16 build --name lkx-sync --research --audio file`). This was not run: it needs a research build and a firmware
boot on the metered API, and the fix itself is covered by the unit test above. The new phase figure should come from
the follow-up measurement, not this commit.

**Side effects.** None. No builds, no emulator sessions started or stopped; all changes are source and test files only.

**Parked tasks.** None — every finding was fixed or (none) refuted.

**Cross-lane requests.** None. (One observation, not a request: a whole-bar phase error in
`timing_probe.follow_phase` is measured modulo one step, because identical clicks on all 16 steps cannot say which
click is step 1; resolving that to the full bar would need the sequencer position — `hwsuite.playhead_steps` already
decodes the playhead step from the panel TX log, but timing_probe does not consume it.)

**Open questions for the owner.**
1. Finding 10's live phase figure: after this fix, `follow_phase` reports phase at onsets whose nearest master beat is
   a downbeat. If the firmware's downbeat genuinely lands ~1 beat off, this reads the sub-beat residual rather than the
   whole-beat offset; the re-run should confirm which regime applies.
2. Finding 12 was fixed by correcting the doc (per the note, "correcting the doc is enough"). If a native-UDP
   50001/50002 reader is wanted later, it is a separate feature, not a doc change.
