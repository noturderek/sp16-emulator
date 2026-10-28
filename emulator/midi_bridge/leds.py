"""LED echo: the emulator's decoded lamp state back to the controller as note or CC velocity.

The app's control socket has no lamp read-out, so the bridge asks it to log the OEM panel's TX frames (`txlog`), follows
the session's tx-frames.jsonl as it grows and decodes each frame with panel_map.decode_lamps, exactly as the panel
view's own lamp feed does. A lamp is sent when its level changes; `resync` sends them all again (a controller that was
plugged in late). The logging is switched off again when the bridge ends: the file grows by about 1 MB a minute.
"""
import json
from pathlib import Path

from . import profile as prof

LOG_NAME = "tx-frames.jsonl"
TX_PREFIX = 3                       # multi, part, parts bytes in front of the 128-byte frame (protocol.TX_FRAME)


def lamp_level(rgb):
    """A lamp's (r, g, b) 0..255 -> controller level 0..127 (the brightest channel; no colour on a note velocity)."""
    return round(max(rgb) * 127 / 255)


def parse_tx_lines(data):
    """tx-frames.jsonl bytes -> [frame bytes]; a line that does not parse is skipped."""
    frames = []
    for line in data.splitlines():
        try:
            frame = bytes.fromhex(json.loads(line)["hex"])[TX_PREFIX:]
        except (ValueError, KeyError, TypeError):
            continue
        frames.append(frame)
    return frames


class LedEcho:
    """Follows a TX frame log and yields the Messages that bring the controller's LEDs in line with the lamps."""

    def __init__(self, session_dir, profile, mapping, decode, names):
        """`decode` is panel_map.decode_lamps; `names` maps its lamp ids to control names."""
        self.path = Path(session_dir) / LOG_NAME
        self.decode, self.names = decode, names
        self.outputs = {lamp: profile["outputs"][out] for lamp, out in (mapping.get("lamps") or {}).items()
                        if out in (profile.get("outputs") or {})}
        self.offset = self.path.stat().st_size if self.path.exists() else 0
        self.sent = {}                                  # lamp -> last level sent
        self.latest = {}                                # lamp -> last level decoded

    def poll(self):
        """Messages for the lamps that changed since the last poll."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
        except OSError:
            return []
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        self.offset += cut + 1
        lamps = None
        for frame in parse_tx_lines(data[:cut + 1]):
            decoded = self.decode(frame)
            if decoded is not None:
                lamps = decoded
        if lamps is None:
            return []
        for control, rgb in lamps.items():
            name = self.names.get(control)
            if name in self.outputs:
                self.latest[name] = lamp_level(rgb)
        return self.changes()

    def changes(self, everything=False):
        out = []
        for lamp, level in self.latest.items():
            if everything or self.sent.get(lamp) != level:
                self.sent[lamp] = level
                out.append(prof.output_message(self.outputs[lamp], level))
        return out

    def resync(self):
        return self.changes(everything=True)

    def blank(self):
        """Messages that switch every echoed LED off (at the end of a run)."""
        return [prof.output_message(spec, 0) for spec in self.outputs.values()]
