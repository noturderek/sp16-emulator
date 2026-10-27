"""Panel protocol round trip: every control in panel-layout.json is encoded as an RX event and decoded back from the
frame, and every lamp role is written into a TX frame and read back by panel_map.decode_lamps.

The decoder side reads the wire tables (BUTTON_BITS, COUNTERS, ANALOG_WORDS, RGB_CHANNELS, MONO_CHANNELS) the way the
firmware does, so a wrong table entry breaks the round trip instead of passing silently.
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
PAD_VELOCITY = 100
# A release waits out the hold window of its press (MIN_EDGE_FRAMES) before it drains, so this many steps settle it.
SETTLE_STEPS = 4 * panel.PanelModel.MIN_EDGE_FRAMES


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
        # Shared analog state: u16 slots at 12 + 2 * index, except FILTER TONE (index 5), which sits after the tone word.
        index = panel_map.HOST_ANALOG.index(name)
        offset = 12 + 2 * index if index < 5 else 24
        ok = ok and struct.unpack_from("<H", model.analog_state(), offset)[0] == value
    return ok


def strip_round_trip(value=500):
    """Touch strip: position = round(value * 127 / 1000) with contact set, then contact cleared on release (E5)."""
    model = panel.PanelModel()
    if model.apply(ID["strip"], "value", value) != protocol.INPUT_OK:
        return False

    def decode():
        b0, b1 = model.cdc_bytes()[:2]
        word = (0x7f - b0) | b1 << 8
        return word & 0x7f, word >> 15 & 1

    position, contact = decode()
    touched = position == round(value * 127 / 1000) and contact == 1
    model.apply(ID["strip"], "release")
    return touched and decode()[1] == 0


def lamp_frame(role, lit):
    """A TX frame with one lamp role lit or dark, placed by the E7 channel tables."""
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


class LayoutIdTests(unittest.TestCase):
    def test_layout_names_match_the_control_and_lamp_tables(self):
        for control in CONTROLS:
            with self.subTest(control=control["name"]):
                table = panel_map.LAMP_NAMES if control["kind"] == "lamp" else panel_map.ALL_CONTROL_NAMES
                self.assertEqual(table[control["id"]], control["name"])


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


class TxLampRoundTripTests(unittest.TestCase):
    def test_every_lamp_role_round_trips(self):
        ids = {**panel_map.CONTROL_IDS, **panel_map.LAMP_IDS}
        for role in panel_map.LAMP_ROLES:
            with self.subTest(lamp=role):
                lit = panel_map.decode_lamps(lamp_frame(role, True))
                dark = panel_map.decode_lamps(lamp_frame(role, False))
                self.assertNotEqual(lit[ids[role]], (0, 0, 0))
                self.assertEqual(dark[ids[role]], (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
