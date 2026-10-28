"""OEM 1.60 panel control map for the isolated native emulator.

The semantic control ids are shared with panel-layout.json and the native window.
Wire placement below is observed, not guessed. `E:` cites the observation; an entry
without a source is unmapped and must stay unsupported instead of being guessed.

Wire format (the panel MCU's 128-byte frames; offsets are frame bytes):
  E1 RX frame: 16-bit words at 0x06..0x25 and 0x3f..0x5e (pads), 0x26/0x28/0x2a (10-bit analog), 0x2c/0x2e and
     0x30..0x3d (encoder counters) are big-endian.
  E2 Bytes 1..5 are a bit-per-button field (37 buttons); byte 1 bit 1 is the selector push. Counters at
     0x30..0x3a (six parameter knobs) and 0x3c (selector) are cumulative int16 values: the first frame is a
     baseline, later frames count by int16 difference. Long presses are counted in frames, so press timing
     depends on the frame cadence.
  E3 Pads: one pair of 16-bit values per pad (offsets 0x06+2i, 0x3f+2i).
  E4 The firmware reads 128-byte panel frames and 4-byte CDC frames; its TX frame is 128 bytes (byte 0x77 =
     0xaa, CRC at 0x7c), sent with the panel ioctl as multi 0, part 0 of 1.
  E5 CDC strip: value = (0x7f - b0) | b1 << 8; position = value & 0x7f, contact = bit 15. Sent on change.
  E6 Boot-time key combinations compare frame bytes 1..5.
  E8 Factory test mode (`testmode` argument; BUTTON TEST, ENCODER TEST, PAD CALIBRATION pages) names the held
     button, shows the six encoder counters (offset 0x30+2(n-1)), the selector counter and raw pad words, and
     clears each pressed button's lamp bit in the TX frame, which identifies the lamp channels below.
  E11 Factory test AD TEST: raw 0x28 shows as MASTER (direct), 0x26 as DSF DRIVE and 0x2a as DSF HIGHPASS
     (both 1023 minus the raw word).
  E7 TX frame: byte 0 = 2; 32 RGB lamp channels (3 bytes each, R,G,B): channel 0x10+k -> bytes 1+3k, channel
     k -> bytes 0x31+3k (k = 0..15); 35 mono lamp channels bit-packed into bytes 0x61..0x65, most with a
     second flag in 0x69..0x6b; extra flags at 0x66, 0x67, 0x68 (bit 0) and a 1..4 level at 0x6e. Byte 0x67 bit 0 is
     the standby transition flag, decoded as "standby".
     Channel -> physical lamp assignments are recorded below only when observed.

Per-version measurements (lamp roles, pad curve limits, the selector window) live in the firmware's compatibility
profile (profiles/<id>.json, docs/architecture.md M1). SP16_PROFILE selects it; the default is the 1.60 profile.
"""
import os

import profiles

PROFILE = profiles.load(os.environ.get("SP16_PROFILE") or profiles.LEGACY_PROFILE)

CONTROL_NAMES = {
    **{i: f"pad{i}" for i in range(1, 17)},
    17: "track", 18: "mute", 19: "slice", 20: "scale",
    21: "shift", 22: "rec", 23: "stop", 24: "play",
    25: "scene", 26: "pattern",
    **{26 + i: f"step{i}" for i in range(1, 17)},
    **{42 + i: f"bar{i}" for i in range(1, 5)},
    **{46 + i: f"knob{i}" for i in range(1, 7)},
    53: "selector", 54: "back", 55: "home", 56: "mode", 57: "hold", 58: "strip",
    59: "usb_stop", 60: "active",
    61: "main_out", 62: "drive", 63: "lp_cutoff", 64: "lp_resonance", 65: "hp_cutoff",
}
# Controls with no place in the panel layout (driven through the research socket): POWER/STANDBY is the M1 key that M2
# relays as RX bit (5,4) (service manual key matrix; the app's PowerManager handles button object 14); INPUT LEVEL is
# the rear analog gain pot; INPUT TONE is a test signal on the input jack and FILTER TONE one at the analog filter's
# input, where the send DAC plays (research stimuli, level 0..1000, 0 = off); PRESSURE 1..16 holds a pad's raw pressure.
HOST_CONTROL_NAMES = {100: "power", 101: "input_level", 102: "input_tone", 103: "filter_tone",
                      **{109 + i: f"pressure{i}" for i in range(1, 17)}}
ALL_CONTROL_NAMES = {**CONTROL_NAMES, **HOST_CONTROL_NAMES}
CONTROL_IDS = {name: control for control, name in ALL_CONTROL_NAMES.items()}
# Analog controls the board applies outside the firmware (no MCU sees them, or, for MAIN OUT, in addition to its ADC
# word): the bridge passes them to the shim's board model (hardware_stub.c), which leaves an untouched control neutral.
# Order = the u16 slots of the shared analog state (offset 12 + 2 * index); the tone frequency sits between slots 4
# and 5 (offset 22), so FILTER TONE is at offset 24.
HOST_ANALOG = ("main_out", "input_level", "lp_cutoff", "lp_resonance", "input_tone", "filter_tone")
INPUT_TONE_HZ = 1000
# PRESSURE n (ids 110..125) holds pad n's raw pressure word (0..PAD_RAW_MAX, the AD value the factory test's pad
# calibration page shows) instead of a velocity, so a calibration walkthrough can send 48..504 exactly. A value above 0
# also sets the contact word; 0 lets go. The pad's velocity press and release keep working and override it.
PAD_RAW_MAX = 1023

FRAME_BYTES = 128
PAD_A_OFFSET, PAD_B_OFFSET = 0x06, 0x3f
KNOB_COUNTERS = tuple(0x30 + 2 * i for i in range(6))
SELECTOR_COUNTER = 0x3c

# E2: button object index -> (frame byte, bit). Dispatch order, not physical order.
BUTTON_OBJECTS = (
    [(1, bit) for bit in range(2, 8)] +
    [(2, bit) for bit in range(8)] +
    [(5, bit) for bit in (4, 5, 6)] +
    [(5, bit) for bit in range(4)] +
    [(3, bit) for bit in range(8)] +
    [(4, bit) for bit in range(8)]
)
assert len(BUTTON_OBJECTS) == 37

# Role -> (byte, bit), from the firmware profile (panel.buttons). Only roles whose OEM effect was observed are
# listed. 1.60 profile: E8 selector push is
# byte 1 bit 1 (PUSH ENCODER STATUS); the button test names each single held bit; E9 from three levels deep
# (HOME > UTILITY > TOUCH STRIP SETTING) bit 1.3 goes straight to HOME while bit 1.2 goes up one level per press.
# Bits (1,2)/(1,3) also step the factory test pages. (1,0) and (5,7) produced no effect and are not among the 37
# dispatched button objects. (5,4) is the power/standby key (role "power"): released, it puts the app in standby.
BUTTON_BITS = {name: (byte, bit) for name, (byte, bit) in PROFILE["panel"]["button_roles"].items()}
SELECTOR_PUSH = BUTTON_BITS.get("selector")

# E11: analog pots with a firmware ADC field (PAGE 5 AD TEST): name -> (frame offset, inverted), from the profile
# (panel.analog). 1.60: MASTER reads the word directly; DSF DRIVE and DSF HIGHPASS read 1023 minus the word. LP CUTOFF
# and RESONANCE have no field (pure analog hardware) and stay stubs. 10-bit full scale = 1023.
ANALOG_WORDS = {name: (offset, inverted) for name, (offset, inverted) in PROFILE["panel"]["analog_words"].items()}
ANALOG_FULL_SCALE = 1023
# Where each analog knob starts, host value 0..1000 (the panel view starts its knobs from the same numbers: "initial" in
# panel-layout.json, checked by a test). DRIVE, RESONANCE and HP CUTOFF at minimum with LP CUTOFF at maximum is the
# filter wide open, the state the service manual's audio tests (MODE3) set; MAIN OUT starts centred. LP CUTOFF and
# RESONANCE are host-only analog state and stay "never moved" until touched, which the shim treats as exactly these
# positions (20 kHz, minimum resonance), so no value is sent for them at start.
ANALOG_DEFAULTS = {"main_out": 500, "drive": 0, "lp_cutoff": 1000, "lp_resonance": 0, "hp_cutoff": 0}
ANALOG_DEFAULT = 500  # any firmware-visible pot not listed above

# Toggle buttons that start off. The stock app lights ACTIVE (the Send & Return filter in the signal path, filter-select
# line high) at every start, observed on a fresh and on a saved data image, so the bridge presses the button until
# its lamp is dark (panel.StartOff).
START_OFF = ("active",)

# Controls that move an int16 counter: name -> (frame offset, wire sign for host +1 detent), from the profile
# (panel.counters). 1.60: E9 in the Utility list a counter increase moves the highlight down (next), so host +1 = next
# = counter +1; E8 counter +N is shown as encoder +N.
COUNTERS = {name: (offset, sign) for name, (offset, sign) in PROFILE["panel"]["counters"].items()}

TX_LENGTH, TX_HEADER, TX_MARKER_OFFSET, TX_MARKER = 128, 2, 0x77, 0xaa
# The standby transition flag: TX byte 0x67 bit 0. The app sets it once it has handled the power key's release (and in
# its start-up frames, so read it only after the home screen has settled); a real unit's M1 then cuts the power.
TX_STANDBY_OFFSET = 0x67

# E7: RGB lamp channel -> first of three consecutive bytes in the TX frame.
RGB_CHANNELS = {**{0x10 + k: 1 + 3 * k for k in range(16)}, **{k: 0x31 + 3 * k for k in range(16)}}

# E7: mono lamp channel -> (value byte, bit, second-flag byte or None).
MONO_CHANNELS = {
    0: (99, 4, 0x6a), 1: (99, 1, 0x6a), 2: (99, 2, 0x6a), 3: (99, 3, 0x6a), 4: (99, 7, None),
    5: (99, 6, 0x6a), 6: (99, 5, 0x6a), 7: (100, 4, 0x6b), 8: (100, 6, 0x6b), 9: (100, 5, 0x6b),
    10: (99, 0, 0x6a), 11: (100, 1, None), 12: (100, 0, None), 13: (100, 2, None),
    14: (100, 3, None), 15: (0x62, 0, None), 16: (0x62, 1, None), 17: (0x62, 2, None),
    18: (0x62, 3, None), 19: (0x62, 4, None), 20: (0x62, 5, None), 21: (0x62, 6, None),
    22: (0x62, 7, None), 23: (0x65, 3, None), 24: (0x65, 2, None), 25: (0x65, 0, None),
    26: (0x61, 4, 0x69), 27: (0x61, 5, 0x69), 28: (0x61, 6, 0x69), 29: (0x61, 7, 0x69),
    30: (0x61, 0, 0x69), 31: (0x61, 1, 0x69), 32: (0x61, 2, 0x69), 33: (0x61, 3, 0x69),
    34: (0x65, 1, None),
}


def decode_tx(frame):
    """E7: a complete OEM TX frame -> raw lamp channel state, or None if it is not one."""
    if len(frame) != TX_LENGTH or frame[0] != TX_HEADER or frame[TX_MARKER_OFFSET] != TX_MARKER:
        return None
    rgb = {ch: tuple(frame[off:off + 3]) for ch, off in RGB_CHANNELS.items()}
    mono = {ch: (frame[b] >> bit & 1, None if second is None else frame[second] >> bit & 1)
            for ch, (b, bit, second) in MONO_CHANNELS.items()}
    return {"rgb": rgb, "mono": mono, "level": frame[0x6e],
            "extra": (frame[0x66] & 1, frame[0x67] & 1, frame[0x68] & 1),
            "standby": bool(frame[TX_STANDBY_OFFSET] & 1)}


# Lamp-only ids shared with panel-layout.json (they have no input).
LAMP_NAMES = {
    **{69 + i: f"strip_led{i}" for i in range(1, 9)},
    80: "lamp_pitch", 81: "lamp_repeat", 82: "lamp_user1", 83: "lamp_user2",
    **{83 + i: f"bar_led{i}" for i in range(1, 5)},
    88: "usb_led", 89: "over_led",
}
LAMP_IDS = {name: lamp for lamp, name in LAMP_NAMES.items()}

# E8/E10: lamp roles -> ("rgb"|"mono", channel), from the firmware profile. The service manual's LED matrix has 8
# ribbon LEDs (Ribbon0..7), no LED on SHIFT or MODE (mono 4 drives nothing, so it has no role here; the profiles list
# it under lamp_unassigned and the channel stays decoded), the STBY LED on the USB STOP/WAKE UP key
# (mono 24), and four LEDs M1 drives directly: ACTIVE (23), STBY (24), OVER (34) and USB CONNECT (25).
# Evidence for the 1.60 profile: E8 buttons; E13 USB LED = mono 25 (lit while the virtual stick is mounted, blinking
# during USB STOP hold and file writes); E14 OVER LED = mono 34 (master output peaks at or above about -0.9 dBFS,
# held about 0.5 s; the role name is an inference from that trigger); E10 one-hot mode indicators (MODE cycles
# 12 -> 11 -> 13 -> 14 = PITCH, REPEAT, USER1, USER2), bar position lamps 30-33 (bar 1 pulses while PLAY runs), strip
# segments 15-22 bottom to top, step n = RGB n-1 and pad n = RGB 0x10+n-1.
LAMP_ROLES = profiles.lamp_roles(PROFILE)

# Mono lamps are single-colour; this is only the host presentation colour. OVER (amber), USB CONNECT (yellow-green),
# the REC and PLAY light pipes, the bar and strip LEDs are sampled from the manufacturer's top-view product render
# and match the service manual's LED matrix colour classes (p11).
MONO_COLORS = {"over_led": (255, 218, 68), "usb_led": (159, 241, 61), "rec": (255, 51, 91), "active": (255, 60, 60),
               "usb_stop": (255, 60, 60), "play": (85, 247, 90), "lamp_pitch": (150, 255, 90),
               "lamp_repeat": (150, 255, 90), "lamp_user1": (150, 255, 90), "lamp_user2": (150, 255, 90),
               **{f"bar_led{i}": (91, 241, 49) for i in range(1, 5)},
               **{f"bar{i}": (70, 140, 255) for i in range(1, 5)},
               **{f"strip_led{i}": (7, 124, 255) for i in range(1, 9)}}
DIM = 0.35
# E12: TX byte 0x6e is the PAD/BUTTON BRIGHTNESS level (Utility: NORMAL = 4, DIMMED 1..3 = 3..1).
# The firmware already scales the RGB lamp bytes with it (step lamp 0x4a/0x25/0x18/0x12 at levels
# 4..1); mono lamp bits carry no level, so the host scales those by level/4. Other values
# (0 when settings are unavailable) are treated as full brightness.
BRIGHTNESS_MAX = 4


def brightness_scale(level):
    return level / BRIGHTNESS_MAX if 1 <= level <= BRIGHTNESS_MAX else 1.0
# E8: the all-lamps-on test page and step/pad lamps reach 0x4a; scale RGB to the full range.
RGB_FULL = 74


def led_srgb(duty):
    """LED PWM duty 0..1 is linear light; return the sRGB-encoded 0..1 value a display needs. Without this the
    firmware's dim track colours (about 22/74 per channel in MUTE mode) draw as muddy near-black hues."""
    duty = min(1.0, max(0.0, duty))
    return 12.92 * duty if duty <= 0.0031308 else 1.055 * duty ** (1 / 2.4) - 0.055


# A lit RGB LED under a diffuser reads as a vivid colour even at low duty, which a flat screen cannot show by
# luminance alone; the peak channel of a lit RGB lamp is lifted to at least this fraction (hue kept), so dim states
# (MUTE-mode track colours at about 22/74) stay as recognisable as on the unit. Full duty is unchanged.
LIT_RGB_FLOOR = 0.5


def rgb_lamp(raw):
    """Firmware RGB lamp bytes (0..RGB_FULL each) -> display (r, g, b)."""
    srgb = [led_srgb(v / RGB_FULL) for v in raw]
    peak = max(srgb)
    if peak <= 0:
        return (0, 0, 0)
    lift = (LIT_RGB_FLOOR + (1 - LIT_RGB_FLOOR) * peak) / peak
    return tuple(round(255 * min(1.0, c * lift)) for c in srgb)

# E3/E10: velocity -> (raw A, raw B).
# 1.60 profile: A_MIN 172 (with the staged all-242 calibration A=169 registers, A=168 does not), A_MAX 874 (firmware
# scaled maximum, knee 242 -> identity mapping; more only saturates), B 512 (fixed contact level).
PAD_A_MIN = PROFILE["panel"]["pad_curve"]["a_min"]
PAD_A_MAX = PROFILE["panel"]["pad_curve"]["a_max"]
PAD_B_CONTACT = PROFILE["panel"]["pad_curve"]["b_contact"]
# Frames per selector notification window (1.60: 8).
SELECTOR_WINDOW_FRAMES = PROFILE["panel"]["selector_window_frames"]


def PAD_CURVE(velocity):
    """E3/E10: host velocity 1..127 -> (pressure word A, contact word B).

    Hits need contact B >= 60 and a pressure A above the firmware's hit threshold. With the
    staged synthetic calibration (all knees 242, identity scaling) the threshold is A = 169
    (measured: 169 hits, 168 misses; the uncalibrated firmware registers from about 20) and the
    scaled maximum is 874. Velocity 1..127 therefore maps linearly onto A = 172..874, so every
    velocity registers under either profile and 127 reaches, but does not exceed, saturation.
    The shape between the endpoints is unverified: no OEM-visible velocity readout was found.
    B is a fixed contact level; physical force is not modelled."""
    if velocity <= 0:
        return 0, 0
    velocity = min(127, velocity)
    return PAD_A_MIN + round((velocity - 1) * (PAD_A_MAX - PAD_A_MIN) / 126), PAD_B_CONTACT


def decode_lamps(frame):
    """E8/E10 TX frame -> {control/lamp id: (r, g, b)} for roles with a proven channel.

    Mono lamps: first bit lit; where a second flag exists, 0 = full brightness and 1 = dim
    (observed: the active pad-mode button has the flag clear). RGB lamps are normalised
    against RGB_FULL. Both are PWM duty (linear light) and are sRGB-encoded for display (led_srgb).
    """
    raw = decode_tx(frame)
    if raw is None:
        return None
    ids = {**CONTROL_IDS, **LAMP_IDS}
    lamps = {}
    level = brightness_scale(raw["level"])
    for role, (kind, channel) in LAMP_ROLES.items():
        if kind == "rgb":
            lamps[ids[role]] = rgb_lamp(raw["rgb"][channel])
            continue
        lit, flag = raw["mono"][channel]
        base = MONO_COLORS.get(role, (255, 255, 255))
        scale = 0.0 if not lit else led_srgb((DIM if flag else 1.0) * level)
        lamps[ids[role]] = tuple(round(c * scale) for c in base)
    return lamps
