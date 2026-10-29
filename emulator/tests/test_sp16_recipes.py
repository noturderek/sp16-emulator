"""emulator/menu/sync_source.py and timing_probe.py's follow measurement helpers."""
import unittest
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import menu.sync_source as recipes  # noqa: E402
import timing_probe  # noqa: E402

STEP_KINDS = {"press", "tap", "turn", "burst", "wait", "expect_title", "expect_text", "focus_row",
              "if_text", "hold_on", "release", "until_gone", "usb", "settle"}


class RecipeTests(unittest.TestCase):
    def test_every_step_is_a_known_kind(self):
        for name, (_, steps) in recipes.RECIPES.items():
            for index, step in enumerate(steps):
                self.assertIsInstance(step, dict, msg=f"{name} step {index}")
                kinds = [k for k in step if k not in ("hold", "value", "gap", "detents", "box", "max_detents",
                                                       "timeout", "note", "then", "else")]
                self.assertEqual(len(kinds), 1, msg=f"{name} step {index} has {kinds}")
                self.assertIn(kinds[0], STEP_KINDS, msg=f"{name} step {index}")

    def test_sync_source_recipe_targets_the_right_rows(self):
        steps = recipes.SYNC_SOURCE_PDL
        rows = [s["focus_row"] for s in steps if "focus_row" in s]
        self.assertIn("SYNC SOURCE SELECT", rows[0])
        self.assertIn("PRO DJ LINK", rows[1])
        # the setting is focused in the left column, the value in the right column
        left = [s for s in steps if "focus_row" in s and s["focus_row"].startswith("SYNC SOURCE")]
        right = [s for s in steps if "focus_row" in s and s["focus_row"] == "PRO DJ LINK"]
        self.assertEqual(left[0]["box"], recipes.LEFT)
        self.assertEqual(right[0]["box"], recipes.RIGHT)

    def test_usb_out_recipe_sets_the_out_mode(self):
        steps = recipes.USB_OUT
        rows = [s["focus_row"] for s in steps if "focus_row" in s]
        self.assertIn("MIDI OUT MODE", rows[0])
        self.assertEqual(rows[1], "OUT$")

    def test_usb_note_out_recipe_targets_the_midi_out_note_cc(self):
        steps = recipes.USB_NOTE_OUT
        rows = [s for s in steps if "focus_row" in s]
        # MIDI OUT MODE (USB) first (unique), then NOTE/CC reached by searching upward (detents -1)
        self.assertEqual(rows[0]["focus_row"], recipes.MIDI_OUT_USB)
        self.assertEqual(rows[1]["focus_row"], "OUT$")
        self.assertEqual(rows[2]["focus_row"], "NOTE/CC")
        self.assertEqual(rows[2]["detents"], -1)
        self.assertEqual(rows[3]["focus_row"], "ENABLE")

    def test_the_usb_mode_label_matches_literal_parentheses(self):
        import re
        self.assertIsNotNone(re.search(recipes.MIDI_OUT_USB, "MIDI OUT MODE (USB)", re.I))
        self.assertIsNone(re.search(recipes.MIDI_OUT_USB, "MIDI OUT MODE (DIN)", re.I))

    def test_set_row_opens_utility_and_returns_home(self):
        steps = recipes.set_row("X", "Y")
        self.assertEqual(steps[0], {"settle": 1.0})
        self.assertEqual(steps[1], {"press": "home"})
        self.assertEqual(steps[3], {"tap": recipes.UTILITY_TAP})
        self.assertEqual(steps[6], {"press": "selector"})
        self.assertEqual(steps[-2], {"press": "home"})
        self.assertEqual(steps[-1], {"expect_title": "HOME"})


class FollowPhaseTests(unittest.TestCase):
    def test_read_master_beats(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("beat 1000000000 1\nbeat 1000468750 2\nbeat 2000000000 1\njunk\n")
            path = f.name
        self.assertEqual(timing_probe.read_master_beats(path),
                         [(1000000000, 1), (1000468750, 2), (2000000000, 1)])

    def test_follow_phase_measures_a_five_ms_offset(self):
        bar_ns = int(4 * 60 / 128 * 1e9)  # 1.875 s
        master = []
        for bar in range(4):
            base = 1_000_000_000 + bar * bar_ns
            for b in range(4):
                master.append((base + b * (bar_ns // 4), b + 1))
        step_frames = 60 / 128 / 4 * 44100  # one 16th note
        onsets = [int(i * step_frames) for i in range(64)]
        window_t0_ns = 1_000_000_000 + 5_000_000  # the window opens 5 ms after the master's downbeat
        phase = timing_probe.follow_phase(onsets, master, window_t0_ns, 128.0)
        self.assertAlmostEqual(phase["step_phase_median_ms"], 5.0, delta=1.0)
        self.assertEqual(phase["onsets"], 64)

    def test_follow_phase_ignores_a_whole_bar_of_latency(self):
        bar_ns = int(4 * 60 / 128 * 1e9)
        master = []
        for bar in range(4):
            base = 1_000_000_000 + bar * bar_ns
            for b in range(4):
                master.append((base + b * (bar_ns // 4), b + 1))
        step_frames = 60 / 128 / 4 * 44100
        onsets = [int(i * step_frames) for i in range(64)]
        # the follower starts exactly one bar later than the master: phase still reads ~0
        window_t0_ns = 1_000_000_000 + bar_ns
        phase = timing_probe.follow_phase(onsets, master, window_t0_ns, 128.0)
        self.assertAlmostEqual(phase["step_phase_median_ms"], 0.0, delta=1.0)

    def test_follow_phase_refuses_little_data(self):
        self.assertEqual(timing_probe.follow_phase([0, 1, 2, 3], [(1, 1)], 0, 128.0)["error"], "too little data")


if __name__ == "__main__":
    unittest.main()
