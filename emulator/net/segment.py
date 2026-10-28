"""Ethernet+IPv4+UDP framing for a host process that joins an `--net mcast` segment (no root, stdlib only).

The emulator's mcast segment carries raw Ethernet frames as UDP datagrams to GROUP:PORT (see net/segment_relay.py and
net/segment_watch.py). A host program such as the fake tempo master joins that segment by sending the same framed
datagrams and reading the frames back, instead of using an interface's own sockets. This module builds the smallest
valid IPv4/UDP encapsulation (IP header checksummed; the UDP checksum is zero, which IPv4 permits) and parses frames
back into their fields.

    frame = udp_frame(src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port, payload)
    parsed = parse_frame(frame)   # -> dict(src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port, payload)
"""
from __future__ import annotations

import socket
import struct


def ip_checksum(header: bytes) -> int:
    """The one's-complement 16-bit checksum of an IPv4 header (odd length is zero-padded)."""
    if len(header) % 2:
        header += b"\x00"
    total = sum(struct.unpack(">%dH" % (len(header) // 2), header))
    total = (total & 0xFFFF) + (total >> 16)
    total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def udp_frame(src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port, payload, ttl=64) -> bytes:
    """One Ethernet frame carrying an IPv4/UDP datagram of `payload` (UDP checksum 0 = none, valid for IPv4)."""
    src_mac = bytes.fromhex(src_mac.replace(":", ""))
    dst_mac = bytes.fromhex(dst_mac.replace(":", ""))
    if len(src_mac) != 6 or len(dst_mac) != 6:
        raise ValueError("MACs must be six bytes, e.g. 02:00:00:00:00:07")
    udp_length = 8 + len(payload)
    total_length = 20 + udp_length
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total_length, 0, 0, ttl, 17, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    ip = ip[:10] + struct.pack(">H", ip_checksum(ip)) + ip[12:]
    udp = struct.pack(">HHHH", src_port, dst_port, udp_length, 0) + payload
    return dst_mac + src_mac + b"\x08\x00" + ip + udp


def parse_frame(frame: bytes) -> dict:
    """The fields of an IPv4/UDP Ethernet frame; a KeyError means it is not such a frame."""
    if len(frame) < 14 or frame[12:14] != b"\x08\x00":
        raise KeyError("not an IPv4 frame")
    dst_mac, src_mac = frame[0:6].hex(":"), frame[6:12].hex(":")
    ihl = (frame[14] & 0x0F) * 4
    if len(frame) < 14 + ihl + 8:
        raise KeyError("short IP/UDP frame")
    if frame[14 + 9] != 17:
        raise KeyError("not UDP")
    src_ip = socket.inet_ntoa(frame[14 + 12:14 + 16])
    dst_ip = socket.inet_ntoa(frame[14 + 16:14 + 20])
    src_port, dst_port, udp_length = struct.unpack(">HHH", frame[14 + ihl:14 + ihl + 6])
    payload = frame[14 + ihl + 8:14 + ihl + udp_length]
    return {"src_mac": src_mac, "dst_mac": dst_mac, "src_ip": src_ip, "dst_ip": dst_ip,
            "src_port": src_port, "dst_port": dst_port, "payload": payload}
