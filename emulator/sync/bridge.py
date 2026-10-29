"""Run the Pro DJ Link -> MIDI clock (and Ableton Link) bridge on an `--net mcast` segment.

Joins the segment (net/segment.py framing), feeds the master's 0x28 beat and 0x0a status packets to
sync/pdl_sync.py's PdlClockBridge, and emits the resulting MIDI clock (0xF8, 24 PPQN) through a `send_clock(byte)`
sink at the master's effective BPM. When a Carabiner listens on 127.0.0.1:17000, the same bridge also sends it
bpm and force-beat-at-time (the Link half; Carabiner is a design-reference app the user runs themselves).

The bridge reads the master's packets straight off the segment, so it runs independently of whether the emulator's
own follower path is engaged. Stdlib only; the CoreMIDI sink is a thin adapter the caller supplies (e.g.
midi_bridge.midi.Port), so the packet->tempo->clock pipeline is unit-testable without CoreMIDI.

    bridge.py --group 230.0.0.1 --port 34567 --clock-out "IAC Driver Bus 1" [--link] [--duration 60]
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "net"))

import segment  # noqa: E402
import sync.pdl_sync as pdl  # noqa: E402

MAGIC = pdl.MAGIC
PPQN = 24.0


class ClockEmitter:
    """A 24-PPQN 0xF8 stream at the bridge's current effective BPM, on a background thread.

    `send(byte)` is the clock sink (a CoreMIDI destination, or a recorder in tests). `set_bpm` retunes the cadence
    without stopping the thread; the period is re-read every pulse so a tempo change lands on the next tick.
    """

    def __init__(self, send, ppqn: float = PPQN):
        self._send = send
        self._ppqn = ppqn
        self._bpm = 0.0
        self._stop = threading.Event()
        self._thread = None

    @property
    def bpm(self) -> float:
        return self._bpm

    def set_bpm(self, bpm: float) -> None:
        if bpm <= 0:
            raise ValueError("bpm must be positive")
        self._bpm = float(bpm)

    def start(self) -> None:
        """Start the clock thread. A second start while the thread is running is a no-op: a second thread would
        double the pulse rate (48 instead of 24 PPQN). After `stop()` the emitter can be started again, and so it
        can after the worker died on a sink that raised: the guard is liveness, not the presence of a Thread object,
        which stays behind when the worker exits."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._bpm > 0:
                self._send(0xF8)
            period = 60.0 / (self._bpm * self._ppqn) if self._bpm > 0 else 0.05
            self._stop.wait(period)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None


def join_segment(group, port):
    """A UDP socket joined to the mcast segment's group on `port`, wildcard-bound (the setup that receives on macOS)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(group) + socket.inet_aton("0.0.0.0"))
    return sock


def feed(bridge, parsed, t):
    """Feed one parsed segment frame to the bridge when it is a master beat/status packet; returns the parsed packet
    or None."""
    payload = parsed.get("payload", b"")
    if len(payload) < 11 or payload[:10] != MAGIC:
        return None
    if parsed.get("dst_port") == 50001 and payload[10] == 0x28:
        return bridge.feed_beat(payload, t)
    if parsed.get("dst_port") == 50002 and payload[10] == 0x0a:
        return bridge.feed_status(payload, t)
    return None


def run(seg, send_clock, link=None, duration=0.0, now=time.monotonic):
    """Read master packets off `seg`, retune `send_clock`'s emitter and drive `link`, until `duration` elapses (0 =
    forever). Returns the number of master packets fed."""
    bridge = pdl.PdlClockBridge(link=link)
    emitter = ClockEmitter(send_clock)
    emitter.start()
    seg.settimeout(0.2)
    start = now()
    fed = 0
    try:
        while not duration or now() - start < duration:
            try:
                frame, _ = seg.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                continue
            try:
                parsed = segment.parse_frame(frame)
            except KeyError:
                continue
            packet = feed(bridge, parsed, now())
            if packet:
                fed += 1
                emitter.set_bpm(bridge.tracker.effective_bpm)
    finally:
        emitter.stop()
    return fed


def clock_sink(name):
    """A CoreMIDI sink that sends 0xF8 to the named destination (imports the midi_bridge helper lazily)."""
    from midi_bridge.midi import Message, Port
    port = Port(["run", "--out", name])

    def send(byte):
        kind = "clock" if byte == 0xF8 else None
        if kind:
            port.send(Message(kind, None, 0, 0))

    send.close = port.close
    return send


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--group", default="230.0.0.1")
    parser.add_argument("--port", type=int, default=34567)
    parser.add_argument("--clock-out", help="CoreMIDI destination name for the 0xF8 stream (omit to only drive Link)")
    parser.add_argument("--link", action="store_true", help="drive a Carabiner on 127.0.0.1:17000")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds (default: run)")
    args = parser.parse_args(argv)

    seg = join_segment(args.group, args.port)
    send = clock_sink(args.clock_out) if args.clock_out else (lambda byte: None)
    link = pdl.CarabinerLink() if args.link else None
    try:
        fed = run(seg, send, link=link, duration=args.duration)
        print(f"fed {fed} master packets", flush=True)
    finally:
        if link is not None:
            link.close()
        close = getattr(send, "close", None)
        if close:
            close()
        seg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
