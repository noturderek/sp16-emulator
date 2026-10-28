---
title: "Lane F2: review follow-ups, the panel stall fix and HW-35"
description: "The residual panel-frame stall, the HW-35 OVER lamp measured rather than assumed, and five review follow-up tasks across the MIDI bridge, the shim, the probes and the docs."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report]
author:
  harness: pi
  agent: lf2-followups
  model: deepseek-flash
  thinking: high
  tokens: unknown
---

# Lane F2 hand-back

Branch `lane/lf2-followups`, cut from `emu/2026-10-28` (`55badbe`). Seven commits, one per task, plus this file. Two
builds were made in this worktree (`build/lf2`, and `build/lf2b` as the second emulator the timing acceptance needs).

## Per task

### 1. `pt-i44.78` — the residual panel-frame stall: `a6751bc`

`emulator/native-window.swift`. `writeTxLog()` took the shared `lock`, formatted 128 bytes with 128 `String(format:)`
calls and wrote the line to the file, all on the thread that reads the bridge socket, so any other lock holder or a
slow disk showed up as a gap between panel frames. The bridge reader thread now does only two things: take the
timestamp where the frame is read (before the lock and the queue hop; `started` and `config` are immutable) and copy
the raw bytes into a `[UInt8]` the queue owns. All 128 hex conversions — built from a static hex-digit table, not
`String(format:)` — and the file write happen on the dedicated serial queue, and the queue is drained and the handle
closed on quit.

- `cd emulator && xcrun swiftc -typecheck -parse-as-library $(python3 -c 'import native; print(" ".join(native.SWIFT_SOURCES))')`
  — no errors (the only output is the pre-existing macOS 27 deprecation warnings).
- Two 60 s `timing_probe.py run --build build/lf2 --seconds 60` runs beside a second emulator (`build/lf2b`,
  `--ephemeral --no-ui-input --no-audio-files`, so two VMs and `qemu_processes: 2` throughout):

  | Run | load (before → after) | stalls > 40 ms | worst gap | bridge gaps > 40 ms | hop | onset sigma |
  |---|---|---|---|---|---|---|
  | A | 14.1/9.4/9.0 → 14.0/10.3/9.3 | **0** | 24.5 ms | 0 | `none` | 0.011 ms |
  | B | 16.6/11.8/10.0 → 24.8/15.2/11.4 | **0** | 24.0 ms | 0 | `none` | 0.006 ms |

  Both runs: `targets_met` all true, no dropped frames, 482/484 onsets at 120.0 BPM. D1 measured 3 stalls over 40 ms,
  worst 46.9 ms, hop `bridge-to-app` on this branch. Reports are in `/tmp/lf2-probe-{1,2}.json` (and the sessions
  under `build/lf2/timing-probe/`).

### 2. `pt-i44.77` — HW-35, the OVER lamp: `95e1bd4` — **parked, with the cause measured**

New probe `emulator/probes/over_led.py` (+ `tests/test_sp16_over_led.py`) sweeps the track volume, strikes a pad
loaded with a 0 dBFS tone at velocity 127 and records, over the same window, the master stream's peak from the sink
WAV and every TX frame's mono lamp set; the -30 dBFS fixture is the control. Measured on build `lf2` (firmware 1.60):

| Pass | master peak | near-full-scale samples | mono 34 (`over_led`) |
|---|---|---|---|
| volume 127, first pass after the knob burst | 0 | 0 | dark |
| volume 127 | **32767 (0.0 dBFS)** | 13280 | **dark** |
| volume 102 | 17352 (-5.52 dBFS) | 0 | dark |
| -30 dBFS control | 549 (-35.5 dBFS) | 0 | dark |

Lamp role, check and level path, decided: the role is right (mono 34 = `LED_CLIP`, service manual E15, protocol
reference, and the fixture's own page-1 LED ALL ON lights it), the check's reading is right, and the level path does
put 0.0 dBFS on both master channels — but the same 0 dBFS sample played at track volume 102 measured **-0.65 dBFS on
2026-10-12** (E14) and measures **-5.52 dBFS** now. The level the firmware's detector sees is about 5 dB lower than
when the lamp was shown to follow it, which is exactly what keeps a ~-0.9 dBFS threshold out of reach. **Which stage
lost those 5 dB is not settled** and needs the app's own level readout sampled at runtime (the class of probe the
filter-send work needs too), so the fix belongs to a task of its own rather than a guess here.

HW-35 therefore still fails, and that verdict is a real finding rather than a test bug: I did not weaken the check.
Its failure detail now carries the peak in dBFS so the next run says how loud the output got, and the probe splits the
three candidates.

Checks: `test_sp16_over_led` 5/5, `test_sp16_hwsuite` 90/90, three live probe runs (`build/lf2/over-led/20261028-123325/over-led.json`).

### 3. `pt-i44.81` — USB shutdown sentinel, EINPROGRESS/EAGAIN connect, DIN dest contract: `25dc15d`

- `emulator/midi/usb.py`: the bounded input queue dropped the shutdown sentinel when full, so `join()` expired with
  the input thread blocked in `get()`. `signal_stop()` discards the oldest queued message to make room; the link is
  closing, so nothing can race it.
- `emulator/hardware_stub.c`: `connect_wait()` waits for an in-flight `EINPROGRESS` connect (POLLOUT + `SO_ERROR`) and
  retries `EAGAIN` (full listen backlog) instead of handing the app a half-connected socket, bounded at 1 s.
- `emulator/tests/sp16_seq_contract.c`: the proxy write is now checked for its address (dest 128:0) and the received
  event for the client/input port the proxy registered; a new case fills a listener's backlog and proves the guest's
  open waits and its **first write** lands.
- `emulator/tests/test_sp16_usb_midi.py`: three: none→sentinel, full→oldest dropped, and the input thread seeing it.

Checks: `test_sp16_usb_midi` 4/4; `bash emulator/arm-contracts.sh` 16/16; the new backlog case **fails on the old
connect** (`sp16_seq_contract.c:77`, the child's open returns -1 and it exits 3) and passes with the fix, 5/5 runs.

### 4. `pt-i44.80` — variable-length guard, shared-port fan-out, stale proxy sockets, USB marker, `--midi` help: `7c2db3a`

- `emulator/midi/seq-bridge.c`: routing is `output_routes()`, a bitmask, so one port subscribed on DIN and USB goes out
  on both (it went to DIN only with the old if/else); the guard is `proxy_packet_is_fixed_event()`, which names the
  layout it depends on — 'E' then a 28-byte event, so flags is `packet[2]`; `unlink_stale_socket()` drops a leftover
  endpoint, refuses a live one (`EADDRINUSE`) and refuses a path that is not a socket, for both listeners.
- `emulator/tests/sp16_din_queue_contract.c`: pins the guard's accepted/rejected packets, the shared-port fan-out, and
  stale/live/not-a-socket.
- `emulator/guest-init.sh`: the USB MIDI mode marker is no longer created at boot; only the app's `usb_gadget.sh`
  request creates it, so the shim's sequencer inventory is what the firmware asked for.
- `emulator/native.py`, `emulator/sp16emu.py`: `--midi` help names both bridges.

**Note on the guard:** the code already read the flags byte correctly; nothing pinned it, so the review's suspicion is
now impossible rather than fixed. Checks: ARM contracts 16/16; the shared-port assert **fails on the old if/else**
(`sp16_din_queue_contract.c:52`); `test_sp16_cli` + `test_sp16_native` 148/148.

**Not verified live:** the USB MIDI route after the marker change. The lane's acceptance is contracts and unit tests,
and no live firmware run was in budget; the seq contract does prove the gating (g_midi appears only with the marker).

### 5. `pt-i44.73` — two stale comments, the nonblocking rawmidi wait, the ARP contract: `27d94a2`

- `emulator/hardware_stub.c`: `rawmidi_write_byte()` slept the 320 us slot before a nonblocking write; a nonblocking
  write now tries the port first, returns `EAGAIN` at once and charges the slot only for a byte that went out. The
  fake-eth0 comment now says link data is faked for the carrier query (`SP16_NET_GLINK` → `ETHTOOL_GLINK`) and
  `EOPNOTSUPP` otherwise.
- `emulator/tests/sp16_network_contract.c`: the arp case reads `SO_DOMAIN`, so pass-through (`AF_PACKET`) and the
  isolated `AF_UNIX` stand-in are told apart — the old check passed for the stand-in.
- `emulator/hwsuite.py`: the page-8 comment says where the status is drawn.

Checks: ARM contracts 16/16; the new rawmidi case **fails on the old code** (`sp16_din_contract.c:61`, child timing
assertion: 20 rejected bytes must not cost 20 wire slots) and passes now; `test_sp16_hwsuite`, `test_sp16_panel`,
`test_sp16_net` 185/185.

### 6. `pt-i44.79` — None vs silence, mixed stall windows, two wordings: `8966d4e`

- `emulator/probes/filter_send.py`: figures a metrics reply does not carry stay `None` (a measured `0.0` is silence);
  `sample()` takes the worst reported value without letting a missing reading erase a real one; the verdict says the
  sink never reported the stream instead of calling it silent.
- `emulator/timing_probe.py`: a window with both app stalls and bridge gaps is `mixed`, not wholly `guest-to-bridge`
  (which a stall the bridge never saw contradicts).
- `emulator/probes/ak4458_transport.py`: 0.90 and 0.005 are the two scores; what they support is a 0.5 threshold
  between them, not "nearly two hundred times" of separation.

Checks: `test_sp16_filter_send` + `test_sp16_timing_probe` 31/31 (four new). The `handleMessage` → `handle(_:)` wording
lives in `docs/changes/ld-measure.md`, another lane's report, so it is a cross-lane request (below), not an edit here.

### 7. `pt-i44.74` — per-layer boot-limit paths, HW-54 in the help: `18ff94e`

- `docs/profiles.md`: `update.max_boot_read_bytes` and `update.enforce_boot_read` are per layer, inside
  `update.tar_layers[]` (`products/sp16.json`, `prepare.py`).
- `emulator/sp16emu.py`: `--testmode-build` now reads `(HW-48..HW-51, HW-53, HW-54)` — the ids `TESTMODE_IDS` really
  holds, not a span that skipped HW-54.
- `emulator/tests/test_sp16_cli.py`: one new check that the hwsuite help names every `TESTMODE_ID` (explicitly or
  through a range) and no id the suite does not have.

Checks: `test_sp16_cli` 92/92; `./sp16 help hwsuite` shows the new list; quick set 445/445.

## Side effects

- Builds made in this worktree: `build/lf2` (research, `--audio file`, rebuilt once after the guest/shim changes) and
  `build/lf2b` (the same, made only to be the second emulator the timing acceptance requires).
- Processes started and stopped by exact PID: `build/lf2b`'s app (48054, stopped), one earlier `build/lf2` app (47103,
  stopped), the ephemeral sessions `timing_probe.py` and `over_led.py` started (each stopped by its own PID through
  `hwsuite.stop`). No emulator is running now.
- Docker: `emulator/arm-contracts.sh` (three full runs, 16/16 each) plus five single-contract runs for the negative
  checks; every image was removed afterwards (the script's own trap, `docker image rm` for mine).
- No firmware, key, image, session capture or device identity is in any commit: only `emulator/` sources, tests,
  probes, `docs/profiles.md` and this file.

## Parked

- **`pt-i44.77` (HW-35)**: parked with the cause measured, not fixed. The level the firmware's detector sees is ~5 dB
  lower than on 2026-10-12 (E14: -0.65 dBFS at track volume 102; now -5.52 dBFS), so a -0.9 dBFS threshold is out of
  reach even at 0.0 dBFS digital. Next step: sample the app's own level readout at runtime (its meter is not in the
  metrics the shim publishes), then find which stage lost the 5 dB. `emulator/probes/over_led.py` is the instrument.

## Cross-lane requests

1. **`docs/changes/ld-measure.md`** (lane `ld-measure`, not mine to edit): the residual-stall section names
   `handleMessage`; the symbol is `handle(_:)` in `emulator/native-window.swift`. Its other point — format the 128
   bytes with a table lookup rather than 128 `String(format:)` calls — was right and is now implemented on the log's
   own queue.
2. **`docs/python-cli-and-setup.md:38`** still describes `./sp16 run --midi` as "CoreMIDI DIN bridge enabled" only; the
   command now starts the USB MIDI byte-stream bridge as well. `docstring`/help text is updated in `native.py` and
   `sp16emu.py`; the doc line needs an owner's edit because existing `docs/*.md` are outside this lane.

## Open questions for the owner

- Is HW-35 meant to gate the profile (it is tier C, so a full run cannot mark a profile verified while it fails)? If
  the ~5 dB level difference is a real emulator regression, HW-35 is doing its job and should stay red until it is
  found; if the OVER lamp is not part of the conformance target on this firmware, the check needs a different grade.
- The USB MIDI route after `guest-init.sh` stopped creating the mode marker: the contract proves the gating, but a
  live run of the firmware switching the gadget to MIDI mode (and the app re-enumerating afterwards) was not in this
  lane's budget. Worth one run before the branch is merged.
