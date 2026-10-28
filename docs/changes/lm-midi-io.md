---
title: Lane M MIDI I/O continuation hand-back
description: Completed clock and codec fixes, tested DIN transport foundations, and parked firmware MIDI acceptance.
type: report
status: partial
created: 2026-10-28
tags: [sp16, emulator, midi, hand-back]
author:
  harness: codex
  agent: lm-midi-io
  model: gpt-6-astra
  thinking: high
  tokens: unknown
---

Tasks 1 and 2 were committed by the preceding run. This continuation preserved its uncommitted DIN work,
added transport tests and cleanup, and committed the resulting partial implementation. **DIN firmware
acceptance failed in two live attempts and is parked under the common brief's two-failure rule. USB
implementation is parked because the lane brief explicitly requires fully verified DIN before task 4.**
Neither MIDI task should be closed from this branch. No PR, push, merge, task mutation, or firmware download occurred.

## Changes and checks per task

### 1. pt-i44.62 — complete — 67bdd7e

Files: `emulator/midi/clock.py`, `emulator/net/qemu-net.sh`,
`emulator/tests/test_sp16_midi_clock.py`, `emulator/tests/test_sp16_net.py`.

Clarifies clock API descriptions, checks the callback pulse grid, and appends QEMU network arguments without eval.
The previous commit records:

- `python3 -m unittest emulator.tests.test_sp16_midi_clock emulator.tests.test_sp16_net`: `Ran 26 tests`, `OK`, 26/26.
- The quick set below: `Ran 429 tests`, `OK`, 429/429.

These task-specific results are inherited from the previous run, not new measurements by the continuation.

### 2. pt-i44.60 — shim complete; profile request outstanding — 39c161b

Files: `emulator/hardware_stub.c`, `emulator/tests/sp16_hwparams_contract.c`.

Refuses non-S16_LE for AK4458 while preserving AK4452, untracked PCM behavior and the research override.
The previous commit records `bash emulator/arm-contracts.sh`: 10/10 contracts, and the quick set: 429/429.
The continuation's expanded contract run passes all 12/12, including this format contract.
Profile capability metadata belongs to lane F; exact request below.

### 3. pt-i44.17 — partial transport committed; firmware acceptance parked — 2cd1213

Files:

- `emulator/hardware_stub.c`: rawmidi UART backend; factory-test loopback stays separate.
- `emulator/midi/din.py`: host virtual CoreMIDI bridge, MIDI framing, bounded byte queues, 320 us input slots,
  realtime priority, clock-engine input release latency, and disconnect cleanup.
- `emulator/midi/seq-bridge.c`: guest ALSA sequencer bridge with 320 us output slots and realtime priority.
- `emulator/midi_bridge/midi.py`: raw callback consumers can disable the otherwise unread message queue.
- `emulator/native.py`: dedicated `sp16.din` virtio port and CLI-launched helper, named `SP-16 DIN [<build>]`.
- `emulator/guest-init.sh`, `emulator/Dockerfile.native-base`,
  `emulator/Dockerfile.native-base.dockerignore`: ALSA support and helper packaging. The apl_start hook and
  `modprobe virtio_pci` remain intact. The Dockerfile-specific allowlist avoids modifying another owned file.
- `emulator/arm-contracts.sh`, `emulator/tests/sp16_din_contract.c`,
  `emulator/tests/sp16_din_queue_contract.c`, `emulator/tests/test_sp16_din_midi.py`: transport checks.

Checks after the continuation's changes:

| Command | Result |
|---|---|
| Quick set below | `Ran 429 tests`, `OK`: 429/429 |
| `python3 -m unittest emulator.tests.test_sp16_din_midi` | `Ran 5 tests`, `OK`: 5/5; includes real CoreMIDI virtual ports through a Unix UART socket in both directions and clean shutdown |
| `python3 -m unittest emulator.midi_bridge.tests.test_midi_bridge` | `Ran 61 tests`, `OK`: 61/61 |
| `bash emulator/arm-contracts.sh` | 12/12 `PASS sp16_*_contract`; includes deterministic slots at 320, 640, 960 and 1280 us, realtime priority, overflow bounds and ARM rawmidi I/O |
| `python3 -m unittest emulator.tests.test_sp16_native` | 54/55; `test_eject_is_a_research_command` fails because unchanged sessionctl source lacks the literal `op="usb"` expected by the unchanged test |
| File-audio research build below | `native_app_built`; two explicit firmware 1.60 boots reached `SP16_NATIVE_READY` |
| `./sp16 smoke --build lm-midi-verify --no-video` | FAIL, 7/8 checks, 19.746 s: pad input counted 7 guest events for 8 edges; 0 failed events. Clean stop passed in 0.6 s |
| `./sp16 hwsuite --build lm-midi-verify --smoke` | Environment exit 3; 0 checks completed: no READY within 120 s. App log says the profile's data image is held by preceding-run PID 33000. No uncalibrated control build was present |
| `git diff --check` and staged diff review | Passed before commit |

The original unfinished guest build lacked native libc headers. After supplying them and packaging the extracted
C helper explicitly, the build passed. A proposed kernel-virmidi alternative was discarded: Debian's arm64
kernel has `CONFIG_SND_VIRMIDI` unset. The committed code does not depend on that unavailable module.

**Live result:** the first sequencer client did not produce visible firmware MIDI input or return MIDI output.
The second attempt corrected the client name and made the card-probe sentinel a regular file. It reached
`SP16_DIN input=active`, but notes/clock/start still produced no observed input indication, and PLAY returned no
clock to the host monitor. This demonstrates host-to-guest delivery, not firmware acceptance. The probe used an
empty ephemeral project, so audible pad triggering was not established either. Acceptance remains unresolved;
no further firmware debugging was performed after this second failed check.

Remaining DIN work includes discovering the firmware's actual sequencer subscription/probe requirements,
THRU on the sequencer path, exercising rawmidi modes/lifecycle beyond the synthetic contract, and audio-position
clock output. The rawmidi THRU code is not coverage of the native sequencer path. The two backends must not consume
the same virtio stream concurrently. The rawmidi write implementation is synchronous; complete nonblocking ALSA
semantics have not been implemented. Host clock-out currently forwards received bytes; it does not yet drive
`ClockGenerator` from audio sample position. The host helper currently starts through the Python CLI only, not
Finder launches. F01/F02, DAW timing and firmware clock-lock acceptance did not run successfully and are not claimed.

### 4. pt-i44.35 — parked; no implementation commit

The lane brief requires task 3 fully committed and verified before starting USB implementation. With DIN's
firmware acceptance parked, this continuation only recorded a read-only feasibility probe of its built guest:

- `CONFIG_USB_DUMMY_HCD=m`, `CONFIG_USB_CONFIGFS_F_MIDI=y`, `CONFIG_SND_SEQ_MIDI=m`.
- `CONFIG_USB_MIDI_GADGET` and `CONFIG_SND_VIRMIDI` are unset.
- The current packaging removes USB gadget/host-controller modules; no dummy HCD or gadget module survives in
  the packaged module tree. This does not mean the distribution kernel cannot support configfs MIDI.

Probe result: 1/1 inspection command completed; **0 USB acceptance checks run**. The dump is
`build/lm-midi-io/usb-kernel-probe.txt`. No USB endpoints, signed MIDIServer plugin, USB firmware pad-output test,
DAW device enumeration, or USB MIDI sync test was implemented or claimed.

A follow-up can retain configfs/libcomposite/USB gadget/USB host dependencies, instantiate a MIDI function on
`dummy_hcd`, and connect the **host-side** rawmidi card to a separate virtio port. Keep the gadget-side card for the
firmware sequencer. Reuse the existing CoreMIDI helper for initial virtual endpoints as the lane brief requests;
a real MIDIDevice/driver remains separate work. References:
[Linux 6.1 gadget testing](https://docs.kernel.org/6.1/usb/gadget-testing.html) and
[ALSA sequencer MIDI encoding](https://www.alsa-project.org/alsa-doc/alsa-lib/group___m_i_d_i___event.html).

## Reproduction

Build and boot (the continuation used a new name so it could leave the earlier run alone):

```sh
./sp16 build --name lm-midi-verify --research --audio file \
  --library build/sp16/library --reuse-qemu-from build/qemu-source
# After guest changes, the same command with --rebuild.
./sp16 run --name lm-midi-verify --ephemeral --no-ui-input --no-audio-files
```

Wait for `SP16_NATIVE_READY` and `MIDI bridge ready` in that build's session console and `din-midi.log`.
The existing run held `build/stick.img`, so explicit live probes booted without it; smoke used its own fixture.
Host load was below 40 (observed averages approximately 9–13) during these checks.
The live probe commands, run twice, were:

```sh
PYTHONPATH=emulator python3 - <<'PY'
from midi_bridge.midi import Message, Port
import subprocess
import time
port = Port(['run', '--in', 'SP-16 DIN [lm-midi-verify]',
             '--out', 'SP-16 DIN [lm-midi-verify]'])
try:
    port.send(Message('start', None, 0, 0))
    for _ in range(48):
        port.send(Message('clock', None, 0, 0))
        port.send(Message('note_on', 0, 60, 100))
        time.sleep(.0208)
        port.send(Message('note_off', 0, 60, 0))
    subprocess.run(['./sp16', 'ctl', '--build', 'lm-midi-verify',
                    'snapshot', 'midi-input.png'], check=True)
    port.send(Message('stop', None, 0, 0))
    subprocess.run(['./sp16', 'ctl', '--build', 'lm-midi-verify', 'press', 'play'], check=True)
    print('host output:', port.drain(2)[:20])  # observed [] on the second attempt
    subprocess.run(['./sp16', 'ctl', '--build', 'lm-midi-verify', 'press', 'stop'], check=True)
finally:
    port.close()
PY
```

The required quick set (never the full suite):

```sh
python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles \
 emulator.tests.test_sp16_fwdev emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace \
 emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite \
 emulator.tests.test_sp16_pcm_tap_analyze emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke \
 emulator.tests.test_sp16_bootstrap emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter
```

Swift typechecking was not run: no Swift source changed. The full test suite was deliberately not run, per the
common brief. Tests and captures remain git-ignored under `build/`.

## Cross-lane requests

1. Lane F, `emulator/profiles/sp16-1.60.json` and its schema: add source-backed AK4458 kernel/SSI capabilities
   for S16_LE, 1–16 channels, and sixteen 16-bit slots. Record the source evidence/hash under the existing provenance
   convention; do not invent one. Keep kernel-advertised rates distinct from board-clock policy. Preserve the app's
   16-channel, 32-byte frame stream and the AK4452 format sets.
2. Lane F, `audio-sink.swift`/`audio-output.swift`: expose a monotonic host-time/audio-frame anchor and transport/BPM
   state on a separate channel so the MIDI helper can feed `ClockGenerator`. Do not poll UI counters as a clock.
3. Lane F, native app lifecycle: launch and stop MIDI helpers for Finder launches as well as CLI launches;
   distinguish source/destination labels and sessions. The existing Swift CoreMIDI helper was reused unchanged.
4. Lane F, `sp16emu.py`: expose an explicit MIDI transport/diagnostic command when the runtime route is ready.
5. Lane F/harness owner: reconcile `test_eject_is_a_research_command` with the current sessionctl implementation,
   and investigate the smoke pad-edge miss. Neither file was edited by this lane.
6. Dispatcher: fold this report into the user docs only with the incomplete firmware/USB acceptance clearly stated.
   Do not describe published virtual endpoints as a real CoreMIDI MIDIDevice.

## Side effects and final hand-back

- Existing commits: pt-i44.62 `67bdd7e` (26/26 targeted, 429/429 quick); pt-i44.60 `39c161b`
  (10/10 original contracts, 429/429 quick; later 12/12 contracts).
- New implementation commit: pt-i44.17 `2cd1213`, partial: 5/5 DIN, 61/61 bridge, 12/12 contracts,
  429/429 quick; native 54/55; smoke 7/8; firmware acceptance failed twice; hardware smoke 0 checks, environment exit 3.
  Changed files and exact commands are listed above.
- pt-i44.35: no implementation commit; 1/1 kernel inspection, 0 USB acceptance checks. Parked behind the explicit
  DIN-completion requirement. This report is the final commit on the lane.
- Created/rebuilt `build/lm-midi-verify` with file audio and reused the existing local library/QEMU. Started explicit
  app PIDs 45966 and 60299, smoke PID 65533, and blocked hardware-smoke PID 66159. All four were stopped by exact
  PID or the suite's own exact-PID cleanup. Their helper/QEMU children exited. The previous run's PID 33000 and its
  children were not stopped. Its build and firmware/data library remain unchanged by the explicit ephemeral probes.
- Temporary contract/inspection Docker images were removed. Docker build caches and ignored build artifacts remain.
- Collected `build/lm-midi-io/midi-diagnostics.zip` with `./sp16 diagnostics --build lm-midi-verify --out
  build/lm-midi-io/midi-diagnostics.zip`; 25 files, nothing uploaded. No firmware/key/image/capture entered git.
- Parked: pt-i44.17 firmware acceptance and remaining clock/THRU semantics; pt-i44.35 implementation and acceptance.
- Owner questions: which session may release the persistent data-image lock for hardware checks; and whether the
  next lane should resolve firmware MIDI discovery first or obtain a firmware-side trace from the RE lane.
