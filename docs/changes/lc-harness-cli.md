---
title: "Lane C: harness, build and CLI"
description: "Wave-2 lane C of the SP-16 emulator run: ten small harness, build and CLI fixes, one commit each, unit-tested."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, harness, cli, build, lane-c]
author:
  harness: pi
  agent: lc-harness-cli
  model: deepseek-flash
  thinking: high
  tokens: unknown
---

# Lane C: harness, build and CLI

Branch `lane/lc-harness-cli`, cut from `emu/2026-10-28`. Ten tasks, all committed separately, in the order the brief
gave. No build was made and no emulator was started in this lane (so no session was stopped either); every task's check
is a unit test, and the three tasks whose acceptance asked for a live run are named under "Not run" below.

## 1. Tasks

| # | Task | Commit | Files | Check | Result |
|---|---|---|---|---|---|
| 1 | `pt-wct33.6` SHIFT lamp role on mono 4 | `96df8ca` | `panel_map.py`, `profiles/sp16-1.{50,60}.json`, `tests/test_sp16_protocol_roundtrip.py`, `tests/test_sp16_profiles.py`, `tests/test_sp16_panel.py` | `python3 -m unittest emulator.tests.test_sp16_protocol_roundtrip emulator.tests.test_sp16_profiles emulator.tests.test_sp16_panel` | 115 tests, OK |
| 2 | `pt-i44.46` RX fixtures through the production decoder | `a1555b7` | `panel.py`, `tests/test_sp16_protocol_roundtrip.py` | `python3 -m unittest emulator.tests.test_sp16_protocol_roundtrip emulator.tests.test_sp16_panel` | 95 tests, OK; mutated decoder fails 7 and 29 assertions |
| 3 | `pt-i44.48` HW-49 reads page 4's status line | `9f69172` | `hwsuite.py`, `tests/test_sp16_hwsuite.py` | `python3 -m unittest emulator.tests.test_sp16_hwsuite` | 76 tests, OK at that commit (81 now) |
| 4 | `pt-wct33.29` HW-34 re-selects the track | `611f18a` | `hwsuite.py`, `tests/test_sp16_hwsuite.py` | `python3 -m unittest emulator.tests.test_sp16_hwsuite` | 81 tests, OK |
| 5 | `pt-i44.43` fwdev waits for `console.txt` | `1afca83` | `fwdev.py`, `tests/test_sp16_fwdev.py` | `python3 -m unittest emulator.tests.test_sp16_fwdev` | 16 tests, OK at that commit (19 now) |
| 6 | `pt-i44.45` dev-overlay record off guest RAM | `0ddda9f` | `guest-firmware.py`, `tests/test_sp16_fwdev.py` | `python3 -m unittest emulator.tests.test_sp16_fwdev` | 19 tests, OK |
| 7 | `pt-i44.42` boot read-length limits | `e4e8f08` | `prepare.py`, `profiles.py`, `profiles/products/sp16.json`, `guest-firmware.py`, `tests/test_sp16_fwpack.py`, `tests/test_sp16_profiles.py` | `python3 -m unittest emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_profiles emulator.tests.test_sp16_firmware_import` | 64 tests, OK |
| 8 | `pt-wct33.38` worktree builds reuse the main library and QEMU | `ef356d0` | `prepare.py`, `tests/test_sp16_prepare.py` (new) | `python3 -m unittest emulator.tests.test_sp16_prepare emulator.tests.test_sp16_native` | 7 and 55 tests, OK |
| 9 | `pt-i44.51` wire midi, ctl power/standby/pressure/calibrate, hwsuite testmode/extra | `63f3447` | `sp16emu.py`, `tests/test_sp16_cli.py` | `python3 -m unittest emulator.tests.test_sp16_cli` | 91 tests, OK |
| 10 | `pt-i44.52` `audio.jacks` from the jack map | `b83244e` | `profiles/sp16-1.{50,60}.json`, `profiles.py`, `tests/test_sp16_profiles.py` | `python3 -m unittest emulator.tests.test_sp16_profiles` | 24 tests, OK |

The quick set from the lane brief, with `test_sp16_prepare` added (the new module), after every task:
`python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev
emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack
emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze
emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap
emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter emulator.tests.test_sp16_prepare` — 436 tests,
OK at the end. `./sp16 test` (the whole suite) was not run: the owner rule is never the full suite without a filter, and
these modules cover every file this lane touched.

### What each task actually changed

1. **SHIFT has no lamp.** The lamp table named a lamp the manual's LED matrix does not have. `shift` is gone from
   `LAMP_ROLES` (both profiles); mono channel 4 stays decoded and stays declared in `lamp_unassigned`, which is what
   HW-14 reads. The round-trip test now asserts the exact table, the profiles golden keeps the pre-M1 literals and
   records the one difference, and the mono brightness test uses HOLD (mono 10) instead of the dropped role.
2. **One RX decoder.** `panel.decode_rx()` is the production RX decoder (buttons by role, counters, every pad's
   pressure/contact, the mapped pots as words and as host levels), refusing a wrong length, header or CRC-16, and
   `PanelModel.rx_state()` reads through it. `panel.decode_strip()` holds E5. The literal-byte fixtures and the layer-3
   pad, counter, analog and strip round trips now go through those functions; mutating the decoder fails them.
3. **HW-49 per-control attribution.** Page 4 prints the control it counted in a status cell left of the LED-BUTTON row.
   HW-49 reads it for every press and requires it to name the pressed control, so two controls with swapped mappings
   (which still move RESULT) now fail. Names are matched as tokens, longest first (USB STOP is not STOP, STEP 16 is not
   STEP 1, OCR-joined `USBSTOP` still matches); a cell that reads as no control is N/A, never a wrong name.
4. **HW-34 on the right track.** `Runner.on_track()` reads the selected tile and re-selects the wanted track when it
   moved. HW-34 checks it before the tone hit and twice per VOLUME level; a lost selection stops the walk and fails a
   new sub-result naming the track the screen showed.
5. **fwdev waits for the console.** `console_since()` polls for `console.txt` (15 s default, clear error naming the
   path), treats a console that shrank below the offset as recreated, and refuses a negative offset or a non-positive
   wait. `latest_runtime()` skips a half-written `runtime.json` instead of raising.
6. **Overlay record survives a restart.** The record of written paths moved from `/run` to the eMMC image's own
   `.sp16` directory (mount from the profile), which outlives a VM restart and is outside the app's data directory; with
   no eMMC mounted it falls back to `/run`. An overlay file that would write the record itself is refused by name.
7. **Boot read lengths.** `max_boot_read_bytes` for all five tar layers (rootfs1 3,970,596, rootfs2 2,422,834, rootfs3
   3,268,503, rootfs4 1,871,918, pdj 4,561,658; the stock 1.60 images are exactly those lengths) and
   `enforce_boot_read` for the two the owner named. `prepare.check_boot_read()` refuses a longer layer with the layer,
   its size, the limit and how much to cut, at unpack (`unpack_layer`, `assemble`) and at repack
   (`guest-firmware.check_repacked_layer`).
8. **Worktree builds.** `prepare.private_path()` asks git which working tree holds the path and accepts a sibling
   worktree with the same common git dir, so `--library ../sp16-emulator/build/sp16/library` and `--reuse-qemu-from
   ../sp16-emulator/build/qemu-source` work; another repository, a non-ignored path and a symlink parent are still
   refused.
9. **CLI wiring.** `./sp16 midi ports|learn|preset|run` (each subcommand only its own options, PYTHONPATH set, the
   build dir default from `SP16EMU_BUILD_DIR`), `./sp16 ctl power|standby|pressure|calibrate`, `./sp16 hwsuite
   --testmode-build NAME` (default `build/NAME-testmode`), `--tier extra`, and the five hwsuite options wave 1 left
   unreachable (`--shots`, `--shots-dir`, `--shots-tolerance`, `--shots-share`, `--testmode-skip`), which also fixes
   `test_hwsuite_flags`, which was already failing on `emu/2026-10-28`.
10. **Output jacks.** `audio.jacks` in both profiles (MAIN/OUT1-2 12/14 O, OUT7-8 0/2 O, OUT3-4 8/10 I, OUT5-6 4/6 I,
    PHONES on the AK4452 stream 0/1 O), validated for structure and checked jack for jack against
    `emulator/audio-jack-map.json`; `profiles.audio_jacks()` returns them for the menu.

## 2. Not run, and why

- No build, no emulator session, no `./sp16 smoke`, no hardware suite: the lane brief says a run is only needed when a
  task says so, and every task's check is a unit test. Nothing was started, so nothing had to be stopped by PID.
- **`pt-i44.43` acceptance** asks for three consecutive `./sp16 mod run --build NAME` launches. Not repeated: it needs
  a built app plus imported firmware, which this lane did not make. The first-launch-only failure is covered by the new
  `console_since`/`latest_runtime` tests instead.
- **`pt-i44.48` and `pt-wct33.29` live behaviour** (page 4's status cell geometry and spellings, HW-34's re-selection on
  a real run) was checked against the wave-1 page-4 captures in `internal/firmware/.../testbench-runs/*/pass*-page4.png`
  and a rendered `BAR 3 : 3` cell, not against a running app: no testmode build exists in this lane.
- **`pt-i44.42` acceptance** mentions a stock 1.60 import unchanged and a repack grown by one byte. The stock sizes were
  confirmed against `internal/firmware/toraiz-sp16-recovery.yrLxON/update-payload/images/*.tar.gz` (exactly the recorded
  limits), and the one-byte case is a unit test; no import or repack was run.
- Swift typecheck, `arm-contracts.sh` and `make -n`: not applicable, no Swift, stub or Makefile change.

## 3. Side effects

- New file `emulator/tests/test_sp16_prepare.py` (owned module for `prepare.private_path`).
- Integration tests under `pt-wct33.38` create and remove temporary git worktrees of this repository in a temp folder;
  `git worktree list` shows only the three lane worktrees and the main checkout afterwards.
- Manual checks that touched the host: `./sp16 midi ports` read the CoreMIDI device list (no session opened),
  `./sp16 builds` and `./sp16 --dry-run` printed plans, and `private_path` was called on the main checkout's
  `build/sp16/library` from this worktree.

## 4. Parked tasks

None.

## 5. Cross-lane requests

For lane A (`emulator/native.py`, which this lane does not own):

- `shipped_profile()` copies only `profiles.RUNTIME_FIELDS` and drops `audio`, so a shipped profile carries no
  `audio.jacks` — the output monitor menu (pt-i44.14) needs them at run time. Please keep that one part of the audio
  section:

  ```python
  def shipped_profile(profile):
      copy = {key: json.loads(json.dumps(profile[key])) for key in profiles.RUNTIME_FIELDS if key in profile}
      copy["firmware"].pop("upd_sha256", None)
      # The output monitor menu reads the jacks; every other audio field is a hardware-suite measurement.
      audio = profile.get("audio") or {}
      copy["audio"] = {key: json.loads(json.dumps(audio[key]))
                       for key in ("alsa_streams", "sound_channels", "jacks") if key in audio}
      return profiles.validate(copy)
  ```

  Checked from this worktree: that copy validates, keeps the five jacks (the map is not in a bundle, so the cross-check
  is skipped) and adds about 1 KB to a shipped profile. `tests/test_sp16_profiles.py::
  test_the_jacks_survive_a_shipped_profile_that_drops_the_audio_section` records today's behaviour and will need
  updating with whichever shape is chosen.

- Lane A also owns `guest-init.sh`: if `dev_state_path()` should ever prefer a mount other than the profile's
  `emmc_mount`, it would be simpler to export `SP16_EMMC_MOUNT` to the `dev-apply` call than to read the profile there.
  Not needed today.

## 6. Open questions for the owner

- `pt-i44.42`: only `rootfs2.tar.gz` and `pdj.tar.gz` are enforced, as asked. The other three limits are recorded but not
  enforced; say if they should be.
- `pt-i44.48`: HW-49 now fails when the page names another control, and reports an unreadable status cell as N/A (it
  cannot tell OCR weakness from a firmware quirk). If a live run shows the cell unreadable for some controls, that
  sub-result is where it will show.
- `pt-i44.45`: the overlay record now lives in the eMMC image's `.sp16` directory, outside the app's data directory. Say
  if it should be refused there instead (option "never write overlay files into /root/data"), which is the other outcome
  the task allowed.
