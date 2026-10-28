---
title: "Lane l3b-harness: hardware suite and control additions"
description: What the l3b-harness lane changed in the SP-16 emulator (standby flag and check, factory test page checks, screenshot regression layer, pad calibration walkthrough), how to use each, and what did not run.
type: report
status: complete
created: 2026-10-27
tags: [sp16, emulator, hwsuite, harness, lane-report]
author:
  harness: claude-code
  agent: l3b-harness
  model: claude-sonnet-5-5
  thinking: max
  tokens: unknown
---

# Lane l3b-harness: hardware suite and control additions

Branch `lane/l3b-harness`. Four tasks, one commit each, in this order, and this note as the last commit. Everything below
was run on firmware 1.60 (profile `sp16-1.60`) in research builds made with `--audio file`: `./sp16 build --name l3b ...`
and `--name l3b-testmode ... --app-arg testmode`. Claims are graded O (observed here), S (static: code or manual),
I (inferred) or U (unknown).

## Summary

| Task | Commit | Files | Live check (result, n of N) | Unit tests on the commit as committed |
|---|---|---|---|---|
| pt-wct33.1 standby flag, HW-47 | `31cdc75` | `panel_map.py`, `sessionctl.py`, `hwsuite.py`, their tests | `./sp16 hwsuite --build l3b --profile 1.60 --only HW-47`: PASS, 12 of 12 | quick set, `test_sp16_sessionctl` |
| pt-i44.11 factory test pages, HW-48..HW-51 | `ccf05cf` | `hwsuite.py`, `tests/test_sp16_hwsuite.py` | `hwsuite.py ... --only HW-48,HW-49,HW-50,HW-51`: 4 PASS (10/10, 8/8, 17/17, 9/9), twice; sabotaged map: HW-49 FAIL | quick set: 366 OK |
| pt-i44.16 screenshot layer, `--shots` | `566413e` | `shotdiff.py` (new), `hwsuite.py`, `tests/test_sp16_shotdiff.py` (new), `tests/test_sp16_hwsuite.py` | record then compare of HW-48..HW-52: 5 PASS, 12 of 12 captures match; colour change: HW-48 FAIL with 4 heat maps | quick set: 373 OK; `test_sp16_shotdiff`: 19 OK |
| pt-i44.19 pad calibration, HW-53 | `a25f99e` | `panel_map.py`, `panel.py`, `sessionctl.py`, `hwsuite.py`, `tests/test_sp16_panel.py`, `tests/test_sp16_sessionctl.py`, `tests/test_sp16_hwsuite.py` | `hwsuite.py ... --only HW-53`: PASS, 13 of 13 (12 graded), twice; staged file 242 -> 300, 48, 504; 47 and 505 rejected | quick set: 385 OK; `test_sp16_sessionctl`: 20 OK; `test_sp16_shotdiff`: 19 OK |

Commits two to four were each tested as an exported copy of their own staged files (`git checkout-index`), so a commit does
not lean on a later one. Pictures (not committed, under `build/l3b/`): references `shots-ref/*.png` (13 captures), the colour-change heat maps
`hwsuite/sab-cmp/shots/testmode-page1-{red,green,blue,white}-panel-diff.png` and the page 4 ones
`testmode-page4-{lcd,panel}-diff.png`, and the results and reports of every run under `hwsuite/<run>/`
(`rec-a`, `cmp-a`, `h53-rec`, `sab-cmp`, `final-cmp`).

## 1. Standby flag, `ctl power` / `ctl standby`, HW-47 (pt-wct33.1)

- `panel_map.decode_tx` returns `"standby"`: TX byte `0x67` bit 0. It is also set in the app's start-up frames, so read it
  only after the home screen has settled (the check does).
- `sessionctl.py`: `power [seconds]` presses and releases the POWER/STANDBY key (host control `power`, RX 5.4) and prints
  the flag; `standby yes` presses it and waits for the flag to latch. Both warn what follows (the stick is unmounted
  and saves stop until the app is restarted: the emulator has no M1 to wake it); `standby` refuses without `yes`.
  Reachable today as `python3 emulator/sessionctl.py --runtime <runtime.json> power`; see Cross-lane requests for
  `./sp16 ctl`.
- `hwsuite.py`: a new tier `X` ("extras", `--tier extra`, or `--only` naming a check). Extras are never part of a full run
  and never decide a profile's verification. HW-47 starts its own throwaway session, presses the key, then checks:
  the flag latches, the UI stays alive (TX frames keep coming, the socket answers, the LCD can still be snapshotted),
  the stick is unmounted with its LED dark, a project save does not complete; it then stops the app cleanly,
  restarts it (the wake-up path of an emulator without M1) and checks that the flag is clear, the stick mounted and a
  save completes.
- Check: `./sp16 hwsuite --build l3b --profile 1.60 --only HW-47` -> HW-47 PASS, 12 of 12 sub-results (about 2 min) (O).
  Not done here: naming the handler in Ghidra (private repo; the HW-09 run and the research findings already carry it).

## 2. Factory test pages as graded checks, HW-48..HW-51 (pt-i44.11)

Pages 1, 2, 4, 5 and 6 of the factory test (service manual QRT1001 pp. 40-43; the pass lines are in our own words), run in
a build made with `--app-arg testmode`:

| Check | Page | What passes |
|---|---|---|
| HW-48 | 1, 2 | page 1: LCD white; TRACK, MUTE, SLICE, SCALE turn all 32 pad and step lamps red, green, blue, white; every single-colour lamp on. Page 2: LCD black, every lamp off |
| HW-49 | 4 | each of the 50 controls (16 pads, 16 steps, 16 other buttons with a lamp = 48; MODE and SHIFT) moves `RESULT(n/50)` up by one; the selector push does not; `LED-BUTTON(48/48)` and `RESULT(50/50)` both show OK |
| HW-50 | 5 | MASTER, DSF DRIVE, DSF HIGHPASS (0-1023) and Y-PAD (the strip, 0-127) read the bottom, the top and the middle of their gauges, and OK shows once both limits were reached |
| HW-51 | 6 | the six encoders count +3, then -2 (the page prints unsigned 16 bit: 65534); the push encoder counts the selector's detents and its status is ON while pushed, OFF after |

The page is read from pixels where OCR is weak (gauge fill, the red OK) and with OCR elsewhere. Page changes are driven
one key at a time (HOME next, BACK previous, 13 pages 0..12, wrapping; page 12, the factory reset, is never visited) and
every value is polled until it is the expected one or a time limit passes, because the app takes seconds to redraw a page on
a loaded host. Without `--testmode-build` the checks are N/A with a `precondition:` reason. The testmode build's firmware
must be the profile's: its library manifest has to carry the profile's application SHA-256, checked before any session
starts (the run is refused with exit 3, as for the main build); the session must also report the id that was checked.
`--testmode-skip bar4` leaves a control unpressed (a negative control that needs no rebuild; unit-tested only, see below);
a name that is not one of the 50 controls is refused, with the closest control suggested.

Run (the held `./sp16 hwsuite` does not pass `--testmode-build` yet; see Cross-lane requests, so call the runner):

```sh
./sp16 build --name l3b-testmode --research --audio file --app-arg testmode --library build/sp16/library --reuse-qemu-from build/qemu-source
python3 emulator/hwsuite.py --profile emulator/profiles/sp16-1.60.json --build-dir build/l3b \
    --testmode-build build/l3b-testmode --only HW-48,HW-49,HW-50,HW-51
```

Results (O), an otherwise idle host, two runs of the same build:

| Check | Verdict | Graded sub-results | Time (run 1 / run 2) |
|---|---|---|---|
| HW-48 pages 1, 2 | PASS | 10 of 10 | 21 s / 21 s |
| HW-49 page 4 | PASS | 8 of 8 | 104 s / 109 s |
| HW-50 page 5 | PASS | 17 of 17 | 27 s / 28 s |
| HW-51 page 6 | PASS | 9 of 9 | 38 s / 39 s |

Acceptance, a sabotaged control map must fail page 4 (O): a build made on purpose from a `panel_map.py` without BAR 4's
button bit (never committed; the file was restored and the build redone) makes HW-49 FAIL: `RESULT` stops at 49 of 50 and
`LED-BUTTON` at 47 of 48, neither shows OK. One thing to know when reading that report: its per-control line named SHIFT,
not BAR 4. The saved captures show the page's status line still on BAR 3 for about 18 s after BAR 4 was pressed, then SHIFT
counting (O); I read that as BAR 4's press arriving as a SHIFT press, so that the real SHIFT press was a repeat and was not
counted (I). The totals and the verdict are right; the name is only as good as the page's own event order.

Not covered (U/S): pages 0 (version), 7 to 11 (touch calibration and the rest) and 12 (factory reset); the page 4 status
line (which button the page thinks was pressed) is not graded; `--testmode-skip` was not run live (the rebuilt map above is
the live negative control).

## 3. Screenshot regression layer (pt-i44.16)

- `emulator/shotdiff.py` (new): pixel comparison with a per-channel tolerance (default 8), masks as fractions of the capture
  (a `.mask` sidecar next to a reference, or the runner's own), a heat map (the capture dimmed to grey, changed pixels red,
  masked pixels tinted blue) and a reference store. It follows the design of the CDJ-3000 emulator's control crate
  (MIT/Apache-2.0: tolerance, masks, region, heat map), written again for Pillow and numpy.
- `hwsuite.py --shots record|compare [--shots-dir DIR] [--shots-tolerance N] [--shots-share F]`. Captures are LCD snapshots
  and whole-panel `panelshot`s; the panel's bottom status line (a frame counter and acknowledgement times) is always
  masked. References are the suite's own earlier captures, by default in `private/hwsuite-shots/<firmware version>/`
  (git-ignored: they show the firmware's screen). Nothing from a manufacturer's artwork is used.
  - `record` writes each capture to `<out>/shots/recorded/` and moves it into the reference folder only when its check
    PASSED, so a failing run never becomes the truth (an earlier design that recorded on the spot did exactly that).
  - `compare` checks each capture against its reference. A capture fails when more than floor(share x compared pixels)
    pixels changed, counted in whole pixels: with the default share of 0 a single changed pixel fails, however large the
    capture. Its sub-result gives the changed pixel count and box. A capture with no reference is BLOCKED ("run once with
    `--shots record`"); a different image size is a FAIL reported as a size mismatch (another display scale gives another
    size); masks that leave no pixel to compare are a FAIL ("mask leaves nothing to compare"), never a match.
  - Every run with `--shots` writes `shots-report.md`, a heat map `shots/<name>-diff.png` for each difference, and a `shots`
    block in the result JSON.
  - `--shots-tolerance N` is a whole number from 0 to 254 and `--shots-share F` is from 0 up to, but not including, 1: the
    parser and `shotdiff` refuse anything else, because a tolerance of 255 or more would call every pixel equal and a share of 1
    would let a completely changed capture pass.
- Captures today (13): HW-52 (a new extra, runs in the normal build): the settled home screen as LCD and as whole panel;
  HW-48: page 1 LCD, four panel colours, page 2 panel; HW-49: page 4 LCD and panel; HW-50: page 5 LCD; HW-51: page 6 LCD;
  HW-53: page 3 LCD.
- Idle regions: the settled home screen and the panel did not move by one pixel between two sessions of the same build (0 of
  384,000 LCD pixels and 0 of 3,911,680 panel pixels differ, tolerance 8), so no mask is needed today (O). A capture taken
  while a sample plays would need masks for the meters and the playhead; `HOME_LCD_MASKS` and `HOME_PANEL_MASKS` are the
  place for them and are empty.

Results (O):

- Record, then compare in a separate session (HW-48..HW-52): record 5 PASS and 12 references promoted; compare 5 PASS and
  12 of 12 captures match with 0 changed pixels.
- Acceptance, a deliberate colour change must fail with a heat map: in the same sabotaged build PLAY's lamp colour was
  changed to magenta. HW-48 FAIL: the four page 1 panel captures differ (9,744 of 3,911,680 pixels, 0.249%, a 146 x 81 box
  around the PLAY key) while every wire-level sub-result of HW-48 still passed, because that colour is applied in the host
  drawing path after the lamp frame is read; only the screenshot layer sees it. Heat maps:
  `build/l3b/hwsuite/sab-cmp/shots/testmode-page1-{red,green,blue,white}-panel-diff.png` (not committed). The same build
  also fails the page 4 LCD (809 pixels) and panel (11,073 pixels) captures.
- After the sabotage the build was redone clean and HW-48, HW-49 and HW-53 were compared once more: 3 PASS, and all 9 of their
  captures match with 0 changed pixels (the page 3 reference, recorded by an earlier HW-53 run, included).

## 4. Pad calibration walkthrough (pt-i44.19)

- New host controls `pressure1`..`pressure16` (ids 110..125, `panel_map.HOST_CONTROL_NAMES`): action `value` holds pad n's raw
  pressure word exactly (0..1023, the AD value the factory test shows) until it is set to 0; the contact word is set while it
  is above 0. The existing velocity press maps 1..127 linearly onto 172..874 (S: `PAD_CURVE`), so it can neither send 48 to
  171 nor an exact value such as 47 or 505; it is unchanged and overrides a held pressure.
- `sessionctl.py`: `pressure PAD RAW` (hold or, with 0, release) and `calibrate PAD [RAW]` (hold the pad at RAW, default 300,
  press WAKE UP (the USB STOP key) three times, 7 s apart, release; the pad is released even if a call fails). Test page 3 must
  be on the LCD (`--app-arg testmode` build). Reachable as `python3 emulator/sessionctl.py --runtime <runtime.json> calibrate 3 300`.
- `hwsuite.py`: HW-53 (extra, in the testmode session) walks pads 1 to 5 at 300, 48, 504, 47 and 505 and reads the staged
  `.sp16/data/padcalibration.xml` from the session's eMMC image after each walk.
- The synthetic default stays: every pad starts at 242 in a fresh session (O: 16 of 16). The guest copies the synthetic file
  only when it is missing (S: `guest-init.sh`, `cp -an`), so a walk in a persistent session survives restarts and an
  `--ephemeral` session starts from 242 again (I).

What page 3 does (O, in the testmode build):

- One press of WAKE UP is one pass: a 3 s spare period then a 1 s average, about 4.5 s with the redraw. The START button is
  pale while passes run and its label counts "START", "START 2", "START 3". After the third pass the three pass values turn
  orange, START is red again, and the pad's value in `padcalibration.xml` is rewritten about 1.5 s later.
- The value written is the held raw value (300 became 300), for that pad only. The range is the manual's 48 to 504 inclusive:
  48 and 504 are recorded; 47 and 505 are not (the three passes still run and START goes red, the file is unchanged).
- Each cell shows the pad number, the raw AD value, the stored value (blue) and two small counters whose meaning is not
  decoded (U).

Results (O): HW-53 PASS, 13 of 13 sub-results (12 graded and one screenshot), 165 s. The staged file changed
242 to 300 (pad 1), 242 to 48 (pad 2), 242 to 504 (pad 3); 47 (pad 4) and 505 (pad 5) left 242; no other pad moved in any walk.
A second run on the rebuilt build passed again (157 s, 13 of 13), and the page 3 LCD matched its reference.

Not covered (U/S): the RESET touch button (held 5 s, initialises the 16 values) is not driven; what the green and red
counters mean; the persistent-session behaviour above is inferred from the guest script, not run.

## Review round 1 (Codex), three fixes

| Finding | Commit | Task | What changed | Tests |
|---|---|---|---|---|
| 1, blocking: screenshot thresholds outside their range switch the comparison off | `5b680b4` | pt-i44.16 | `shotdiff.check_tolerance` (whole number, 0 to 254) and `check_share` (0 up to, not including, 1) in `changed_pixels` (so `compare` and `heat_map`) and `ShotStore`; the same checks in the `--shots-tolerance` and `--shots-share` parser; the `--shots` help corrected | quick set 387 OK; shotdiff 23 OK; 6 new tests fail on the old code |
| 2, blocking: the testmode build's firmware was not checked against the profile | `8e7d26f` | pt-i44.11 | `check_firmware` takes the build and returns the id it checked; `run()` refuses before any VM starts, `testmode_checks` checks again where it launches, passes that id to `launch` and stops a session that reports another id; an unreadable manifest is an `EnvError`; the result records the testmode build and firmware | quick set 393 OK; 6 new tests fail on the old code; live HW-48 PASS 10 of 10 |
| 3, non-blocking: `--testmode-skip` accepted unknown control names | `52a78bb` | pt-i44.11 | `check_testmode_skip` for the parser and the `Runner`; an unknown name is refused with the closest control suggested | quick set 397 OK, `test_sp16_sessionctl` 20 OK; 4 new tests fail on the old code |

Round 2 (the commit after `965c97e`), two more blocking findings in `shotdiff`: the changed share was rounded to six
places before it was compared with the allowed share, so one changed pixel in 3.9 million passed at share 0; and masks that
covered the whole capture left nothing compared, which counted as a match. The decision is now on whole pixels
(`allowed_changes`: floor(share x compared pixels), the share read as the decimal it was written as) and an empty comparison
is an `EmptyComparison` error that fails (status `empty`). The fraction is kept exact and rounded only where it is shown.
Per-control checking in HW-49 (the attribution weakness in section 2) is left to a follow-up task by the owner's decision.

Two choices to know about:

- The tolerance range is 0 to 254, not 0 to 255: two 8-bit channels differ by at most 255, so 255 switches the comparison off
  exactly like 256. Say if 255 should be accepted.
- A bad threshold or skip name is refused by the argument parser, which exits with 2 (argparse's code, as for `--tier`), not 3. The
  module's own usage errors (a missing `--profile`) exit 3.

## Cross-lane requests

`emulator/sp16emu.py` (held):
- `CTL_COMMANDS` and `help_ctl_lines()`: add `power [SECONDS]`, `standby yes`, `pressure PAD RAW` and `calibrate PAD [RAW]`
  (sessionctl already accepts them).
- `HWSUITE_OPTS`: `--tier` choices gain `extra`; add `--testmode-build NAME` (resolved like `--control`, a build made with
  `--app-arg testmode`), `--testmode-skip NAMES`, `--shots record|compare`, `--shots-dir DIR`, `--shots-tolerance N`,
  `--shots-share F`. `plan_hwsuite` passes the testmode build as `--testmode-build <path>`.

`docs/hardware-suite.md` (held): the check count changes from 46 to 53; add a paragraph on the extras tier (HW-47 standby;
HW-48 to HW-51 and HW-53 factory test pages with `--testmode-build`; HW-52 and `--shots`), and that a full run never
includes them.

`HARDWARE_EPOCH` / profile verification: `panel.py` and `panel_map.py` are in the hardware fingerprint and changed (decoded
`standby`, the `pressureN` host controls). The changes are additive: no verdict of HW-01..HW-46 can change. A maintainer
decides whether to bump the epoch.

## What did not run

- `./sp16 hwsuite --smoke` and any of HW-01..HW-46: not run. The brief accepts the new checks alone; the changes to
  `panel.py` are a new branch for names starting with `pressure` and a new decoded key, covered by unit tests.
- After the review fixes only HW-48 was run live (the launch path changed for all testmode checks; HW-49, HW-50, HW-51 and
  HW-53 were not repeated, their own code did not change).
- Unit tests only: `--testmode-skip`, the `calibrate` command and its timing (a fixed 7 s per pass; HW-53 drives the same
  presses), `shotdiff` masks and `--shots-tolerance`.

## Side effects

- Builds (all under the git-ignored `build/`): `l3b` (made before the pressure control existed, used unchanged for HW-47 and
  HW-52) and `l3b-testmode` (rebuilt in place twice for the sabotage demonstration; the source file was restored each time
  and the final build is the clean one).
- Processes: every emulator session the suite or my scripts started was stopped by its exact app PID. One testmode app, early
  in the lane under heavy host load, did not stop within 25 s and was ended with SIGKILL by its exact PID; no other process
  was touched. No firmware was downloaded and no hardware was flashed.
- Files outside the repo: scratch scripts and exploration captures in the session's scratchpad; shotdiff references and
  heat maps under `build/l3b/` (`shots-ref/`, `hwsuite/*/shots/`), not committed.

## Parked tasks

None.

## Open questions for the owner

- References are per firmware version and per display scale (a Retina panel capture is 2560x1600; another display gives
  another size, reported as a size mismatch, not a diff).
- HW-49 attribution by name is weak when the page reorders events (see section 2). A stronger check would compare the
  page's status line with the control just pressed; the owner made it a follow-up task.
- Should extras (HW-47..HW-53) get their own line in `docs/hardware-suite.md`'s verification rules, or stay out of them as now?
