---
title: Lane S MIDI hand-back
description: DIN realtime queue and capacity, the first firmware MIDI boundary, and parked downstream acceptance
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, midi, din, usb]
author:
  harness: codex
  agent: ls-midi
  model: gpt-6-sol
  thinking: high
  tokens: unknown
---

# Lane S MIDI hand-back

The normal app's first ALSA sequencer open is the blocking boundary. Its 32-bit process opens `/dev/snd/seq`, then `qemu-arm-static` returns `ENOSYS` for the sequencer version ioctl `0x80045300`. This happens for input and output before either subscription, callback, DIN ioctl or USB route. The native guest DIN bridge does register as client 128, port 0, and the host CoreMIDI helper becomes ready; those facts alone do not establish app MIDI acceptance. The trace is under the ignored `build/ls-midi/sessions/native-98376a20-2cbd-46d0-92e5-fb451dbfced7/console.txt` (lines 1380–1390). The file-audio boot reported `SP16_NATIVE_READY`.

## Tasks

1. **`pt-i44.69` — `8ba9f42` (transport implemented; live acceptance blocked).** Files: `emulator/hardware_stub.c`, `emulator/guest-init.sh`, `emulator/midi/seq-bridge.c`, `emulator/arm-contracts.sh`, `emulator/tests/sp16_din_ioctl_contract.c`, `emulator/tests/sp16_din_queue_contract.c`. The normal `/dev/din_midi` ioctl forwards the decoded realtime or F2 request to the bridge's one bounded UART queue; the proc capacity read reports that queue's current free bytes. Malformed/full/unavailable requests fail explicitly; factory loopback remains separate. `bash emulator/arm-contracts.sh`: **14/14 PASS**, including request forwarding, queue priority and capacity refill. The required 14-module quick unittest command from the common brief: **445/445 OK**. `python3 -m unittest emulator.tests.test_sp16_din_midi`: **5/5 OK**. `./sp16 build --name ls-midi --research --audio file --library build/sp16/library --reuse-qemu-from build/qemu-source`: `native_app_built`. The live FA/F8/FC and channel-recovery check could not pass because the app never opens its sequencer.
2. **`pt-i44.70` — `4869a76` (first failing boundary identified).** File: `emulator/hardware_stub.c`. The app boundary now logs ALSA sequencer opens, subscription results and the first input events. `bash emulator/arm-contracts.sh`: **14/14 PASS**. The same quick unittest command: **445/445 OK**. A firmware 1.60 file-audio boot with `--trace strace` captured **2/2 sequencer opens failing `ENOSYS`** at the version ioctl; **0 subscriptions and 0 callbacks** were reached. The firmware UI showed machine sync source INTERNAL and project MIDI OUT clock, transport and note/CC ENABLE. A host monitor received **0 messages** after PLAY/STOP. The loaded-sample and deterministic input-note steps were not run: the app cannot subscribe to an input peer at this earlier boundary.
3. **`pt-i44.17` — parked, no new commit.** The earlier DIN transport remains present. The acceptance check is **0/2** (audible note and clock output) because both app sequencer opens fail before either route. F01/F02 were not run for the same reason. The `midi/clock.py` engine is not evidence of firmware-generated clock delivery.
4. **`pt-i44.71` — parked, no commit.** The translator lifecycle/mode fields and song-position input-key asymmetry remain **U**. Setter-to-UI and input-consumer checks are **0/2**; the DIN chain stopped at the first app boundary rather than assigning meanings without evidence.
5. **`pt-i44.35` — parked, no commit.** The brief requires DIN acceptance before USB-B implementation. The same app sequencer failure would also block app enumeration of a `g_midi` peer. USB notes/realtime, mode-switch re-registration, and DAW-device checks are **0/3**; no USB-B transport or signed CoreMIDI device was claimed.
6. **`pt-i44.37` — parked, no commit.** The requested port table, clock toggles, and persistence require accepted DIN and USB routes. Their checks are **0/3**; no preference UI was claimed.

The quick unittest command used for tasks 1 and 2 (`Q`) was:

```sh
python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter
```

Result each time: `Ran 445 tests in 9 s`, `OK` (445/445). The live trace build command was `./sp16 build --name ls-midi --research --trace strace --audio file --library build/sp16/library --reuse-qemu-from build/qemu-source --rebuild`; it returned `native_app_built`. `./sp16 run --name ls-midi --ephemeral --no-ui-input --no-audio-files --midi` reached `SP16_NATIVE_READY` and the two `ENOSYS` failures above.

## Other checks and side effects

- Normal file-audio research build: `native_app_built`; multiple rebuilds were made to test the trace and restore a normal build. No firmware or data image was downloaded or committed.
- `./sp16 smoke --build ls-midi --no-video`: **7/8** in 20 s. Boot, display, lamps, selector, PLAY/STOP, USB mount and clean stop passed. The pad-edge check saw 3 guest events for 8 edges, with 0 failed events; the previous lane reported the same type of miss.
- `./sp16 hwsuite --build ls-midi --smoke`: **14 PASS, 0 FAIL, 2 BLOCKED of 16** (exit 2). HW-24 was blocked because no uncalibrated control build was supplied. HW-45 was blocked because `/dev/snd/seq` is now an observed but unserved app-facing device; that is the same sequencer boundary. Report: ignored `build/ls-midi/hwsuite/20261028-091920-bdb26d/hwsuite-report.md`. System load exceeded 40 during this run (observed above 70); timing results need that context.
- Five explicit ephemeral app sessions were started and stopped by exact PID (68940, 80656, 82623, 84270, 88609). The smoke and hardware suites started their own sessions and perform exact-PID cleanup. No emulator process started by this lane is intentionally left running.
- `./sp16 diagnostics --build ls-midi --sessions 8 --out build/ls-midi/midi-diagnostics.zip`: 74 redacted files, 741,952 bytes, no refused files; the ignored zip was not uploaded or committed.
- `git diff --check` passed before each implementation commit. The full unfiltered `./sp16 test` was not run, per the common brief. Swift typecheck was not run because no Swift source changed.

## Cross-lane requests

- Dispatcher/integration: provide a supported 32-bit ALSA sequencer path for the firmware process. QEMU user mode returns `ENOSYS` on the first ALSA sequencer ioctl; DIN and USB acceptance cannot proceed until the app can create clients and subscribe. A native guest bridge already works, but it does not make the 32-bit app's ioctl succeed. Decide whether the compatibility boundary belongs in QEMU user mode or an app-facing proxy, then rerun the explicit settings/sample/clock recipes.
- Lane G2 documentation: describe DIN and USB application MIDI as unaccepted until the sequencer boundary is resolved. The published DIN virtual CoreMIDI endpoints are not a real USB MIDI device.

## Open questions for the owner

- Which supported path should implement the 32-bit ALSA sequencer ABI: QEMU user-mode ioctl support or an app-facing native proxy?

## Final hand-back

Tasks 1 and 2 have local commits and passing offline checks; task 2 establishes the first live failure. Tasks 3–6 are parked with the prerequisite boundary above. No push, PR, merge or task-state mutation was made.
