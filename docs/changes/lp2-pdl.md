---
title: Lane P2 Pro DJ Link diagnostics hand-back
description: Socket startup is confirmed; QEMU multicast delivery blocks keep-alive, fake-master, and sync-bridge acceptance.
type: report
status: partial
created: 2026-10-28
tags: [sp16, emulator, prodjlink, network, diagnostics]
author:
  harness: codex
  agent: lp2-pdl
  model: gpt-6-sol
  thinking: high
  tokens: unknown
---

# Lane P2 hand-back

`pt-i44.82` is implemented and committed. A networked OEM 1.60 boot proves the UDP 50000 receiver starts and binds, followed by binds on 50002 and 50001. It also proves the separate sender obtains `SO_BROADCAST` and returns successful 54-byte `sendto` calls. A QEMU guest-NIC filter sees those frames, but a host listener on the configured multicast segment receives none. No fake-master or sync-bridge acceptance is claimed.

## Checks and task results

`Q` is the required quick set:

```sh
python3 -m unittest emulator.tests.test_sp16_panel emulator.tests.test_sp16_profiles emulator.tests.test_sp16_fwdev emulator.tests.test_sp16_cli_usb emulator.tests.test_sp16_exectrace emulator.tests.test_sp16_fwpack emulator.tests.test_sp16_emulator emulator.tests.test_sp16_hwsuite emulator.tests.test_sp16_pcm_tap_analyze emulator.tests.test_sp16_e2e_screen emulator.tests.test_sp16_smoke emulator.tests.test_sp16_bootstrap emulator.tests.test_sp16_firmware_import emulator.tests.test_sp16_enter
```

1. **`pt-i44.82`, complete — `7cb5e53`.** Changed `emulator/hardware_stub.c`, `emulator/guest-init.sh`, `emulator/net/diag/carrier.py`, `emulator/arm-contracts.sh`, and `emulator/tests/sp16_network_contract.c`. The shim logs numeric results and failure errno for address queries, binds, closes, socket options, and sends without logging addresses, MACs, or payloads. `SP16_NET_DIAG=1` is forwarded only by the networked `--research --trace gdb` build. Checks: `Q` **445/445 OK**; `python3 -m unittest emulator.tests.test_sp16_net` **17/17 OK**; `bash emulator/arm-contracts.sh` **17/17 contract runs PASS**, including the new diagnostic line checks; `python3 -m py_compile emulator/net/diag/carrier.py` **exit 0**; LLDB empty-target import **17/17 probes registered**; research `./sp16 build --name lp2 --research --trace gdb --audio file --library build/sp16/library --reuse-qemu-from build/qemu-source` **pass**; `./sp16 doctor` **ready**; `./sp16 run --name lp2 --net mcast --no-ui-input --no-audio-files` reached `SP16_NATIVE_READY`. On the live boot, GLINK returned 1 and Autoip message `0x1d` was observed; later address and mask ioctls returned nonzero, then real binds on 50000, 50002, and 50001 returned 0. At least 23 consecutive 54-byte sends to 50000 returned 54 with broadcast option result 0. With close logging enabled, no close of the bound 50000 socket was observed before stopping the app. The first probe-driven continuation ended with a trace trap after GLINK and `0x1d`; the subsequent no-breakpoint boot supplied the socket results. The full test suite, Swift typecheck, smoke, and hardware suite were not run under the brief's scope and because this is an opt-in diagnostic change.

2. **`pt-i44.57` / `pt-i44.38`, parked — no task-2 commit.** `python3 emulator/net/segment_watch.py --seconds 65 --pcap build/lp2/prodjlink-final.pcap` returned **0/1 required keep-alive capture**, zero decoded lines and a 24-byte header-only PCAP, while the guest logged 23 successful 54-byte sends. A QEMU `filter-dump` experiment under ignored `build/lp2` found **54 54-byte UDP-50000 frames at the guest NIC**; its simultaneous 35-second segment watch captured **0/1**. A synthetic host multicast frame reached the watcher (**1/1**), which checks its receive path. Explicit QEMU `localaddr=127.0.0.1` with a loopback-group listener, then binding the listener to the multicast group, both yielded **0/1**. A minimal macOS socket reproduction of QEMU's multicast-address bind reported a successful send but no delivery to a second listener. [QEMU's socket backend](https://gitlab.com/qemu-project/qemu/-/raw/master/net/socket.c) binds its multicast socket to the group address; this is the leading cause, but the actual host-side QEMU `sendto` return has not been traced. No production network fix passed the acceptance check, so task 2 is parked after the two attempted configuration changes.

3. **`pt-i44.39`, parked — no commit.** The fake master, 130.56 BPM, 5 ms phase, and 60-second WAV checks were **0/1 not run**, because the keep-alive is not observable on the segment. No follower result is inferred.

4. **`pt-i44.41`, parked — no commit.** Link-peer and CoreMIDI clock checks were **0/1 not run**, because no verified Pro DJ Link master tempo reaches the emulator. No sync bridge was added on an unverified source path.

`git diff --check` and `git diff --cached --check` passed before the implementation commit. The full `./sp16 test` was intentionally not run; the common brief assigns it to the dispatcher.

## Side effects

Built and rebuilt silent audio-file research build `build/lp2`, reusing the existing private firmware library and QEMU source build. Started six bounded emulator boots and their local GDB bridges; stopped each app by its exact PID and quit its bridge. `ps` found no remaining listed app or QEMU PID. Logs, debugger scripts, synthetic probes, WAV settings, and PCAPs remain only under ignored `build/lp2`. No firmware, keys, device identity, data image, or capture was committed. No hardware operation, download, push, PR, merge, rebase, or task-queue mutation occurred.

## Parked tasks and cross-lane requests

`pt-i44.57` and `pt-i44.38` need a host-side network handoff that delivers QEMU's Ethernet frames to a listener and accepts frames back. The dispatcher should assign a contained QEMU backend patch or a local frame relay, then repeat the 54-byte/two-second segment check before resuming `pt-i44.39` and `pt-i44.41`. Existing `README.md` and `docs/*.md` were not edited in this lane.

## Open questions for the owner

None needed to continue the network repair. The exact host-side QEMU send return and the chosen supported backend design remain open engineering findings.
