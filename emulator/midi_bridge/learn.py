"""Learn mode: ask for a control, listen, and record what the hardware sent.

`read(timeout)` returns the next Message or None; `say(text)` shows a line. Both are injected, so the same code runs
against a CoreMIDI port and against a scripted list in the tests.

A knob is told apart from its encodings by the byte values a turn produces (v = the 7-bit controller value):
  absolute   many distinct values spread over the range, or distinct values near the top that never repeat
  twos       two's complement: +n is n, -n is 128-n        (1, 2 / 127, 126; 127, 127, 127 alone)
  signmag    sign and magnitude: +n is n, -n is 64+n       (1, 2 / 65, 66)
  offset     binary offset: +n is 64+n, -n is 64-n         (65, 66 / 63, 62)
One direction alone cannot always settle it (1,1,1 reads as twos or signmag), so a turn that is still ambiguous asks
for the other direction.
"""
import time

from . import profile as prof

LOW = range(1, 16)
TWOS_NEG = range(113, 128)
SIGN_NEG = range(65, 80)
OFFSET_NEG = range(49, 64)
BUTTON_ACTIONS_NOT = ("knob", "selector", "strip", "main_out", "drive", "lp_cutoff", "lp_resonance", "hp_cutoff",
                      "input_level")
DEFAULT_CONTROLS = [f"pad{i}" for i in range(1, 9)] + [f"knob{i}" for i in range(1, 5)]


def relative_delta(encoding, value, invert=False):
    """A relative controller value 0..127 -> signed detents."""
    if encoding == "twos":
        delta = value if value < 64 else value - 128
    elif encoding == "signmag":
        delta = value if value < 64 else -(value - 64)
    elif encoding == "offset":
        delta = value - 64
    else:
        raise ValueError(f"unknown encoding {encoding}")
    return -delta if invert else delta


def repeats(values, count=3):
    """Whether one value comes `count` times in a row. A moving absolute knob never sends the same value twice running,
    an encoder turned slowly sends the same step again and again."""
    run = 1
    for before, now in zip(values, values[1:]):
        run = run + 1 if now == before else 1
        if run >= count:
            return True
    return False


def classify(values):
    """Controller values seen while one knob turned (in the order they came) -> ("absolute", None), ("relative", encoding)
    or (None, None) when the values do not settle it yet.

    A knob is called relative only on evidence an absolute knob cannot produce: values on both sides of the encoding
    (some small, some near the top, nothing between), or, for the values near the top alone, the same value repeating.
    Distinct values near the top that never repeat (127, 126, 125) are an absolute knob turned down."""
    values = list(values)
    seen = set(values)
    if not seen:
        return None, None
    low, twos, sign, off = (any(v in r for v in seen) for r in (LOW, TWOS_NEG, SIGN_NEG, OFFSET_NEG))
    if seen <= set(LOW) | set(TWOS_NEG) and twos and low:
        return "relative", "twos"
    if seen <= set(TWOS_NEG):
        if repeats(values):
            return "relative", "twos"
        if len(seen) >= 3:
            return "absolute", None
        return None, None                           # one or two values: an encoder's -1, -2 or a knob just touched
    if seen <= set(LOW) | set(SIGN_NEG) and sign and low:
        return "relative", "signmag"
    if seen <= set(OFFSET_NEG) | set(SIGN_NEG) and sign and off:
        return "relative", "offset"
    if max(seen) - min(seen) >= 24 or len(seen) > 14:
        return "absolute", None
    return None, None


def listen(read, first_timeout, idle=1.0, limit=10.0, accept=None, clock=time.monotonic):
    """Messages until `idle` seconds pass after the first (at most `limit` in all). `accept` filters messages."""
    got, hard = [], None
    deadline = clock() + first_timeout
    while clock() < deadline:
        message = read(0.05)
        if message is None or (accept and not accept(message)):
            continue
        if not got:
            hard = clock() + limit
        got.append(message)
        deadline = min(clock() + idle, hard)
    return got


def wants_button(action):
    return not action.startswith(BUTTON_ACTIONS_NOT)


def learn_one(action, read, say, timeout=30.0, idle=1.0, rounds=3, clock=time.monotonic):
    """Learn the control for one emulator action. Returns an input spec, or None when nothing was heard."""
    if wants_button(action):
        say(f"{action}: press the control (it ends after {timeout:g} s without one)")
        got = listen(read, timeout, idle=0.3, limit=3.0, clock=clock,
                     accept=lambda m: m.type in ("note_on", "start", "stop", "continue") or (m.type == "cc" and m.value > 0))
        return prof.spec_from_message(got[0], "button") if got else None
    say(f"{action}: turn or move the control, both directions for an encoder ({timeout:g} s)")
    values, first = [], None
    for attempt in range(rounds):
        got = listen(read, timeout, idle=idle, clock=clock,
                     accept=lambda m: m.type in ("cc", "pitchbend", "aftertouch")
                     and (first is None or prof.message_key(m) == prof.message_key(first)))
        if not got:
            return None
        first = first or got[0]
        values += [m.value for m in got]
        if first.type != "cc":                      # pitch bend and pressure are always absolute
            return prof.spec_from_message(first, "absolute")
        kind, encoding = classify(values)
        if kind:
            return prof.spec_from_message(first, kind, encoding)
        say(f"{action}: not sure yet what it sends (saw {sorted(set(values))[:8]}); turn it the other way as well")
    # still ambiguous: the commonest encoder encoding, said out loud
    say(f"{action}: assuming two's complement relative; edit the profile if the knob runs backwards")
    return prof.spec_from_message(first, "relative", "twos")


def learn(controls, read, say, name, timeout=30.0, led_echo=True, clock=time.monotonic):
    """Learn every control in `controls` (emulator action names). Returns (profile, mapping); controls that were not heard
    are left out and reported."""
    inputs, outputs, bindings, lamps, skipped = {}, {}, {}, {}, []
    for action in controls:
        spec = learn_one(action, read, say, timeout, clock=clock)
        if spec is None:
            skipped.append(action)
            say(f"{action}: nothing heard, skipped")
            continue
        inputs[action] = spec
        bindings[action] = action
        if led_echo and spec["kind"] == "button" and spec["type"] in ("note", "cc"):
            outputs[action] = {"type": spec["type"], "channel": spec["channel"], "number": spec["number"], "mode": "level"}
            lamps[action] = action
        say(f"{action}: {describe(spec)}")
    profile = prof.validate_profile({"name": name, "inputs": inputs, "outputs": outputs})
    mapping = {"name": name, "profile": name, "bindings": bindings, "lamps": lamps}
    return profile, mapping, skipped


def describe(spec):
    where = "any channel" if spec.get("channel") is None else f"channel {spec['channel'] + 1}"
    what = {"note": f"note {spec.get('number')}", "cc": f"CC {spec.get('number')}"}.get(spec["type"], spec["type"])
    extra = f" {spec['encoding']}" if spec.get("encoding") else ""
    return f"{spec['kind']}{extra}, {what}, {where}"
