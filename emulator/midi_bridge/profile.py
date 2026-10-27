"""Profiles and mappings.

A *profile* is what a piece of hardware sends and accepts, learned or shipped:
    {"name": "...", "inputs":  {NAME: {"type": "note"|"cc"|"pitchbend"|"aftertouch"|"transport", "channel": 0..15|null,
                                       "number": N, "kind": "button"|"absolute"|"relative",
                                       "encoding": "twos"|"signmag"|"offset" (relative only)}},
                     "outputs": {NAME: {"type": "note"|"cc", "channel": 0..15, "number": N, "mode": "level"|"onoff"}}}
A *mapping* says which named emulator action each input drives, and which lamp feeds which output:
    {"name": "...", "profile": "...", "bindings": {INPUT: ACTION | {"action": ACTION, "velocity": 1..127, "invert": true,
                                                                  "scale": X}},
                                       "lamps": {LAMP: OUTPUT}}
ACTION and LAMP are emulator control names (panel_map: pad1..pad16, knob1..knob6, play, strip, main_out, ...; lamps
also strip_led1..8, bar_led1..4, ...). Keeping the two apart means a relearned controller keeps its mapping, and one
mapping fits any controller with the same input names.
"""
import json
from pathlib import Path
import re

from .midi import HERE, REPO

USER_DIR = REPO / "private" / "midi"            # learned files (git-ignored)
SHIPPED = {"profile": HERE / "profiles", "mapping": HERE / "mappings"}
INPUT_TYPES = {"note", "cc", "pitchbend", "aftertouch", "transport"}
KINDS = {"button", "absolute", "relative"}
ENCODINGS = ("twos", "signmag", "offset")
TRANSPORT = {"start": 0xFA, "continue": 0xFB, "stop": 0xFC}
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ProfileError(ValueError):
    pass


def validate_profile(profile):
    for name, spec in (profile.get("inputs") or {}).items():
        if spec.get("type") not in INPUT_TYPES or spec.get("kind") not in KINDS:
            raise ProfileError(f"input {name}: type {spec.get('type')!r} / kind {spec.get('kind')!r} not understood")
        if spec["kind"] == "relative" and spec.get("encoding") not in ENCODINGS:
            raise ProfileError(f"input {name}: a relative input needs encoding {'|'.join(ENCODINGS)}")
        if spec["type"] in ("note", "cc") and not isinstance(spec.get("number"), int):
            raise ProfileError(f"input {name}: needs a number")
        if spec["type"] == "transport" and spec.get("number") not in TRANSPORT.values():
            raise ProfileError(f"input {name}: transport number must be one of {sorted(TRANSPORT.values())}")
    for name, spec in (profile.get("outputs") or {}).items():
        if spec.get("type") not in ("note", "cc") or not isinstance(spec.get("number"), int) \
                or not isinstance(spec.get("channel"), int):
            raise ProfileError(f"output {name}: needs type note|cc, channel and number")
    return profile


def validate_mapping(mapping, profile=None):
    inputs = (profile or {}).get("inputs")
    outputs = (profile or {}).get("outputs")
    for name in mapping.get("bindings") or {}:
        if inputs is not None and name not in inputs:
            raise ProfileError(f"mapping binds {name}, which the profile has no input for")
    for lamp, out in (mapping.get("lamps") or {}).items():
        if outputs is not None and out not in outputs:
            raise ProfileError(f"mapping sends lamp {lamp} to {out}, which the profile has no output for")
    return mapping


def binding(mapping, input_name):
    """The binding of an input as a dict with an "action" key, or None."""
    entry = (mapping.get("bindings") or {}).get(input_name)
    if entry is None:
        return None
    return {"action": entry} if isinstance(entry, str) else dict(entry)


def find_file(kind, name):
    """A profile/mapping by path or by name: private/midi/<kind>s first, then the shipped ones."""
    if "/" in name or name.endswith(".json"):
        path = Path(name).expanduser()
        if path.is_file():
            return path
        raise ProfileError(f"no {kind} file {name}")
    for folder in (USER_DIR / f"{kind}s", SHIPPED[kind]):
        path = folder / f"{name}.json"
        if path.is_file():
            return path
    raise ProfileError(f"no {kind} called {name!r} (looked in {USER_DIR / (kind + 's')} and {SHIPPED[kind]})")


def load(kind, name):
    data = json.loads(find_file(kind, name).read_text())
    return validate_profile(data) if kind == "profile" else data


def save(kind, name, data, folder=None):
    if not NAME_RE.match(name):
        raise ProfileError(f"{name!r} is not a plain file name")
    folder = Path(folder) if folder else USER_DIR / f"{kind}s"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


def spec_from_message(message, kind, encoding=None):
    """The input spec a first message of a control implies."""
    if message.type in ("note_on", "note_off"):
        spec = {"type": "note", "channel": message.channel, "number": message.number}
    elif message.type == "cc":
        spec = {"type": "cc", "channel": message.channel, "number": message.number}
    elif message.type in ("pitchbend", "aftertouch"):
        spec = {"type": message.type, "channel": message.channel, "number": 0}
    elif message.type in ("start", "stop", "continue"):
        return {"type": "transport", "channel": None, "number": TRANSPORT[message.type], "kind": "button"}
    else:
        raise ProfileError(f"cannot use a {message.type} message as an input")
    spec["kind"] = kind
    if encoding:
        spec["encoding"] = encoding
    return spec


def message_key(message):
    """(type, channel, number) as the profile index sees a message, or None for a message no input can match."""
    if message.type in ("note_on", "note_off"):
        return ("note", message.channel, message.number)
    if message.type == "cc":
        return ("cc", message.channel, message.number)
    if message.type in ("pitchbend", "aftertouch"):
        return (message.type, message.channel, 0)
    if message.type in TRANSPORT:
        return ("transport", None, TRANSPORT[message.type])
    return None


class Index:
    """Message -> (input name, spec). A spec with channel null matches every channel."""

    def __init__(self, profile):
        self.exact, self.omni = {}, {}
        for name, spec in (profile.get("inputs") or {}).items():
            number = spec.get("number", 0) if spec["type"] in ("note", "cc", "transport") else 0
            if spec.get("channel") is None:
                self.omni[(spec["type"], number)] = (name, spec)
            else:
                self.exact[(spec["type"], spec["channel"], number)] = (name, spec)

    def match(self, message):
        key = message_key(message)
        if key is None:
            return None
        return self.exact.get(key) or self.omni.get((key[0], key[2]))


def output_message(spec, level):
    """An output spec and a level 0..127 -> the Message that sets it."""
    from .midi import Message
    level = max(0, min(127, int(level)))
    if spec.get("mode") == "onoff":
        level = 127 if level else 0
    if spec["type"] == "cc":
        return Message("cc", spec["channel"], spec["number"], level)
    return Message("note_on" if level else "note_off", spec["channel"], spec["number"], level)
