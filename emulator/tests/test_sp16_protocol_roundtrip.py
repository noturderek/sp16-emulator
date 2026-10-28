"""Panel protocol round trip and reference check.

Three layers:

1. Golden tables, hand-written from the panel protocol reference (internal docs/reference/toraiz-sp16-panel-protocol.md:
   "Panel RX frame", "Panel TX frame", "Touch strip", evidence E2, E7, E8, E11-E15). They are not derived from
   panel_map.py, so a wrong canonical entry fails here. Entries the reference does not give are left out; they are
   listed as unverified (grade U) in docs/changes/l3a-harness-easy.md.
2. Raw-frame fixtures: literal bytes decoded to the expected control, so the decoder is checked against wire bytes and
   not only against the tables it reads.
3. Self-consistency round trip: every control in panel-layout.json is sent as an event through PanelModel and decoded
   back, and every lamp role is written into a TX frame and read back by panel_map.decode_lamps. This proves the encoder
   and decoder agree; layers 1 and 2 prove they agree with the reference.
"""
import json
import struct
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import panel
import panel_map
import protocol

LAYOUT = json.loads((TOOLS / "panel-layout.json").read_text())
CONTROLS = LAYOUT["controls"]
ID = panel_map.CONTROL_IDS
LAMP_ID = {**panel_map.CONTROL_IDS, **panel_map.LAMP_IDS}
PAD_VELOCITY = 100
# A release waits out the hold window of its press (MIN_EDGE_FRAMES) before it drains, so this many steps settle it.
SETTLE_STEPS = 4 * panel.PanelModel.MIN_EDGE_FRAMES

# ---------------------------------------------------------------------------------------------------------------------
# Golden tables (layer 1). Source: the panel protocol reference, as cited next to each table.
# ---------------------------------------------------------------------------------------------------------------------

# RX buttons, frame byte -> bit (reference "Buttons", E2 and E15). Byte 1 bit 0 and byte 5 bit 7 are not buttons.
GOLDEN_BUTTONS = {
    "selector": (1, 1), "back": (1, 2), "home": (1, 3), "track": (1, 4), "mute": (1, 5), "slice": (1, 6),
    "scale": (1, 7),
    "shift": (2, 0), "rec": (2, 1), "stop": (2, 2), "play": (2, 3), "hold": (2, 4), "mode": (2, 5),
    "scene": (2, 6), "pattern": (2, 7),
    **{f"step{n}": (3, n - 1) for n in range(1, 9)},
    **{f"step{n}": (4, n - 9) for n in range(9, 17)},
    **{f"bar{n}": (5, n - 1) for n in range(1, 5)},
    "power": (5, 4), "active": (5, 5), "usb_stop": (5, 6),
}

# RX counters, offset (big-endian int16) -> wire sign of a +1 detent (reference "Counters": knobs at 0x30..0x3a,
# selector at 0x3c; "counter + n = n detents toward increase"; selector "+1 moves the highlight down").
GOLDEN_COUNTERS = {**{f"knob{n}": (0x30 + 2 * (n - 1), 1) for n in range(1, 7)}, "selector": (0x3c, 1)}

# RX 10-bit pots, name -> (offset, stored inverted) (reference "Other fields", E11): MAIN OUT direct at 0x28,
# DSF DRIVE and DSF HIGHPASS stored as 0x3ff minus the word at 0x26 and 0x2a.
GOLDEN_ANALOG_WORDS = {"main_out": (0x28, False), "drive": (0x26, True), "hp_cutoff": (0x2a, True)}
GOLDEN_ANALOG_FULL_SCALE = 1023

# RX pads (reference "Pads", E3): pad n pressure word at 0x06 + 2(n-1), contact word at 0x3f + 2(n-1).
GOLDEN_PAD_PRESSURE_BASE, GOLDEN_PAD_CONTACT_BASE = 0x06, 0x3f

# TX RGB lamps, role -> first of three bytes (reference "Panel TX frame"): pad n = channel 0x10+n-1 at bytes 1+3k;
# step n = channel n-1 at bytes 0x31+3k. Peak value 0x4a.
GOLDEN_RGB_OFFSETS = {
    **{f"step{n}": 0x31 + 3 * (n - 1) for n in range(1, 17)},
    **{f"pad{n}": 1 + 3 * (n - 1) for n in range(1, 17)},
}
GOLDEN_RGB_PEAK = 0x4a

# TX mono lamps, role -> channel (reference table of mono channels; E8, E10, E13, E14, E15). Mono 4 is listed as SHIFT
# and has no physical LED, so it has no role here (see the expected failure below).
GOLDEN_MONO_CHANNELS = {
    "track": 0, "mute": 1, "slice": 2, "scale": 3, "rec": 5, "stop": 6, "play": 7, "scene": 8, "pattern": 9,
    "hold": 10, "lamp_repeat": 11, "lamp_pitch": 12, "lamp_user1": 13, "lamp_user2": 14,
    **{f"strip_led{n}": 14 + n for n in range(1, 9)},
    "active": 23, "usb_stop": 24, "usb_led": 25,
    **{f"bar{n}": 25 + n for n in range(1, 5)},
    **{f"bar_led{n}": 29 + n for n in range(1, 5)},
    "over_led": 34,
}

# TX mono channels with a byte and bit given by the reference: USB LED = byte 0x65 bit 0 (E13), OVER LED = 0x65 bit 1
# (E14). The rest of the placement is not in the reference (grade U).
GOLDEN_MONO_BITS = {"usb_led": (0x65, 0), "over_led": (0x65, 1)}

# Factory test PAGE 1 (LED ALL ON) lights all 35 mono channels: bytes 0x61..0x65 read ff ff ff 7f 0f (E12).
GOLDEN_MONO_FINGERPRINT = {0x61: 0xff, 0x62: 0xff, 0x63: 0xff, 0x64: 0x7f, 0x65: 0x0f}

GOLDEN_LAMP_ROLES = {
    **{f"step{n}": ("rgb", n - 1) for n in range(1, 17)},
    **{f"pad{n}": ("rgb", 0x10 + n - 1) for n in range(1, 17)},
    **{role: ("mono", channel) for role, channel in GOLDEN_MONO_CHANNELS.items()},
}


def decode_strip(b0, b1):
    """E5: value = (0x7f - b0) | b1 << 8; position = value & 0x7f, contact = bit 15 (byte 1 bit 7)."""
    word = (0x7f - b0) | b1 << 8
    return word & 0x7f, word >> 15 & 1


def rx_frame(parts):
    """A 128-byte RX frame built from literal bytes: `parts` maps a frame offset to the bytes written there."""
    frame = bytearray(128)
    frame[0] = 0x01
    for offset, data in parts.items():
        frame[offset:offset + len(data)] = data
    return bytes(frame)


def tx_frame(parts):
    """A 128-byte TX frame built from literal bytes: header 0x02, marker 0xaa at 0x77, then `parts`."""
    frame = bytearray(128)
    frame[0] = 0x02
    frame[0x77] = 0xaa
    for offset, data in parts.items():
        frame[offset:offset + len(data)] = data
    return bytes(frame)


# ---------------------------------------------------------------------------------------------------------------------
# Self-consistency helpers (layer 3).
# ---------------------------------------------------------------------------------------------------------------------

def counter(frame, offset):
    return struct.unpack_from(">H", frame, offset)[0]


def settle(model):
    for _ in range(SETTLE_STEPS):
        model.step()
    return model.frame()


def button_round_trip(name, encode_bits, decode_bits):
    """Press then release `name` on a model built from encode_bits; True when decode_bits reads the same bits back."""
    model = panel.PanelModel(bits=encode_bits)
    if model.apply(ID[name], "press") != protocol.INPUT_OK:
        return False
    model.step()
    byte, bit = decode_bits[name]
    pressed = model.frame()[byte] >> bit & 1
    model.apply(ID[name], "release")
    released = settle(model)[byte] >> bit & 1
    return pressed == 1 and released == 0


def pad_round_trip(name):
    """Pressure (A) and contact (B) words of one pad, written on press and cleared on release."""
    index = int(name[3:]) - 1
    a_offset, b_offset = panel_map.PAD_A_OFFSET + 2 * index, panel_map.PAD_B_OFFSET + 2 * index
    model = panel.PanelModel()
    if model.apply(ID[name], "press", PAD_VELOCITY) != protocol.INPUT_OK:
        return False
    model.step()
    frame = model.frame()
    want_a, want_b = panel_map.PAD_CURVE(PAD_VELOCITY)
    pressed = counter(frame, a_offset) == want_a and counter(frame, b_offset) == want_b
    model.apply(ID[name], "release")
    frame = settle(model)
    return pressed and counter(frame, a_offset) == 0 and counter(frame, b_offset) == 0


def counter_round_trip(name):
    """One detent on an int16 counter: the decoded counter moves by the table's sign."""
    offset, sign = panel_map.COUNTERS[name]
    model = panel.PanelModel()
    before = counter(model.frame(), offset)
    if model.apply(ID[name], "delta", 1) != protocol.INPUT_OK:
        return False
    model.step()
    return counter(model.frame(), offset) == (before + sign) & 0xffff


def analog_round_trip(name, value=750):
    """A pot set to `value` (0..1000) reads back within one count, through the frame word and/or the shared state."""
    model = panel.PanelModel()
    if model.apply(ID[name], "value", value) != protocol.INPUT_OK:
        return False
    ok = True
    if name in panel_map.ANALOG_WORDS:
        offset, inverted = panel_map.ANALOG_WORDS[name]
        word = counter(model.frame(), offset)
        if inverted:
            word = panel_map.ANALOG_FULL_SCALE - word
        ok = ok and abs(round(word * 1000 / panel_map.ANALOG_FULL_SCALE) - value) <= 1
    if name in panel_map.HOST_ANALOG:
        # Host-only state (not a panel protocol field, grade U): u16 slots at 12 + 2 * index, FILTER TONE after the tone.
        index = panel_map.HOST_ANALOG.index(name)
        offset = 12 + 2 * index if index < 5 else 24
        ok = ok and struct.unpack_from("<H", model.analog_state(), offset)[0] == value
    return ok


def strip_round_trip(value=500):
    """Touch strip: position = round(value * 127 / 1000) with contact set, then contact cleared on release (E5)."""
    model = panel.PanelModel()
    if model.apply(ID["strip"], "value", value) != protocol.INPUT_OK:
        return False
    position, contact = decode_strip(*model.cdc_bytes()[:2])
    touched = position == round(value * 127 / 1000) and contact == 1
    model.apply(ID["strip"], "release")
    return touched and decode_strip(*model.cdc_bytes()[:2])[1] == 0


def lamp_frame(role, lit):
    """A TX frame with one lamp role lit or dark, placed by the canonical channel tables."""
    frame = bytearray(panel_map.TX_LENGTH)
    frame[0] = panel_map.TX_HEADER
    frame[panel_map.TX_MARKER_OFFSET] = panel_map.TX_MARKER
    kind, channel = panel_map.LAMP_ROLES[role]
    if kind == "rgb":
        if lit:
            offset = panel_map.RGB_CHANNELS[channel]
            frame[offset:offset + 3] = bytes((panel_map.RGB_FULL,) * 3)
    else:
        byte, bit, _ = panel_map.MONO_CHANNELS[channel]
        if lit:
            frame[byte] |= 1 << bit
    return bytes(frame)


def lit_lamps(frame):
    """Ids of the lamps that decode_lamps reports non-dark for a TX frame."""
    return {lamp for lamp, colour in panel_map.decode_lamps(frame).items() if colour != (0, 0, 0)}


# ---------------------------------------------------------------------------------------------------------------------
# Layer 1: canonical tables equal the reference golden tables.
# ---------------------------------------------------------------------------------------------------------------------

class GoldenTableTests(unittest.TestCase):
    def test_buttons_match_the_reference(self):
        self.assertEqual(panel_map.BUTTON_BITS, GOLDEN_BUTTONS)

    def test_counters_match_the_reference(self):
        self.assertEqual(panel_map.COUNTERS, GOLDEN_COUNTERS)

    def test_analog_words_match_the_reference(self):
        self.assertEqual(panel_map.ANALOG_WORDS, GOLDEN_ANALOG_WORDS)
        self.assertEqual(panel_map.ANALOG_FULL_SCALE, GOLDEN_ANALOG_FULL_SCALE)

    def test_pad_bases_and_frame_sizes_match_the_reference(self):
        self.assertEqual((panel_map.PAD_A_OFFSET, panel_map.PAD_B_OFFSET),
                         (GOLDEN_PAD_PRESSURE_BASE, GOLDEN_PAD_CONTACT_BASE))
        self.assertEqual(panel_map.FRAME_BYTES, 128)
        self.assertEqual(panel_map.TX_LENGTH, 128)
        self.assertEqual((panel_map.TX_HEADER, panel_map.TX_MARKER_OFFSET, panel_map.TX_MARKER), (0x02, 0x77, 0xaa))
        self.assertEqual(panel.PanelModel().frame()[0], 0x01)

    def test_rgb_channels_place_each_role_where_the_reference_says(self):
        self.assertEqual(panel_map.RGB_FULL, GOLDEN_RGB_PEAK)
        self.assertEqual(len(panel_map.RGB_CHANNELS), 32)
        for role, offset in GOLDEN_RGB_OFFSETS.items():
            with self.subTest(role=role):
                _, channel = GOLDEN_LAMP_ROLES[role]
                self.assertEqual(panel_map.RGB_CHANNELS[channel], offset)

    def test_mono_channels_cover_the_factory_page_bits(self):
        """All 35 mono channels occupy distinct bits, and together they are exactly the bits PAGE 1 lights."""
        positions = [(byte, bit) for byte, bit, _ in panel_map.MONO_CHANNELS.values()]
        expected = {(byte, bit) for byte, value in GOLDEN_MONO_FINGERPRINT.items() for bit in range(8)
                    if value >> bit & 1}
        self.assertEqual(len(positions), 35)
        self.assertEqual(len(set(positions)), 35)
        self.assertEqual(set(positions), expected)

    def test_reference_placed_mono_bits_match(self):
        for role, (byte, bit) in GOLDEN_MONO_BITS.items():
            with self.subTest(role=role):
                placed, placed_bit, _ = panel_map.MONO_CHANNELS[GOLDEN_MONO_CHANNELS[role]]
                self.assertEqual((placed, placed_bit), (byte, bit))

    def test_lamp_roles_match_the_reference(self):
        canonical = {role: spec for role, spec in panel_map.LAMP_ROLES.items() if role != "shift"}
        self.assertEqual(canonical, GOLDEN_LAMP_ROLES)

    @unittest.expectedFailure
    def test_shift_has_no_lamp_role(self):
        """Known discrepancy, cross-lane request: the reference (E15) says mono 4 drives no physical LED, yet
        panel_map.LAMP_ROLES maps a shift lamp to it. panel_map.py is held by another lane, so this stays an expected
        failure until that file drops the role. When it is fixed, this unexpected pass fails the module: remove the
        decorator then."""
        self.assertNotIn("shift", panel_map.LAMP_ROLES)


# ---------------------------------------------------------------------------------------------------------------------
# Layer 2: raw-frame fixtures, literal bytes decoded to the expected control.
# ---------------------------------------------------------------------------------------------------------------------

class RawFrameFixtureTests(unittest.TestCase):
    def test_rx_button_bytes_decode_to_the_named_controls(self):
        fixtures = [
            ({2: b"\x08"}, {"play"}),                 # byte 2 bit 3: PLAY
            ({1: b"\x02"}, {"selector"}),             # byte 1 bit 1: selector push
            ({5: b"\x10"}, {"power"}),                # byte 5 bit 4: POWER/STANDBY
            ({3: b"\x80"}, {"step8"}),                # byte 3 bit 7: STEP 8
            ({4: b"\x01"}, {"step9"}),                # byte 4 bit 0: STEP 9
            ({5: b"\x40"}, {"usb_stop"}),             # byte 5 bit 6: USB STOP / WAKE UP
        ]
        for parts, expected in fixtures:
            with self.subTest(expected=sorted(expected)):
                frame = rx_frame(parts)
                decoded = {name for name, (byte, bit) in GOLDEN_BUTTONS.items() if frame[byte] >> bit & 1}
                self.assertEqual(decoded, expected)

    def test_rx_counter_and_pad_words_are_big_endian(self):
        frame = rx_frame({0x30: b"\x00\x05", 0x06: b"\x02\xae", 0x3f: b"\x02\x00"})
        self.assertEqual(counter(frame, GOLDEN_COUNTERS["knob1"][0]), 5)
        self.assertEqual(counter(frame, GOLDEN_PAD_PRESSURE_BASE), 0x02ae)
        self.assertEqual(counter(frame, GOLDEN_PAD_CONTACT_BASE), 0x0200)

    def test_rx_analog_words_are_inverted_where_the_reference_says(self):
        frame = rx_frame({0x28: b"\x03\xff", 0x2a: b"\x03\xff"})
        full = GOLDEN_ANALOG_FULL_SCALE
        main = round(counter(frame, 0x28) * 1000 / full)
        hp = round((full - counter(frame, 0x2a)) * 1000 / full)
        self.assertEqual((main, hp), (1000, 0))

    def test_rx_crc_check_value(self):
        """CRC-16/X-25 (reflected 0x8408, init and final 0xffff) as the reference names it. The check value is the
        standard catalogue value for the ASCII string '123456789'; the reference itself gives no check value."""
        self.assertEqual(panel.crc16(b"123456789"), 0x906E)

    def test_touch_strip_bytes_decode_to_the_reference_positions(self):
        self.assertEqual(decode_strip(0x3f, 0x00), (64, 0))  # no contact: centred, E5 and the reference
        self.assertEqual(decode_strip(0x7f, 0x80), (0, 1))   # contact at position 0
        self.assertEqual(decode_strip(0x00, 0x80), (127, 1))  # contact at position 127

    def test_tx_step1_colour_bytes_decode_to_the_reference_colour(self):
        frame = tx_frame({0x31: b"\x26\x0b\x4a"})  # step 1 lamp, the NORMAL brightness sample in the reference
        self.assertEqual(panel_map.decode_tx(frame)["rgb"][0], (0x26, 0x0b, 0x4a))

    def test_tx_byte_0x65_bits_light_only_usb_or_over(self):
        self.assertEqual(lit_lamps(tx_frame({0x65: b"\x01"})), {panel_map.LAMP_IDS["usb_led"]})
        self.assertEqual(lit_lamps(tx_frame({0x65: b"\x02"})), {panel_map.LAMP_IDS["over_led"]})

    def test_tx_page1_bytes_light_every_mono_role(self):
        """The factory PAGE 1 bytes (E12) light all 35 mono channels; every golden mono role must be among them."""
        parts = {offset: bytes((value,)) for offset, value in GOLDEN_MONO_FINGERPRINT.items()}
        lit = lit_lamps(tx_frame(parts))
        for role in GOLDEN_MONO_CHANNELS:
            with self.subTest(role=role):
                self.assertIn(LAMP_ID[role], lit)


# ---------------------------------------------------------------------------------------------------------------------
# Layer 3: self-consistency round trip (encoder and decoder agree; placement is checked in layers 1 and 2).
# ---------------------------------------------------------------------------------------------------------------------

class RxRoundTripTests(unittest.TestCase):
    def test_every_layout_control_round_trips(self):
        checked = 0
        for control in CONTROLS:
            name, kind = control["name"], control["kind"]
            with self.subTest(control=name, kind=kind):
                if kind == "lamp":
                    # Lamps have no input: the protocol refuses an event for them.
                    self.assertEqual(panel.PanelModel().apply(control["id"], "press"), protocol.INPUT_INVALID)
                elif kind == "pad":
                    self.assertTrue(pad_round_trip(name))
                elif kind == "strip":
                    self.assertTrue(strip_round_trip())
                elif kind == "knob":
                    self.assertTrue(counter_round_trip(name))
                elif kind == "selector":
                    self.assertTrue(button_round_trip(name, panel_map.BUTTON_BITS, panel_map.BUTTON_BITS))
                    self.assertTrue(counter_round_trip(name))
                elif kind == "analog":
                    self.assertTrue(analog_round_trip(name))
                else:
                    # button, step and bar keys all dispatch through BUTTON_BITS.
                    self.assertTrue(button_round_trip(name, panel_map.BUTTON_BITS, panel_map.BUTTON_BITS))
                checked += 1
        self.assertEqual(checked, len(CONTROLS))

    def test_flipped_button_table_copy_fails_the_round_trip(self):
        """Negative case: the model is built from a copy with PLAY moved onto STOP's bit. Decoding with the real table
        must then fail, which shows the round trip really depends on the table."""
        flipped = dict(panel_map.BUTTON_BITS)
        flipped["play"] = (2, 2)  # the real entry is (2, 3)
        self.assertTrue(button_round_trip("play", panel_map.BUTTON_BITS, panel_map.BUTTON_BITS))
        self.assertFalse(button_round_trip("play", flipped, panel_map.BUTTON_BITS))

    def test_layout_names_match_the_control_and_lamp_tables(self):
        for control in CONTROLS:
            with self.subTest(control=control["name"]):
                table = panel_map.LAMP_NAMES if control["kind"] == "lamp" else panel_map.ALL_CONTROL_NAMES
                self.assertEqual(table[control["id"]], control["name"])


class TxLampRoundTripTests(unittest.TestCase):
    def test_every_lamp_role_round_trips(self):
        """Self-consistency for every canonical role, including the grade U placements (see the module docstring)."""
        for role in panel_map.LAMP_ROLES:
            with self.subTest(lamp=role):
                lit = panel_map.decode_lamps(lamp_frame(role, True))
                dark = panel_map.decode_lamps(lamp_frame(role, False))
                self.assertNotEqual(lit[LAMP_ID[role]], (0, 0, 0))
                self.assertEqual(dark[LAMP_ID[role]], (0, 0, 0))

    def test_rgb_roles_light_only_their_reference_offset(self):
        for role, offset in GOLDEN_RGB_OFFSETS.items():
            with self.subTest(role=role):
                lit = lit_lamps(tx_frame({offset: bytes((GOLDEN_RGB_PEAK,) * 3)}))
                rgb_lit = {r for r in GOLDEN_RGB_OFFSETS if LAMP_ID[r] in lit}
                self.assertEqual(rgb_lit, {role})


if __name__ == "__main__":
    unittest.main()
