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

The module has three layers. Only layer 1 and layer 2 check the tables against the panel protocol reference; layer 3
alone would pass a wrong table, because encoder and decoder read the same entries.

1. Golden tables, hand-written from the reference (`internal/docs/reference/toraiz-sp16-panel-protocol.md`, sections
   "Panel RX frame", "Panel TX frame", "Touch strip" and evidence E2, E7, E8, E11-E15). The canonical tables must equal
   them: `BUTTON_BITS` (38 entries), `COUNTERS`, `ANALOG_WORDS`, pad and frame constants, `RGB_CHANNELS`, the mono
   channel bit placement as a set, the mono bits for USB LED and OVER LED, and `LAMP_ROLES` (66 of 67 roles).
2. Raw-frame fixtures in literal bytes, decoded to the expected control: RX buttons (PLAY, selector push, POWER, STEP
   8 and 9, USB STOP), big-endian counter and pad words, inverted analog words, the touch strip (no contact, both ends),
   the step 1 colour bytes, TX bytes `0x65` bit 0 and bit 1 (USB, OVER), and the factory PAGE 1 bytes lighting every mono
   role. The CRC check value for `123456789` is the standard CRC-16/X-25 catalogue value, not from the reference.
3. Self-consistency round trip: every control in `emulator/panel-layout.json` is sent to `PanelModel` as an event and
   decoded back, and every lamp role is written into a TX frame and read back by `panel_map.decode_lamps`. Plus one
   negative case: a copy of `BUTTON_BITS` moves PLAY onto STOP's bit and must fail the round trip.

Mutation check, run by hand: swapping PLAY and STOP in `BUTTON_BITS` fails `test_buttons_match_the_reference`; moving the
PLAY lamp to channel 6 fails `test_lamp_roles_match_the_reference`. The layer-3 round trip passes the moved lamp, which
is why layer 1 exists.

Check: `python3 -m unittest emulator.tests.test_sp16_protocol_roundtrip -v`

Result: `Ran 22 tests ... OK (expected failures=1)`. The expected failure is the `shift` lamp role (see Cross-lane
requests). Coverage: 83 of 83 layout controls round-tripped (65 input, 18 lamps refused as input); 66 of 67 lamp roles
checked against the reference, the 67th is the `shift` discrepancy; 67 of 67 lamp roles round-tripped.

Quick set (brief list plus this module): `Ran 380 tests ... OK (expected failures=1)`, 5.7 s.

Grade by entry (E = evidence in the reference):
- O/S: `BUTTON_BITS`, `COUNTERS`, `ANALOG_WORDS`, pad and frame constants, `RGB_CHANNELS` (E2, E3, E7, E11, E15).
- O: USB LED = `0x65` bit 0 (E13), OVER LED = `0x65` bit 1 (E14); the PAGE 1 bit set (E12).
- U, not asserted against the reference, only self-consistent (layer 3):
  - Mono bit placement for channels 0-24 and 26-33. The reference gives the channel names and the total bits, not
    the byte and bit of each channel.
  - Second-flag bytes `0x69`-`0x6b` and which channel uses which flag. The reference only says the flag means dim.
  - The host-only analog slots (LP CUTOFF, LP RESONANCE, the shared-state offsets). These are emulator state, not
    panel protocol fields.
  - Mono 23 (ACTIVE) and 24 (STBY) bit placement inside `0x65`: the reference gives the channel number, not the bit.
  - Pad velocity curve and contact value 512: host choices, not protocol.
- Contradicted by the reference: the `shift` lamp role (mono 4). E15 says mono 4 drives no physical LED.

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
2. Owner of `emulator/panel_map.py` (held by another lane): remove the `shift` lamp role. `LAMP_ROLES` maps `shift` to
   mono 4, but the reference (E15) says mono 4 has no physical LED. Exact change: delete the `"shift": ["mono", 4]`
   entry from `panel.lamp_roles` in `emulator/profiles/sp16-1.60.json` (the default profile that `LAMP_ROLES` reads),
   and check whether `emulator/profiles/sp16-1.50.json` has the same entry. Then remove the `@unittest.expectedFailure` on
   `test_shift_has_no_lamp_role` in `emulator/tests/test_sp16_protocol_roundtrip.py`. The module fails on purpose once
   that is fixed, until the decorator is removed.

## Owner decisions (applied)

- The overlay's render path is no longer a default in the repo. It comes from `--image` or `SP16_TOP_VIEW` (commit
  `437c10b`), so no private path is stored in the repo.
- Pillow: the owner wants it installed in the emulator repo. The script stops with the install command when it is
  missing. The README line is the cross-lane request above.
- Centre offset: from the chassis top-left corner, as the script does now. No change.
