---
title: Lane N integration hand-back
description: GLINK and ARP integration, LCD backlight, USB over-current, DIN and touch follow-ups, and live HW-54 and touch checks.
type: report
status: partial
created: 2026-10-28
tags: [sp16, emulator, integration, prodjlink, hardware]
author:
  harness: codex
  agent: ln-integration
  model: gpt-6-sol
  thinking: high
  tokens: unknown
---

# Lane N integration hand-back

The Pro DJ Link carrier and ARP compatibility patch works at the shim boundary, but the firmware did not emit a keep-alive on the multicast segment. The dependent fake-master and sync-bridge acceptances are parked. Backlight, USB over-current, DIN follow-ups, touch clamping, HW-54, and a live LCD touch interaction are complete at the checks below. No captured device identity is committed.

## Checks used

`Q` is the required quick set, run from the repository root:

```sh
python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter
```

`A` is `bash emulator/arm-contracts.sh` (13/13 ARM contracts passed). `S` is `cd emulator && xcrun swiftc -typecheck -parse-as-library $(python3 -c 'import native; print(" ".join(native.SWIFT_SOURCES))')` (exit 0; unrelated SDK deprecation warnings). `git diff --check` passed before each commit. The full `./sp16 test` and full hardware suite were not run: the common brief reserves them for the dispatcher.

## Per-task hand-back

1. **`pt-i44.57`, partial — `cfadade`.** Changed `emulator/hardware_stub.c`, `emulator/guest-init.sh`, `emulator/arm-contracts.sh`, and new `emulator/tests/sp16_network_contract.c`. The opt-in eth0 command-10 ioctl writes carrier 1 (or an explicitly requested 0); other requests and real address ioctls pass through, and network mode opens a real ARP packet socket. Checks: `Q` **444/444 OK**; `A` **13/13 PASS**; research `./sp16 build --name ln --research --audio file --library build/sp16/library --reuse-qemu-from build/qemu-source` **pass**; `./sp16 run --name ln --net mcast --ephemeral --no-ui-input --no-audio-files` reached `SP16_NATIVE_READY`. An instrumented strace run logged `network eth0 GLINK=1`, a real AF_PACKET bind, and UDP binds on 50001 and 50002. UDP 50000 and message `0x1d` were not directly observed; full activation is not claimed. No debugger carrier override was used.

2. **`pt-i44.38`, parked — no commit.** `python3 emulator/net/segment_watch.py --seconds 60 --pcap build/ln/prodjlink-60s.pcap > build/ln/prodjlink-60s-watch.txt` captured **0/1 required keep-alives**: 0 decoded lines and a 24-byte empty PCAP. The app stayed running during the capture; host load was below 40. The 54-byte packet layout and two-second interval could not be measured. NIC support from the preceding lanes was retained.

3. **`pt-i44.39`, parked — no commit.** `emulator/net/fake_master.py` and `emulator/timing_probe.py` were not run against the firmware because step 2 produced no keep-alive and UDP 50000 was absent. Acceptance is **0/1**; the 130.56 BPM, 5 ms phase, and 60-second WAV checks remain open. No timing result is inferred.

4. **`pt-i44.41`, parked — no commit.** The Link peer and CoreMIDI clock checks were not run because the Pro DJ Link source in step 3 was not established. Acceptance is **0/1**. No Ableton Link bridge was added on an unverified source path.

5. **`pt-i44.26`, complete — `81b8118`.** Changed `emulator/hardware_stub.c`, `emulator/guest-bridge.py`, `emulator/native-window.swift`, `emulator/panel-view.swift`, `emulator/arm-contracts.sh`, `emulator/tests/sp16_stub_contract.c`, and `emulator/tests/test_sp16_panel.py`. The stub records the `/dev/backlight` ioctl `0x40047000` raw 0–255 level, the bridge carries it in board stats, and the window scales LCD brightness. Checks: `Q` **444/444 OK**; `A` **13/13 PASS**; `S` **exit 0**; research rebuild **pass**. In firmware 1.60 Utility, LCD BRIGHTNESS 3 to 1 changed the raw level from 91 to 19 and visibly dimmed the panel: `build/ln/sessions/native-5f5aee87-dfde-4f20-afd5-a370080d94f3/brightness-level3.png` and `build/ln/sessions/native-5f5aee87-dfde-4f20-afd5-a370080d94f3/brightness-level1.png` (**1/1 live check**). These panelshots remain ignored under `build/`.

6. **`pt-i44.18` over-current remainder, complete — `a0dcc2d`.** Changed `emulator/hardware_stub.c`, `emulator/guest-init.sh`, `emulator/guest-bridge.py`, `emulator/protocol.py`, `emulator/native-window.swift`, `emulator/sessionctl.py`, `emulator/sp16emu.py`, `emulator/arm-contracts.sh`, `emulator/tests/sp16_board_contract.c`, and `emulator/tests/test_sp16_panel.py`. `./sp16 ctl --build ln usb-oc on|off` asserts/releases active-low GPIO4_IO14. Checks: `Q` **444/444 OK**; `python3 -m unittest emulator.tests.test_sp16_cli` **91/91 OK**; `A` **13/13 PASS**; `S` **exit 0**; `./sp16 help ctl` lists the command. A live `usb-oc on` produced the app warning “USB ERROR / REMOVE THE DEVICE” (**1/1 live check**), captured at `build/ln/sessions/native-b3b123f3-901a-4ba4-ad86-926a8d4f30b1/usb-oc-on.png`; `off` returned the GPIO high. Hot attach itself was delivered by the preceding lane and was not rechecked here.

7. **`pt-i44.67`, complete at the requested contracts — `1aa9016`.** Changed `emulator/hardware_stub.c`, `emulator/midi/din.py`, `emulator/native.py`, `emulator/sp16emu.py`, `emulator/tests/sp16_din_contract.c`, `emulator/tests/sp16_hwparams_contract.c`, and `emulator/tests/test_sp16_native.py`. THRU and OUT share the locked 320 µs wire path; input/output handles keep independent nonblocking state; AK4458 subdevices share the S16_LE restriction; `--midi` gates the host CoreMIDI helper; `PacedInput` reads the clock once per byte. Checks: `Q` **444/444 OK**; `python3 -m unittest emulator.tests.test_sp16_din_midi emulator.tests.test_sp16_native emulator.tests.test_sp16_cli` **153/153 OK**; `A` **13/13 PASS**; `./sp16 --dry-run run --name ln --midi --no-usb` forwarded `--midi`. No new live firmware DIN routing or clock acceptance was run; lane M recorded that remaining firmware path separately.

8. **`pt-i44.66`, complete — `de1f287`.** Changed `emulator/panel.py`, `emulator/hwsuite.py`, and `emulator/tests/test_sp16_panel.py`. Screen-converted raw touch words clamp to 0–1023, and the suite docstring says 54 checks. A synthetic non-default calibration mapped an edge beyond both bounds; the frame decoded with words `(1023, 0)`. Check: `Q` **445/445 OK**. No firmware boot was needed for the calibrated boundary test.

9. **`pt-wct33.7` and `pt-i44.59` live checks — `f6e6ffc` for the HW-54 OCR fix.** Changed `emulator/hwsuite.py` and `emulator/tests/test_sp16_hwsuite.py`. The first `./sp16 hwsuite --build ln --tier extra --testmode-build ln-testmode --only HW-54` failed **0/1** because full-page OCR missed the small status; the app did run its MIDI loopback. After cropping the status cell, the same command reported `[hwsuite] HW-54 PASS 21.55 s`, **1/1**. Report: `build/ln/hwsuite/20261028-084016-58118d/hwsuite-report.md`. `Q` passed **445/445** after updating the OCR test. In a separate testmode session, on **PAGE 8: DEVICE TEST**, LCD touch down `(105,112)`, move `(115,112)`, up `(115,112)` changed MIDI CHECK from `---` to `OK` (**1/1 live touch check**). Before/after snapshots: `build/ln-testmode/sessions/native-e5c7486d-822d-4b00-837d-b603050d9b5d/touch-page8-before.png` and `build/ln-testmode/sessions/native-e5c7486d-822d-4b00-837d-b603050d9b5d/touch-page8-after.png`. The suite did not run its other extra-tier checks.

## Side effects

- Built/rebuilt silent `--audio file` research builds `build/ln` and `build/ln-testmode`. The latter uses `--app-arg testmode`; both share the pre-existing imported library and QEMU source clone. Captures, traces, WAV settings, and snapshots stayed under ignored `build/` paths.
- Started only this worktree's emulator sessions for network, backlight, over-current, and testmode checks. Each manually started app was stopped by its exact PID; the hardware suite stopped the session it owned. No lane emulator or QEMU process remains.
- No firmware, update, key, device identity, data image, or session capture was committed. No network download, hardware flash, push, PR, merge, or rebase was performed.

## Parked tasks

`pt-i44.38`, `pt-i44.39`, and `pt-i44.41` remain dependent on the missing UDP 50000 keep-alive after GLINK was answered and ARP passed through. `pt-i44.57` has a committed compatibility patch but its message-0x1d and full listener acceptance remain unproven. The next Pro DJ Link investigation should identify why the app binds 50001/50002 but does not send or bind 50000 in this run, while keeping raw captures under `build/`.

## Cross-lane requests

- The dispatcher should update the existing user guides it owns for `./sp16 run --midi`, `./sp16 ctl usb-oc on|off`, firmware-driven LCD brightness, and HW-54's extra-tier check. This lane did not edit `README.md` or existing `docs/*.md`.
- The dispatcher should preserve `guest-init.sh`'s `apl_start` hook, `modprobe virtio_pci`, and DIN setup when folding this branch; all remain in place here.

## Open questions for the owner

- Pro DJ Link activation beyond carrier/ARP needs a source-backed explanation of the missing port-50000 service. The Link/MIDI bridge should wait for a verified tempo/phase source.
- The host DIN helper is now opt-in with `--midi`; the passive `sp16.din` virtio port remains in the QEMU build arguments. Decide whether the port itself should also be removed from sessions without MIDI, separately from the completed helper gate.
