---
title: "Lane F: host features and follow-ups"
description: "LCD touch delivered as RX frame words 0x2c/0x2e, the page-8 hwsuite check, and six review follow-ups on host-side files."
type: report
status: active
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-lf-features, touch]
author:
  harness: pi
  agent: lf-features
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane F: host features and follow-ups

Lane F owns the panel/bridge/protocol host path, the app Swift files, the harness and the modding guide. Seven tasks,
one commit each, in the order given. No live firmware runs were made (no firmware is imported in this worktree, so the
two live acceptance checks are left for the dispatcher's review boot), and no builds or processes were started.

| # | Task | Commit | Files |
|---|---|---|---|
| 1 | `pt-i44.59` LCD touch as RX frame words | `d5b60f5` | `panel.py`, `panel_map.py`, `guest-bridge.py`, `tests/test_sp16_panel.py` |
| 2 | `pt-wct33.7` hwsuite check for page 8 | `5ccea85` | `hwsuite.py`, `tests/test_sp16_hwsuite.py` |
| 3 | `pt-i44.55` `decode_rx` returns None for an out-of-range pot word | `f003a2e` | `panel.py`, `tests/test_sp16_protocol_roundtrip.py` |
| 4 | `pt-i44.63` `ctl fault` argument validation and blink timing | `3b2f5e4` | `sessionctl.py`, `panel-view.swift`, `tests/test_sp16_sessionctl.py` |
| 5 | `pt-i44.65` USB hot-swap recovery, overlay tests, 9 s wording | `4d5b4c2` | `native-window.swift`, `panel-view.swift`, `tests/test_sp16_native.py`, `tests/test_sp16_panel.py` |
| 6 | `pt-i44.64` `test_sp16_prepare` build/ permission independence | `c304847` | `tests/test_sp16_prepare.py` |
| 7 | `pt-i44.54` modding guide and `apl_hook_line` agree on tabs | `fca5afe` | `docs/modding.md`, `guest-firmware.py` |

## Per-task hand-back

**1. `pt-i44.59` — `d5b60f5`.** Touch no longer reaches the guest through X11. A distinct `lcd_touch` path on the
panel model converts 800x480 screen coordinates with the loaded calibration (the inverse of the firmware's mapping,
defaults from `2026-10-28-sp16-firmware-gates/03-lcd-touch.md`) and writes big-endian X at +0x2c and Y at +0x2e. The
model keeps the firmware's contact debounce: an idle->point jump is held for at least five frames, a drag moves at most
32 raw units per frame, and a release writes (0, 0), outside the contact rectangle under every calibration.
`lcd_touch_raw` stays for research. The ribbon strip is unchanged.
Check: `python3 -m unittest emulator.tests.test_sp16_panel` — 81 tests OK; quick set — 439 OK. **Not run:** the live
check (one down/move/up reaches `UITouchPanel`; a three-frame pulse is rejected) and the synthetic non-default
calibration boot, because they need a firmware import and a boot.

**2. `pt-wct33.7` — `5ccea85`.** Added HW-54 in the extra tier (`--testmode-build`), alongside HW-48..HW-53: step to
page 8, tap the MIDI CHECK touch button at (115, 112) and poll the OCR until "MIDI CHECK : OK". The table, tier lists
and the `len(CHECKS)` assertion moved from 53 to 54.
Check: `python3 -m unittest emulator.tests.test_sp16_hwsuite` — 85 tests OK; quick set — 443 OK. **Not run:** the live
page-8 run, which needs a testmode build and firmware (and the DIN MIDI loopback the stub already serves).

**3. `pt-i44.55` — `f003a2e`.** `decode_rx` now returns None for a full-length frame with a good header and CRC that
carries a >10-bit pot word, instead of letting `analog_level` raise ValueError; `rx_state` tolerates that None so the
bridge's stats thread cannot crash on a research raw pin.
Check: `python3 -m unittest emulator.tests.test_sp16_protocol_roundtrip emulator.tests.test_sp16_panel` — 106 OK.

**4. `pt-i44.63` — `3b2f5e4`.** `sessionctl fault` refuses anything but `on|off` (a mistyped argument no longer
silently forces the fault). In the panel view, `startFaultClock` re-checks the fault state and is re-armed after the
layout loads, so a fault set before the layout load no longer leaves WAKE UP static.
Check: `python3 -m unittest emulator.tests.test_sp16_sessionctl` — 26 OK; Swift typecheck exit 0; quick set — 469 OK.

**5. `pt-i44.65` — `4d5b4c2`.** A failed QMP hot-swap now re-attaches the previous stick: `hotAttachUSB` tracks
`currentUSBImagePath`, and `QMP.restoreUSB` tolerates the part-removed state (a missing device or node is not an
error) and adds the old image back; if that too fails the error says the state is retryable. Added the missing
committed tests: a compiled panel harness exercises every fault/standby order (the two overlays stay independent), and
source checks pin the eject guard and the restore path. The 9 s teardown bound's comment now admits it can fire
mid-sync on a very slow host, not only on a hang.
Check: `python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_native` — 138 OK; Swift typecheck
exit 0. Also fixed the pre-existing stale assertion in `test_eject_is_a_research_command` (`op="usb"` -> `"op": "usb"`
after lane B moved `session.usb` to a dict), which was failing on the base branch.

**6. `pt-i44.64` — `c304847`.** `test_this_checkouts_own_workspace_is_accepted` now creates `build/` and chmods it
owner-only itself, so it no longer depends on the mode another test left behind under umask 022.
Check: `python3 -m unittest emulator.tests.test_sp16_prepare` — 7 OK.

**7. `pt-i44.54` — `fca5afe`.** The guide, `apl_hook_line`'s docstring and its refusal message now all say a tab is
tolerated, matching the code and the existing `test_crlf_and_tabs_are_tolerated` test.
Check: `python3 -m unittest emulator.tests.test_sp16_fwpack` — 14 OK.

Final combined check (quick set plus the modules of the files changed): `python3 -m unittest
emulator.tests.test_sp16_panel ... emulator.tests.test_sp16_sessionctl emulator.tests.test_sp16_native` — 526 OK; Swift
typecheck of the panel sources exit 0.

## Side effects

None. No builds were made, no emulator or QEMU processes were started or stopped, and no firmware, keys, images or
session captures were touched. `git diff --check` was clean before every commit.

## Cross-lane requests

- `docs/hardware-suite.md` still lists HW-47..HW-53 as the extras and its verification rules; it was not mine to edit.
  It should gain HW-54 (the page-8 MIDI CHECK touch button) in its extra-tier and "not emulated" notes.

## Open questions for the owner

- The two live acceptance checks (task 1's down/move/up through `UITouchPanel` and task 2's page-8 "MIDI CHECK : OK")
  were not run: this worktree has no imported firmware. They need a boot in the review/dispatcher environment.
- `emulator/input.py`'s X11 `touch` function is now unused by the bridge (touch goes through the RX frame). It is kept
  because `navigation_key` still uses the module; remove the dead `touch` path only if nothing else needs it.
