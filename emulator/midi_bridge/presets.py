"""The "mirror the real SP-16" preset: the unit's own MIDI OUT as the controller.

The 1.60 addendum (pp. 47-51, "MIDI OUT") says what a unit set to USB MIDI sends from a MIDI track, and these are the facts
used here (grade S, from the manual):
  - each track transmits on its own channel, TRACK n -> CH n by default (2.2, CHANNEL MAPPING RESET);
  - a pad plays the track's NOTE (C0..B9), up to three more notes as offsets, at the track's VELOCITY (2.3);
  - the six parameter knobs of the MIDI CC1 page send PITCH BEND, AFTER TOUCH, CC 7, CC 1, CC 2, CC 10 and the CC2 page
    CC 71..76 (2.4, the defaults the screens show; the CC numbers of the last four are editable per track);
  - START/STOP/CONTINUE are sent when enabled (2.1).
What the manual does not say, and this preset therefore does not claim: which note each pad sends (it is a track setting;
`note_base` is the default the screenshot suggests, NOTE C5 = 60 with C0 = 0, grade I), that the unit has any LED input
over MIDI (U: no lamp mapping is generated) the range and behaviour of the knobs' values (assumed absolute, grade I: the bridge turns a change of value
into detents).
"""
from . import profile as prof

CC1_PAGE = (("pitchbend", 0), ("aftertouch", 0), ("cc", 7), ("cc", 1), ("cc", 2), ("cc", 10))
CC2_PAGE = tuple(("cc", n) for n in range(71, 77))
NAME = "sp16-midi-out"


def sp16_midi_out(channel=1, note_base=60, cc_page=1, name=NAME):
    """(profile, mapping) for a unit sending on `channel` (1..16, or None for any channel). Pad n sends note
    note_base + n - 1."""
    chan = None if channel is None else channel - 1
    if chan is not None and not 0 <= chan <= 15:
        raise prof.ProfileError("channel must be 1..16")
    if cc_page not in (1, 2):
        raise prof.ProfileError("cc_page must be 1 or 2")
    inputs, bindings = {}, {}
    for pad in range(1, 17):
        note = note_base + pad - 1
        if not 0 <= note <= 127:
            raise prof.ProfileError(f"pad {pad} would be note {note}")
        inputs[f"pad{pad}"] = {"type": "note", "channel": chan, "number": note, "kind": "button"}
        bindings[f"pad{pad}"] = f"pad{pad}"
    for knob, (kind, number) in enumerate(CC1_PAGE if cc_page == 1 else CC2_PAGE, 1):
        inputs[f"knob{knob}"] = {"type": kind, "channel": chan, "number": number, "kind": "absolute"}
        bindings[f"knob{knob}"] = f"knob{knob}"
    for message, control in (("start", "play"), ("stop", "stop"), ("continue", "play")):
        inputs[message] = {"type": "transport", "channel": None, "number": prof.TRANSPORT[message], "kind": "button"}
        bindings[message] = control
    profile = {"name": name, "inputs": inputs, "outputs": {}}
    mapping = {"name": name, "profile": name, "bindings": bindings, "lamps": {}}
    return prof.validate_profile(profile), mapping
