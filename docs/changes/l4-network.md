# Lane l4-network: a NIC for the guest, and the Pro DJ Link follower

Tasks `pt-i44.38` (NIC) and `pt-i44.39` (follow a fake tempo master). Grades: O observed, S static, I inferred, U unknown.

## pt-i44.38 - `./sp16 run --net none|host|bridged:IF|mcast[:PORT]`

Use:

```bash
./sp16 build --name NAME --rebuild        # once: the build folder gets net/qemu-net.sh and a guest with virtio_net
./sp16 run --name NAME --net host         # socket_vmnet host network (bridge100 on this Mac)
./sp16 run --name NAME --net bridged:en0  # the real network, through a bridged socket_vmnet daemon
./sp16 run --name NAME --net mcast        # no root: emulators on this Mac share one segment (230.0.0.1:34567)
python3 emulator/net/segment_watch.py --seconds 10 --pcap seg.pcap   # decode an mcast segment, no root
python3 emulator/net/fake_master.py --iface bridge100 --bpm 128 --pitch 2   # fake tempo master (task 39)
```

How it works:

- The Swift app builds QEMU's command line, so `--net` cannot add arguments there. `native.py launch` passes
  `SP16_NET_KIND` / `SP16_NET_TARGET` with `open --env`; QEMU now starts through `net/qemu-net.sh` (copied into the
  build folder by `bundle()`, `default-config.json` points at it). Without `--net` it execs QEMU unchanged; with it, it
  adds `-netdev stream,addr.type=unix,addr.path=<socket_vmnet socket>` (host, bridged) or `-netdev socket,mcast=`
  and `-device virtio-net-device,mac=52:54:00:<random>` (a new MAC at every start, locally administered, never a unit's).
  It `exec`s QEMU, so the app, `qemu_reaper` and `./sp16 ctl` see the same process as before (O).
- Socket names (Homebrew service layout): host = `/opt/homebrew/var/run/socket_vmnet`, bridged:IF =
  `/opt/homebrew/var/run/socket_vmnet.bridged.IF` (also `/usr/local/var/run`, `/var/run`). A missing socket stops
  `run` with a message that names the socket and suggests `--net mcast`.
- `guest-init.sh`: when a virtio-net device exists, `modprobe virtio_net`, `eth0` up, and address 169.254.A.B/16 from the
  MAC (printed as `SP16_NET eth0 <mac> <ip>` in `console.txt`). The factory-test fake eth0 (`SP16_FAKE_ETH0`) is now set only
  when there is no real eth0. No `--net`: nothing changes (O: baseline boots, no `SP16_NET` line).
- Deviation from the task text ("let the firmware's Autoip pick"): the guest assigns the address. O: the app dies with
  SIGSEGV (crash handler runs `cp /root/pdj/SIGSEGV.txt ...`, seen in `console.txt`) about 20 s after boot when eth0 is up
  with no IPv4 address; with an address it does not (three runs each). The unit's `rc.local` also sets one before the app
  starts (`ifconfig $netdev $ipaddr netmask $netmask`, boot.md section 4, S).

### Owner commands

`socket_vmnet` is already running in host mode (O: pid 94180, `--vmnet-gateway=192.168.105.1`, socket
`/opt/homebrew/var/run/socket_vmnet`, `bridge100` at 192.168.105.1; QEMU connected to it from this lane without root).
For `--net bridged:en0` start a second, bridged daemon (root, needs the Mac's network permission prompt):

```bash
sudo /opt/homebrew/opt/socket_vmnet/bin/socket_vmnet --vmnet-mode=bridged --vmnet-interface=en0 /opt/homebrew/var/run/socket_vmnet.bridged.en0
```

The acceptance capture needs root too (this lane has no BPF access):

```bash
sudo tcpdump -n -i bridge100 -w /tmp/sp16-seg.pcap 'udp portrange 50000-50002'   # then open it with the nudge dissectors
```

### Not met: no keep-alive on the segment

Acceptance asks for the 54-byte `TORAIZ` keep-alive every ~2 s. It does not appear (O): in `--net mcast` and `--net host`
runs of 60-120 s the guest transmits only kernel frames, and `/proc/net/udp` in the guest stays empty, so the app never
binds UDP 50000-50002. What the app does with the network (strace build, S/O):

- it opens a netlink route socket at start (also without a NIC) and, once eth0 has an address, reads eth0's MAC with
  `SIOCGIFHWADDR` (one `socket(PF_INET)` + ioctl, O); it creates no PF_PACKET socket, binds no INET socket, sends no ARP;
- the binary carries `Autoip`, `NetworkManager`, `ProDjLinkCallback` and `LinkClockInterpreter` classes, an `udhcpc -i eth0`
  step (three tries, 1, 2 and 4 s) and a `169.254.` ARP-probe path (S, `strings`); no `udhcpc` ran in the guest (the stub
  blocks helper execs but none was logged for it, O), so that path never started;
- a link bounce 40 s after boot (netlink link event) changed nothing (O);
- SYNC SOURCE SELECT (UTILITY > MACHINE SETTING) cannot be moved to PRO DJ LINK: turning the selector from INTERNAL stops at
  USB MIDI (O, screenshots `net8.png` .. `net11.png` in the session folder). I (not verified): the option is gated on
  something the network manager reports (a LAN link or a Pro DJ Link partner).

Next steps for whoever continues (U until tried): find the caller of the `### AUTOIP MODULE STARTED ###` string in
`EP085_Main_R` (Ghidra/Hopper; Hopper was not authorized to be driven here) and the condition before it; the firmware's
`rc.local` also runs `apl_start`, whose environment the guest does not reproduce (`fw_printenv netdev/ipaddr/netmask`,
S); `hardware_stub.c` (held) may need an ethtool/MII link answer for eth0.

### Cross-lane requests

1. `emulator/Dockerfile.native-base` (held): the module cull deletes `virtio_net`; keep it and its dependencies, and
   require it in the check loop. Without this the guest prints `SP16_NET no eth0` and runs as before (O). Patch (also saved in the
   notes folder as `l4-dockerfile-virtio-net.patch`; the build used for every test above had it applied):

```diff
-  ! -name 'virtio_blk.ko*' ! -path '*/fs/fat/*' ! -path '*/fs/nls/*' \
+  ! -name 'virtio_blk.ko*' ! -name 'virtio_net.ko*' ! -name 'net_failover.ko*' ! -name 'failover.ko*' ! -path '*/fs/fat/*' ! -path '*/fs/nls/*' \
...
-    sp16_need='virtio_blk vfat dm_crypt isofs squashfs overlay 9p 9pnet_virtio'; \
+    sp16_need='virtio_blk virtio_net vfat dm_crypt isofs squashfs overlay 9p 9pnet_virtio'; \
```

2. Optional (Swift, held): a native `--net` flag in `native-window.swift` that appends the `-netdev`/`-device` pair itself
   would replace the `qemu-net.sh` wrapper (the Swift app already owns the argument list and the `--usb-image` plumbing).
3. `README.md` / `docs/` (held): a line on `./sp16 run --net` once the keep-alive works.

## pt-i44.39 - follow a fake tempo master (parked)

`emulator/net/fake_master.py` is ready (keep-alive, beat 0x28 on 50001, status 0x0a on 50002 with master/sync/play flags;
checked against python-prodj-link's parser). Run against the host segment it was received by a host listener on UDP
50000-50002 (O), but the guest never answered and, with no sockets open in the app, nothing could follow it: no 130.56 BPM
or downbeat measurement was possible. Parked with the evidence above; the packet facts the app needs (does it need status
packets or a player number?) stay U.

## Checks and side effects

- Quick set + `test_sp16_net`: 366 tests OK (then 11 in `test_sp16_net` after the fake-master tests); `test_sp16_native` +
  `test_sp16_cli`: 141 tests OK. The full suite was not run (owner rule).
- Builds made: `build/l4` (research, audio file) and `build/l4t` (research, strace); both in the git-ignored `build/`. All
  emulator processes started by this lane (exact pids, `kill`, then `kill -9` for an app that ignored it) are stopped.
- Temporary: a local edit of `Dockerfile.native-base` was used for the builds and reverted before committing.
- Not run: `--net bridged:en0` (no bridged daemon; no sudo), the acceptance `tcpdump` (no BPF access), Wireshark dissector
  decoding (no Wireshark); `segment_watch.py` decodes the same fields for mcast segments.
