---
title: "Lane L1: panel UI"
description: "What lane L1 changed in the SP-16 emulator panel and app windows, how to use it, measurements and cross-lane requests."
type: report
status: active
created: 2026-10-27
tags: [sp16, emulator, panel, lane-l1]
author:
  harness: claude-code
  agent: emu-l1-panel-ui
  model: claude-sonnet-5-5
  thinking: unknown
  tokens: unknown
---

# Lane L1: panel UI

Files owned: `emulator/panel-view.swift`, `emulator/panel-layout.json`, `emulator/native-window.swift`,
`emulator/firmware-window.swift`. Grades: O observed, S static, I inferred, U unknown.

## What changed and how to use it

**pt-i44.22 Lamp bloom.** Lit pads, steps, bars, buttons and LEDs throw an additive halo (plus-lighter blend, drawn
after all controls, LCD glass excluded). Off by default; View > Lamp Glow turns it on (not remembered between launches). Static, so an idle panel repaints nothing.

**pt-i44.24 Redraw cost.** `metrics.json` and the research `metrics` op carry `panel_redraw`: passes, total and worst
ms, mean dirty-area fraction. Research op `redraw-bench count=N` returns the mean ms of a whole-view repaint.
Measured on a host at load average ~160 (absolute times inflated): idle 0 passes in 30 s; playback 49 passes at 4.9 ms,
19 % of the view each; whole view 67.5 ms. The panel already repainted by dirty rect; the reductions are a two-entry
static-layer cache (panelshot's backing scale used to force a chassis rebuild on every switch) and a status line that
only changes its text when it differs.

**pt-i44.23 Rear I/O panel.** View > Rear I/O Panel (Cmd-R), drawn in code from the `rear` section of
`panel-layout.json`; research op `view name=rear on=true`. Jack order, left to right, from the private render
SP16-003: DC IN, power, LINK (RJ45), MIDI OUT/THRU and IN, USB-B, INPUT R and L (MONO) with LEVEL, OUT 7-8, 5-6, 3-4,
1-2/MASTER (8..1), lock slot, PHONES. No OEM pixels and no logo are used (S). Nothing behind the jacks works.

**pt-i44.28 Private skin overlay.** Put a transparent PNG with the panel's proportions (1773 x 1011 or a multiple)
at `private/skin/overlay.png` of the checkout (or point `SP16_SKIN_OVERLAY` at a file). View > Private Skin Overlay
turns it on (greyed out when no file exists; remembered). Default off; without a file the panel is unchanged. `private/`
is git-ignored and the build never reads it. Suggested README rule text (README.md is held): "The repository ships no
vendor logos or artwork. For a personal build you may draw your own from `private/skin/overlay.png`; never commit it."

**pt-i44.21 Power on with keys held.** Firmware > Power On With Keys Held. Normal restarts the firmware app (the guest's
`app-restart`). Test Mode (HOLD + TRACK + STEP 1) relaunches the emulator with the new research-only launch flag
`--app-arg testmode` (full boot, no rebuild) and lands on PAGE 0 : VERSION (O). Update Mode (MODE + HOLD) holds those
keys across an app restart; no update-mode screen was observed (U), so it is labelled experimental. Research op
`power-on mode=normal|test|update` skips the confirmation.

**pt-i44.25 Boot overlay and first-run wizard.** The LCD shows what the emulator is doing, a bar and elapsed seconds
until the firmware's first non-black frame (the firmware sends black frames while booting). The first-run import window
is a stepped wizard (checklist, progress bar, Return starts the firmware). It wraps the existing firmware tool; key
handling is unchanged and nothing about the key is logged beyond its kind and part count.

**pt-i44.26 LCD backlight (parked).** `PanelView.lcdBacklight` and research op `lcd-backlight percent=N` dim the LCD
(floor 20 %). The guest does not report a level, so Utility brightness does not dim the window yet.

## Cross-lane requests

1. `guest-init.sh` (guest-bridge/sessionctl lane): let the control command `app-restart` carry an argument,
   `app-restart <token>`, token checked against the profile's `runtime.app_args` and written to `/run/sp16-app.arg`;
   the supervisor loop reads it into `sp16_app_arg` (and sets the MIDI loopback env) before each start, `app-restarted`
   as the reply. With that, Test Mode needs no relaunch: replace `relaunch(appArg:)` in `NativeApplication.powerOn`
   by `requestControl(message: "app-restart testmode")`. `sessionctl.py`: `app-restart [TOKEN]`.
2. LCD backlight (shim and bridge lanes): `hardware_stub.c` accepts `/dev/backlight` ioctl `0x40047000` and discards
   its argument (the firmware passes a pointer to a 4-byte value, I). Record that value, send it to the host in the
   feedback message (or a new research-visible kind), and call `panel.lcdBacklight = level / full` from
   `handle(_:)` in `native-window.swift`. The scale (0..255 or 1..4) is U until traced; TX byte 0x6e is the pad and
   button brightness (panel_map E12), not the LCD.
3. `README.md` (held): the skin overlay rule text above.

## Verification

- `swiftc -typecheck` of the lane's SWIFT_SOURCES after each task; research build `l1panel` of firmware 1.60 boots to
  HOME (final build includes every task).
- Quick set: 358 tests OK; `test_sp16_native`: 55 tests OK. (`emulator.tests.test_sp16_panel` includes a harness that
  compiles `panel-view.swift` alone, so it must stay free of other sources.)
- Panelshots (copied to the run notes folder, `l1-panelshots/`): `bloom-lit-pads.png`, `rear-io-panel.png`,
  `skin-overlay-dummy.png` (dummy overlay under `private/skin/`, untracked), `test-mode-page0.png`, `boot-overlay.png`,
  `wizard-first-run.png`, `lcd-backlight-40pct.png`.
- Not run: a full firmware import through the wizard (no update or key used in this session); hwsuite, smoke, full suite
  (owner rule). The host was at load average 150-300 during the run, so timings are relative only. One relaunched app
  (Normal after Test Mode) vanished without a log line during the run; a retry was fine; cause unknown (I).
