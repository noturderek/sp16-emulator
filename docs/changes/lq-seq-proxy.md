---
title: Lane Q2 sequencer proxy hand-back
description: ARM sequencer proxy, DIN and virtual USB MIDI acceptance, and DIN bridge follow-ups
type: report
status: partial
created: 2026-10-28
tags: [sp16, emulator, midi, sequencer, handback]
author:
  harness: codex
  agent: lq-seq-proxy
  model: gpt-6-sol
  thinking: high
  tokens: unknown
---

# Lane Q2 sequencer proxy

This lane provides the firmware with an ARM ABI ALSA sequencer proxy. Firmware 1.60 registered its application client, enumerated `snddinmidi` and `g_midi`, created local ports, and subscribed both DIN and USB routes. The host USB route is a **virtual CoreMIDI endpoint**, not a physical USB gadget. The routing preferences task remains open.

## Task results

1. **`pt-i44.75` sequencer proxy — `0754d0f`.** Files: `emulator/arm-contracts.sh`, `emulator/guest-init.sh`, `emulator/hardware_stub.c`, `emulator/midi/seq-bridge.c`, `emulator/tests/sp16_seq_contract.c`. Firmware trace observed application input/output ports and successful DIN subscriptions through the proxy (O). Check: `bash emulator/arm-contracts.sh` **15/15 PASS**. Required quick set: **444/445**, with the unowned `/dev/snd/seq` inventory mismatch described below. No full test suite was run, per the common brief.
2. **`pt-i44.69` realtime output — no new lane commit.** Existing DIN ioctl path was checked live with sync source INTERNAL, MIDI OUT enabled, and PLAY/STOP. The CoreMIDI monitor received `FA`, 158 `F8`, `FC`; 120 BPM produced 48.05 clocks/s (O). The implementation was already present in the lane's base commit. No F01/F02 hardware tests were run.
3. **`pt-i44.17` DIN acceptance — `d38bb8c`.** Files: `emulator/hardware_stub.c`, `emulator/midi/seq-bridge.c`, `emulator/tests/sp16_seq_contract.c`. A virtual CoreMIDI note 0 addressed to the app's subscribed input port triggered a loaded sample: nonzero PCM frames rose from 353111 to 438575 (O). A file-audio run at 120 BPM emitted `FA`, 158 `F8`, `FC` at 48.05 clocks/s (O). Check: `bash emulator/arm-contracts.sh` **15/15 PASS**; required quick set **444/445**, known inventory mismatch. F01/F02 hardware tests were not run.
4. **`pt-i44.71` lifecycle and mode bytes — parked, no lane commit.** Read-only firmware disassembly at `0x147ec4`, `0x14689c`, and `0x2b3afc` confirmed gate bytes at `+0x94`, `+0x95`, `+0x96` and the source note's song-position asymmetry (S). Their setters and complete input-consumer semantics remain unknown (U); no values were invented. No executable check or hardware test was run.
5. **`pt-i44.35` USB MIDI — `254852c`, partial physical-device outcome.** Files: `emulator/guest-init.sh`, `emulator/hardware_stub.c`, `emulator/midi/din.py`, `emulator/midi/seq-bridge.c`, `emulator/midi/usb.py`, `emulator/native.py`, `emulator/tests/sp16_seq_contract.c`, `emulator/tests/test_sp16_native.py`, `emulator/tests/test_sp16_usb_midi.py`. Added a separate `g_midi` proxy client, guest `sp16.usb-midi` transport, and host virtual CoreMIDI endpoints. A USB note 0 raised nonzero PCM frames from 23865 to 196092. USB `FA`/`F8`/`FC` arrived at the subscribed app port; after a mass-storage/MIDI mode round trip all four ports re-registered and both DIN and USB input notes had one callback each. USB output clock after the round trip measured 48.03 Hz at 120 BPM (O). Check: `bash emulator/arm-contracts.sh` **15/15 PASS**; `python3 -m unittest emulator.tests.test_sp16_native emulator.tests.test_sp16_din_midi emulator.tests.test_sp16_usb_midi` **63/63 PASS**; required quick set **444/445**, known inventory mismatch. `./sp16 smoke --build lq-seq-proxy --no-video` **7/8 PASS**, with the pad-edge check observing 3 guest events for 8 edges. `./sp16 hwsuite --build lq-seq-proxy --smoke` timed out at 120 s after six checks passed (HW-01, HW-31, HW-05, HW-06, HW-07, HW-18); the remaining checks, including HW-24, were not run. A physical macOS USB MIDI device and confirmed firmware-generated note output were not demonstrated.
6. **`pt-i44.37` routing preferences — parked, no lane commit.** The task requires a live CoreMIDI port table, hot-plug notification, persistent route toggles, per-output clock offset, and lock/jitter readout. The lane has virtual DIN/USB endpoints but no preference UI or persisted routing model; its physical USB device dependency is also incomplete. No implementation or check was run. The dispatcher should keep this task open for a dedicated UI and bridge-config pass.
7. **`pt-i44.76` DIN bridge follow-ups — `37db511`.** Files: `emulator/guest-init.sh`, `emulator/midi/seq-bridge.c`, `emulator/tests/sp16_din_ioctl_contract.c`. Accepted control clients now use nonblocking sockets in the poll loop; the contract removes a stale socket before binding; startup reports `sequencer=started` only when both proxy sockets exist. Check: `bash emulator/arm-contracts.sh` **15/15 PASS**; `sh -n emulator/guest-init.sh` **PASS**; required quick set **444/445**, known inventory mismatch. The full emulator smoke and hardware suite were not rerun after this small follow-up.

## Side effects

- Built `build/lq-seq-proxy` with `./sp16 build --name lq-seq-proxy --research --audio file --library build/sp16/library --reuse-qemu-from build/qemu-source --rebuild`. Build and session artifacts remain ignored.
- Ran short-lived firmware sessions, the smoke test, and a bounded hardware suite. The suite timeout left emulator PID 22288, which was stopped by exact PID. A later process check found no running lane emulator or MIDI helpers.
- No firmware, update, key, data image, or capture was committed. No push or PR was made.

## Parked tasks

- `pt-i44.71`: gate-byte setter and input consumer meanings remain U; requires further firmware trace or disassembly.
- `pt-i44.37`: routing preferences and persistence need a dedicated implementation pass.
- `pt-i44.35`: physical USB device behavior and firmware-generated note output remain unverified; the committed route is virtual CoreMIDI.

## Cross-lane requests

- The owner of `emulator/guest-firmware.py` should add `/dev/snd/seq` to `SHIM_DEVICES`. The quick-set test `test_shim_inventory_matches_the_c_source` sees the shim interception in `hardware_stub.c` but not this inventory entry; this is the sole failure in **445** tests. This lane does not own that file.

## Open questions for the owner

- Is a virtual CoreMIDI endpoint acceptable for the USB route, or must macOS enumerate an actual USB MIDI gadget with unit identity?
- Should `pt-i44.37` proceed as a separate preferences/UI lane after the USB device decision?
