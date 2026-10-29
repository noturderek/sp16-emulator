---
title: "Review fix A, round 3 (lkx-sync): preserve partial frames and qualify downbeat alignment"
description: "Round-3 fixes for the relay's overflow framing corruption, consistent unverified downbeat alignment wording, and backpressure tests on the production Outbox. Earlier round reports are retained below."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, lkx-sync, review-fix]
author:
  harness: codex
  agent: lkx-sync
  model: gpt-6
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

## Round 2 (Codex Sol re-review, run `agent-dispatch-zwvfelih/final.txt`)

Sol re-reviewed the round-1 commits and rejected them with five blocking findings. Each is fixed with a unit test that
reproduces it first.

- **FINDING 1 [blocking]** `fake_master.py:83` — inserting `master` before `counter` broke the positional callers
  (`fake_master.py:197`, `segment_master.py:160`): their status counter became the master flag, so the first status
  was non-master and every serialized counter stayed 0. `master` is now keyword-only after `counter`, and both callers
  plus the `test_sp16_pdl_sync` helper pass it by keyword. Commit `ac3eaac`.
- **FINDING 2 [blocking]** `pdl_sync.py:253` — `_on_tempo` gated the ClockGenerator retune on `_sent_bpm`, so a tempo
  whose delivery failed left the generator at the failed tempo (120 ok, 140 failed, 120 left it at 140). The generator
  now follows the packet BPM gated only on its own current bpm; `_sent_bpm` caches only what Link accepted.
  Commit `c52eb3c`.
- **FINDING 3 [blocking]** `timing_probe.py:260` — picking clicks by proximity to the master cannot establish the
  follower's downbeat; `follow_phase` now reports the residual phase modulo one step as
  `step_phase_median_ms` / `step_phase_max_ms` and no longer claims a `downbeats` count. Commit `d7a296e`.
- **FINDING 4 [blocking]** `segment_relay.py:141` — the pending QEMU stream grew without bound and each immutable
  concatenation copied the backlog. Frames now queue in a bounded `Outbox` (a deque under a 1 MiB budget); overflow
  drops the oldest records, counts them and prints one line. Commit `ad7a74d`.
- **FINDING 5 [blocking]** `timing_probe.py:250` — a master log with no downbeat reached `min()` on an empty list.
  `follow_phase` now returns a structured insufficient-data result when the log has no `beat_in_bar 1`.
  Commit `ff0bcf4`.

## Round-2 hand-back

| Finding | Commit | Files | Check | Result |
|---|---|---|---|---|
| 1 | `ac3eaac` | net/fake_master.py, net/segment_master.py, tests/test_sp16_net.py, tests/test_sp16_pdl_sync.py | `python3 -m unittest emulator.tests.test_sp16_net emulator.tests.test_sp16_pdl_sync emulator.tests.test_sp16_sync_bridge` | 56/56 OK |
| 2 | `c52eb3c` | sync/pdl_sync.py, tests/test_sp16_pdl_sync.py | `python3 -m unittest emulator.tests.test_sp16_pdl_sync` | 23/23 OK |
| 3 | `d7a296e` | timing_probe.py, tests/test_sp16_timing_probe.py, tests/test_sp16_recipes.py | `python3 -m unittest emulator.tests.test_sp16_timing_probe emulator.tests.test_sp16_recipes` | 36/36 OK |
| 4 | `ad7a74d` | net/segment_relay.py, tests/test_sp16_segment_relay.py | `python3 -m unittest emulator.tests.test_sp16_segment_relay emulator.tests.test_sp16_segment` | 26/26 OK |
| 5 | `ff0bcf4` | timing_probe.py, tests/test_sp16_timing_probe.py | `python3 -m unittest emulator.tests.test_sp16_timing_probe emulator.tests.test_sp16_recipes` | 37/37 OK |

Full check (the quick set plus every changed module and the timing-probe tests):
`python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev
emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack
emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze
emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap
emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter emulator.tests.test_sp16_sync_bridge
emulator.tests.test_sp16_pdl_sync emulator.tests.test_sp16_net emulator.tests.test_sp16_segment
emulator.tests.test_sp16_segment_relay emulator.tests.test_sp16_timing_probe emulator.tests.test_sp16_recipes`
→ 576 tests, OK. `git diff --check` clean before every commit.

**What did not run and why.** No live run, build or hardware: all five fixes are source and unit-test changes, and the
relay's sustained backpressure is covered by a paced subprocess flood plus deterministic `Outbox` unit tests. Swift and
arm-contracts checks do not apply (no Swift or stub/contract files touched).

**Side effects.** None — no builds, no emulator sessions, no processes left running.

**Parked tasks.** None.

**Cross-lane requests.** None. (Same observation as round 1: `follow_phase` now measures modulo one step, so the
follower's bar position is still not observable from the clicks alone; `hwsuite.playhead_steps` would supply it if a
downbeat measurement is wanted later.)

**Open questions for the owner.**
1. The live `follow` block now reports `step_phase_median_ms` / `step_phase_max_ms` (residual modulo one step). A
   whole-step or whole-bar offset reads 0, so a future `timing_probe.py run --net mcast …` cannot confirm downbeat
   alignment by itself; that would need the sequencer position.

## Round 3 (Codex Astra re-review)

This round addresses the three findings in `agent-dispatch-1ohbzke7/final.txt`. The earlier reports above describe
DeepSeek's work; this round used Codex, thinking high. The session identifies its model family as `gpt-6`; an exact
runtime model identifier was not exposed, so the author metadata and trailers use that available identity.

1. **Preserve an in-progress frame during overflow.** `Outbox` retains complete records and the head's sent offset.
   Overflow evicts the oldest untouched record, skipping an in-progress head even when an oversized arrival must
   itself be discarded. A socket-pair regression fills the queue, partially sends a frame, overflows, drains, and
   parses exactly the expected length-prefixed frames with the QEMU-side reader. Ten subcases cover prefix and
   payload splits (offsets 1, 3, 4, 8 and 15), normal and oversized arrivals, and a later overflow after the protected
   record completes. Before the fix this test failed all ten subcases (five failures, five timeouts); afterward all
   pass. Commit `a0cd308`.
2. **Describe downbeat alignment as unverified consistently.** Correct the guide heading, both README claims and
   both architecture claims, including the overview table. The guide keeps the earlier ~495 ms figure only to
   explain why it cannot establish downbeat alignment. Commit `8011049`.
3. **Exercise production backpressure handling.** Remove unused `flush_stream()` and move its two tests to
   `Outbox.flush()`. Cover `BlockingIOError`, `InterruptedError`, zero-byte sends, preservation of a partially sent
   head, repeated partial sends, byte accounting and FIFO completion. No Python references to the removed helper
   remain. Commit `3abc839`.

**Complete Outbox review.** Read the entire class and its relay call sites. Only `flush()` advances the sent offset;
it resumes the head before attempting any following record and resets the offset only after completing that head.
Blocked, interrupted and zero-byte writes preserve both the offset and byte count. `push()` appends at the tail and
can evict only untouched records, including the new arrival if necessary. No other partial-frame splitting or
reordering path was found. The byte budget counts unsent bytes; retaining the head also retains its already-sent
prefix until completion (bounded by one frame, at most 65,539 bytes including framing at the relay receive limit).

## Round-3 hand-back

The check names below expand to the exact commands following the table. Every commit passed `git diff --check`,
and its complete staged diff was read before committing.

| Finding | Commit | Files changed | Check result lines |
|---|---|---|---|
| 1 | `a0cd308` | `emulator/net/segment_relay.py`, `emulator/tests/test_sp16_segment_relay.py` | Q: `Ran 456 tests`, `OK` (456/456); N: `Ran 52 tests`, `FAILED (failures=1, errors=1)` (50/52); I: `Ran 48 tests`, `OK` (48/48); F: `Ran 4 tests`, `OK` (4/4) |
| 2 | `8011049` | `README.md`, `docs/pro-dj-link.md`, `docs/architecture.md` | Q: `Ran 456 tests`, `OK` (456/456); all downbeat/alignment mentions in the three documents reviewed |
| 3 | `3abc839` | `emulator/net/segment_relay.py`, `emulator/tests/test_sp16_segment_relay.py` | Q: `Ran 456 tests`, `OK` (456/456); N: `Ran 52 tests`, `FAILED (failures=1, errors=1)` (50/52); I: `Ran 50 tests`, `OK` (50/50); F: `Ran 6 tests`, `OK` (6/6) |

Q (quick set, separately run after each finding):

```sh
python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter
```

N (requested network selection, run after findings 1 and 3):

```sh
python3 -m unittest emulator.tests.test_sp16_segment_relay emulator.tests.test_sp16_net emulator.tests.test_sp16_segment
```

I (selection without the two failing multicast tests; also excludes the obsolete helper tests before finding 3):

```sh
python3 -m unittest emulator.tests.test_sp16_segment_relay.ParseTests emulator.tests.test_sp16_segment_relay.OutboxTests emulator.tests.test_sp16_segment_relay.RelayTests.test_relay_exits_without_qemu emulator.tests.test_sp16_net emulator.tests.test_sp16_segment
```

F (deterministic production Outbox tests):

```sh
python3 -m unittest emulator.tests.test_sp16_segment_relay.OutboxTests
```

**Multicast failures reproduced before the fix.** N has two failures on this host:
`test_relays_both_ways_and_filters_own_echo` times out waiting for the guest frame;
`test_relay_bounds_the_outbox_under_sustained_backpressure` receives zero frames after the flood. To compare against
the unchanged relay, a temporary directory was populated with
`git show b3c196c:emulator/net/segment_relay.py`, the test module's `TOOLS` was pointed at that directory, and those
two `RelayTests` were run via `unittest.TextTestRunner`. Result: `Ran 2 tests`,
`FAILED (failures=1, errors=1)` (0/2), at the same assertions. This establishes that they predate these changes;
the host/network cause was not diagnosed. The deterministic socket-pair regression does not depend on multicast.

**What did not run and why.** Per the common brief, no full unit suite. No builds, native emulator smoke, live timing,
CoreMIDI, Carabiner or hardware runs: these findings concern host Python queue behavior and documentation, covered
by the checks above. Swift typechecking and ARM contracts do not apply; no Swift, stub or contract files changed.

**Side effects.** Tests started and cleaned up their own relay subprocesses and temporary sockets/directories. The
baseline comparison's temporary source tree was removed. No builds or firmware sessions started, no unrelated
processes stopped, no processes left running, and no push or PR.

**Parked tasks.** None of the three requested findings.

**Cross-lane requests.** None; this round's brief explicitly authorized the README and existing documentation edits.

**Open questions for the owner.** The two unchanged-relay multicast delivery failures need a separate diagnosis or
a rerun on a working multicast setup. Downbeat alignment still requires sequencer-position evidence; the current
probe alone cannot verify it.
