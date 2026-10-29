"""List the Pro DJ Link devices a real network carries, and say whether an emulator could follow its master.

Listens on UDP 50000 (keep-alives), 50001 (beats) and 50002 (status) on every interface for a few seconds and prints
one line per device: name, player number, which packets it sent, and from its beat packets the base BPM, pitch and
effective BPM. An emulator started with `--net bridged:IF` shows up as `TORAIZ`. Stdlib only, no root:

    python3 emulator/net/pdl_scan.py                  # 6 s scan
    python3 emulator/net/pdl_scan.py --seconds 15

Status packets are usually sent straight to each announced player rather than broadcast, so a host scan often sees
none; the follower's checks that depend on them (the master flag, the play state) are listed as hints instead.
"""
from __future__ import annotations

import argparse
import select
import socket
import struct
import sys
import time

MAGIC = b"Qspt1WmJOL"
PORTS = (50000, 50001, 50002)
EMULATOR_NAME = "TORAIZ"


def decode(port, data):
    """One Pro DJ Link datagram -> a dict (port, kind, name, and the fields this tool reports), or None.

    The name sits at 0x0c in port-50000 packets and at 0x0b in the 50001/50002 ones. Beat packets (type 0x28) carry
    the pitch at 0x54 (0x100000 = +0 %), the BPM x 100 at 0x5a, the beat-in-bar at 0x5c and the player number at 0x21;
    keep-alives (type 0x06) carry it at 0x24; status packets (type 0x0a) carry it at 0x24, the flags at 0x89 (0x20 =
    tempo master) and the play state P3 at 0x9d."""
    if len(data) < 0x20 or not data.startswith(MAGIC):
        return None
    kind = data[10]
    offset = 0x0c if port == 50000 else 0x0b
    name = data[offset:offset + 20].split(b"\0")[0].decode("ascii", "replace").strip()
    out = {"port": port, "kind": kind, "name": name}
    if port == 50000 and kind == 0x06 and len(data) > 0x24:
        out["player"] = data[0x24]
    elif port == 50001 and kind == 0x28 and len(data) >= 0x5d:
        pitch = struct.unpack(">I", data[0x54:0x58])[0] / 0x100000
        bpm = struct.unpack(">H", data[0x5a:0x5c])[0] / 100
        out.update(player=data[0x21], bpm=bpm, pitch=pitch, effective=bpm * pitch, beat_in_bar=data[0x5c])
    elif port == 50002 and kind == 0x0a and len(data) > 0x9d:
        out.update(player=data[0x24], master=bool(data[0x89] & 0x20), play_state=data[0x9d])
    return out


def summarise(packets):
    """Per device (name and player number, so two players of one model stay apart): the packet kinds seen and the
    latest beat/status fields. A packet without a player number is filed under the name alone."""
    devices = {}
    for p in packets:
        key = p["name"] if "player" not in p else f'{p["name"]} #{p["player"]}'
        d = devices.setdefault(key, {"kinds": set(), "name": p["name"]})
        d["kinds"].add({50000: "keep-alive", 50001: "beat", 50002: "status"}[p["port"]])
        for key in ("player", "bpm", "pitch", "effective", "beat_in_bar", "master", "play_state"):
            if key in p:
                d[key] = p[key]
    return devices


def hints(devices):
    """Plain-language checks for following a master (from the 1.60 follower's gates)."""
    out = []
    players = {n: d for n, d in devices.items() if d.get("name", n) != EMULATOR_NAME}
    if not any(d.get("name", n) == EMULATOR_NAME for n, d in devices.items()):
        out.append("no TORAIZ keep-alive: start the emulator with --net bridged:IF on this network")
    if not players:
        out.append("no other Pro DJ Link device seen: check the cable, the adapter and that the player is on")
    for name, d in players.items():
        if "player" in d and not 1 <= d["player"] <= 4:
            out.append(f"{name} is player {d['player']}: the SP-16 follows players 1-4 only")
        if "beat" not in d["kinds"]:
            out.append(f"{name} sends no beat packets: load and play a track on it")
        if d.get("master") is False:
            out.append(f"{name} is not the tempo master: press MASTER on it")
        if "play_state" in d and not d["play_state"] & 0x09:
            out.append(f"{name} has no track loaded and playing: the SP-16 follows a playing master only")
    if players and not any("status" in d["kinds"] for d in players.values()):
        out.append("status packets are sent to each player directly, so this scan cannot see the master flag: if the "
                   "emulator shows LINK but keeps its own BPM, press MASTER on the player and make sure a track is playing")
    return out


def listen(seconds):
    socks = {}
    for port in PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", port))
        socks[s] = port
    packets, end = [], time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select(list(socks), [], [], 0.25)
            for s in ready:
                data, _ = s.recvfrom(2048)
                packet = decode(socks[s], data)
                if packet:
                    packets.append(packet)
    finally:
        for s in socks:
            s.close()
    return packets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=6.0, help="how long to listen (default 6)")
    args = parser.parse_args(argv)
    devices = summarise(listen(args.seconds))
    for _, d in sorted(devices.items()):
        line = f"{d['name'] or '?':<12} player {d.get('player', '?'):<3} {', '.join(sorted(d['kinds']))}"
        if "bpm" in d:
            line += (f"  {d['bpm']:.2f} BPM x pitch {(d['pitch'] - 1) * 100:+.2f} percent = {d['effective']:.2f} BPM,"
                     f" beat {d['beat_in_bar']}")
        if "master" in d:
            line += f"  master {'yes' if d['master'] else 'no'}"
        print(line)
    for hint in hints(devices):
        print("hint:", hint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
