"""Semantic panel protocol, RX frame model and layout/control-ID consistency."""
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import struct
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import native
import panel
import panel_map
import protocol


def counter(frame, offset):
    return struct.unpack_from(">H", frame, offset)[0]


def frames(model, count=1):
    """Advance the model by `count` frame periods and return the last frame."""
    frame = None
    for _ in range(count):
        model.step()
        frame = model.frame()
    return frame


class ProtocolTests(unittest.TestCase):
    def test_control_round_trip_and_bounds(self):
        wire = protocol.encode_control(7, "press", -3, 0xdeadbeef)
        self.assertEqual(protocol.decode_control(wire[5:]), (7, "press", -3, 0xdeadbeef))
        for bad in (b"\0" * 7, struct.pack(">BBhI", 0, 1, 0, 1), struct.pack(">BBhI", 1, 4, 0, 1)):
            with self.assertRaises(ValueError):
                protocol.decode_control(bad)

    def test_feedback_round_trip_and_size_bound(self):
        lamps = {70: (1, 2, 3), 5: (255, 0, 9)}
        wire = protocol.encode_feedback(5, True, lamps)
        self.assertEqual(protocol.decode_feedback(wire[5:]), (5, True, lamps))
        with self.assertRaises(ValueError):
            protocol.decode_feedback(wire[5:-1])
        with self.assertRaises(ValueError):
            protocol.encode_feedback(1, True, {i: (0, 0, 0) for i in range(129)})

    def test_research_operations_are_bounded(self):
        self.assertEqual(protocol.decode_research(bytes([1, 0x3c, 0xaa])), ("raw", 0x3c, b"\xaa"))
        self.assertEqual(protocol.decode_research(bytes([2, 1, 2, 3, 4]))[0], "cdc")
        for bad in (b"", bytes([1, 127, 1, 2]), bytes([2, 1]), bytes([4, 2]), bytes([9])):
            with self.assertRaises(ValueError):
                protocol.decode_research(bad)


class PanelModelTests(unittest.TestCase):
    def selector(self):
        return panel_map.CONTROL_IDS["selector"]

    def test_neutral_frame_matches_the_accepted_baseline_except_analog_pots(self):
        frame = bytearray(panel.PanelModel().frame())
        for offset, _ in panel_map.ANALOG_WORDS.values():
            frame[offset:offset + 2] = bytes(2)
        frame[124:126] = bytes(2)
        expected = bytearray(panel.packet(0))
        expected[124:126] = bytes(2)
        self.assertEqual(frame, expected)

    def test_analog_pots_write_the_proven_adc_words_with_inversion(self):
        model = panel.PanelModel()
        main, drive, hp = (panel_map.CONTROL_IDS[n] for n in ("main_out", "drive", "hp_cutoff"))
        model.apply(main, "value", 1000)
        model.apply(drive, "value", 400)
        model.apply(hp, "value", 0)
        frame = model.frame()
        self.assertEqual(counter(frame, 0x28), 1023)          # MASTER direct
        self.assertEqual(counter(frame, 0x26), 1023 - 409)    # DRIVE shown as 1023 - raw
        self.assertEqual(counter(frame, 0x2a), 1023)          # HIGHPASS shown as 1023 - raw
        self.assertEqual(model.apply(main, "press"), protocol.INPUT_INVALID)
        self.assertEqual(model.apply(drive, "value", 1001), protocol.INPUT_INVALID)
        # CUTOFF has no ADC word: it is host analog state for the shim's board model and leaves the frame alone.
        before = model.frame()
        self.assertEqual(model.apply(panel_map.CONTROL_IDS["lp_cutoff"], "value", 10), protocol.INPUT_OK)
        self.assertEqual(model.frame(), before)
        self.assertEqual(model.apply(panel_map.CONTROL_IDS["lp_cutoff"], "press"), protocol.INPUT_INVALID)

    def test_analog_knobs_start_with_the_filter_wide_open(self):
        frame = panel.PanelModel().frame()
        self.assertEqual(counter(frame, 0x28), 512)    # MAIN OUT centred, read directly
        self.assertEqual(counter(frame, 0x26), 1023)   # DRIVE at minimum (shown as 1023 - raw = 0)
        self.assertEqual(counter(frame, 0x2a), 1023)   # HP CUTOFF at minimum (shown as 1023 - raw = 0)

    def test_adc_readback_reports_the_words_in_the_frame_and_through_sessionctl(self):
        import sessionctl
        model = panel.PanelModel()
        main, drive = (panel_map.CONTROL_IDS[n] for n in ("main_out", "drive"))
        model.apply(main, "value", 500)
        model.apply(drive, "value", 1000)
        words = model.adc_words()
        frame = model.frame()
        self.assertEqual(words["0x28"], counter(frame, 0x28))
        self.assertEqual(words["0x26"], counter(frame, 0x26))
        self.assertEqual(words["drive"], 0)
        self.assertEqual(words["main_out"], words["0x28"])
        model.set_raw(0x2a, b"\x01\x02")
        self.assertEqual(model.adc_words()["0x2a"], 0x0102)
        self.assertEqual(sessionctl.adc_words({"guest": {"adc": words}}), words)
        with self.assertRaises(RuntimeError):
            sessionctl.adc_words({"guest": {}})

    def test_frame_has_valid_crc_and_header(self):
        frame = panel.PanelModel().frame()
        self.assertEqual(frame[0], 1)
        self.assertEqual(struct.unpack_from("<H", frame, 124)[0], panel.crc16(frame[:124]))

    def test_selector_detents_are_applied_one_per_step_with_wrap(self):
        model = panel.PanelModel()
        self.assertEqual(model.apply(self.selector(), "delta", 3), protocol.INPUT_OK)
        seen = []
        while model.pending:
            model.step()
            seen.append(counter(model.frame(), panel_map.SELECTOR_COUNTER))
        self.assertEqual(sorted(set(seen)), [1, 2, 3])
        model.apply(self.selector(), "delta", -4)
        for _ in range(4 * panel.PanelModel.SELECTOR_WINDOW_FRAMES):
            model.step()
        self.assertEqual(counter(model.frame(), panel_map.SELECTOR_COUNTER), 0xffff)

    def test_selector_released_once_per_window_knobs_every_frame(self):
        model = panel.PanelModel()
        window = panel.PanelModel.SELECTOR_WINDOW_FRAMES
        model.apply(self.selector(), "delta", 3)
        model.apply(panel_map.CONTROL_IDS["knob1"], "delta", 3)
        sel, knob = [], []
        for _ in range(2 * window + 2):
            model.step()
            sel.append(counter(model.frame(), panel_map.SELECTOR_COUNTER))
            knob.append(counter(model.frame(), panel_map.KNOB_COUNTERS[0]))
        steps = [i for i in range(len(sel)) if sel[i] != (sel[i - 1] if i else 0)]
        self.assertEqual(steps, [0, window, 2 * window])
        self.assertEqual(knob[:3], [1, 2, 3])

    def test_hot_cadence_after_input_then_expires(self):
        model = panel.PanelModel()
        model.step()
        model.apply(panel_map.CONTROL_IDS["knob1"], "delta", 1)
        model.step()
        n = 0
        while model.active():
            model.step()
            n += 1
            self.assertLess(n, 100)
        self.assertGreaterEqual(n, panel.PanelModel.HOT_FRAMES - 1)

    def test_selector_push_preserves_counter_and_releases(self):
        model = panel.PanelModel()
        model.apply(self.selector(), "delta", 1)
        model.step()
        model.apply(self.selector(), "press")
        held = frames(model)
        self.assertEqual(held[1], 0x02)
        self.assertEqual(counter(held, panel_map.SELECTOR_COUNTER), 1)
        model.apply(self.selector(), "release")
        self.assertEqual(frames(model, 3)[1], 0)

    def test_press_is_reference_counted(self):
        model = panel.PanelModel(bits={"play": (3, 0)})
        play = panel_map.CONTROL_IDS["play"]
        model.apply(play, "press")
        model.apply(play, "press")
        model.apply(play, "release")
        self.assertEqual(frames(model, 12)[3] & 1, 1)
        model.apply(play, "release")
        self.assertEqual(frames(model, 6)[3] & 1, 0)

    def test_unmapped_and_invalid_controls_are_rejected_not_guessed(self):
        model = panel.PanelModel(bits={}, counters={}, pad_curve=None)
        before = model.frame()
        self.assertEqual(model.apply(panel_map.CONTROL_IDS["shift"], "press"), protocol.INPUT_UNSUPPORTED)
        self.assertEqual(model.apply(panel_map.CONTROL_IDS["knob1"], "delta", 1), protocol.INPUT_UNSUPPORTED)
        self.assertEqual(model.apply(panel_map.CONTROL_IDS["pad1"], "press", 100), protocol.INPUT_UNSUPPORTED)
        self.assertEqual(model.apply(200, "press"), protocol.INPUT_INVALID)
        self.assertEqual(model.frame(), before)

    def test_independent_knob_counters_do_not_disturb_held_state(self):
        counters = {f"knob{i}": (panel_map.KNOB_COUNTERS[i - 1], 1) for i in range(1, 7)}
        model = panel.PanelModel(bits={"shift": (1, 4)}, counters=counters)
        model.apply(panel_map.CONTROL_IDS["shift"], "press")
        model.apply(panel_map.CONTROL_IDS["knob2"], "delta", 2)
        model.apply(panel_map.CONTROL_IDS["knob5"], "delta", -1)
        while model.active() and (model.step() or True) and model.pending:
            pass
        frame = model.frame()
        self.assertEqual(frame[1] & 0x10, 0x10)
        self.assertEqual(counter(frame, panel_map.KNOB_COUNTERS[1]), 2)
        self.assertEqual(counter(frame, panel_map.KNOB_COUNTERS[4]), 0xffff)
        self.assertEqual(counter(frame, panel_map.KNOB_COUNTERS[0]), 0)

    def test_pad_uses_measured_curve_only_when_one_exists(self):
        model = panel.PanelModel(pad_curve=lambda velocity: (velocity * 8, 100 if velocity else 0))
        model.apply(panel_map.CONTROL_IDS["pad3"], "press", 50)
        frame = frames(model)
        self.assertEqual(counter(frame, panel_map.PAD_A_OFFSET + 4), 400)
        self.assertEqual(counter(frame, panel_map.PAD_B_OFFSET + 4), 100)
        model.apply(panel_map.CONTROL_IDS["pad3"], "release", 0)
        self.assertEqual(counter(frames(model, 4), panel_map.PAD_A_OFFSET + 4), 0)

    def test_a_tap_shorter_than_a_frame_period_is_still_shown_for_several_frames(self):
        model = panel.PanelModel()
        pad = panel_map.CONTROL_IDS["pad3"]
        model.apply(pad, "press", 100)
        model.apply(pad, "release", 0)
        model.apply(pad, "press", 100)
        model.apply(pad, "release", 0)
        seen = [counter(frames(model), panel_map.PAD_A_OFFSET + 4) > 0 for _ in range(12)]
        self.assertEqual(seen, [True] * 3 + [False] * 3 + [True] * 3 + [False] * 3)

    def test_zero_sum_detents_do_not_emit_a_spurious_step_and_deltas_are_clamped(self):
        model = panel.PanelModel()
        selector = panel_map.CONTROL_IDS["selector"]
        model.apply(selector, "delta", 3)
        model.apply(selector, "delta", -3)
        self.assertFalse(model.active())
        model.apply(selector, "delta", 30000)
        self.assertLessEqual(abs(model.pending[panel_map.SELECTOR_COUNTER]), 4 * panel.PanelModel.MAX_DELTA)

    def test_pad_pressure_may_follow_a_press_that_is_still_queued(self):
        model = panel.PanelModel()
        pad = panel_map.CONTROL_IDS["pad2"]
        model.apply(pad, "press", 20)
        self.assertEqual(model.apply(pad, "value", 120), protocol.INPUT_OK)
        shown = counter(frames(model), panel_map.PAD_A_OFFSET + 2)
        self.assertEqual(shown, panel_map.PAD_CURVE(120)[0])

    def test_release_all_also_drops_research_pins(self):
        model = panel.PanelModel()
        model.set_raw(1, b"\x04")
        model.set_cdc_raw(b"\x01\x02\x03\x04")
        model.release_all()
        self.assertEqual(model.frame()[1], 0)
        self.assertEqual(model.cdc_bytes()[1] & 0x80, 0)

    def test_anything_held_keeps_the_fast_cadence_beyond_the_hot_window(self):
        for name, value in (("play", 0), ("pad4", 90), ("strip", 500)):
            model = panel.PanelModel(pad_curve=lambda v: (v, v))
            model.apply(panel_map.CONTROL_IDS[name], "press", value)
            for _ in range(4 * panel.PanelModel.HOT_FRAMES):
                model.step()
            self.assertEqual(model.hot, 0, name)
            self.assertTrue(model.active(), name)
            model.apply(panel_map.CONTROL_IDS[name], "release", 0)
            for _ in range(4 * panel.PanelModel.HOT_FRAMES):
                model.step()
            self.assertFalse(model.active(), name)

    def test_release_all_clears_holds_pads_and_queued_motion(self):
        model = panel.PanelModel(bits={"play": (3, 0)}, pad_curve=lambda v: (v, v))
        model.apply(panel_map.CONTROL_IDS["play"], "press")
        model.apply(panel_map.CONTROL_IDS["pad1"], "press", 90)
        model.apply(panel_map.CONTROL_IDS["selector"], "delta", 5)
        frames(model, 2)
        model.release_all()
        self.assertFalse(model.active())
        frame = model.frame()
        self.assertEqual(frame[1:6], bytes(5))
        self.assertEqual(counter(frame, panel_map.PAD_A_OFFSET), 0)

    def test_raw_research_overrides_are_applied_last_and_clearable(self):
        model = panel.PanelModel()
        model.set_raw(0x2c, b"\x12\x34")
        self.assertEqual(model.frame()[0x2c:0x2e], b"\x12\x34")
        model.clear_overrides()
        self.assertEqual(model.frame()[0x2c:0x2e], b"\0\0")

    def test_cdc_strip_bytes_follow_the_wire_decoder(self):
        model = panel.PanelModel()
        strip = panel_map.CONTROL_IDS["strip"]
        self.assertEqual(model.cdc_bytes()[1] & 0x80, 0)
        model.apply(strip, "press", 1000)
        data = model.cdc_bytes()
        self.assertEqual(((0x7f - data[0]) | data[1] << 8) & 0x7f, 127)
        self.assertEqual(data[1] & 0x80, 0x80)
        model.apply(strip, "value", 0)
        self.assertEqual(model.cdc_bytes()[0], 0x7f)
        model.apply(strip, "release", 0)
        centred = model.cdc_bytes()
        self.assertEqual((0x7f - centred[0], centred[1] & 0x80), (64, 0))
        self.assertEqual(model.apply(strip, "press", 1001), protocol.INPUT_INVALID)


class StartOffTests(unittest.TestCase):
    def test_start_off_controls_are_buttons_with_a_lamp(self):
        for name in panel_map.START_OFF:
            self.assertIn(name, panel_map.BUTTON_BITS)
            self.assertIn(name, panel_map.LAMP_ROLES)

    def test_a_lamp_that_stays_dark_is_never_pressed(self):
        start = panel.StartOff(("active",))
        presses = [n for t in range(61) for n in start.step(float(t), {"active": False})]
        self.assertEqual(presses, [])
        start.step(start.WINDOW + 1, {"active": False})
        self.assertEqual(start.pending, {})

    def test_a_lamp_that_is_dark_for_the_first_seconds_of_a_boot_is_still_handled(self):
        start = panel.StartOff(("active",))
        for t in range(10):
            self.assertEqual(start.step(float(t), {"active": False}), [])
        self.assertEqual(start.step(10.0, {"active": True}), [])
        self.assertEqual(start.step(13.0, {"active": True}), ["active"])

    def test_a_lit_lamp_is_pressed_once_it_has_settled_then_left_alone_when_it_goes_dark(self):
        start = panel.StartOff(("active",))
        self.assertEqual(start.step(0.0, {"active": True}), [])
        self.assertEqual(start.step(2.9, {"active": True}), [])
        self.assertEqual(start.step(3.0, {"active": True}), ["active"])
        self.assertEqual(start.step(3.2, {"active": True}), [])    # the lamp has not followed yet: wait
        self.assertEqual(start.step(3.4, {"active": False}), [])
        self.assertEqual(start.pending, {})
        self.assertEqual(start.step(9.0, {"active": True}), [])    # the user's own later presses are never undone

    def test_a_press_the_app_missed_is_retried_a_bounded_number_of_times(self):
        start = panel.StartOff(("active",))
        times = [t / 10 for t in range(0, 200)]
        pressed = [t for t in times if start.step(t, {"active": True})]
        self.assertEqual(pressed, [3.0, 5.5, 8.0, 10.5])
        self.assertEqual(start.pending, {})

    def test_an_unknown_lamp_state_waits_and_does_not_use_up_the_window(self):
        start = panel.StartOff(("active",))
        self.assertEqual([n for t in range(300) for n in start.step(float(t), {"active": None})], [])
        self.assertEqual(start.step(300.0, {"active": True}), [])
        self.assertEqual(start.step(303.0, {"active": True}), ["active"])

    def test_a_lamp_that_flickers_dark_before_lighting_is_still_handled(self):
        start = panel.StartOff(("active",))
        self.assertEqual(start.step(0.0, {"active": False}), [])
        self.assertEqual(start.step(1.0, {"active": False}), [])
        self.assertEqual(start.step(1.5, {"active": True}), [])
        self.assertEqual(start.step(4.4, {"active": True}), [])
        self.assertEqual(start.step(4.5, {"active": True}), ["active"])

    def test_nothing_is_pressed_after_the_window(self):
        start = panel.StartOff(("active",))
        self.assertEqual(start.step(0.0, {"active": True}), [])
        self.assertEqual(start.step(start.WINDOW + 1, {"active": True}), [])
        self.assertEqual(start.pending, {})

    def test_bridge_wires_the_helper_to_the_decoded_lamps(self):
        source = (TOOLS / "guest-bridge.py").read_text()
        self.assertIn("start_off = StartOff(panel_map.START_OFF)", source)
        self.assertIn("start_off.step(now, lit)", source)
        self.assertIn('state["lamps"].get(panel_map.CONTROL_IDS[name]', source)


class BoardModelTests(unittest.TestCase):
    """Host side of the shim's analog board model: the shared state file and the board-model metrics lines."""

    def test_host_analog_controls_fill_the_shared_state_and_leave_untouched_ones_neutral(self):
        model = panel.PanelModel()
        state = model.analog_state()
        self.assertEqual(len(state), 64)
        self.assertEqual(state[:8], b"SP16ANA1")
        self.assertEqual(struct.unpack_from("<5H", state, 12), (0xFFFF,) * 5)
        self.assertEqual(struct.unpack_from("<H", state, 24)[0], 0xFFFF)
        self.assertEqual(struct.unpack_from("<H", state, 22)[0], panel_map.INPUT_TONE_HZ)
        for name, value in (("main_out", 900), ("input_level", 250), ("lp_resonance", 1000), ("input_tone", 600),
                            ("filter_tone", 700)):
            self.assertEqual(model.apply(panel_map.CONTROL_IDS[name], "value", value), protocol.INPUT_OK)
        self.assertTrue(model.analog_changed)
        state = model.analog_state()
        self.assertEqual(struct.unpack_from("<5H", state, 12), (900, 250, 0xFFFF, 1000, 600))
        self.assertEqual(struct.unpack_from("<H", state, 24)[0], 700)
        self.assertFalse(model.analog_changed)
        self.assertEqual(counter(model.frame(), 0x28), round(900 * 1023 / 1000))  # MAIN OUT keeps its ADC word
        self.assertEqual(model.apply(panel_map.CONTROL_IDS["input_tone"], "value", 1001), protocol.INPUT_INVALID)

    def test_state_file_updates_in_place_under_an_even_sequence(self):
        import tempfile
        model = panel.PanelModel()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analog"
            writer = panel.AnalogStateFile(str(path), model)
            model.apply(panel_map.CONTROL_IDS["lp_cutoff"], "value", 123)
            writer.update()
            writer.update()
            data = path.read_bytes()
            writer.close()
        self.assertEqual(struct.unpack_from("<I", data, 8)[0], 4)
        self.assertEqual(struct.unpack_from("<H", data, 16)[0], 123)

    def test_power_key_drives_rx_bit_5_4(self):
        model = panel.PanelModel()
        model.apply(panel_map.CONTROL_IDS["power"], "press")
        self.assertEqual(frames(model)[5] & 0x10, 0x10)
        model.apply(panel_map.CONTROL_IDS["power"], "release")
        self.assertEqual(frames(model, 4)[5] & 0x10, 0)
        self.assertNotIn(100, panel_map.CONTROL_NAMES)  # no place in the panel layout

    def test_pressure_controls_hold_a_raw_pad_pressure_word(self):
        model = panel.PanelModel()
        ids = panel_map.CONTROL_IDS
        self.assertEqual([ids[f"pressure{n}"] for n in (1, 16)], [110, 125])
        self.assertEqual(len(set(panel_map.ALL_CONTROL_NAMES)), len(panel_map.ALL_CONTROL_NAMES))
        for pad, raw in ((1, 48), (5, 242), (16, 504)):
            self.assertEqual(model.apply(ids[f"pressure{pad}"], "value", raw), protocol.INPUT_OK)
        frame = frames(model)
        for pad, raw in ((1, 48), (5, 242), (16, 504)):
            self.assertEqual(counter(frame, panel_map.PAD_A_OFFSET + 2 * (pad - 1)), raw)
            self.assertEqual(counter(frame, panel_map.PAD_B_OFFSET + 2 * (pad - 1)), panel_map.PAD_B_CONTACT)
        self.assertEqual(counter(frame, panel_map.PAD_A_OFFSET + 2), 0)             # pad 2 untouched
        model.apply(ids["pressure1"], "value", 0)
        frame = frames(model)
        self.assertEqual((counter(frame, panel_map.PAD_A_OFFSET), counter(frame, panel_map.PAD_B_OFFSET)), (0, 0))

    def test_pressure_controls_reject_other_actions_and_out_of_range_values(self):
        model = panel.PanelModel()
        control = panel_map.CONTROL_IDS["pressure3"]
        self.assertEqual(model.apply(control, "press", 100), protocol.INPUT_INVALID)
        self.assertEqual(model.apply(control, "value", 1024), protocol.INPUT_INVALID)
        self.assertEqual(model.apply(control, "value", -1), protocol.INPUT_INVALID)

    def test_a_velocity_press_still_overrides_a_held_pressure(self):
        model = panel.PanelModel()
        model.apply(panel_map.CONTROL_IDS["pressure1"], "value", 300)
        model.apply(panel_map.CONTROL_IDS["pad1"], "press", 127)
        frame = frames(model, 6)
        self.assertEqual(counter(frame, panel_map.PAD_A_OFFSET), panel_map.PAD_CURVE(127)[0])

    def test_bridge_reads_the_board_model_lines(self):
        spec = importlib.util.spec_from_file_location("sp16_bridge_lines", TOOLS / "guest-bridge.py")
        source = (TOOLS / "guest-bridge.py").read_text()
        start, end = source.index("DAC_WORD = "), source.index("def main():")
        scope = {"re": re}
        exec(compile(source[start:end], str(spec.origin), "exec"), scope)
        board_line = scope["board_line"]
        self.assertEqual(board_line("[sp16-ui-stub] dac hpf word=0x1abc ctrl=1 code=2748 writes=7"),
                         ("dac", {"word": 0x1abc, "ctrl": 1, "code": 2748, "writes": 7}))
        key, value = board_line("[sp16-ui-stub] filter loop send_peak=0.5000 return_peak=0.2500 frames=44100 "
                                "underruns=3 drops=0 lp_hz=1200 hp=dac")
        self.assertEqual((key, value["return_peak"], value["lp_hz"], value["hp"]), ("filter_loop", .25, 1200, "dac"))
        self.assertEqual(board_line("[sp16-ui-stub] midi loopback /dev/ttymxc1 first bytes=4"),
                         ("midi_loopback", {"node": "/dev/ttymxc1", "first_bytes": 4}))
        self.assertIsNone(board_line("[sp16-ui-stub] dac write of 7 bytes ignored (expected 2)"))
        self.assertEqual(board_line("[sp16-ui-stub] gpio GPIO5_IO05=1 SEL_xTHRU/DSF"), ("gpio", ("GPIO5_IO05", 1)))

    def test_shim_board_lines_match_the_bridge_patterns(self):
        source = (TOOLS / "hardware_stub.c").read_text()
        self.assertIn('"[sp16-ui-stub] dac hpf word=0x%04x ctrl=%u code=%u writes=%lu\\n"', source)
        self.assertIn("filter loop send_peak=%.4f return_peak=%.4f frames=%lu underruns=%lu drops=%lu lp_hz=%.0f hp=%s",
                      source)
        self.assertIn('"[sp16-ui-stub] midi loopback %s first bytes=%ld\\n"', source)
        self.assertIn('"[sp16-ui-stub] gpio GPIO%u_IO%02u=%u %s\\n"', source)


class GeometryTests(unittest.TestCase):
    """Layout resource properties that the native view relies on (no AppKit needed)."""

    @classmethod
    def setUpClass(cls):
        cls.layout = json.loads((TOOLS / "panel-layout.json").read_text())
        cls.inputs = [c for c in cls.layout["controls"] if c["kind"] != "lamp"]

    def test_layout_draws_no_vendor_logos(self):
        self.assertEqual([d for d in self.layout["decor"] if d["type"] == "svg"], [])

    @staticmethod
    def bounds(control):
        if "r" in control and "w" not in control:
            return (control["cx"] - control["r"], control["cy"] - control["r"],
                    control["cx"] + control["r"], control["cy"] + control["r"])
        half_w, half_h = control["w"] / 2, control["h"] / 2
        return (control["cx"] - half_w, control["cy"] - half_h,
                control["cx"] + half_w, control["cy"] + half_h)

    def test_every_control_is_inside_the_canvas(self):
        canvas = self.layout["canvas"]
        for control in self.layout["controls"]:
            x0, y0, x1, y1 = self.bounds(control)
            self.assertTrue(0 <= x0 and 0 <= y0 and x1 <= canvas["width"] and y1 <= canvas["height"],
                            control["name"])

    def test_input_hit_regions_do_not_overlap(self):
        boxes = [(c["name"], self.bounds(c)) for c in self.inputs]
        for i, (name_a, a) in enumerate(boxes):
            for name_b, b in boxes[i + 1:]:
                overlap = min(a[2], b[2]) - max(a[0], b[0]) > 0.5 and min(a[3], b[3]) - max(a[1], b[1]) > 0.5
                self.assertFalse(overlap, f"{name_a} overlaps {name_b}")

    def test_lcd_active_area_is_five_by_three_inside_its_bezel_and_clear_of_controls(self):
        lcd = self.layout["lcd"]
        active, bezel = lcd["active"], lcd["bezel"]
        self.assertAlmostEqual(active["w"] * 480, active["h"] * 800, delta=2)
        self.assertTrue(bezel["x"] <= active["x"] and bezel["y"] <= active["y"])
        self.assertTrue(active["x"] + active["w"] <= bezel["x"] + bezel["w"])
        self.assertTrue(active["y"] + active["h"] <= bezel["y"] + bezel["h"])
        for control in self.inputs:
            x0, y0, x1, y1 = self.bounds(control)
            clear = (x1 <= bezel["x"] or x0 >= bezel["x"] + bezel["w"] or
                     y1 <= bezel["y"] or y0 >= bezel["y"] + bezel["h"])
            self.assertTrue(clear, control["name"])

    def test_pad_grid_numbering_and_knob_alignment(self):
        by_name = {c["name"]: c for c in self.layout["controls"]}
        self.assertLess(by_name["pad13"]["cy"], by_name["pad1"]["cy"])  # 13-16 top row, 1-4 bottom row
        self.assertLess(by_name["pad1"]["cx"], by_name["pad4"]["cx"])
        columns = [by_name[f"knob{i}"]["cx"] for i in range(1, 7)]
        self.assertEqual(columns, sorted(columns))
        lcd = self.layout["lcd"]["active"]
        pitch = lcd["w"] / 6
        for index, x in enumerate(columns):
            centre = lcd["x"] + pitch * (index + 0.5)
            self.assertLess(abs(x - centre), pitch / 2, f"knob{index + 1} under its LCD parameter column")


class PadCalibrationTests(unittest.TestCase):
    """The synthetic calibration staged by the full-panel profile must satisfy the OEM loader's constraints."""

    def test_xml_has_sixteen_in_range_pad_values(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(native.pad_calibration_xml(242))
        self.assertEqual(root.tag, "PadCalibrationData")
        self.assertEqual(sorted(root.attrib), sorted(f"CALIB_VALUE_PAD{n}" for n in range(1, 17)))
        for value in root.attrib.values():
            self.assertTrue(0x30 <= int(value) <= 0x1f8)

    def test_out_of_range_values_are_rejected(self):
        for bad in (0, 47, 505, 1023, -1, "242", None, 242.0):
            with self.assertRaises(ValueError):
                native.pad_calibration_xml(bad)
        native.pad_calibration_xml(0x30)
        native.pad_calibration_xml(0x1f8)

    def test_fullpanel_is_calibrated_by_default_and_others_are_not(self):
        self.assertEqual(native.resolve_pad_calibration("fullpanel"), native.DEFAULT_PAD_CALIBRATION)
        self.assertEqual(native.resolve_pad_calibration("fullpanel", 300), 300)
        self.assertIsNone(native.resolve_pad_calibration("fullpanel", uncalibrated=True))
        self.assertIsNone(native.resolve_pad_calibration(None))  # no profile, no calibration file
        self.assertNotIn("padcal", native.PROFILES)

    def test_invalid_combinations_are_rejected(self):
        for args in ((None, 242, False), (None, None, True), ("fullpanel", 242, True)):
            with self.assertRaises(ValueError):
                native.resolve_pad_calibration(*args)
        with self.assertRaises(ValueError):
            native.build("/nonexistent", profile="testmode", pad_calibration=242)
        with self.assertRaises(ValueError):
            native.build("/nonexistent", profile="fullpanel", uncalibrated=True, pad_calibration=242)


class IconTests(unittest.TestCase):
    def test_icon_sources_exist_and_neutral_art_is_flat_vector(self):
        self.assertTrue((TOOLS / native.ICON_TOOL).is_file())
        text = (TOOLS / native.NEUTRAL_ICON).read_text()
        self.assertIn('fill="#', text)  # the icon generator renders flat fills
        for forbidden in ("<image", "<text", "<script"):
            self.assertNotIn(forbidden, text)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc") and shutil.which("iconutil"),
                         "needs the macOS toolchain")
    def test_icon_builds_to_a_valid_icns(self):
        with tempfile.TemporaryDirectory() as directory:
            output, resources = Path(directory), Path(directory) / "Resources"
            resources.mkdir()
            native.build_icon(output, resources)
            icns = (resources / f"{native.DECOUPLED_ICON_FILE}.icns").read_bytes()
            self.assertEqual(icns[:4], b"icns")
            self.assertGreater(len(icns), 50_000)


class MapTests(unittest.TestCase):
    def test_button_objects_cover_every_wire_bit_once(self):
        self.assertEqual(len(panel_map.BUTTON_OBJECTS), 37)
        self.assertEqual(len(set(panel_map.BUTTON_OBJECTS)), 37)
        self.assertNotIn((1, 0), panel_map.BUTTON_OBJECTS)
        self.assertNotIn(panel_map.SELECTOR_PUSH, panel_map.BUTTON_OBJECTS)

    def test_mapped_roles_are_known_unique_controls(self):
        for name, bit in panel_map.BUTTON_BITS.items():
            self.assertIn(name, panel_map.CONTROL_IDS)
            self.assertTrue(bit == panel_map.SELECTOR_PUSH or bit in panel_map.BUTTON_OBJECTS)
        bits = list(panel_map.BUTTON_BITS.values())
        self.assertEqual(len(bits), len(set(bits)))

    def test_layout_resource_matches_control_ids(self):
        layout = json.loads((TOOLS / "panel-layout.json").read_text())
        by_id = {c["id"]: c for c in layout["controls"]}
        self.assertEqual(len(by_id), len(layout["controls"]))
        for control_id, name in panel_map.CONTROL_NAMES.items():
            self.assertEqual(by_id[control_id]["name"], name)
        fb = layout["lcd"]["framebuffer"]
        active = layout["lcd"]["active"]
        self.assertAlmostEqual(active["w"] / active["h"], fb["width"] / fb["height"], places=2)

    def test_layout_starts_the_analog_knobs_where_the_panel_model_does(self):
        layout = json.loads((TOOLS / "panel-layout.json").read_text())
        initial = {c["name"]: c.get("initial") for c in layout["controls"] if c["kind"] == "analog"}
        self.assertEqual(initial, panel_map.ANALOG_DEFAULTS)
        self.assertTrue(all(0 <= value <= 1000 for value in initial.values()))

    def test_tx_channel_tables_cover_the_wire_frame_without_overlap(self):
        self.assertEqual(len(panel_map.RGB_CHANNELS), 32)
        covered = sorted(byte for offset in panel_map.RGB_CHANNELS.values() for byte in range(offset, offset + 3))
        self.assertEqual(covered, list(range(1, 97)))
        self.assertEqual(len(panel_map.MONO_CHANNELS), 35)
        bits = [(byte, bit) for byte, bit, _ in panel_map.MONO_CHANNELS.values()]
        self.assertEqual(len(bits), len(set(bits)))
        self.assertTrue(all(0x61 <= byte <= 0x65 and 0 <= bit < 8 for byte, bit in bits))

    def test_decode_tx_reads_rgb_and_mono_fields_and_rejects_other_frames(self):
        frame = bytearray(128)
        frame[0], frame[0x77] = 2, 0xaa
        frame[1:4] = b"\x10\x20\x30"
        frame[0x31:0x34] = b"\x01\x02\x03"
        frame[99], frame[0x6a], frame[0x65] = 0x10, 0x10, 0x02
        decoded = panel_map.decode_tx(bytes(frame))
        self.assertEqual(decoded["rgb"][0x10], (0x10, 0x20, 0x30))
        self.assertEqual(decoded["rgb"][0], (1, 2, 3))
        self.assertEqual(decoded["mono"][0], (1, 1))
        self.assertEqual(decoded["mono"][34], (1, None))
        self.assertFalse(decoded["standby"])
        frame[0x67] = 0xfe
        self.assertFalse(panel_map.decode_tx(bytes(frame))["standby"])
        frame[0x67] = 0x01
        decoded = panel_map.decode_tx(bytes(frame))
        self.assertTrue(decoded["standby"])
        self.assertEqual(decoded["extra"][1], 1)
        frame[0] = 1
        self.assertIsNone(panel_map.decode_tx(bytes(frame)))
        frame[0], frame[0x77] = 2, 0
        self.assertIsNone(panel_map.decode_tx(bytes(frame)))
        self.assertIsNone(panel_map.decode_tx(bytes(127)))

    def test_button_bits_are_the_firmware_labelled_test_page_bits(self):
        bits = panel_map.BUTTON_BITS
        self.assertEqual((bits["track"], bits["play"], bits["step9"], bits["step16"], bits["bar4"]),
                         ((1, 4), (2, 3), (4, 0), (4, 7), (5, 3)))
        controls = [name for name in bits if name != "selector"]
        self.assertEqual(len(controls), 37)  # every dispatched button object; the selector push is not one of them
        self.assertEqual(bits["power"], (5, 4))
        self.assertEqual(sorted(bits[name] for name in controls), sorted(panel_map.BUTTON_OBJECTS))
        self.assertEqual(len(set(bits.values())), len(bits))

    def test_default_pad_curve_reaches_the_firmware_hit_thresholds(self):
        a, b = panel_map.PAD_CURVE(1)
        self.assertGreaterEqual(a, 20)
        self.assertGreaterEqual(b, 60)
        self.assertEqual(panel_map.PAD_CURVE(127)[0], panel_map.PAD_A_MAX)
        values = [panel_map.PAD_CURVE(v)[0] for v in range(1, 128)]
        self.assertEqual(values, sorted(set(values)))  # strictly monotonic
        self.assertGreater(values[0], 169)  # registers under the staged calibration
        self.assertLessEqual(values[-1], 874)
        self.assertEqual(panel_map.PAD_CURVE(0), (0, 0))
        model = panel.PanelModel()
        pad = panel_map.CONTROL_IDS["pad7"]
        self.assertEqual(model.apply(pad, "value", 50), protocol.INPUT_INVALID)
        model.apply(pad, "press", 100)
        first = frames(model)
        model.apply(pad, "value", 40)
        second = model.frame()
        self.assertEqual(first[panel_map.PAD_B_OFFSET + 12:panel_map.PAD_B_OFFSET + 14],
                         second[panel_map.PAD_B_OFFSET + 12:panel_map.PAD_B_OFFSET + 14])
        self.assertNotEqual(first[panel_map.PAD_A_OFFSET + 12:panel_map.PAD_A_OFFSET + 14],
                            second[panel_map.PAD_A_OFFSET + 12:panel_map.PAD_A_OFFSET + 14])

    def test_lamp_ids_are_disjoint_from_controls_and_cover_every_role(self):
        self.assertFalse(set(panel_map.LAMP_IDS) & set(panel_map.CONTROL_IDS))
        known = set(panel_map.CONTROL_IDS) | set(panel_map.LAMP_IDS)
        self.assertTrue(set(panel_map.LAMP_ROLES) <= known)
        layout = json.loads((TOOLS / "panel-layout.json").read_text())
        by_name = {c["name"]: c["id"] for c in layout["controls"]}
        for name, lamp in panel_map.LAMP_IDS.items():
            self.assertEqual(by_name[name], lamp)

    def test_usb_led_is_mono_25(self):
        frame = bytearray(128)
        frame[0], frame[0x77] = 2, 0xaa
        usb = panel_map.LAMP_IDS["usb_led"]
        self.assertEqual(panel_map.LAMP_ROLES["usb_led"], ("mono", 25))
        self.assertEqual(panel_map.decode_lamps(bytes(frame))[usb], (0, 0, 0))
        frame[0x65] = 0x01
        self.assertEqual(panel_map.decode_lamps(bytes(frame))[usb], panel_map.MONO_COLORS["usb_led"])
        frame[0x65] = 0x02  # mono 34 must not drive it
        self.assertEqual(panel_map.decode_lamps(bytes(frame))[usb], (0, 0, 0))

    def test_over_led_is_mono_34(self):
        frame = bytearray(128)
        frame[0], frame[0x77] = 2, 0xaa
        over = panel_map.LAMP_IDS["over_led"]
        self.assertEqual(panel_map.LAMP_ROLES["over_led"], ("mono", 34))
        self.assertEqual(panel_map.decode_lamps(bytes(frame))[over], (0, 0, 0))
        frame[0x65] = 0x02
        self.assertNotEqual(panel_map.decode_lamps(bytes(frame))[over], (0, 0, 0))
        frame[0x65] = 0x01  # mono 25 (USB) must not drive it
        self.assertEqual(panel_map.decode_lamps(bytes(frame))[over], (0, 0, 0))

    def test_mono_brightness_and_rgb_normalisation(self):
        frame = bytearray(128)
        frame[0], frame[0x77] = 2, 0xaa
        frame[99] = 0x13   # TRACK(bit4), MUTE(bit1), HOLD(bit0) lit
        frame[0x6a] = 0x02  # only MUTE's flag set: dim
        frame[0x31:0x34] = bytes((74, 37, 0))
        lamps = panel_map.decode_lamps(bytes(frame))
        ids = panel_map.CONTROL_IDS
        self.assertEqual(lamps[ids["track"]], (255, 255, 255))
        self.assertEqual(lamps[ids["mute"]], (160, 160, 160))  # duty 0.35, sRGB-encoded
        self.assertEqual(lamps[ids["slice"]], (0, 0, 0))
        self.assertEqual(lamps[ids["step1"]], (255, 188, 0))  # 37/74 duty, sRGB-encoded

    def test_mono_lamps_scale_with_the_pad_button_brightness_level(self):
        def decode(level):
            frame = bytearray(128)
            frame[0], frame[0x77], frame[0x6e] = 2, 0xaa, level
            frame[99] = 0x01                      # HOLD (mono 10), no second flag
            frame[0x31:0x34] = bytes((0x25, 0x13, 0x25))  # RGB already scaled by the firmware
            return panel_map.decode_lamps(bytes(frame))
        hold, step1 = panel_map.CONTROL_IDS["hold"], panel_map.CONTROL_IDS["step1"]
        self.assertEqual(decode(4)[hold], (255, 255, 255))
        self.assertEqual(decode(2)[hold], (188, 188, 188))
        self.assertEqual(decode(1)[hold], (137, 137, 137))
        self.assertEqual(decode(0)[hold], (255, 255, 255))  # no level: full
        self.assertEqual(decode(4)[step1], decode(1)[step1])  # RGB is not rescaled again

    def test_proven_lamp_roles_decode_buttons_steps_and_bars(self):
        frame = bytearray(128)
        frame[0], frame[0x77] = 2, 0xaa
        frame[99] = 0x10   # TRACK lamp (mono 0)
        frame[100] = 0x10  # PLAY lamp (mono 7)
        frame[0x61] = 0x40  # BAR 3 (mono 28)
        frame[0x31 + 3 * 4:0x31 + 3 * 4 + 3] = b"\x20\x30\x40"  # STEP 5 = RGB channel 4
        lamps = panel_map.decode_lamps(bytes(frame))
        ids = panel_map.CONTROL_IDS
        self.assertEqual(lamps[ids["track"]], (255, 255, 255))
        self.assertEqual(lamps[ids["play"]], panel_map.MONO_COLORS["play"])
        self.assertEqual(lamps[ids["mute"]], (0, 0, 0))
        self.assertEqual(lamps[ids["bar3"]], (70, 140, 255))
        self.assertEqual(lamps[ids["step5"]], panel_map.rgb_lamp((0x20, 0x30, 0x40)))

    def test_dim_rgb_lamps_keep_their_hue_and_stay_visible(self):
        dim_red = panel_map.rgb_lamp((22, 1, 0))      # MUTE-mode track colour
        self.assertGreaterEqual(max(dim_red), round(255 * panel_map.LIT_RGB_FLOOR))
        self.assertGreater(dim_red[0], 4 * dim_red[1])
        self.assertEqual(dim_red[2], 0)
        self.assertEqual(panel_map.rgb_lamp((74, 74, 74)), (255, 255, 255))
        self.assertEqual(panel_map.rgb_lamp((0, 0, 0)), (0, 0, 0))
        self.assertIsNone(panel_map.decode_lamps(bytes(128)))


class AccessibilityEnabledContract(unittest.TestCase):
    def test_ax_elements_override_enabled(self):
        src = (TOOLS / "panel-view.swift").read_text()
        self.assertIn("override func isAccessibilityEnabled() -> Bool", src)
        self.assertIn("func accessibilityEnabled(forControl", src)


# Compiled with the real panel-view.swift: feeds the same NSEvents and accessibility actions to a panel with UI input on
# and off (bench mode, `--no-ui-input`) and prints what reached the delegate for each.
PANEL_INPUT_HARNESS = r"""import AppKit

// Feeds the same real NSEvents and accessibility actions to a panel with UI input on and off; prints what reached the
// delegate for each, plus the bench badge state, as JSON.
final class Recorder: PanelViewDelegate {
    var events: [String] = []
    func panelControl(_ id: UInt8, action: UInt8, value: Int16) { events.append("control \(id) \(action)") }
    func panelTouch(x: Int, y: Int, action: UInt8) { events.append("touch \(action)") }
}

func exercise(layout: URL, inputEnabled: Bool) -> [String: Any] {
    let panel = PanelView(layoutURL: layout)
    let recorder = Recorder()
    panel.delegate = recorder
    panel.frame = NSRect(x: 0, y: 0, width: 1280, height: 800)
    panel.uiInputEnabled = inputEnabled
    func at(_ p: NSPoint) -> NSPoint { panel.convert(p, to: nil) }
    func centre(_ r: NSRect) -> NSPoint { NSPoint(x: r.midX, y: r.midY) }
    func mouse(_ type: NSEvent.EventType, _ p: NSPoint) -> NSEvent {
        NSEvent.mouseEvent(with: type, location: at(p), modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
                           eventNumber: 0, clickCount: 1, pressure: 1)!
    }
    func key(_ type: NSEvent.EventType, _ chars: String, _ code: UInt16, _ flags: NSEvent.ModifierFlags = []) -> NSEvent {
        NSEvent.keyEvent(with: type, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0, context: nil,
                         characters: chars, charactersIgnoringModifiers: chars, isARepeat: false, keyCode: code)!
    }
    func scroll(_ p: NSPoint) -> NSEvent {
        // A CGEvent's location is in global coordinates; probe how NSEvent maps it, then aim at the view point.
        let probe = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: 3, wheel2: 0, wheel3: 0)!
        probe.location = .zero
        let origin = NSEvent(cgEvent: probe)!.locationInWindow
        let w = at(p)
        probe.location = CGPoint(x: w.x - origin.x, y: origin.y - w.y)
        return NSEvent(cgEvent: probe)!
    }
    var result: [String: Any] = [:]
    func record(_ name: String, _ body: () -> Void) {
        recorder.events = []
        body()
        result[name] = recorder.events
    }
    let pad = centre(panel.controlFrame(1)), pot = centre(panel.controlFrame(61)), lcd = centre(panel.lcdFrame)
    record("click_pad") { panel.mouseDown(with: mouse(.leftMouseDown, pad)); panel.mouseUp(with: mouse(.leftMouseUp, pad)) }
    record("right_click_pad") {     // a right click latches the pad; a second one releases it
        for _ in 0..<2 { panel.rightMouseDown(with: mouse(.rightMouseDown, pad)); panel.rightMouseUp(with: mouse(.rightMouseUp, pad)) }
    }
    record("touch_lcd") {
        panel.mouseDown(with: mouse(.leftMouseDown, lcd))
        panel.mouseDragged(with: mouse(.leftMouseDragged, NSPoint(x: lcd.x + 20, y: lcd.y)))
        panel.mouseUp(with: mouse(.leftMouseUp, lcd))
    }
    record("drag_pot") {
        panel.mouseDown(with: mouse(.leftMouseDown, pot))
        panel.mouseDragged(with: mouse(.leftMouseDragged, NSPoint(x: pot.x, y: pot.y - 60)))
        panel.mouseUp(with: mouse(.leftMouseUp, NSPoint(x: pot.x, y: pot.y - 60)))
    }
    record("scroll_pot") { panel.scrollWheel(with: scroll(pot)) }
    record("key_pad") { panel.keyDown(with: key(.keyDown, "z", 6)); panel.keyUp(with: key(.keyUp, "z", 6)) }
    record("shift") {
        panel.flagsChanged(with: key(.flagsChanged, "", 56, .shift))
        panel.flagsChanged(with: key(.flagsChanged, "", 56))
    }
    let elements = (panel.accessibilityChildren() ?? []).compactMap { $0 as? PanelAccessibilityElement }
    let label = { (id: UInt8) in panel.panelLayout!.controls.first { $0.id == id }!.accessibility }
    let potElement = elements.first { $0.accessibilityLabel() == label(61) }!
    let padElement = elements.first { $0.accessibilityLabel() == label(1) }!
    record("ax_increment") { _ = potElement.accessibilityPerformIncrement() }
    record("ax_press") {
        _ = padElement.accessibilityPerformPress()
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))     // the release follows after 40 ms
    }
    result["ax_enabled"] = potElement.isAccessibilityEnabled()
    let toolbar = panel.subviews.compactMap { $0 as? PanelToolbar }.first!
    result["badge_visible"] = !toolbar.benchBadge.isHidden
    result["latch_visible"] = !toolbar.latch.isHidden
    return result
}

let layout = URL(fileURLWithPath: CommandLine.arguments[1])
let report = ["on": exercise(layout: layout, inputEnabled: true), "off": exercise(layout: layout, inputEnabled: false)]
print(String(data: try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]), encoding: .utf8)!)
"""


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "macOS swiftc required")
class BenchModeInputGateTests(unittest.TestCase):
    """--no-ui-input: the window drops every pointer, scroll, key and accessibility input; nothing reaches the guest."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as work:
            main = Path(work) / "main.swift"
            main.write_text(PANEL_INPUT_HARNESS)
            binary = Path(work) / "harness"
            subprocess.run(["swiftc", main, TOOLS / "panel-view.swift", "-o", binary], check=True, capture_output=True)
            output = subprocess.run([binary, TOOLS / "panel-layout.json"], check=True, capture_output=True, text=True,
                                    timeout=60).stdout
        cls.report = json.loads(output)

    CASES = ("click_pad", "right_click_pad", "touch_lcd", "drag_pot", "scroll_pot", "key_pad", "shift", "ax_increment",
             "ax_press")

    def test_every_input_path_reaches_the_guest_when_input_is_on(self):
        for case in self.CASES:
            with self.subTest(case=case):
                self.assertTrue(self.report["on"][case], f"{case} emitted nothing, so the off case would prove nothing")
        self.assertEqual(self.report["on"]["scroll_pot"], ["control 61 3"])   # the AD TEST disturbance this guards
        self.assertTrue(self.report["on"]["ax_enabled"])
        self.assertFalse(self.report["on"]["badge_visible"])
        self.assertTrue(self.report["on"]["latch_visible"])

    def test_bench_mode_drops_every_input_path_and_shows_the_badge(self):
        for case in self.CASES:
            with self.subTest(case=case):
                self.assertEqual(self.report["off"][case], [])
        self.assertFalse(self.report["off"]["ax_enabled"])
        self.assertTrue(self.report["off"]["badge_visible"])
        self.assertFalse(self.report["off"]["latch_visible"])

    def test_the_app_reads_the_flag_and_does_not_take_focus_in_bench_mode(self):
        source = (TOOLS / "native-window.swift").read_text()
        self.assertIn('uiInputDisabled = arguments.contains("--no-ui-input")', source)
        self.assertIn("panel.uiInputEnabled = !uiInputDisabled", source)
        self.assertIn("if !uiInputDisabled { NSApp.activate(ignoringOtherApps: true) }", source)


if __name__ == "__main__":
    unittest.main()
