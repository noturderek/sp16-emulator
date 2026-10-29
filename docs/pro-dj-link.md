# Pro DJ Link

The emulated sampler can join a Pro DJ Link network, publish its keep-alive and follow a tempo master. This document
covers the network modes, the host-side segment the emulator uses, the tools that fake and watch a segment, and how far
each part of the feature goes.

Grades: **O** observed in this emulator, **S** static, **I** inferred, **U** unknown. Nothing here was measured on a
unit, and no device identity, MAC or IP of a real unit appears in this repository.

## Network modes

`./sp16 run --net MODE` (`--net-ip ADDR[/PREFIX]` for a static address) gives the guest a virtio-net NIC:

| Mode | What it connects to | Needs |
|---|---|---|
| `none` (default) | no NIC | — |
| `host` | the `socket_vmnet` daemon's shared network (`bridge100`) | the root `socket_vmnet` daemon running |
| `bridged:IF` | a physical interface (for example `bridged:en0`), optionally with `--net-ip` | the daemon's bridged instance for that interface |
| `mcast[:PORT]` | a local multicast segment, `230.0.0.1:34567` by default | nothing, no root |

`host` and `bridged:IF` are the modes that can reach real hardware on your LAN. `mcast` links emulators and host tools
on this Mac only, which is what the tests and the fake master below use.

The two daemon modes need `socket_vmnet` (`brew install socket_vmnet`) with its daemon running as root: the host one
listens on `/opt/homebrew/var/run/socket_vmnet` (or `/usr/local/var/run` on Intel), and a bridged one per interface on
`/opt/homebrew/var/run/socket_vmnet.bridged.<if>`. Start the bridged daemon yourself, for example:

```sh
sudo /opt/homebrew/opt/socket_vmnet/bin/socket_vmnet --vmnet-mode=bridged --vmnet-interface=en0 \
  /opt/homebrew/var/run/socket_vmnet.bridged.en0
```

`--net host` and `--net bridged:IF` say which socket is missing when the daemon is not up. Each start takes a random locally
administered MAC (`52:54:00:…`), so two instances never share one and none is a real unit's.

The guest assigns itself a link-local `169.254.A.B` address on its own. The shim answers the firmware's GLINK carrier
query with a fixed carrier=1 and passes the firmware's real ARP packets to the NIC, so the app believes the link is up
even with no other peer present.

A build made before this version has no network wrapper: `--net` on such a build says to rebuild it (`./sp16 build
--rebuild`).

## The segment relay

`--net mcast` does not use QEMU's own `socket,mcast` backend. That backend binds its UDP socket to the multicast group
address, and on macOS a datagram sent that way is never delivered to another local listener that joined the group — so
the guest's keep-alive never reached a watcher on the same Mac.

Instead the build's wrapper (`emulator/net/qemu-net.sh`) starts a host-side relay, `emulator/net/segment_relay.py`, and
attaches QEMU to it over a unix `stream` with the framing socket_vmnet speaks (a 4-byte big-endian length, then the raw
Ethernet frame). The relay:

- joins the multicast group on a wildcard-bound socket, the setup that does deliver on macOS, and sends the guest's
  frames to `group:port`;
- hands frames arriving from other relays (or any host process) back to the guest;
- drops its own multicast echo by matching the frame's source MAC;
- tolerates transient multicast errors and reports the traffic it forwards.

If the relay's socket never appears the wrapper exits 2. `SP16_NET_NO_RELAY=1` leaves the relay out (for an
already-running relay named by `SP16_NET_SOCKET`), and `SP16_NET_SOCKET` overrides the socket path.

## Tools

All of them are stdlib-only Python under `emulator/net/`, and none needs root.

| Tool | What it does |
|---|---|
| `segment_watch.py [--port 34567] [--group G] [--seconds 10] [--pcap FILE]` | Prints the Pro DJ Link traffic on the `--net mcast` segment: ARP, and UDP 50000–50002 with the Pro DJ Link magic (keep-alive type, name, MAC, IP). `--pcap` writes a file Wireshark opens, with hints for its dissectors |
| `segment_master.py [--group G] [--port P] [--bpm 128] [--pitch 2] [--player 1] [--name CDJ-3000] [--duration S] [--change-after S --change-to BPM] [--log FILE]` | A fake tempo master that lives **on** the segment: keep-alive, beat and player-status frames as Ethernet frames, exactly where the relay picks them up. It reads frames back, so each guest keep-alive it sees is printed as proof the app accepted it. `--log` writes `beat <monotonic-ns> <beat-in-bar>` lines for phase measurement |
| `fake_master.py --iface IF \| --bind-ip ADDR [--broadcast ADDR] [--bpm] [--pitch] [--player] [--name] [--duration] [--change-after/--change-to] [--log]` | The same packets through an interface's own sockets, for `host`/`bridged` runs and real hardware. SIGUSR1/SIGUSR2 change the tempo by ±4 BPM while it runs |
| `timing_probe.py run --net mcast --build NAME [--master-bpm 128] [--master-pitch 2] …` | A follow run end to end: starts the segment master, launches its own `--net mcast` session, sets the firmware's sync source with the `sync-source-pdl` recipe, plays, and reports the jitter and panel-frame measurements plus a `follow` block (master effective BPM, measured tempo and error, downbeat phase) |

The packet layouts follow the public Pro DJ Link write-up. Names are decoded at the offset of their packet family: the
54-byte keep-alive and the other port-50000 frames carry the name at 0x0c, the port-50001/50002 beat and status frames
at 0x0b (`fake_master.packet_name`).

## Following a master

1. Start the emulator on a segment: `./sp16 run --name NAME --net mcast`.
2. Start the master on the same segment: `python3 emulator/net/segment_master.py --bpm 128 --pitch 2 --duration 120`.
3. Set the firmware's sync source with the menu recipe from [MIDI](midi.md#firmware-menu-settings):
   `python3 emulator/menu/sync_source.py --build NAME sync-source-pdl`. The HOME header gains `LINK` once the app has
   accepted the master.
4. Press PLAY. `segment_master.py` prints each keep-alive it receives from the guest.

All four steps in one command:

```sh
python3 emulator/timing_probe.py run --build NAME --net mcast --master-bpm 128 --master-pitch 2 --seconds 60
```

### State

- **Real traffic, both ways (O).** The guest binds UDP 50000, 50002 and 50001 and emits its 54-byte keep-alive
  (type `0x06`, name `TORAIZ`) about every 2 s; the relay carries it to `segment_watch.py` and to `segment_master.py`,
  which reads it back.
- **Tempo follows (O).** With SYNC SOURCE = PRO DJ LINK the sequencer runs at the master's effective tempo: 130.56 BPM
  against a master at 128 BPM +2 %, `tempo_error_bpm` 0.0. The gate was the player-status packet's play state: the
  status frame's `P3` byte must say the player is loaded and playing forward, and the master-meaningful byte and the
  rekordbox `Mv` multiplier must be set (`emulator/net/fake_master.py`).
- **Downbeat does not align (not finished).** The phase is a constant **~495 ms** off over 33 downbeats in a 60 s
  window (median 495.256 ms, maximum 495.277 ms), so the 5 ms acceptance is unmet. The remaining question is the beat
  packet's phase semantics (`nextBeat`, `Bb`). It is also unconfirmed whether the figure is a firmware phase gap or an
  artifact of the probe taking every 16th click onset, which need not start at step 1.
- **Linking with an emulated CDJ-3000 (not attempted).** It depends on the downbeat working, so it was left parked.

## The sync bridge

`emulator/sync/bridge.py` reads the master's beat and status packets straight off the segment and drives two
followers, independently of whether the emulator's own follower is engaged:

```sh
python3 emulator/sync/bridge.py --clock-out "IAC Driver Bus 1"          # MIDI clock only
python3 emulator/sync/bridge.py --clock-out "IAC Driver Bus 1" --link   # and Ableton Link
python3 emulator/sync/bridge.py --duration 60                           # --link only, no clock destination
```

- **MIDI clock (works, unit-tested).** The master's effective BPM (base BPM times the actual pitch) retunes a 24 PPQN
  0xF8 stream on a background thread; a tempo change lands on the next pulse without restarting the thread. The CoreMIDI
  sink is a thin adapter, so the packet → tempo → clock pipeline is tested without CoreMIDI.
- **Ableton Link (not tested live).** `--link` drives a minimal Carabiner client on TCP `127.0.0.1:17000`: `set-tempo`
  on a tempo change and `force-beat-at-time` on every downbeat (beat 0 of the next bar). Carabiner is a design
  reference the user runs themselves; it is not bundled, and no live acceptance was run (the Mac used had no
  Carabiner listening). With no Carabiner the bridge prints one diagnostic per outage
  (`sync: no Carabiner listening on 127.0.0.1:17000 …; Link tempo/phase sync is off`) and keeps the MIDI clock running;
  a later outage after a recovery warns once more.

The bridge is stdlib only and reads the segment directly, so it also works against a real master on a bridged or host
network as long as the packets reach it.

## Diagnosing the network

A build with `--research --trace gdb` and a NIC keeps the shim's network diagnostics on: `SP16_NET_DIAG=1` makes the
shim print one line per address query, ioctl, bind, close, socket option and `sendto` with its numeric result, errno
and port — never an address, a MAC or a payload. Ordinary sessions keep it off.

The diagnostics themselves show up on the guest console, which is what makes a "the app binds but the segment sees
nothing" question answerable: if the guest logs `sendto` returning 54 bytes and the relay or `segment_watch.py` sees no
frame, the loss is host-side. `emulator/net/diag/carrier.py` is an optional LLDB probe script for the same session (it
stops at the firmware's GLINK, autoconfiguration and socket routines, and its `force_up` switch only overrides a return
register for observation — it is not validation of anything).

## Not there

- Beat/DJ-link features beyond tempo: track info, cue and seek messages, and the on-screen link view are not emulated
  or verified.
- The pass-through of real Pro DJ Link traffic between the guest and a physical network is untested on `host` and
  `bridged` after the relay change; those modes reuse the unchanged socket_vmnet path.
- No emulator-to-emulator link is tested beyond one guest and one fake master on the same `mcast` segment.
