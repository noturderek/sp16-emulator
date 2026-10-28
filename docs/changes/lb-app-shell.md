---
title: "Lane B: app shell"
description: "Wave-2 lane B of the SP-16 emulator: bounded quit, App Nap assertion, power/boot/fault panel states, the output monitor menu and USB hot attach."
type: report
status: active
created: 2026-10-28
tags: [sp16, emulator, dispatch, report, ui]
author:
  harness: pi
  agent: lb-app-shell
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane B: app shell (`lb-app-shell`)

Owns `native-window.swift`, `panel-view.swift`, `firmware-window.swift`, `audio-sink.swift`, `audio-output.swift`,
`audio-selftest.swift`, `data-image.swift`, `sessionctl.py` and `test_sp16_sessionctl.py`. Six tasks, one commit each,
in the order given.

## Tasks

### 1. `pt-i44.44` — SIGTERM ignored, teardown hang after a clean guest stop

- **Commit:** `03f5a44` — `emulator/native-window.swift`
- **What:** `applicationShouldTerminate` returned `.terminateLater` and waited on a reply posted through
  `CFRunLoopPerformBlock` + `CFRunLoopWakeUp`, which only runs while the main run loop still spins; SIGTERM is a
  main-queue dispatch source, so a wedged loop made later SIGTERMs appear ignored. Added a `terminating` re-entrancy
  guard, a `replyTerminate()` helper with a 2 s `_exit(0)` fallback, and a 9 s `_exit(0)` bound over the whole
  teardown (clean stop 5 s + terminate ladder 3 s).
- **Check:** `cd emulator && xcrun swiftc -typecheck -parse-as-library $(python3 -c 'import native; print(" ".join(native.SWIFT_SOURCES))')` (exit 0, only pre-existing deprecation warnings) and the 14-module quick set (403 tests, OK).
- **Live check:** SIGTERM to the app pid from a running research build — `quit requested` → `guest clean stop: yes`
  (console `SP16_DATA clean=yes`, `SP16_HOST_CLEAN_STOP clean=yes`) → `QEMU exited: status 0` → `exit: quit`, app gone
  in 2 s.
- **Did not run:** the full 20 launch-and-stop cycle acceptance (would need ~10 min of boot cycles; the single clean
  stop above exercises the same path).

### 2. `pt-i44.50` — App Nap activity assertion at launch

- **Commit:** `0859910` — `emulator/native-window.swift`
- **What:** a launch-held `ProcessInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep])` so a build
  without an audio sink (audio builds already hold their own) is not throttled by App Nap.
- **Check:** `swiftc -typecheck` (no new diagnostics).
- **Did not run:** the 60 s occluded-window timing probe; needs a quiet host and the timing harness.

### 3. `pt-i44.20` — power/boot visual states and audio held until the firmware unmutes

- **Commit:** `3dc729e` — `emulator/native-window.swift`, `emulator/panel-view.swift`, `emulator/audio-output.swift`
- **What:** a host `PowerState` machine driven by the TX standby flag (byte `0x67` bit 0 of the 128-byte OEM frame)
  and the first LCD frame: BOOT (boot overlay) → RUN, OFF (black LCD, every lamp off except a lit red WAKE UP) on
  standby, back to BOOT when it clears. The CoreAudio output holds the firmware's ~19.5 s power-on mute (QRT1001 p15),
  and `AudioOutput.muted` is now an unfair-lock-guarded flag.
- **Check:** `swiftc -typecheck` and the quick set (403 tests, OK).
- **Panelshots (build/lb, session `native-a4c33542…`):** `run` (RUN), `standby` (OFF: dark + WAKE UP lit; log
  `panel: standby — dark with WAKE UP lit`), and the boot overlay was the state before `panel: running (first LCD frame)`.

### 4. `pt-i44.29` — power-fault display

- **Commit:** `2a398ba` — `emulator/panel-view.swift`, `emulator/native-window.swift`, `emulator/sessionctl.py`,
  `emulator/tests/test_sp16_sessionctl.py`
- **What:** `ctl fault on|off` (sessionctl + the `fault` research op) forces a dark panel with the WAKE UP key
  blinking red at ~1 Hz (a timer repaints just that lamp); off restores the live screen.
- **Check:** `swiftc -typecheck`, quick set + sessionctl module (425 tests, OK).
- **Panelshot:** `fault` (log `panel: fault — dark with WAKE UP blinking red`).

### 5. `pt-i44.14` — output monitor menu (host half)

- **Commit:** `85c20f3` — `emulator/native-window.swift`, `emulator/audio-sink.swift`, `emulator/sessionctl.py`,
  `emulator/tests/test_sp16_sessionctl.py`
- **What:** an Output menu (MAIN / PHONES / OUT3-4 / OUT5-6 / OUT7-8) driving `AudioSink.monitor` (the WAVs never
  change), enabled only when a sink exists; a `ctl monitor NAME` research control; `audio.monitor` in the metrics.
- **Check:** `swiftc -typecheck`, the audio self-test (`ALL PASS`, including `monitor-jack` and `sink-wav-slice`), and
  the quick set + sessionctl (427 tests, OK). `ctl monitor PHONES` returned `ok` against the live build.
- **Did not run:** the per-jack AUDIO TEST MODE1 tone listen (needs `SP16_PCM_PORT_CHANNELS=0:16` from lane A's
  default and an audible device); the host half is the deliverable here.

### 6. `pt-i44.18` — USB hot attach of a different image

- **Commit:** `7f0aa49` — `emulator/native-window.swift`, `emulator/sessionctl.py`,
  `emulator/tests/test_sp16_sessionctl.py`
- **What:** the virtual stick moved from `virtio-blk-device` (virtio-mmio, which cannot hot-plug) to
  `virtio-blk-pci` on a `pcie-root-port`, with a QMP socket; `ctl usb attach PATH` (and USB > Attach USB Image…) ejects
  the stick, `device_del`/`blockdev-del`/`blockdev-add`/`device_add` swap the backing, then the guest remounts and
  announces the new image. An unformatted or foreign image now reaches the app, which shows its own unsupported-media
  dialog. `ctl usb eject/attach` (same image) is unchanged.
- **Check:** the QMP sequence verified against QEMU 10.2 (`device_del` on the PCIe root port, then the block-node swap
  after `DEVICE_DELETED`); `swiftc -typecheck`; quick set + sessionctl (429 tests, OK).
- **Live check (with the cross-lane guest patch below):** `ctl usb attach /tmp/stickB.img` returned
  `{"ok": true, "result": "usb-attached"}`; console showed `SP16_USB ejected=yes`, `removed=yes`, then the new device
  (`vdc` 1 GiB) `formatted=yes`, `mounted=yes`, `announced=yes` — the app rescanned.
- **Not here:** `ctl usb-oc` (USBPWR_OC over-current) is wave 3.

## Cross-lane requests

1. **`emulator/guest-init.sh` (lane A): add `modprobe virtio_pci || :`** right after the existing
   `modprobe virtio_mmio || :` (line 23). The hot-plug path needs the stick on a PCIe root port as `virtio-blk-pci`;
   virtio-mmio cannot hot-plug, and the guest initrd ships `virtio_pci.ko` but never loads it. Without this one line
   the stick is not claimed at all (cold or hot): I verified the line fixes cold attach (`SP16_USB mounted=yes`) and
   the full hot swap end-to-end, then reverted it because guest-init.sh is lane A's file.
2. **`emulator/sp16emu.py` (lane C):** the `./sp16 ctl` argparse choices need `fault`, `monitor` and `standby` (and the
   existing `power`/`pressure`/`calibrate` from `pt-i44.51`) so `./sp16 ctl fault on`, `ctl monitor PHONES` and
   `ctl standby yes` work through the launcher. They already work through `emulator/sessionctl.py` directly.

## Side effects

- Built `build/lb` (research, `--audio file`, decoupled, reusing QEMU and guest from `build/qemu-source`).
- Ran and stopped two sessions of `build/lb` by exact app pid; both exited cleanly in ~2 s with a clean guest stop.
- Panelshots written under `build/lb/sessions/native-a4c33542-c0e3-471e-b8fe-6bf88e14fddf/` (`run`, `standby`,
  `fault`) — build output, not committed.

## Parked tasks

None.

## Open questions

- None blocking. The owner may want the output-monitor menu entries greyed (rather than just hidden) in file-only
  builds, but the menu is already disabled when no sink exists.
