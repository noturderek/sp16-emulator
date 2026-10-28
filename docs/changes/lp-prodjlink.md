---
title: Pro DJ Link runtime diagnosis
description: Constructor completion, log visibility, and the remaining network activation trace.
type: report
status: in-progress
created: 2026-10-28
tags: [sp16, emulator, prodjlink, diagnosis]
author:
  harness: codex
  agent: lp-prodjlink
  model: gpt-6-astra
  thinking: high
  tokens: unknown
---

# Findings

Grades: O = observed runtime, S = source/static evidence, I = inference, U = unresolved.
Addresses apply only to the imported OEM 1.60 application, SHA-256
`3d1392a61323e73ea4f2fddef8fe9e0142add7a6f6fab79ab0eb0d5396555464`.

## pt-i44.56: construction completed

**O:** In a fresh network-enabled GDB build, the singleton was initially zero, the constructor entered,
the banner call returned successfully, and the constructor returned to `createMembers`. This rules out
no construction and a pre-banner stall in this run. The statement that the emulator never starts Autoip
is false for this checkout. Identity-free LLDB excerpts:

```text
stop pc=0x001bce98  singleton[0x00f6e2a0]=0x00000000  guards[0x00f53c94]=00 00
stop pc=0x00522aa0  lr=0x001c0ca0
stop pc=0x00522c44  r0=0x0092cee0
stop pc=0x00522c48  r0=0x0000001e
stop pc=0x001c0ca0
```

**S:** `guest-init.sh` redirects application stdout/stderr to guest `/output/application.txt`.
`guest-bridge.py:diagnostics` exports only lines beginning `[sp16-ui-stub]` or `[sp16-mod]`.
The OEM Autoip banner cannot appear in host `console.txt` through that path, even after stdout flushes.
Successful `puts` is not proof of a flushed file: buffering remains U, but is not needed to establish
constructor completion. **O:** The host console subsequently contains
`[sp16-ui-stub] ARP socket -> isolated idle fd=24`, independently confirming the Autoip worker ran.

No startup or NIC shim is requested for this task. Retain the default OEM log privacy filter.
The narrowly scoped debugger probes provide observable startup status without exporting arbitrary OEM logs.

# Reproduce

```sh
./sp16 doctor
./sp16 build --name lp --research --trace gdb --audio file \
  --library build/sp16/library --reuse-qemu-from build/qemu-source
./sp16 run --name lp --net mcast --no-ui-input --no-audio-files
python3 emulator/gdb-bridge.py --build lp --port 1237
# Separate terminal, before the firmware app has run:
lldb -o 'gdb-remote 127.0.0.1:1237'
# In LLDB:
command script import emulator/net/diag/startup.py
continue
```

The captured investigation used the same five addresses as manual breakpoints and register reads.
The script automates those observations without writing target registers or data. Software breakpoints
temporarily instrument execution and debugger pauses perturb timing; this is not a timing measurement.
Host load before boot was 2.65 / 4.15 / 5.62, below the brief's load-40 cutoff.
Session evidence stays under ignored `build/lp/sessions/`.

# Verification

`./sp16 doctor`: all prerequisites passed. Research GDB build and network-enabled launch succeeded;
host console reported `SP16_NATIVE_READY` and `SP16_APP_STARTED`.

Required quick set (run after each task):

```sh
python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter
```

Task 56: `Ran 429 tests in 6.828s`, `OK` — 429 of 429 passed.
No full suite (brief forbids it), Swift check, ARM contracts, or hardware/smoke suite: no runtime implementation
changed. The instrumented boot above supplies the requested runtime evidence.

# Hand-back (work in progress)

Task 56: this commit adds `emulator/net/diag/startup.py` and this report; checks above passed.
Task 57: carrier and UDP activation investigation in progress. One GDB emulator and its bridge are running;
the final hand-back will record their exact-PID cleanup and cross-lane patches.
