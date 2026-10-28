"""Bridge one emulated DIN UART to CoreMIDI virtual ports.

Run directly with ``python3 -m midi.din --socket PATH`` from emulator/, or let
native.py launch it for the newest session. The QEMU socket carries raw MIDI
bytes in both directions. No firmware data passes through this process.
"""
import argparse
from collections import deque
from contextlib import suppress
import json
from pathlib import Path
import socket
import sys
import threading
import time

from midi.clock import ClockDLL
from midi_bridge.midi import Port, hex_line

BYTE_NS = 320_000  # 31,250 bit/s, ten bits per MIDI byte
MAX_QUEUE = 8192


class Messages:
    """Assemble UART bytes into CoreMIDI messages, preserving running status and realtime."""

    def __init__(self):
        self.running = None
        self.message = bytearray()
        self.expected = 0
        self.sysex = False

    def feed(self, data):
        completed = []
        for byte in data:
            if byte >= 0xF8:
                completed.append(bytes([byte]))
                continue
            if self.sysex:
                self.message.append(byte)
                if byte == 0xF7:
                    completed.append(bytes(self.message))
                    self.message.clear()
                    self.sysex = False
                elif len(self.message) > 1024:
                    self.message.clear()
                    self.sysex = False
                continue
            if byte == 0xF0:
                self.message = bytearray([byte])
                self.running = None
                self.sysex = True
                continue
            if byte & 0x80:
                if byte >= 0xF0:
                    self.running = None
                    self.expected = {0xF1: 2, 0xF2: 3, 0xF3: 2}.get(byte, 1)
                else:
                    self.running = byte
                    self.expected = 2 if byte & 0xF0 in (0xC0, 0xD0) else 3
                self.message = bytearray([byte])
            else:
                if not self.message and self.running is not None:
                    self.message = bytearray([self.running])
                    self.expected = 2 if self.running & 0xF0 in (0xC0, 0xD0) else 3
                if self.message:
                    self.message.append(byte)
            if self.message and len(self.message) == self.expected:
                completed.append(bytes(self.message))
                self.message.clear()
        return completed


class PacedInput:
    """Serialize CoreMIDI input to the UART, with realtime bytes taking the next free slot."""

    def __init__(self, write, clock=time.monotonic_ns, sleep=time.sleep):
        self.write, self.clock, self.sleep = write, clock, sleep
        self.realtime, self.normal = deque(), deque()
        self.next_ns = 0
        self.dropped = 0
        self.lock = threading.Condition()
        self.closed = False
        self.cancelled = threading.Event()

    def add(self, data, release_ns=None):
        if not data:
            return
        release_ns = self.clock() if release_ns is None else release_ns
        with self.lock:
            if self.closed:
                return
            if len(self.realtime) + len(self.normal) + len(data) > MAX_QUEUE:
                self.dropped += len(data)
                return
            for byte in data:
                (self.realtime if byte >= 0xF8 else self.normal).append((release_ns, byte))
            self.lock.notify()

    def run(self):
        while True:
            with self.lock:
                while not self.realtime and not self.normal and not self.closed:
                    self.lock.wait()
                if not self.realtime and not self.normal:
                    return
                release_ns, byte = (self.realtime if self.realtime else self.normal).popleft()
            now = self.clock()
            deadline = max(now, self.next_ns, release_ns) + BYTE_NS
            delay = (deadline - now) / 1_000_000_000
            if delay > 0:
                self.sleep(delay)
            if self.cancelled.is_set():
                return
            try:
                self.write(bytes([byte]))
            except OSError:
                self.close(discard=True)
                return
            self.next_ns = deadline

    def close(self, discard=False):
        with self.lock:
            self.closed = True
            if discard:
                self.cancelled.set()
                self.normal.clear()
                self.realtime.clear()
            self.lock.notify_all()


def connect(path, timeout=60):
    until = time.monotonic() + timeout
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            if time.monotonic() >= until:
                raise TimeoutError(f"DIN UART socket did not appear: {path}") from None
            time.sleep(0.1)


def session_socket(build, since, timeout=60, suffix=".midi"):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        root = Path(build) / "sessions"
        for runtime_path in sorted(root.glob("*/runtime.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if runtime_path.stat().st_mtime < since:
                continue
            try:
                runtime = json.loads(runtime_path.read_text())
                path = Path(runtime["socket"] + suffix)
            except (OSError, ValueError, KeyError):
                continue
            if path.exists():
                return path
        time.sleep(0.1)
    raise TimeoutError(f"no new MIDI session appeared under {build}")


def bridge(path, build_dir=None, name="SP-16 DIN"):
    """Run until the guest disconnects; cancel pending input before releasing its socket."""
    sock = connect(path)
    writer = PacedInput(sock.sendall)
    worker = threading.Thread(target=writer.run, name="din-midi-input", daemon=True)
    worker.start()
    dll = ClockDLL()

    def received(data):
        stamp = time.monotonic()
        for byte in data:
            if byte == 0xF8:
                dll.tick(stamp)
        writer.add(data, int(dll.release_time(stamp) * 1_000_000_000))

    port = None
    try:
        port = Port(["virtual", name], build_dir=build_dir, on_raw=received, queue_messages=False)
        print(f"MIDI bridge ready: {name}", flush=True)
        messages = Messages()
        while True:
            data = sock.recv(4096)
            if not data:
                break
            for message in messages.feed(data):
                port.process.stdin.write("out " + hex_line(message) + "\n")
                port.process.stdin.flush()
    finally:
        writer.close(discard=True)
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        worker.join(timeout=1)
        if port is not None:
            port.close()
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--socket", type=Path, help="QEMU session socket plus .midi")
    source.add_argument("--wait-for-session", type=Path, help="build directory to watch after --since")
    parser.add_argument("--name", default="SP-16 DIN", help="CoreMIDI endpoint label")
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--build-dir", type=Path, help="helper binary cache")
    args = parser.parse_args(argv)
    path = args.socket or session_socket(args.wait_for_session, args.since)
    bridge(path, args.build_dir, args.name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, TimeoutError) as error:
        print(f"din-midi: {error}", file=sys.stderr)
        sys.exit(1)
