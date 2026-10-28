"""The MIDI bridge without hardware: message coding, learn, translation to control calls, LED echo and one end-to-end run
through two virtual CoreMIDI endpoints (skipped where the helper cannot be built or CoreMIDI refuses endpoints).

Run: python3 -m unittest emulator.midi_bridge.tests.test_midi_bridge
"""
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest

EMULATOR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(EMULATOR))
import panel_map

from emulator.midi_bridge import learn, leds, midi, presets
from emulator.midi_bridge import profile as prof
from emulator.midi_bridge.actions import Translator
from emulator.midi_bridge.bridge import Bridge

M = midi.Message


class Recorder:
    def __init__(self):
        self.calls = []

    def event(self, name, action, value=0):
        self.calls.append((name, action, value))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def pad_profile():
    return prof.validate_profile({"name": "t", "inputs": {
        "pad1": {"type": "note", "channel": 9, "number": 36, "kind": "button"},
        "cut": {"type": "cc", "channel": 0, "number": 74, "kind": "absolute"},
        "enc": {"type": "cc", "channel": 0, "number": 16, "kind": "relative", "encoding": "twos"},
        "ribbon": {"type": "pitchbend", "channel": 0, "number": 0, "kind": "absolute"},
        "go": {"type": "transport", "channel": None, "number": 0xFA, "kind": "button"},
        "rec": {"type": "cc", "channel": 0, "number": 20, "kind": "button"},
    }, "outputs": {"led1": {"type": "note", "channel": 9, "number": 36, "mode": "level"}}})


def pad_mapping(**extra):
    return {"bindings": {"pad1": {"action": "pad1"}, "cut": "main_out", "enc": "knob1", "ribbon": "strip",
                         "go": "play", "rec": "rec", **extra}, "lamps": {"pad1": "led1"}}


class MessageTests(unittest.TestCase):
    def test_round_trip(self):
        for message in (M("note_on", 9, 36, 100), M("note_off", 9, 36, 0), M("cc", 0, 74, 127), M("pitchbend", 2, 0, 12345),
                        M("aftertouch", 1, 0, 64), M("start", None, 0, 0), M("stop", None, 0, 0)):
            self.assertEqual(midi.parse(midi.encode(message)), message)

    def test_note_on_velocity_zero_is_note_off(self):
        self.assertEqual(midi.parse(bytes([0x90, 60, 0])), M("note_off", 0, 60, 0))

    def test_ignored_messages(self):
        self.assertIsNone(midi.parse(bytes([0xF0, 1, 2, 0xF7])))
        self.assertIsNone(midi.parse(b""))
        self.assertEqual(midi.parse(bytes([0xF8])).type, "clock")


class ClassifyTests(unittest.TestCase):
    def test_encodings(self):
        self.assertEqual(learn.classify([1, 2, 1, 127, 126, 127]), ("relative", "twos"))
        self.assertEqual(learn.classify([1, 2, 3, 65, 66, 65]), ("relative", "signmag"))
        self.assertEqual(learn.classify([65, 66, 65, 63, 62, 63]), ("relative", "offset"))
        self.assertEqual(learn.classify(list(range(0, 128, 4))), ("absolute", None))

    def test_one_direction_can_be_unsettled(self):
        self.assertEqual(learn.classify([1, 1, 2, 1]), (None, None))      # twos or sign-magnitude
        self.assertEqual(learn.classify([127, 126]), ("relative", "twos"))   # only two's complement goes there
        self.assertEqual(learn.classify([]), (None, None))

    def test_relative_delta(self):
        self.assertEqual([learn.relative_delta("twos", v) for v in (1, 2, 127, 126)], [1, 2, -1, -2])
        self.assertEqual([learn.relative_delta("signmag", v) for v in (1, 2, 65, 66)], [1, 2, -1, -2])
        self.assertEqual([learn.relative_delta("offset", v) for v in (65, 66, 63, 62)], [1, 2, -1, -2])
        self.assertEqual(learn.relative_delta("twos", 1, invert=True), -1)


class LearnTests(unittest.TestCase):
    def reader(self, script):
        """A read() over (seconds after the previous, message) pairs on a fake clock."""
        clock, queue = FakeClock(), list(script)

        def read(timeout):
            if queue and queue[0][0] <= timeout + 1e-9:
                gap, message = queue.pop(0)
                clock.now += gap
                return message
            clock.now += timeout
            if queue:
                queue[0] = (queue[0][0] - timeout, queue[0][1])
            return None
        return read, clock

    def test_learns_pad_and_knobs(self):
        script = [(0.2, M("note_on", 9, 40, 90)), (0.1, M("note_off", 9, 40, 0)),
                  (1.5, M("cc", 0, 16, 1)), (0.1, M("cc", 0, 16, 2)), (0.1, M("cc", 0, 16, 127)), (0.1, M("cc", 0, 16, 126)),
                  (1.5, M("cc", 0, 74, 5))] + [(0.05, M("cc", 0, 74, v)) for v in range(10, 120, 10)]
        read, clock = self.reader(script)
        said = []
        profile, mapping, skipped = learn.learn(["pad1", "knob1", "main_out"], read, said.append, "t", timeout=2.0,
                                                clock=clock)
        self.assertEqual(skipped, [])
        self.assertEqual(profile["inputs"]["pad1"], {"type": "note", "channel": 9, "number": 40, "kind": "button"})
        self.assertEqual(profile["inputs"]["knob1"]["encoding"], "twos")
        self.assertEqual(profile["inputs"]["main_out"]["kind"], "absolute")
        self.assertEqual(mapping["bindings"]["knob1"], "knob1")
        self.assertEqual(profile["outputs"]["pad1"]["number"], 40)       # a pad's LED is its own note by default
        self.assertEqual(mapping["lamps"], {"pad1": "pad1"})
        prof.validate_mapping(mapping, profile)

    def test_silence_is_skipped(self):
        read, clock = self.reader([])
        profile, mapping, skipped = learn.learn(["pad1"], read, lambda text: None, "t", timeout=0.5, clock=clock)
        self.assertEqual((profile["inputs"], skipped), ({}, ["pad1"]))


class ProfileTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(prof.ProfileError):
            prof.validate_profile({"inputs": {"x": {"type": "cc", "number": 1, "kind": "relative"}}})
        with self.assertRaises(prof.ProfileError):
            prof.validate_mapping({"bindings": {"nope": "pad1"}}, pad_profile())
        with self.assertRaises(prof.ProfileError):
            prof.validate_mapping({"lamps": {"pad1": "nope"}}, pad_profile())
        prof.validate_mapping(pad_mapping(), pad_profile())

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as folder:
            path = prof.save("profile", "mine", pad_profile(), folder=folder)
            self.assertEqual(prof.load("profile", str(path)), pad_profile())
            with self.assertRaises(prof.ProfileError):
                prof.save("profile", "../escape", pad_profile(), folder=folder)

    def test_omni_input_matches_any_channel(self):
        index = prof.Index(presets.sp16_midi_out(channel=None)[0])
        self.assertEqual(index.match(M("note_on", 5, 60, 90))[0], "pad1")
        self.assertIsNone(prof.Index(presets.sp16_midi_out(channel=2)[0]).match(M("note_on", 5, 60, 90)))

    def test_output_message(self):
        spec = {"type": "note", "channel": 9, "number": 36, "mode": "level"}
        self.assertEqual(prof.output_message(spec, 64), M("note_on", 9, 36, 64))
        self.assertEqual(prof.output_message(spec, 0), M("note_off", 9, 36, 0))
        self.assertEqual(prof.output_message({**spec, "mode": "onoff"}, 5).value, 127)
        self.assertEqual(prof.output_message({**spec, "type": "cc"}, 9), M("cc", 9, 36, 9))


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        self.target, self.clock = Recorder(), FakeClock()
        self.t = Translator(self.target, pad_profile(), pad_mapping(), analog_start={"main_out": 500}, clock=self.clock)

    def test_pad_velocity_and_release(self):
        self.t.handle([M("note_on", 9, 36, 99)])
        self.t.handle([M("note_off", 9, 36, 0)])
        self.assertEqual(self.target.calls, [("pad1", "press", 99), ("pad1", "release", 0)])

    def test_other_channel_is_ignored(self):
        self.t.handle([M("note_on", 3, 36, 99)])
        self.assertEqual(self.target.calls, [])

    def test_cc_button(self):
        self.t.handle([M("cc", 0, 20, 127), M("cc", 0, 20, 0)])
        self.assertEqual(self.target.calls, [("rec", "press", 127), ("rec", "release", 0)])

    def test_relative_knob_sums_a_burst(self):
        self.t.handle([M("cc", 0, 16, 1), M("cc", 0, 16, 1), M("cc", 0, 16, 127)])
        self.assertEqual(self.target.calls, [("knob1", "delta", 1)])

    def test_absolute_analog_scales_and_coalesces(self):
        self.t.handle([M("cc", 0, 74, 10), M("cc", 0, 74, 127)])
        self.assertEqual(self.target.calls, [("main_out", "value", 1000)])
        self.t.handle([M("cc", 0, 74, 127)])
        self.assertEqual(len(self.target.calls), 1)                       # unchanged value: no call

    def test_relative_to_analog(self):
        t = Translator(self.target, pad_profile(), pad_mapping(enc="main_out"), analog_start={"main_out": 500})
        t.handle([M("cc", 0, 16, 3)])
        self.assertEqual(self.target.calls, [("main_out", "value", 530)])

    def test_absolute_to_detents(self):
        t = Translator(self.target, pad_profile(), pad_mapping(cut="knob2"))
        t.handle([M("cc", 0, 74, 60)])                                    # baseline
        t.handle([M("cc", 0, 74, 63)])
        t.handle([M("cc", 0, 74, 61)])
        self.assertEqual(self.target.calls, [("knob2", "delta", 3), ("knob2", "delta", -2)])

    def test_strip_contact_and_idle_release(self):
        self.t.handle([M("pitchbend", 0, 0, 16383)])
        self.assertEqual(self.target.calls, [("strip", "value", 1000)])
        self.t.handle([M("pitchbend", 0, 0, 8192)])                       # back to centre
        self.assertEqual(self.target.calls[-1], ("strip", "release", 0))

    def test_strip_from_cc_releases_when_idle(self):
        t = Translator(self.target, pad_profile(), pad_mapping(cut="strip"), clock=self.clock)
        t.handle([M("cc", 0, 74, 64)])
        self.clock.now = 1.0
        t.tick()
        self.assertEqual([c[:2] for c in self.target.calls], [("strip", "value"), ("strip", "release")])

    def test_transport_is_a_short_tap(self):
        self.t.handle([M("start", None, 0, 0)])
        self.assertEqual(self.target.calls, [("play", "press", 100)])
        self.clock.now = 1.0
        self.t.tick()
        self.assertEqual(self.target.calls[-1], ("play", "release", 0))

    def test_release_all_lets_go_of_held_pads(self):
        self.t.handle([M("note_on", 9, 36, 99)])
        self.t.release_all()
        self.assertEqual(self.target.calls[-1], ("pad1", "release", 0))

    def test_release_all_sends_a_transport_tap_still_waiting(self):
        self.t.handle([M("start", None, 0, 0)])
        self.t.release_all()                                              # well inside the 60 ms tap
        self.assertEqual(self.target.calls, [("play", "press", 100), ("play", "release", 0)])
        self.clock.now = 1.0
        self.t.tick()
        self.assertEqual(len(self.target.calls), 2)                       # the timer is gone: no second release

    def test_absolute_to_detents_applies_scale(self):
        t = Translator(self.target, pad_profile(), pad_mapping(cut={"action": "knob2", "scale": 0.5}))
        t.handle([M("cc", 0, 74, 60)])                                    # baseline
        t.handle([M("cc", 0, 74, 64)])                                    # +4 -> 2 detents
        t.handle([M("cc", 0, 74, 65)])                                    # +1 -> half a detent, carried
        t.handle([M("cc", 0, 74, 66)])                                    # +1 -> the carried half completes one
        self.assertEqual(self.target.calls, [("knob2", "delta", 2), ("knob2", "delta", 1)])

    def test_absolute_to_detents_scale_and_invert(self):
        t = Translator(self.target, pad_profile(), pad_mapping(cut={"action": "knob2", "scale": 2, "invert": True}))
        t.handle([M("cc", 0, 74, 60)])
        t.handle([M("cc", 0, 74, 63)])
        self.assertEqual(self.target.calls, [("knob2", "delta", -6)])

    # -- scale, every input kind (button, absolute, relative) against every control kind (button, detent, analog, strip)
    def scaled(self, bound, messages, **options):
        """Calls made for `messages` (one batch each) by a translator whose input `bound` has `options` added."""
        target, logged = Recorder(), []
        mapping = pad_mapping(**{bound[0]: {"action": bound[1], **options}})
        t = Translator(target, pad_profile(), mapping, analog_start={"main_out": 500}, clock=self.clock, log=logged.append)
        for message in messages:
            t.handle([message])
        return target.calls, logged

    def test_scale_button_to_detent_accumulates(self):
        presses = [M("cc", 0, 20, 127), M("cc", 0, 20, 0)] * 2
        calls, _ = self.scaled(("rec", "knob3+"), presses, scale=0.5)
        self.assertEqual(calls, [("knob3", "delta", 1)])                  # the second press completes a detent
        calls, _ = self.scaled(("rec", "knob3-"), presses[:2], scale=3)
        self.assertEqual(calls, [("knob3", "delta", -3)])

    def test_scale_button_to_button_leaves_velocity_alone(self):
        calls, _ = self.scaled(("pad1", "pad2"), [M("note_on", 9, 36, 99), M("note_off", 9, 36, 0)], scale=0.5)
        self.assertEqual(calls, [("pad2", "press", 99), ("pad2", "release", 0)])

    def test_scale_button_cannot_drive_analog_or_strip(self):
        for action in ("main_out", "strip"):
            calls, logged = self.scaled(("rec", action), [M("cc", 0, 20, 127)], scale=0.5)
            self.assertEqual((calls, len(logged)), ([], 1), action)

    def test_scale_absolute_to_detent(self):
        calls, _ = self.scaled(("cut", "knob2"), [M("cc", 0, 74, v) for v in (60, 64, 65, 66)], scale=0.5)
        self.assertEqual(calls, [("knob2", "delta", 2), ("knob2", "delta", 1)])

    def test_scale_absolute_to_analog_and_strip(self):
        calls, _ = self.scaled(("cut", "main_out"), [M("cc", 0, 74, 127)], scale=0.4)
        self.assertEqual(calls, [("main_out", "value", 400)])
        calls, _ = self.scaled(("cut", "strip"), [M("cc", 0, 74, 127)], scale=0.5)
        self.assertEqual(calls, [("strip", "value", 500)])
        calls, _ = self.scaled(("ribbon", "strip"), [M("pitchbend", 0, 0, 16383)], scale=0.25)
        self.assertEqual(calls, [("strip", "value", 250)])

    def test_scale_absolute_cannot_drive_a_button(self):
        calls, logged = self.scaled(("cut", "pad2"), [M("cc", 0, 74, 127)], scale=0.5)
        self.assertEqual((calls, len(logged)), ([], 1))

    def test_scale_relative_to_detent_accumulates_across_events(self):
        calls, _ = self.scaled(("enc", "knob1"), [M("cc", 0, 16, 1)] * 2, scale=0.5)
        self.assertEqual(calls, [("knob1", "delta", 1)])                  # one tick alone sends nothing
        calls, _ = self.scaled(("enc", "knob1"), [M("cc", 0, 16, 1)] * 10, scale=0.1)
        self.assertEqual(calls, [("knob1", "delta", 1)])                  # float sums must not fall just short
        calls, _ = self.scaled(("enc", "knob1"), [M("cc", 0, 16, 1)] * 3, scale=2)
        self.assertEqual(calls, [("knob1", "delta", 2)] * 3)
        calls, _ = self.scaled(("enc", "knob1"), [M("cc", 0, 16, 127)] * 2, scale=0.5, invert=True)
        self.assertEqual(calls, [("knob1", "delta", 1)])                  # inverted: -(-1) per tick

    def test_scale_relative_to_detent_carry_survives_a_reversal(self):
        calls, _ = self.scaled(("enc", "knob1"), [M("cc", 0, 16, 1), M("cc", 0, 16, 127), M("cc", 0, 16, 1)], scale=0.5)
        self.assertEqual(calls, [])                                       # +0.5, -0.5, +0.5: never a whole detent

    def test_scale_relative_to_detent_keeps_the_clamp(self):
        calls, _ = self.scaled(("enc", "knob1"), [M("cc", 0, 16, 60)], scale=10)
        self.assertEqual(calls, [("knob1", "delta", 64)])

    def test_scale_relative_to_analog(self):
        calls, _ = self.scaled(("enc", "main_out"), [M("cc", 0, 16, 4)], scale=0.5)
        self.assertEqual(calls, [("main_out", "value", 520)])             # 4 ticks * 10 units * 0.5
        calls, _ = self.scaled(("enc", "main_out"), [M("cc", 0, 16, 1)] * 4, scale=0.05)
        self.assertEqual(calls, [("main_out", "value", 501), ("main_out", "value", 502)])   # half a unit carried over
        calls, _ = self.scaled(("enc", "main_out"), [M("cc", 0, 16, 5)], scale=1000)
        self.assertEqual(calls, [("main_out", "value", 1000)])            # still clamped to the control's range

    def test_scale_relative_cannot_drive_a_button_or_the_strip(self):
        for action in ("pad2", "strip"):
            calls, logged = self.scaled(("enc", action), [M("cc", 0, 16, 1)], scale=0.5)
            self.assertEqual((calls, len(logged)), ([], 1), action)

    def test_pitch_bend_to_detents_uses_its_14_bit_range(self):
        bends = [round(i * 16383 / 127) for i in range(128)]               # a full upward sweep in 127 equal steps
        calls, _ = self.scaled(("ribbon", "knob1"), [M("pitchbend", 0, 0, v) for v in bends])
        self.assertEqual(sum(c[2] for c in calls), 127)                   # not 16383, and nothing hits the clamp
        self.assertTrue(all(c[:2] == ("knob1", "delta") and abs(c[2]) <= 2 for c in calls), calls)
        calls, _ = self.scaled(("ribbon", "knob1"), [M("pitchbend", 0, 0, v) for v in reversed(bends)])
        self.assertEqual(sum(c[2] for c in calls), -127)

    def test_pitch_bend_small_bend_is_less_than_a_detent(self):
        calls, _ = self.scaled(("ribbon", "knob1"), [M("pitchbend", 0, 0, v) for v in (8192, 8260, 8192)])
        self.assertEqual(calls, [])                                       # +0.5 then -0.5 detent: carried, never sent
        calls, _ = self.scaled(("ribbon", "knob1"), [M("pitchbend", 0, 0, v) for v in (8192, 8192 + 129)])
        self.assertEqual(calls, [("knob1", "delta", 1)])                  # 129 of 16383 is a little over one of 127

    def test_pitch_bend_to_detents_scale_invert_and_clamp(self):
        sweep = [M("pitchbend", 0, 0, v) for v in (0, 16383)]
        calls, _ = self.scaled(("ribbon", "knob1"), sweep, scale=0.25)
        self.assertEqual(calls, [("knob1", "delta", 31)])                 # 127 * 0.25 = 31.75, the rest is carried
        calls, _ = self.scaled(("ribbon", "knob1"), sweep, invert=True, scale=0.25)
        self.assertEqual(calls, [("knob1", "delta", -31)])
        calls, _ = self.scaled(("ribbon", "knob1"), sweep)
        self.assertEqual(calls, [("knob1", "delta", 64)])                 # one jump over the whole range: per-event clamp

    def test_seven_and_fourteen_bit_inputs_turn_a_knob_alike(self):
        cc, _ = self.scaled(("cut", "knob2"), [M("cc", 0, 74, v) for v in (0, 127)])
        bend, _ = self.scaled(("ribbon", "knob2"), [M("pitchbend", 0, 0, v) for v in (0, 16383)])
        self.assertEqual(cc, [("knob2", "delta", 64)])
        self.assertEqual(bend, cc)

    def test_failed_call_is_counted_not_raised(self):
        class Broken:
            def event(self, *args):
                raise RuntimeError("app gone")
        t = Translator(Broken(), pad_profile(), pad_mapping())
        t.handle([M("note_on", 9, 36, 99)])
        self.assertEqual((t.sent, t.errors), (0, 1))

    def test_button_steps_a_knob(self):
        t = Translator(self.target, pad_profile(), pad_mapping(rec="knob3-"))
        t.handle([M("cc", 0, 20, 127), M("cc", 0, 20, 0)])
        self.assertEqual(self.target.calls, [("knob3", "delta", -1)])

    def test_every_action_name_the_docs_use_is_a_control(self):
        names = {*panel_map.CONTROL_IDS, *panel_map.LAMP_IDS}
        self.assertTrue({"pad1", "knob1", "strip", "play", "main_out", "strip_led1", "bar_led1"} <= names)


class PresetTests(unittest.TestCase):
    def test_mirror_preset(self):
        profile, mapping = presets.sp16_midi_out(channel=3, note_base=60)
        self.assertEqual(profile["inputs"]["pad16"], {"type": "note", "channel": 2, "number": 75, "kind": "button"})
        self.assertEqual([profile["inputs"][f"knob{i}"]["type"] for i in (1, 2)], ["pitchbend", "aftertouch"])
        self.assertEqual([profile["inputs"][f"knob{i}"]["number"] for i in (3, 4, 5, 6)], [7, 1, 2, 10])
        cc2 = presets.sp16_midi_out(cc_page=2)[0]
        self.assertEqual([cc2["inputs"][f"knob{i}"]["number"] for i in range(1, 7)], [71, 72, 73, 74, 75, 76])
        self.assertEqual(mapping["bindings"]["start"], "play")
        self.assertEqual(mapping["lamps"], {})                           # no LED input is documented
        prof.validate_mapping(mapping, profile)

    def test_bad_arguments(self):
        for kwargs in ({"channel": 17}, {"cc_page": 3}, {"note_base": 120}):
            with self.assertRaises(prof.ProfileError):
                presets.sp16_midi_out(**kwargs)

    def test_preset_drives_the_emulator_calls(self):
        profile, mapping = presets.sp16_midi_out(channel=1)
        target = Recorder()
        t = Translator(target, profile, mapping)
        t.handle([M("note_on", 0, 60, 80), M("cc", 0, 7, 20)])
        t.handle([M("cc", 0, 7, 24)])
        self.assertEqual(target.calls, [("pad1", "press", 80), ("knob3", "delta", 4)])


def tx_line(frame):
    return json.dumps({"t": 0.0, "hex": (b"\x00\x00\x01" + frame).hex()}) + "\n"


def lamp_frame(pad1=0, level=4):
    frame = bytearray(128)
    frame[0], frame[0x77] = panel_map.TX_HEADER, panel_map.TX_MARKER
    frame[0x6e] = level
    off = panel_map.RGB_CHANNELS[0x10]                                   # pad 1
    frame[off:off + 3] = bytes((pad1, 0, 0))
    return bytes(frame)


class LedEchoTests(unittest.TestCase):
    def make(self, folder):
        names = {**panel_map.CONTROL_NAMES, **panel_map.LAMP_NAMES}
        return leds.LedEcho(folder, pad_profile(), pad_mapping(), panel_map.decode_lamps, names)

    def test_follows_the_log_and_sends_changes_only(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / leds.LOG_NAME
            log.write_text("")
            echo = self.make(folder)
            self.assertEqual(echo.poll(), [])
            with open(log, "a") as handle:
                handle.write(tx_line(lamp_frame(0)))
            self.assertEqual(echo.poll(), [M("note_off", 9, 36, 0)])
            with open(log, "a") as handle:
                handle.write(tx_line(lamp_frame(74)) + tx_line(lamp_frame(74)))
            lit = echo.poll()
            self.assertEqual(len(lit), 1)
            self.assertEqual((lit[0].type, lit[0].number), ("note_on", 36))
            self.assertGreater(lit[0].value, 100)
            with open(log, "a") as handle:
                handle.write(tx_line(lamp_frame(74)))
            self.assertEqual(echo.poll(), [])                            # unchanged
            self.assertEqual(len(echo.resync()), 1)
            self.assertEqual(echo.blank(), [M("note_off", 9, 36, 0)])

    def test_partial_line_waits(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / leds.LOG_NAME
            log.write_text("")
            echo = self.make(folder)
            line = tx_line(lamp_frame(74))
            with open(log, "a") as handle:
                handle.write(line[:40])
            self.assertEqual(echo.poll(), [])
            with open(log, "a") as handle:
                handle.write(line[40:])
            self.assertEqual(len(echo.poll()), 1)

    def test_lamp_level(self):
        self.assertEqual(leds.lamp_level((0, 0, 0)), 0)
        self.assertEqual(leds.lamp_level((255, 10, 0)), 127)


class FakePort:
    def __init__(self, messages):
        self.messages, self.sent = list(messages), []

    def drain(self, wait):
        batch, self.messages = self.messages, []
        return batch

    def send(self, message):
        self.sent.append(message)


class BridgeTests(unittest.TestCase):
    def test_step_translates_and_echoes(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / leds.LOG_NAME).write_text(tx_line(lamp_frame(0)))
            names = {**panel_map.CONTROL_NAMES, **panel_map.LAMP_NAMES}
            echo = leds.LedEcho(folder, pad_profile(), pad_mapping(), panel_map.decode_lamps, names)
            target, port = Recorder(), FakePort([M("note_on", 9, 36, 70)])
            bridge = Bridge(port, Translator(target, pad_profile(), pad_mapping()), echo, log=lambda text: None)
            with open(Path(folder) / leds.LOG_NAME, "a") as handle:
                handle.write(tx_line(lamp_frame(74)))
            bridge.step()
            self.assertEqual(target.calls, [("pad1", "press", 70)])
            self.assertEqual(port.sent[0].type, "note_on")
            bridge.run(stop=lambda: True)
            self.assertEqual(target.calls[-1], ("pad1", "release", 0))   # held pads are released at the end
            self.assertEqual(port.sent[-1], M("note_off", 9, 36, 0))     # and the LEDs go dark


def helper_or_none():
    try:
        return midi.helper_binary()
    except RuntimeError:
        return None


@unittest.skipUnless(sys.platform == "darwin" and helper_or_none(), "needs macOS and swiftc for the CoreMIDI helper")
class CoreMidiTests(unittest.TestCase):
    """A virtual controller (a CoreMIDI source and destination) on one side, the bridge's own port on the other."""

    def test_virtual_controller_round_trip(self):
        name = f"SP16 L5 test {int(time.time())}"
        try:
            controller = midi.Port(["virtual", name])
        except RuntimeError as error:
            self.skipTest(f"CoreMIDI refused a virtual endpoint: {error}")
        time.sleep(0.3)
        port = midi.Port(["run", "--in", name, "--out", name])
        self.addCleanup(controller.close)
        self.addCleanup(port.close)
        time.sleep(0.3)
        controller.send(M("note_on", 9, 36, 99))
        controller.send(M("cc", 0, 16, 1))
        got = [port.get(2), port.get(2)]
        self.assertEqual(got, [M("note_on", 9, 36, 99), M("cc", 0, 16, 1)])
        port.send(M("note_on", 9, 36, 127))
        self.assertEqual(controller.get(2), M("note_on", 9, 36, 127))

    def test_a_long_packet_list_decodes_completely_and_in_order(self):
        """Regression: the receive path stepped through a list from a copy of its first packet, which only holds while
        the list is short (the old walk decoded 86 of these 300). `decode` runs that path on a hand-built list."""
        sent = [f"b0 {i % 100:02x} {i % 128:02x}" for i in range(1, 301)]
        out = subprocess.run([str(midi.helper_binary()), "decode", " | ".join(sent)], capture_output=True, text=True,
                             timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split("\n")[:-1], ["in " + line for line in sent])

    def test_several_packets_in_one_send_arrive_in_order(self):
        name = f"SP16 L5 burst {int(time.time())}"
        try:
            controller = midi.Port(["virtual", name])
        except RuntimeError as error:
            self.skipTest(f"CoreMIDI refused a virtual endpoint: {error}")
        time.sleep(0.3)
        port = midi.Port(["run", "--in", name])
        self.addCleanup(controller.close)
        self.addCleanup(port.close)
        time.sleep(0.3)
        sent = [M("note_on", 9, 36, 99), M("cc", 0, 16, 1), M("pitchbend", 2, 0, 12345), M("note_off", 9, 36, 0)]
        controller.send_many(sent)
        self.assertEqual([port.get(2) for _ in sent], sent)


if __name__ == "__main__":
    unittest.main()
