---
title: Lane l3a-harness-easy
description: "Panel protocol round-trip tests and a layout overlay tool: what each checks, results, offsets and open questions."
type: report
status: complete
created: 2026-10-27
tags: [sp16, emulator, tests, panel, layout]
author:
  harness: claude-code
  agent: emu-l3a-harness-easy
  model: claude-haiku-5-5
  thinking: high
  tokens: unknown
---

# Lane l3a-harness-easy

Two test and tool additions for the panel layout. No existing file was changed.

## pt-i44.15: panel protocol round-trip tests

Commit `4ddad8d`. Files added: `emulator/tests/test_sp16_protocol_roundtrip.py`.

What it checks:

- Every control in `emulator/panel-layout.json` is sent to `PanelModel` as an event (press, release, turn, move or
  touch, by kind), then decoded back from the 128-byte RX frame with the wire tables (`BUTTON_BITS`, `COUNTERS`,
  `ANALOG_WORDS`, pad words, the touch-strip bytes). Analog pots that are host-only (LP CUTOFF, LP RESONANCE) are read
  back from the shared analog state. Lamp entries are checked to refuse input.
- Every lamp role in `LAMP_ROLES` is written into a TX frame using the E7 channel tables and read back with
  `panel_map.decode_lamps`: non-zero when lit, zero when dark.
- Negative case: a copy of `BUTTON_BITS` moves PLAY onto STOP's bit. A round trip built on that copy decodes back
  with the real table and must fail. The test asserts the failure.
- Layout names match the control and lamp tables.

Check: `python3 -m unittest emulator.tests.test_sp16_protocol_roundtrip -v`

Result: `Ran 4 tests ... OK`, that is 4 of 4 tests passing. Coverage: 83 of 83 layout controls (65 input controls
round-tripped, 18 lamps refused as input), and 67 of 67 lamp roles decoded lit and dark.

Grade: S (static tables) and O (the decoder is run on the tables). Nothing on the emulator or hardware.

Not run: the full suite (owner rule), and no emulator build or panelshot (this lane is tests only).

## pt-i44.27: layout overlay on the top-view render

Commit `774eb3f`. Files added: `emulator/layout_overlay.py`. Output in `build/` (git-ignored), not committed.

How it works:

- The layout canvas is the chassis. Its width (1773 units) is the chassis width, 436.5 mm, so one unit is
  436.5 / 1773 mm.
- The render is 3501 x 2078 px, so the scale is 8.021 px/mm. The layout is laid on it uniformly and anchored at the
  top left. The layout covers 248.9 mm of the render's 259.1 mm height, so the vertical fit is not exact. This is a
  check, not a fix.
- Offsets are from the chassis top-left corner, in mm, for each control centre.

Run: `SP16_TOP_VIEW=/path/to/render.png python3 emulator/layout_overlay.py` (or `--image PATH`). Needs Pillow:
`python3 -m pip install pillow`. The render path is never stored in the repo.

Overlay PNG: `build/layout-overlay.png` (inside the worktree, git-ignored). Visual check: pads, step keys, knobs, transport
and bar LEDs sit on their render features.

Offset table, top 10 largest (distance from the chassis top-left corner):

| id | name | x_mm | y_mm | dist_mm |
|---:|---|---:|---:|---:|
| 46 | bar4 | 411.14 | 217.39 | 465.08 |
| 87 | bar_led4 | 411.14 | 202.86 | 458.47 |
| 45 | bar3 | 395.63 | 217.39 | 451.42 |
| 86 | bar_led3 | 395.63 | 202.86 | 444.61 |
| 26 | pattern | 406.71 | 178.00 | 443.96 |
| 44 | bar2 | 379.88 | 217.39 | 437.68 |
| 85 | bar_led2 | 379.88 | 202.86 | 430.65 |
| 43 | bar1 | 364.37 | 217.39 | 424.29 |
| 55 | home | 417.54 | 58.10 | 421.57 |
| 84 | bar_led1 | 364.37 | 202.86 | 417.03 |

Full table: all 83 controls, largest first, printed by the script.

Grade: O for the render's chassis width and the overlay (measured from the image). I for the offsets: they assume the
layout canvas is the chassis.

## Side effects

- Builds made: none. No emulator started, no process started or stopped.
- Files written: `build/layout-overlay.png` (git-ignored). Scratch previews live in the session scratchpad only.
- The private render was read in place and never copied into the repo.

## Parked tasks

None.

## Cross-lane requests

1. Owner decision: Pillow must be installed in the emulator repo's Python. `README.md` already lists it for the
   hardware suite (line 26); the owner wants it stated as a requirement for the overlay tool too. Exact change for the
   file's owner, in `README.md` under the Python tools paragraph:
   "`pillow` is needed by `emulator/layout_overlay.py` (the panel overlay) as well as the hardware suite:
   `python3 -m pip install pillow`."
   This lane does not edit `README.md` (held). The script itself already reports the install command when Pillow is
   missing.

## Owner decisions (applied)

- The overlay's render path is no longer a default in the repo. It comes from `--image` or `SP16_TOP_VIEW` (commit
  `437c10b`), so no private path is stored in the repo.
- Pillow: the owner wants it installed in the emulator repo. The script stops with the install command when it is
  missing. The README line is the cross-lane request above.
- Centre offset: from the chassis top-left corner, as the script does now. No change.
