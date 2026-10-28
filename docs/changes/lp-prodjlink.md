---
title: Pro DJ Link runtime diagnosis
description: Completed Autoip construction, failed GLINK, and an exact opt-in compatibility patch for wave 3b.
type: report
status: complete
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

## pt-i44.57: GLINK fails; activation acceptance remains open

**O:** The unmodified eth0 query reaches `0x52a290` with `r0=0xffffffff` after
`ioctl(fd, 0x8946, ifreq)`. The command word at `sp+0x20` is 10. The helper returns
`r0=0` at `0x52a408`: link-down. The output word is uninitialized on this error path;
its nonzero stack contents must not be interpreted as carrier-up.
`SP16_FAKE_ETH0` is not enabled for this normal network run (**S**, launcher).
The failed request thus takes the real-ioctl path in the current shim. **U:** errno
and the precise failing layer (QEMU user ioctl translation versus guest kernel/driver)
were not captured; do not claim a specific errno or a virtio driver defect.

A single instrumented guest covered the failed-GLINK branch, followed by an explicitly
labelled debugger intervention in that same guest. At the helper return, setting only
`r0=1` reached the link-up callback `0x525884` and the post-message call at `0x5258a8`
with `r1=0x1d`. An operation-3 request was observed at `0x5114f8` and `0x50cd18`;
operation 2 was not observed. The later probe supplied link-up on subsequent helper
returns while retaining/logging each actual failed ioctl:

```text
SP16_NET_DIAG GLINK_IOCTL_RETURN r0=0xffffffff cmd=10 data=undefined
SP16_NET_DIAG GLINK_HELPER_RETURN actual=0 force_up=True
SP16_NET_DIAG AUTOIP_MESSAGE message=0x1d
SP16_NET_DIAG GLINK_IOCTL_RETURN r0=0xffffffff cmd=10 data=undefined
Process 1 exited with status = -1 (0xffffffff) lost connection
```

**O:** Console evidence after intervention:

```text
[sp16-ui-stub] blocked guest helper exec: /bin/sh argc=3 argv=[sh -c udhcpc -i eth0 -T 1 -t 1 -n -q]
[sp16-ui-stub] blocked guest helper exec: /bin/sh argc=3 argv=[sh -c udhcpc -i eth0 -T 2 -t 1 -n -q]
[sp16-ui-stub] blocked guest helper exec: /bin/sh argc=3 argv=[sh -c udhcpc -i eth0 -T 4 -t 1 -n -q]
Trace/breakpoint trap
```

The GDB connection reset while the outer system VM remained alive. The cause of the
trap is **U**; it is not evidence of an ordinary uninstrumented firmware crash. The
final scripts stop at probes and require manual continuation, avoiding the initial
script's automatic resume loop. This revised mode was import-checked, not boot-retested.
The bounded branch attempt ended here. No second link-already-up branch exists in this
baseline, and no retry or unrelated state-machine investigation was made.

**O:** The 60-second segment watcher captured zero frames (24-byte empty PCAP, zero
text lines). It overlapped debugger stops and the target exit; it is not a valid
60-second continuously running keep-alive test. **U:** successful binds to UDP
50000, 50001, 50002 and emitted keep-alive. No listener inventory was taken.
The new socket probes are follow-up tooling, not evidence that a bind happened.

**S/O:** The existing packet-socket shim replaces Autoip's ARP socket with an idle
socket; its console line was observed before the intervention. Real ARP probing is
therefore independently blocked. **I:** the exact opt-in patch below removes both
identified compatibility barriers; it is not a demonstrated complete Pro DJ Link fix.
DHCP helpers remain blocked and may require follow-up, but enabling arbitrary guest
helper execution is not justified by this run.

# Cross-lane requests (wave 3b, lane M)

Apply the following patch to `emulator/hardware_stub.c` and `emulator/guest-init.sh`.
It was generated against this branch and passed `git apply --check`; it was **not
applied, compiled, or runtime-tested**. No `native.py` or lane F change is required.

- `SP16_NET_GLINK=1` explicitly supplies connected, `=0` disconnected; unset or other
  values preserve the existing path. Only eth0 command 10 is handled. It writes the
  output before returning success. Other ethtool requests and actual MAC/IP/netmask
  queries remain untouched. This is a synthetic, fixed carrier contract for an
  explicitly networked emulator, not physical cable-state tracking.
- `SP16_NET_ARP=1` lets the real packet socket open. Its descriptor is not registered
  as `PACKET`, so the existing `bind` and `sendto` wrappers naturally pass it through;
  deleting their isolated-mode protections is unnecessary.
- The launcher opts in only when it has configured a real virtio NIC. Offline and
  fake-eth0 factory-test behavior remains unchanged. Do not enable fake eth0 for this.

```diff
--- a/emulator/hardware_stub.c
+++ b/emulator/hardware_stub.c
@@ -468,6 +468,28 @@
 #define SP16_SIOCGIFNETMASK 0x891bUL
 #define SP16_SIOCGIFHWADDR 0x8927UL
 #define SP16_SIOCETHTOOL 0x8946UL
+/* Network-only compatibility: QEMU user-mode may reject SIOCETHTOOL.
+ * Unset preserves the real ioctl. Explicit 0/1 supplies disconnected/connected.
+ * Never substitute the real interface's address, netmask or MAC. */
+static int network_glink_ioctl(unsigned long request, void *arg, int *result) {
+    const char *value = getenv("SP16_NET_GLINK");
+    if (!value || (strcmp(value, "0") && strcmp(value, "1")) ||
+        request != SP16_SIOCETHTOOL || !arg) return 0;
+    struct ifreq *ifr = arg;
+    if (strncmp(ifr->ifr_name, "eth0", IFNAMSIZ)) return 0;
+    if (!ifr->ifr_data) { errno = EFAULT; *result = -1; return 1; }
+    uint32_t *ethtool = (uint32_t *)ifr->ifr_data;
+    if (ethtool[0] != 10) return 0; /* ETHTOOL_GLINK only */
+    ethtool[1] = value[0] == '1' ? 1U : 0U;
+    *result = 0;
+    return 1;
+}
+
+static int network_arp_enabled(void) {
+    const char *value = getenv("SP16_NET_ARP");
+    return value && !strcmp(value, "1");
+}
+
 static int fake_eth0_enabled(void) {
     static int enabled=-1;
     if (enabled<0) { const char *v=getenv("SP16_FAKE_ETH0"); enabled = v && !strcmp(v,"1"); }
@@ -522,6 +544,7 @@
     struct state *s = device(fd);
     if (!s) {
         int fake_result;
+        if (network_glink_ioctl(request, arg, &fake_result)) return fake_result;
         if (fake_eth0_ioctl(request, arg, &fake_result)) return fake_result;
         return original_ioctl(fd, request, arg);
     }
@@ -1584,7 +1607,7 @@

 int socket(int domain,int type,int protocol) {
     int (*real)(int,int,int)=dlsym(RTLD_NEXT,"socket");
-    if (domain==AF_PACKET && (type&0xf)==SOCK_DGRAM && protocol==1544) {
+    if (domain==AF_PACKET && (type&0xf)==SOCK_DGRAM && protocol==1544 && !network_arp_enabled()) {
         int fd=idle_socket(PACKET);
         fprintf(stderr,"[sp16-ui-stub] ARP socket -> isolated idle fd=%d\n",fd);
         return fd;
--- a/emulator/guest-init.sh
+++ b/emulator/guest-init.sh
@@ -257,6 +257,12 @@
   # A real eth0 (--net) is answered by the kernel; the fake one is only for runs without a NIC.
   [ -n "$sp16_have_nic" ] || sp16_midi_env="$sp16_midi_env -E SP16_FAKE_ETH0=1"
 fi
+# A configured virtio NIC uses its real interface identity and packet sockets.
+# Supply the GLINK compatibility answer only for this explicitly networked guest.
+sp16_net_env=
+if [ -n "$sp16_have_nic" ]; then
+  sp16_net_env="-E SP16_NET_GLINK=1 -E SP16_NET_ARP=1"
+fi
 # Research builds only (native.py --research --trace): opt-in QEMU user-mode tracing, default off.
 sp16_trace_args=
 case " $(cat /proc/cmdline) " in
@@ -609,7 +615,7 @@
     SP16_APP_CWD=$sp16_app_cwd python3 /opt/sp16/enter.py /tmp/sp16-root /qemu-arm-static $sp16_trace_args -cpu $sp16_cpu \
      -E DISPLAY=127.0.0.1:99 -E LD_LIBRARY_PATH=$sp16_ldpath \
      -E LD_PRELOAD=$sp16_preload -E ALSA_CONFIG_PATH=/etc/sp16-alsa.conf \
-     -E SP16_PANEL_INPUT=1 $sp16_tap_env $sp16_audio_env $sp16_midi_env \
+     -E SP16_PANEL_INPUT=1 $sp16_tap_env $sp16_audio_env $sp16_midi_env $sp16_net_env \
      $sp16_app_path $sp16_app_arg >> /output/application.txt 2>&1 &
     echo $! > /run/sp16-app.pid
     echo "SP16_APP_STARTED pid=$!" > /dev/console
```

Before integration, lane M must add ARM contracts for opt-in/out, eth0 versus another
interface, command 10 versus another command, output 1 and measured 0, null data,
unchanged real address ioctls, and ARP passthrough versus offline isolation. Then run
`bash emulator/arm-contracts.sh`, rebuild, and verify the entire acceptance chain
without a debugger carrier override: link-up/message 0x1d, all three UDP listeners,
and a 54-byte TORAIZ keep-alive on the isolated multicast segment. If GLINK works
natively on a different runtime, leave this synthetic override disabled and trace
operation 2 with the probes instead. Preserve real carrier behavior there.

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
# Repeat continue after each probe stop.
```

The captured investigation used the same five addresses as manual breakpoints and register reads.
The startup script automates those observations without writing target registers or data. Software breakpoints
temporarily instrument execution and debugger pauses perturb timing; this is not a timing measurement.
Host load before boot was 2.65 / 4.15 / 5.62, below the brief's load-40 cutoff.
Session evidence stays under ignored `build/lp/sessions/`.

For carrier probes, import `emulator/net/diag/carrier.py` before continuing the fresh
guest. It defaults to observation only. At the ioctl return inspect the emitted
command/result; at the helper return inspect the actual Boolean. If explicitly
repeating the intervention, `script carrier.force_up = True` changes that helper's
return register only. Disable other manual breakpoints first to avoid duplicate stops.
`script carrier.force_up = False` removes the intervention. Do not use it for acceptance.
The probes read ports and packet type/length only, never interface or packet identities.

```sh
python3 emulator/net/segment_watch.py --seconds 60 \
  --pcap build/lp/carrier-override.pcap > build/lp/carrier-override-watch.txt
./sp16 diagnostics --build lp --sessions 1 --out build/lp/lp-prodjlink-diagnostics.zip
```

Do not commit raw watcher output: it includes emulator interface addresses. No fake
master was started; carrier detection fails before a partner could establish acceptance.
No known-flush OEM stdout capture was exported: the successful banner call and constructor
return establish construction independently; raw stdout buffering is explicitly unresolved.

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

Task 57 quick set: `Ran 429 tests in 7.759s`, `OK` — 429 of 429 passed.
LLDB import check: 11 of 11 breakpoint definitions registered (5 startup, 6 carrier),
using an empty ARM target. No successful socket return callback was exercised.

```sh
lldb --batch \
  -o 'script lldb.debugger.CreateTargetWithFileAndArch(None, "arm")' \
  -o 'command script import emulator/net/diag/startup.py' \
  -o 'command script import emulator/net/diag/carrier.py' \
  -o 'breakpoint list'
git apply --check build/lp/wave3b-network.patch
git diff --check
```

Both patch-check and whitespace-check exited 0. The first empty-target import attempt
used unsupported `target create --arch arm` without an executable; the Python target
creation above corrected it. This was a tooling invocation error, not a probe failure.
Diagnostics: `diagnostics_written`, 17 files, 21,834 bytes, no refused files; local only.

# Hand-back

| Task | Commit | Files changed | Check / result | Acceptance |
|---|---|---|---|---|
| `pt-i44.56` | `11d1542` | `emulator/net/diag/startup.py`, this report | Quick-set command above: 429 of 429, `OK`; manual GDB constructor trace | Construction completed established, 1 of 1 requested classification |
| `pt-i44.57` | `7f200c7` | `emulator/net/diag/carrier.py`, manual-stop adjustment to `startup.py` | Quick-set command above: 429 of 429, `OK`; 11 of 11 probe definitions import | Failed GLINK established; only message 0x1d observed under intervention; full acceptance remains open |

This final documentation commit adds the complete evidence, exact cross-lane patch and
hand-back. `git log -1 -- docs/changes/lp-prodjlink.md` identifies it after commit.
The mandated `Fixes:` commit trailers identify diagnosis work; they are not a request
to close the unvalidated networking task. No task queue state was changed.

**Not run:** full suite (brief forbids it), ARM contracts/Swift typecheck (no implementation
changes), automated smoke/hardware suite (not a changed product build), patched runtime
acceptance (other lane owns implementation). No public push, PR, merge or hardware access.

**Side effects:** built `build/lp` with GDB/research/audio-file mode, reusing the supplied
QEMU and imported library. Booted one multicast guest; audio-file output was disabled at
run time. The supplied private virtual USB/data images were attached normally. Started
app PID 30266, system QEMU PID 30273, bridge PID 30303, LLDB, and a bounded segment watcher.
The inner application exited during tracing. Sent SIGTERM only to our exact app PID 30266
and bridge PID 30303, quit LLDB, and let the watcher finish; `ps` confirmed all three
listed PIDs absent, including QEMU. Generated build logs, empty PCAP, watcher text,
patch candidate, check logs and diagnostics ZIP remain under ignored `build/lp`; no
captures, firmware, keys, device identity or OEM artwork are committed.

**Parked:** task 57's production link/message/listener/keep-alive acceptance pending lane M's
patch implementation and wave-3b verification. No extra branch retries after the trap.
**Cross-lane requests:** lane M patch and contracts above; no request to lane F.
**Owner questions:** none needed to apply the proposed isolated-emulator experiment.
Physical cable-down semantics and the failure's exact errno remain open engineering
questions, not grounds to mark networking complete.
