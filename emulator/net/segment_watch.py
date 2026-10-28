#!/usr/bin/env python3
"""Print the Pro DJ Link traffic on an emulator `--net mcast[:PORT]` segment (no root needed).

QEMU's socket,mcast backend carries each Ethernet frame as one UDP datagram to 230.0.0.1:PORT; this joins the group and
decodes the frames: ARP, and UDP on 50000-50002 with the Pro DJ Link magic (keep-alive type, name, MAC, IP).
  segment_watch.py [--port 34567] [--seconds 10] [--pcap FILE]   (--pcap writes a file Wireshark opens, with the nudge dissectors)
"""
import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_master

MAGIC = fake_master.MAGIC


def describe(frame):
    if len(frame) < 14:
        return "short frame"
    src, dst = frame[6:12].hex(":"), frame[0:6].hex(":")
    ethertype = struct.unpack(">H", frame[12:14])[0]
    if ethertype == 0x0806:
        return f"ARP {src} -> {dst} sender-ip {socket.inet_ntoa(frame[28:32])} target-ip {socket.inet_ntoa(frame[38:42])}"
    if ethertype != 0x0800:
        return f"ethertype {ethertype:#06x} {src} -> {dst}"
    ihl = (frame[14] & 15) * 4
    proto, ip_src, ip_dst = frame[23], socket.inet_ntoa(frame[26:30]), socket.inet_ntoa(frame[30:34])
    if proto != 17:
        return f"IP proto {proto} {ip_src} -> {ip_dst}"
    sport, dport, length = struct.unpack(">HHH", frame[14 + ihl:14 + ihl + 6])
    payload = frame[14 + ihl + 8:14 + ihl + length]
    text = f"UDP {ip_src}:{sport} -> {ip_dst}:{dport} len {len(payload)}"
    if payload.startswith(MAGIC):
        # The name's offset depends on the packet family: 0x0c on port 50000, 0x0b on 50001/50002.
        text += f" PRODJLINK type {payload[10]:#04x} name {fake_master.packet_name(payload, dport)!r}"
        if dport == 50000 and len(payload) == 54:
            text += f" mac {payload[0x26:0x2c].hex(':')} ip {socket.inet_ntoa(payload[0x2c:0x30])}"
        if dport == 50001 and payload[10] == 0x28 and len(payload) >= 0x5c:
            pitch, _, bpm, beat = struct.unpack(">IHHB", payload[0x54:0x5d])
            text += f" beat {beat} bpm {bpm / 100:.2f} pitch {pitch / 0x100000 - 1:+.2%}"
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=34567)
    parser.add_argument("--group", default="230.0.0.1")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--pcap")
    args = parser.parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", args.port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(args.group) + socket.inet_aton("0.0.0.0"))
    sock.settimeout(0.5)
    pcap = open(args.pcap, "wb") if args.pcap else None
    if pcap:
        pcap.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    end = time.time() + args.seconds
    while time.time() < end:
        try:
            frame, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        now = time.time()
        print(f"{now - (end - args.seconds):8.3f} {describe(frame)}", flush=True)
        if pcap:
            pcap.write(struct.pack("<IIII", int(now), int((now % 1) * 1e6), len(frame), len(frame)) + frame)


if __name__ == "__main__":
    main()
