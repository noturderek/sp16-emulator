#!/usr/bin/env python3
"""A fake Pro DJ Link tempo master that lives on an `--net mcast` segment (no root, stdlib only).

`net/fake_master.py` sends its keep-alive/beat/status packets through an interface's own sockets; it cannot reach the
emulator's guest on the isolated mcast segment. This driver uses the same packet layouts but transports them as
Ethernet frames onto the segment (net/segment.py), exactly where net/segment_relay.py picks frames up for the guest
and where net/segment_watch.py can decode them. It also reads frames back off the segment, so it can confirm the
guest's 54-byte keep-alive arrived (the SP-16's acceptance of a master).

  segment_master.py --group 230.0.0.1 --port 34567 --bpm 128 --pitch 2 [--duration 60] [--log beats.txt]

The master announces on 169.254.7.7, broadcasts to 169.254.255.255 (the link-local subnet the guest uses), and prints
each guest keep-alive it sees so acceptance is observable without root or tcpdump.
"""
import argparse
import signal
import socket
import struct
import time

import fake_master
import segment

MAGIC = fake_master.MAGIC
GROUP, PORT = "230.0.0.1", 34567
MASTER_IP, BROADCAST_IP, MASTER_MAC = "169.254.7.7", "169.254.255.255", "02:00:00:00:00:07"


def join_segment(group, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    sock.bind(("", port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(group) + socket.inet_aton("0.0.0.0"))
    return sock


def send_packet(seg, group, port, src_mac, src_ip, dst_ip, dst_port, payload):
    frame = segment.udp_frame(src_mac, "ff:ff:ff:ff:ff:ff", src_ip, dst_ip, dst_port, dst_port, payload)
    seg.sendto(frame, (group, port))


def frame_name(parsed):
    """The device name in one parsed segment frame, decoded at its packet family's offset."""
    return fake_master.packet_name(parsed["payload"], parsed["dst_port"])


def keepalive_line(seen, parsed):
    """The line printed for one guest keep-alive: its source, its type byte and its decoded name."""
    return (f"guest keep-alive {seen}: {parsed['src_mac']} {parsed['src_ip']} "
            f"type {parsed['payload'][10]:#04x} name {frame_name(parsed)!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", default=GROUP)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--bpm", type=float, default=128.0)
    parser.add_argument("--pitch", type=float, default=0.0, help="pitch in percent (+2 = 2 %% fast)")
    parser.add_argument("--player", type=int, default=1)
    parser.add_argument("--name", default="CDJ-3000")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds")
    parser.add_argument("--change-after", type=float, help="seconds until the tempo changes to --change-to")
    parser.add_argument("--change-to", type=float, help="new BPM for --change-after")
    parser.add_argument("--log", help="write 'beat <monotonic-ns> <beat-in-bar>' lines here")
    args = parser.parse_args(argv)

    seg = join_segment(args.group, args.port)
    seg.settimeout(0.2)
    mac = MASTER_MAC
    mac_bytes = bytes.fromhex(MASTER_MAC.replace(":", ""))
    ip_bytes = socket.inet_aton(MASTER_IP)
    pitch = args.pitch / 100.0
    state = {"bpm": args.bpm}
    signal.signal(signal.SIGUSR1, lambda *_: state.update(bpm=state["bpm"] + 4))
    signal.signal(signal.SIGUSR2, lambda *_: state.update(bpm=state["bpm"] - 4))
    log = open(args.log, "w", buffering=1) if args.log else None

    def send(dst_port, payload):
        send_packet(seg, args.group, args.port, mac, MASTER_IP, BROADCAST_IP, dst_port, payload)

    print(f"segment master {args.name} player {args.player} on {args.group}:{args.port} at {MASTER_IP} -> "
          f"{BROADCAST_IP}, {args.bpm} BPM {args.pitch:+} % (effective {args.bpm * (1 + pitch):.2f})", flush=True)
    for stage in (1, 2, 3):
        send(fake_master.PORT_KEEPALIVE, MAGIC + b"\x0a\x00" + fake_master.name_field(args.name) + b"\x01\x02\x00\x25\x01")
        time.sleep(0.3)
    for stage in (1, 2, 3):
        send(fake_master.PORT_KEEPALIVE, MAGIC + b"\x04\x00" + fake_master.name_field(args.name) + b"\x01\x02\x00\x26" +
             bytes([args.player, stage]))
        time.sleep(0.3)
    start = time.monotonic()
    next_keepalive = next_status = next_beat = start
    beat_count, beat_in_bar, changed, status_count = 1, 1, False, 0
    seen = 0
    while not args.duration or time.monotonic() - start < args.duration:
        now = time.monotonic()
        if args.change_after is not None and not changed and now - start >= args.change_after:
            state["bpm"], changed = args.change_to, True
            print(f"tempo change to {state['bpm']} BPM", flush=True)
        effective = state["bpm"] * (1 + pitch)
        beat_s = 60.0 / effective
        if now >= next_keepalive:
            send(fake_master.PORT_KEEPALIVE, fake_master.keepalive(args.name, args.player, mac_bytes, ip_bytes))
            next_keepalive += 1.5
        if now >= next_status:
            send(fake_master.PORT_STATUS, fake_master.status_packet(args.name, args.player, state["bpm"], pitch,
                                                                    beat_in_bar, beat_count, True, status_count))
            status_count += 1
            next_status += 0.2
        if now >= next_beat:
            send(fake_master.PORT_BEAT, fake_master.beat_packet(args.name, args.player, state["bpm"], pitch,
                                                                beat_in_bar, beat_s * 1000))
            if log:
                log.write(f"beat {time.monotonic_ns()} {beat_in_bar}\n")
            beat_count += 1
            beat_in_bar = beat_in_bar % 4 + 1
            next_beat += beat_s
        # Read frames back: log each guest keep-alive so acceptance is observable here.
        try:
            frame, _ = seg.recvfrom(65535)
        except socket.timeout:
            frame = None
        except OSError:
            frame = None
        if frame:
            try:
                parsed = segment.parse_frame(frame)
            except KeyError:
                continue
            if parsed["src_mac"] == mac:
                continue  # our own frames echoed back by the segment's multicast loopback
            if parsed["dst_port"] == fake_master.PORT_KEEPALIVE and parsed["payload"][:10] == MAGIC:
                seen += 1
                if seen <= 12 or seen % 10 == 0:
                    print(keepalive_line(seen, parsed), flush=True)
        time.sleep(max(0.0, min(next_keepalive, next_status, next_beat) - time.monotonic()))
    if log:
        log.close()


if __name__ == "__main__":
    main()
