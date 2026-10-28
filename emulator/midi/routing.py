"""The MIDI routing preferences the app's window writes, and the route each bridge port carries.

The preferences window (MIDI > Routing..., emulator/midi-routing.swift) writes one JSON document::

    {"clockSource": "internal" or a source's unique ID as a decimal string,
     "bpm": 120.0,
     "ports": [{"name": "SP-16 DIN [build]", "uniqueID": 1193046, "isSource": true,
                "inEnabled": true, "outEnabled": true, "sendClock": false, "receiveClock": true}, ...]}

``ports`` holds one entry per port the user has toggled; a port that is absent carries everything, which is what a
bridge does with no preferences file at all. An entry is matched by display ``name``, because a bridge knows the
label it gave its own virtual endpoint but not the CoreMIDI unique ID the system assigned it; ``uniqueID`` is what
the window remembers a re-plugged device by.

A row is one CoreMIDI endpoint, so it only carries the toggles its kind has: a source row (what the host reads)
gates the guest-to-host direction with In and Receive clock, a destination row (what the host writes) gates the
host-to-guest direction with Out and Send clock. As in a DAW's MIDI preferences, In/Out gate the whole stream and
the clock toggles gate only the 0xF8 within it. ``clockSource`` and ``bpm`` belong to the window's own clock
generator; a bridge only routes bytes and ignores them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import NamedTuple

CLOCK = 0xF8  # the clock byte the two clock toggles gate
CONFIG_NAME = "midi-routing.json"
SUPPORT_DIR = "SP-16 Emulator"  # ~/Library/Application Support/SP-16 Emulator/
DEFAULT_REFRESH = 0.25  # seconds between checks of the file's mtime


def default_path():
    """Where the app writes the preferences (FileManager .applicationSupportDirectory + the app's folder)."""
    return Path.home() / "Library" / "Application Support" / SUPPORT_DIR / CONFIG_NAME


def config_argument(text):
    """argparse type of --config: the preferences file to read. An absent file is not an error (every route then
    carries); a directory can never be one, so it is refused here rather than silently routing everything."""
    path = Path(text)
    if path.is_dir():
        raise argparse.ArgumentTypeError(f"{text} is a directory, not a {CONFIG_NAME}")
    return path


class Route(NamedTuple):
    """What one port carries. Defaults are a bridge with no preferences: both directions carry everything."""

    in_enabled: bool = True
    out_enabled: bool = True
    send_clock: bool = True
    receive_clock: bool = True

    def carries_from_guest(self, message):
        """Whether one guest message (bytes) reaches the host: the source row's In and Receive clock."""
        if not self.in_enabled:
            return False
        return self.receive_clock if message == bytes([CLOCK]) else True

    def carries_to_guest(self, byte):
        """Whether one guest-bound byte from the host reaches it: the destination row's Out and Send clock."""
        if not self.out_enabled:
            return False
        return self.send_clock if byte == CLOCK else True


def route_from(entry):
    """A Route from one ports[] entry. A toggle that is not exactly a JSON true carries nothing on that route, so a
    damaged entry silences rather than opens its direction."""
    return Route(in_enabled=entry.get("inEnabled") is True,
                 out_enabled=entry.get("outEnabled") is True,
                 send_clock=entry.get("sendClock") is True,
                 receive_clock=entry.get("receiveClock") is True)


class Routing:
    """The preferences file, re-read when it changes so a toggle in the window takes effect on the next message."""

    def __init__(self, path=None, refresh_seconds=DEFAULT_REFRESH, clock=time.monotonic):
        if refresh_seconds < 0:
            raise ValueError("refresh_seconds must not be negative")
        self.path = Path(path) if path is not None else default_path()
        self.refresh_seconds = float(refresh_seconds)
        self.clock = clock
        self.ports = {}      # (display name, is_source) -> Route
        self.stamp = None    # (mtime_ns, size, inode) of the file the current routes came from
        self.checked = None  # when the file was last stat'ed
        self.problem = None  # why the last read kept the previous routes, or None when the file parsed
        self.reload()

    def reload(self, force=True):
        """Read the file; keep the previous routes if it cannot be read or parsed, so a half-written or hand-edited
        file never silently reopens a direction the user closed. Returns the routes."""
        try:
            stat = self.path.stat()
        except OSError:
            self.ports, self.stamp, self.checked, self.problem = {}, None, self.clock(), None
            return self.ports
        self.checked = self.clock()
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if not force and stamp == self.stamp:
            return self.ports
        try:
            text = self.path.read_text()
        except OSError as error:
            self.problem = f"{self.path}: {error}"  # stamp stays behind, so the next call retries
            return self.ports
        self.stamp = stamp
        try:
            document = json.loads(text)
        except ValueError as error:
            self.problem = f"{self.path}: {error}"
            return self.ports
        if not isinstance(document, dict):
            self.problem = f"{self.path}: not a JSON object"
            return self.ports
        entries = document.get("ports")
        ports = {}
        for entry in entries if isinstance(entries, list) else []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name:
                continue  # an entry without a name cannot be matched; it never overrides one that can
            ports.setdefault((name, entry.get("isSource") is True), route_from(entry))
        self.ports, self.problem = ports, None
        return self.ports

    def route(self, name, is_source):
        """The route for our own port `name` (a source row when `is_source`, else a destination row)."""
        if not name:
            raise ValueError("a port needs a name to look its route up")
        now = self.clock()
        if self.checked is None or now - self.checked >= self.refresh_seconds:
            self.reload(force=False)
        return self.ports.get((name, bool(is_source)), Route())
