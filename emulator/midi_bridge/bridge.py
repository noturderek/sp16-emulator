"""The bridge loop: controller messages in, control-socket calls out, lamp echo back."""
import json
from pathlib import Path
import sys
import time

REPO_EMULATOR = Path(__file__).resolve().parents[1]
if str(REPO_EMULATOR) not in sys.path:               # sessionctl and panel_map import each other flat
    sys.path.insert(0, str(REPO_EMULATOR))

from .actions import Translator
from .leds import LedEcho
from . import profile as prof

POLL_SECONDS = 0.01
RESYNC_SECONDS = 5.0


def find_runtime(build_dir, build_name=None):
    """The runtime.json of the newest running research session (a live control socket) in build/<name>/sessions."""
    import os
    root = Path(build_dir)
    candidates = [root / build_name] if build_name else [p for p in sorted(root.iterdir()) if p.is_dir()] if root.is_dir() else []
    found = []
    for build in candidates:
        for folder in (build / "sessions").glob("*/") if (build / "sessions").is_dir() else []:
            path = folder / "runtime.json"
            try:
                runtime = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if runtime.get("control_socket") and os.path.exists(runtime["control_socket"]):
                found.append((path.stat().st_mtime, path))
    if not found:
        raise RuntimeError(f"no running research session under {root}" + (f"/{build_name}" if build_name else "")
                           + "; start one with: ./sp16 run --name NAME (a build made with --research)")
    return max(found)[1]


class Bridge:
    """Joins a MIDI `port` (get/drain/send), a translator and, when the mapping has lamps, an LED echo."""

    def __init__(self, port, translator, leds=None, clock=time.monotonic, log=print):
        self.port, self.translator, self.leds, self.clock, self.log = port, translator, leds, clock, log
        self.next_resync = clock() + RESYNC_SECONDS
        self.received = 0

    def step(self, wait=POLL_SECONDS):
        batch = self.port.drain(wait)
        self.received += len(batch)
        if batch:
            self.translator.handle(batch)
        self.translator.tick()
        if self.leds:
            for message in self.leds.poll():
                self.port.send(message)
            if self.clock() >= self.next_resync:
                self.next_resync = self.clock() + RESYNC_SECONDS
                for message in self.leds.resync():
                    self.port.send(message)

    def run(self, stop=lambda: False):
        try:
            while not stop():
                self.step()
        finally:
            self.translator.release_all()
            if self.leds:
                for message in self.leds.blank():
                    self.port.send(message)
