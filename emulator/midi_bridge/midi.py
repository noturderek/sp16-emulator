"""MIDI messages, and the CoreMIDI helper process that sends and receives them.

CoreMIDI is reached through coremidi-io.swift, compiled on first use (`swiftc -O`, like the repo's other Swift tools)
into build/midi-bridge/. It speaks one text line per message over stdin/stdout, so the bridge needs no pip package and
every test can substitute a pipe for it.
"""
from collections import namedtuple
import fcntl
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HELPER_SOURCE = HERE / "coremidi-io.swift"

# type: note_on, note_off, cc, pitchbend (value 0..16383), aftertouch (channel pressure), program, or a realtime name
#   (clock, start, continue, stop). channel is 0..15 (None for realtime); number is the note or controller number.
Message = namedtuple("Message", "type channel number value")

REALTIME = {0xF8: "clock", 0xFA: "start", 0xFB: "continue", 0xFC: "stop"}
REALTIME_BYTES = {name: byte for byte, name in REALTIME.items()}


def parse(data):
    """Raw MIDI bytes -> Message, or None for what the bridge does not use (sysex, poly pressure, ...).
    A note-on with velocity 0 is a note-off."""
    if not data:
        return None
    status = data[0]
    if status in REALTIME:
        return Message(REALTIME[status], None, 0, 0)
    if status < 0x80 or status >= 0xF0:
        return None
    kind, channel = status & 0xF0, status & 0x0F
    arg = list(data[1:])
    if kind in (0x80, 0x90, 0xB0) and len(arg) >= 2:
        if kind == 0xB0:
            return Message("cc", channel, arg[0] & 0x7F, arg[1] & 0x7F)
        if kind == 0x90 and arg[1] > 0:
            return Message("note_on", channel, arg[0] & 0x7F, arg[1] & 0x7F)
        return Message("note_off", channel, arg[0] & 0x7F, 0)
    if kind == 0xE0 and len(arg) >= 2:
        return Message("pitchbend", channel, 0, (arg[1] & 0x7F) << 7 | (arg[0] & 0x7F))
    if kind == 0xD0 and arg:
        return Message("aftertouch", channel, 0, arg[0] & 0x7F)
    if kind == 0xC0 and arg:
        return Message("program", channel, arg[0] & 0x7F, 0)
    return None


def encode(message):
    """Message -> raw MIDI bytes."""
    kind, channel, number, value = message
    if kind in REALTIME_BYTES:
        return bytes([REALTIME_BYTES[kind]])
    if kind == "note_on":
        return bytes([0x90 | channel, number, value])
    if kind == "note_off":
        return bytes([0x80 | channel, number, 0])
    if kind == "cc":
        return bytes([0xB0 | channel, number, value])
    if kind == "pitchbend":
        return bytes([0xE0 | channel, value & 0x7F, value >> 7 & 0x7F])
    if kind == "aftertouch":
        return bytes([0xD0 | channel, value])
    raise ValueError(f"cannot encode {kind}")


def hex_line(data):
    return " ".join(f"{b:02x}" for b in data)


def helper_binary(build_dir=None):
    """The compiled helper, built when missing or older than its source. Raises RuntimeError without swiftc.

    The DIN and USB bridges both call this on first use, so the build is serialised under a lock file: the second
    bridge re-checks after acquiring the lock and finds the binary already up to date, instead of racing swiftc's
    linker on the same output path (which fails with "ld: File exists")."""
    folder = Path(build_dir or REPO / "build") / "midi-bridge"
    binary = folder / "coremidi-io"
    if binary.exists() and binary.stat().st_mtime >= HELPER_SOURCE.stat().st_mtime:
        return binary
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "coremidi-io.lock").open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # Re-check under the lock: another bridge may have built the helper while this one waited.
            if binary.exists() and binary.stat().st_mtime >= HELPER_SOURCE.stat().st_mtime:
                return binary
            try:
                result = subprocess.run(["swiftc", "-O", str(HELPER_SOURCE), "-framework", "CoreMIDI",
                                         "-o", str(binary)], capture_output=True, text=True, timeout=300)
            except FileNotFoundError:
                raise RuntimeError("swiftc is missing; install the Xcode Command Line Tools with: "
                                   "xcode-select --install") from None
            if result.returncode:
                raise RuntimeError(f"building the CoreMIDI helper failed:\n{result.stderr.strip()}")
            return binary
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def list_ports(build_dir=None):
    """{"source": [names], "destination": [names]} of the CoreMIDI endpoints."""
    out = subprocess.run([str(helper_binary(build_dir)), "list"], capture_output=True, text=True, timeout=30).stdout
    ports = {"source": [], "destination": []}
    for line in out.splitlines():
        kind, _, name = line.partition(" ")
        if kind in ports:
            ports[kind].append(name)
    return ports


class Port:
    """A running helper: `get` yields received Messages, `send` writes one. `args` are the helper's arguments after
    its program name ("run --in NAME --out NAME" or "virtual NAME")."""

    def __init__(self, args, build_dir=None, on_raw=None, queue_messages=True):
        self.process = subprocess.Popen([str(helper_binary(build_dir)), *args], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.messages = queue.Queue()
        self.on_raw = on_raw
        self.queue_messages = queue_messages
        self.ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        if not self.ready.wait(10):
            error = self.close_error()
            raise RuntimeError(error or "the CoreMIDI helper did not become ready")

    def close_error(self):
        if self.process.poll() is None:
            self.process.kill()
        return (self.process.stderr.read() or "").strip()

    def _read(self):
        for line in self.process.stdout:
            line = line.strip()
            if line == "ready":
                self.ready.set()
            elif line.startswith("in "):
                data = bytes(int(t, 16) for t in line[3:].split())
                if self.on_raw:
                    self.on_raw(data)
                message = parse(data)
                if message is not None and self.queue_messages:
                    self.messages.put(message)

    def get(self, timeout=None):
        """The next message, or None after `timeout` seconds."""
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, timeout):
        """Wait up to `timeout` for a message, then take everything else already queued."""
        first = self.get(timeout)
        if first is None:
            return []
        batch = [first]
        while True:
            try:
                batch.append(self.messages.get_nowait())
            except queue.Empty:
                return batch

    def send(self, message):
        try:
            self.process.stdin.write("out " + hex_line(encode(message)) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def send_many(self, messages):
        """Several messages in one CoreMIDI packet list (one packet each), in order."""
        try:
            self.process.stdin.write("burst " + " | ".join(hex_line(encode(m)) for m in messages) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def close(self):
        try:
            self.process.stdin.write("quit\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        try:
            self.process.wait(3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except OSError:
                pass
