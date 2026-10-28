"""A decoded controller message -> a control-socket call.

`target` is anything with `event(name, action, value)` (sessionctl.Session, or a recorder in the tests); `name` is a
control name from panel_map and `action` is release|press|delta|value, the same four the app's control socket takes.

What an input does depends on the action it is bound to:
  pad1..16          button: press with the note's velocity, release on note-off
  other buttons     button: press/release (transport input from START/STOP/CONTINUE: a short press)
  knob1..6, selector  relative: signed detents; absolute: the change since the last value, as detents;
                      button: +1 detent per press (action "knob1+") or -1 ("knob1-")
  main_out, drive, lp_cutoff, lp_resonance, hp_cutoff, input_level   absolute: value 0..1000;
                      relative: steps of 1% from the panel's starting position
  strip             absolute: contact and position 0..1000; released when the value stops changing (or, from pitch bend,
                      when it returns to centre)
"""
import time

from . import profile as prof
from .learn import relative_delta
from .midi import Message

PAD_HOLD_DEFAULT = 100
TAP_SECONDS = 0.06
STRIP_RELEASE_SECONDS = 0.6
MAX_DELTA = 64                      # panel.Panel.MAX_DELTA: more per event is clamped by the app
ANALOG = ("main_out", "drive", "lp_cutoff", "lp_resonance", "hp_cutoff", "input_level")
DETENTS = ("selector", *(f"knob{i}" for i in range(1, 7)))
RELATIVE_STEP = 10                  # analog value units (of 1000) per detent of a relative encoder


def kind_of(action):
    base = action.rstrip("+-")
    if base in ANALOG:
        return "analog"
    if base in DETENTS:
        return "detent"
    if base == "strip":
        return "strip"
    return "button"


class Translator:
    """Turns messages into calls on `target`; `tick(now)` runs the timed releases. Calls that fail are counted, not
    raised: a hiccup of the app must not end a performance."""

    def __init__(self, target, profile, mapping, analog_start=None, clock=time.monotonic, log=None):
        self.target, self.mapping, self.clock = target, mapping, clock
        self.index = prof.Index(profile)
        self.analog = dict(analog_start or {})          # last value sent per analog control
        self.last = {}                                  # last absolute value per input (for detents)
        self.carry = {}                                 # input name -> scaled detents not yet sent (absolute -> detent)
        self.held = {}                                  # input name -> control currently pressed
        self.timers = []                                # (due, control, action, value)
        self.strip_until = None
        self.sent = self.errors = 0
        self.log = log or (lambda text: None)
        self.unmatched = set()

    # -- output
    def event(self, name, action, value=0):
        try:
            self.target.event(name, action, value)
            self.sent += 1
        except Exception as error:                       # noqa: BLE001 - see the class doc
            self.errors += 1
            self.log(f"{name} {action}: {error}")

    def later(self, seconds, name, action, value=0):
        self.timers.append((self.clock() + seconds, name, action, value))

    def tick(self, now=None):
        now = self.clock() if now is None else now
        due = [t for t in self.timers if t[0] <= now]
        self.timers = [t for t in self.timers if t[0] > now]
        for _, name, action, value in due:
            self.event(name, action, value)
        if self.strip_until is not None and now >= self.strip_until:
            self.strip_until = None
            self.event("strip", "release", 0)

    def release_all(self):
        for control in list(self.held.values()):
            self.event(control, "release", 0)
        self.held.clear()
        pending, self.timers = self.timers, []              # a tap still waiting for its release: send it now
        for _, name, action, value in pending:
            self.event(name, action, value)
        if self.strip_until is not None:
            self.strip_until = None
            self.event("strip", "release", 0)

    # -- input
    def handle(self, messages):
        """A batch of messages. Absolute values of one input collapse to the last one and relative deltas to their sum:
        a burst of controller traffic costs one socket call, not hundreds."""
        merged, order = {}, []
        for message in messages:
            hit = self.index.match(message)
            if hit is None:
                key = prof.message_key(message)
                if key is not None and key not in self.unmatched:
                    self.unmatched.add(key)
                    self.log(f"ignored {message.type} {key[1:]}: no input in the profile")
                continue
            name, spec = hit
            if spec["kind"] == "button" or name not in merged:
                order.append((name, spec, [message]))
                if spec["kind"] != "button":
                    merged[name] = order[-1][2]
            else:
                merged[name].append(message)
        for name, spec, group in order:
            entry = prof.binding(self.mapping, name)
            if entry is None:
                continue
            if spec["kind"] == "button":
                self.button(name, spec, entry, group[0])
            elif spec["kind"] == "absolute":
                self.absolute(name, spec, entry, group[-1])
            else:
                delta = sum(relative_delta(spec["encoding"], m.value, entry.get("invert", False)) for m in group)
                self.relative(name, entry, delta)

    def button(self, name, spec, entry, message):
        action = entry["action"]
        kind = kind_of(action)
        pressed = message.type in ("note_on", "start", "stop", "continue") or (message.type == "cc" and message.value > 0)
        if spec["type"] == "transport":
            self.event(action, "press", entry.get("velocity", PAD_HOLD_DEFAULT))
            self.later(TAP_SECONDS, action, "release", 0)
            return
        if kind == "detent":
            if pressed:
                self.event(action.rstrip("+-"), "delta", -1 if action.endswith("-") else 1)
            return
        if kind != "button":
            self.log(f"{name}: a button cannot drive {action}")
            return
        if pressed:
            velocity = entry.get("velocity") or (message.value if message.type in ("note_on", "cc") else PAD_HOLD_DEFAULT)
            self.held[name] = action
            self.event(action, "press", max(1, min(127, int(velocity))))
        elif name in self.held:
            self.event(self.held.pop(name), "release", 0)

    def fraction(self, spec, value):
        return value / 16383 if spec["type"] == "pitchbend" else value / 127

    def absolute(self, name, spec, entry, message):
        action = entry["action"]
        kind = kind_of(action)
        fraction = self.fraction(spec, message.value)
        if entry.get("invert"):
            fraction = 1 - fraction
        fraction = max(0.0, min(1.0, fraction * entry.get("scale", 1.0)))
        if kind == "analog":
            self.set_analog(action, round(fraction * 1000))
        elif kind == "detent":
            last = self.last.get(name)
            self.last[name] = message.value
            if last is not None and message.value != last:
                diff = message.value - last
                # scale applies as it does to the other kinds; the part of a detent a small move leaves over is carried
                total = self.carry.get(name, 0.0) + (-diff if entry.get("invert") else diff) * entry.get("scale", 1.0)
                detents = int(total)
                self.carry[name] = total - detents
                if detents:
                    self.event(action.rstrip("+-"), "delta", max(-MAX_DELTA, min(MAX_DELTA, detents)))
        elif kind == "strip":
            centred = spec["type"] == "pitchbend" and abs(message.value - 8192) <= 64
            if centred:
                self.strip_until = None
                self.event("strip", "release", 0)
                return
            self.event("strip", "value", round(fraction * 1000))
            self.strip_until = self.clock() + STRIP_RELEASE_SECONDS if spec["type"] != "pitchbend" else None
        else:
            self.log(f"{name}: an absolute control cannot drive {action}")

    def relative(self, name, entry, delta):
        action = entry["action"]
        kind = kind_of(action)
        if not delta:
            return
        if kind == "detent":
            delta *= entry.get("scale", 1)
            delta = max(-MAX_DELTA, min(MAX_DELTA, int(delta)))
            self.event(action.rstrip("+-"), "delta", delta)
        elif kind == "analog":
            start = self.analog.get(action, 500)
            self.set_analog(action, start + delta * RELATIVE_STEP)
        else:
            self.log(f"{name}: a relative control cannot drive {action}")

    def set_analog(self, action, value):
        value = max(0, min(1000, int(value)))
        if self.analog.get(action) != value:
            self.analog[action] = value
            self.event(action, "value", value)
