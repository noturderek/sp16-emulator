#!/usr/bin/env python3
"""A fake Pro DJ Link tempo master for testing the emulator's SYNC SOURCE = PRO DJ LINK.

It announces itself as a CDJ (keep-alive stages and then one keep-alive every 1.5 s on UDP 50000), broadcasts a beat
packet on UDP 50001 on every beat and a player status packet on UDP 50002 about every 200 ms, with the master flag set.
Stdlib only. The packet layouts follow the public protocol write-up (Deep Symmetry's dysentery / beat-link
documentation); sniff and decode with `tcpdump -w` and the nudge Wireshark dissectors.

  fake_master.py --iface bridge100 --bpm 128 --pitch 2        # master at 128 BPM, +2 % (effective 130.56)
  fake_master.py --bind-ip 169.254.7.7 --broadcast 169.254.255.255 --bpm 120

Without --bind-ip the address of --iface is used (ifconfig). Change the tempo while it runs by sending SIGUSR1 (BPM up
by 4) or SIGUSR2 (down by 4); `--change-after SECONDS --change-to BPM` does one change by itself.
"""
import argparse
import ipaddress
import signal
import socket
import struct
import subprocess
import time

MAGIC = b"Qspt1WmJOL"
PORT_KEEPALIVE, PORT_BEAT, PORT_STATUS = 50000, 50001, 50002
NAME_LENGTH = 20
NAME_OFFSET_KEEPALIVE, NAME_OFFSET = 0x0c, 0x0b


def name_field(name):
    return name.encode("ascii")[:NAME_LENGTH].ljust(NAME_LENGTH, b"\0")


def name_offset(payload, port=None):
    """Where the 20-byte device name starts in a Pro DJ Link payload (the magic is 10 bytes, the type byte 0x0a).

    Port-50000 packets (the 54-byte keep-alive, the join stages and the other 50000 types) carry one reserved zero
    byte at 0x0b, so their name starts at 0x0c; the port-50001/50002 packets (beat 0x28, status 0x0a) put the name
    at 0x0b. A payload whose port is not one of the three is placed by shape: a 54-byte type-0x06 keep-alive has the
    50000 layout, anything else the 0x0b one.
    """
    if len(payload) < NAME_OFFSET:
        raise ValueError(f"a Pro DJ Link payload carries a type byte at {NAME_OFFSET:#04x}, not {len(payload)} bytes")
    if port == PORT_KEEPALIVE:
        return NAME_OFFSET_KEEPALIVE
    if port in (PORT_BEAT, PORT_STATUS):
        return NAME_OFFSET
    if len(payload) == 54 and payload[0x0a] == 0x06:
        return NAME_OFFSET_KEEPALIVE
    return NAME_OFFSET


def packet_name(payload, port=None):
    """The device name of a Pro DJ Link payload, read at its packet family's offset (see name_offset)."""
    offset = name_offset(payload, port)
    if len(payload) < offset + NAME_LENGTH:
        raise ValueError(f"a {len(payload)}-byte payload has no {NAME_LENGTH}-byte name at {offset:#04x}")
    return payload[offset:offset + NAME_LENGTH].split(b"\0")[0].decode("ascii", "replace")


def keepalive(name, player, mac, ip):
    """54-byte standard keep-alive (type 0x06): what a CDJ broadcasts every 1.5 s once it has a player number."""
    packet = bytearray(MAGIC + b"\x06\x00" + name_field(name) + b"\x01\x02\x00\x36")
    packet += bytes([player, 1]) + mac + ip + bytes([2, 0, 0, 0, 1, 0x64])
    assert len(packet) == 54, len(packet)
    return bytes(packet)


def beat_packet(name, player, bpm, pitch, beat_in_bar, next_beat_ms):
    """Beat packet (type 0x28, 96 bytes): distances to the next beats and bars, pitch (0x100000 = 0 %), BPM x 100."""
    effective = bpm * (1 + pitch)
    beat_ms = 60000.0 / effective
    to_bar = next_beat_ms + beat_ms * ((4 - beat_in_bar) % 4)
    distances = [next_beat_ms, next_beat_ms + beat_ms, to_bar, next_beat_ms + 3 * beat_ms, to_bar + 4 * beat_ms,
                 next_beat_ms + 7 * beat_ms]
    packet = bytearray(MAGIC + b"\x28" + name_field(name) + struct.pack(">HBBB", 256, player, 0, 0x3c))
    packet += struct.pack(">6I", *(int(round(d)) for d in distances)) + bytes(24)
    packet += struct.pack(">IHHBHB", int(round((1 + pitch) * 0x100000)), 0, int(round(bpm * 100)), beat_in_bar, 0, player)
    # The trailing 12 bytes end 0x5d=0x00, 0x5e=0x00, 0x5f=player: the redundant device number the follower's beat
    # handler reads at byte 0x5f and requires to be 1-4 and equal to the followed master.
    return bytes(packet)


def status_packet(name, player, bpm, pitch, beat_in_bar, beat_count, playing=True, counter=0, *, master=True):
    """Player status (type 0x0a, 212 bytes) with the fields a follower needs: the P3 play state (0x9d, 0x0d while
    playing so the follower's (P3 & 1) || (P3 & 8) gate passes), the master-meaningful and Mv multiplier bytes,
    playing/master/sync flags, pitch, BPM, beat counter and beat in bar (offsets from the public protocol write-up;
    see the header of this file)."""
    packet = bytearray(212)
    packet[0:10] = MAGIC
    packet[0x0a] = 0x0a
    packet[0x0b:0x1f] = name_field(name)
    packet[0x1f], packet[0x20], packet[0x21] = 1, 4, player
    struct.pack_into(">H", packet, 0x22, 0xf8)
    packet[0x24] = player
    packet[0x27] = 1 if playing else 0
    packet[0x28] = player                                  # loaded player: own number
    packet[0x7b] = 3 if playing else 5                     # play state: playing / stopped
    packet[0x89] = 0x84 | (0x40 if playing else 0) | (0x20 if master else 0) | 0x10 | 0x08   # play, master, sync, on-air
    struct.pack_into(">I", packet, 0x8c, int(round((1 + pitch) * 0x100000)))
    struct.pack_into(">H", packet, 0x90, 0x8000)          # Mv: rekordbox-track BPM multiplier (not the 0xffff sentinel)
    struct.pack_into(">H", packet, 0x92, int(round(bpm * 100)))
    struct.pack_into(">I", packet, 0x98, int(round((1 + pitch) * 0x100000)))
    packet[0x9d] = 0x0d if playing else 0x00              # P3: playing forward (CDJ) / no track loaded
    packet[0x9e] = 0x01                                   # Mm: master meaningful
    packet[0x9f] = 0xff                                    # no master hand-off pending
    struct.pack_into(">I", packet, 0xa0, beat_count)
    packet[0xa6] = beat_in_bar
    struct.pack_into(">I", packet, 0xc8, counter)
    packet[0xcc] = 0x1f
    return bytes(packet)


def interface_address(iface):
    out = subprocess.run(["ifconfig", iface], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["inet"]:
            return parts[1], parts[parts.index("netmask") + 1] if "netmask" in parts else None
    raise SystemExit(f"{iface} has no IPv4 address")


def interface_mac(iface):
    out = subprocess.run(["ifconfig", iface], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["ether"]:
            return bytes(int(x, 16) for x in parts[1].split(":"))
    return bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x07])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iface", help="interface to announce on (its address and MAC are used)")
    parser.add_argument("--bind-ip", help="this master's IP address (default: --iface's)")
    parser.add_argument("--broadcast", help="broadcast address (default: derived from --iface's netmask, or 255.255.255.255)")
    parser.add_argument("--bpm", type=float, default=128.0)
    parser.add_argument("--pitch", type=float, default=0.0, help="pitch in percent (+2 = 2 %% fast)")
    parser.add_argument("--player", type=int, default=1)
    parser.add_argument("--name", default="CDJ-3000")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (default: run until ^C)")
    parser.add_argument("--change-after", type=float, help="seconds until the tempo changes to --change-to")
    parser.add_argument("--change-to", type=float, help="new BPM for --change-after")
    parser.add_argument("--log", help="write 'beat <monotonic-ns> <beat-in-bar>' lines here (for measuring phase error)")
    args = parser.parse_args()

    ip, mask = (args.bind_ip, None) if args.bind_ip else interface_address(args.iface or "lo0")
    if args.broadcast:
        broadcast = args.broadcast
    elif mask and mask.startswith("0x"):
        broadcast = str(ipaddress.IPv4Network(f"{ip}/{ipaddress.IPv4Address(int(mask, 16))}", strict=False).broadcast_address)
    else:
        broadcast = "255.255.255.255"
    mac = interface_mac(args.iface) if args.iface else bytes([0x02, 0, 0, 0, 0, 0x07])
    ip_bytes = socket.inet_aton(ip)

    sockets = {}
    for port in (PORT_KEEPALIVE, PORT_BEAT, PORT_STATUS):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind((ip, port))
        sockets[port] = s

    state = {"bpm": args.bpm}
    signal.signal(signal.SIGUSR1, lambda *_: state.update(bpm=state["bpm"] + 4))
    signal.signal(signal.SIGUSR2, lambda *_: state.update(bpm=state["bpm"] - 4))
    pitch = args.pitch / 100.0
    log = open(args.log, "w", buffering=1) if args.log else None

    def send(port, data):
        sockets[port].sendto(data, (broadcast, port))

    print(f"fake master {args.name} player {args.player} at {ip} -> {broadcast}, {args.bpm} BPM {args.pitch:+} % "
          f"(effective {args.bpm * (1 + pitch):.2f})", flush=True)
    # Join: three hello, three number and three mac/ip stages, then the 1.5 s keep-alive.
    for stage in (1, 2, 3):
        send(PORT_KEEPALIVE, MAGIC + b"\x0a\x00" + name_field(args.name) + b"\x01\x02\x00\x25\x01")
        time.sleep(0.3)
    for stage in (1, 2, 3):
        send(PORT_KEEPALIVE, MAGIC + b"\x04\x00" + name_field(args.name) + b"\x01\x02\x00\x26" + bytes([args.player, stage]))
        time.sleep(0.3)
    start = time.monotonic()
    next_keepalive = next_status = next_beat = start
    beat_count, beat_in_bar, changed, status_count = 1, 1, False, 0
    while not args.duration or time.monotonic() - start < args.duration:
        now = time.monotonic()
        if args.change_after is not None and not changed and now - start >= args.change_after:
            state["bpm"], changed = args.change_to, True
            print(f"tempo change to {state['bpm']} BPM", flush=True)
        effective = state["bpm"] * (1 + pitch)
        beat_s = 60.0 / effective
        if now >= next_keepalive:
            send(PORT_KEEPALIVE, keepalive(args.name, args.player, mac, ip_bytes))
            next_keepalive += 1.5
        if now >= next_status:
            send(PORT_STATUS, status_packet(args.name, args.player, state["bpm"], pitch, beat_in_bar, beat_count,
                                            playing=True, counter=status_count, master=True))
            status_count += 1
            next_status += 0.2
        if now >= next_beat:
            send(PORT_BEAT, beat_packet(args.name, args.player, state["bpm"], pitch, beat_in_bar, beat_s * 1000))
            if log:
                log.write(f"beat {time.monotonic_ns()} {beat_in_bar}\n")
            beat_count += 1
            beat_in_bar = beat_in_bar % 4 + 1
            next_beat += beat_s
        time.sleep(max(0.0, min(next_keepalive, next_status, next_beat) - time.monotonic()))


if __name__ == "__main__":
    main()
