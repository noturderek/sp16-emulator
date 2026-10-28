"""Tests for emulator/probes/ak4458_transport.py: the tolerant capture reader and the word classification."""
import base64
import contextlib
import importlib.util
import math
import os
import random
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "probes"))

import hwsuite  # noqa: E402
import ak4458_transport as transport  # noqa: E402

RATE = 44100
PIECE = 2000  # base64 characters per console line, as guest-init.sh prints them


def capture_text(words, frames, name="ak4458audio-0.7", done=2, pieces=True):
    """The console text a finished capture is printed as: a 64-byte header plus the zlib'd interleaved frames."""
    body = b"".join(struct.pack(f"<{len(words)}h", *[int(words[w][i] * 32767) for w in range(len(words))])
                    for i in range(frames))
    head = bytearray(64)
    head[:8] = b"SP16PCM1"
    struct.pack_into("<I", head, 8, 1)
    struct.pack_into("<I", head, 12, done)
    struct.pack_into("<I", head, 16, len(words))
    struct.pack_into("<I", head, 20, 2)  # S16_LE
    struct.pack_into("<I", head, 24, RATE)
    struct.pack_into("<I", head, 28, 2 * len(words))
    struct.pack_into("<Q", head, 32, frames)
    struct.pack_into("<Q", head, 40, frames)
    blob = base64.b64encode(bytes(head) + zlib.compress(body, 6)).decode()
    chunks = [blob[i:i + PIECE] for i in range(0, len(blob), PIECE)]
    lines = [f"SP16_PCM_DUMP {name} begin pieces={len(chunks)} bytes={len(blob)}"]
    lines += [f"SP16_PCM_DUMP {name} {i} {chunk}" for i, chunk in enumerate(chunks)]
    lines.append(f"SP16_PCM_DUMP {name} end")
    return lines if pieces else lines[1:-1]


def sine(frequency, frames, amplitude, phase=0.0, rate=RATE):
    return [amplitude * math.sin(2 * math.pi * frequency * i / rate + phase) for i in range(frames)]


def noise(frames, seed=1):
    generator = random.Random(seed)
    return [generator.uniform(-1.0, 1.0) for _ in range(frames)]


class CaptureReaderTest(unittest.TestCase):
    def test_a_clean_capture_round_trips(self):
        words = [sine(1000, 2000, 0.04) if w % 4 == 0 else [0.0] * 2000 for w in range(16)]
        found = transport.captures("\n".join(capture_text(words, 2000)))
        self.assertEqual(list(found), ["ak4458audio-0.7"])
        analysis = transport.analyse_capture(found["ak4458audio-0.7"])
        self.assertEqual(analysis["frames"], 2000)
        self.assertEqual(analysis["declared_frames"], 2000)
        self.assertEqual(transport.classify(analysis), [0, 4, 8, 12])
        self.assertAlmostEqual(analysis["words"][0]["peak"], 0.04, places=3)
        self.assertAlmostEqual(analysis["words"][0]["dominant_hz"], 1000.0, delta=1.0)

    def test_a_damaged_piece_truncates_the_capture_without_destroying_it(self):
        """A piece the console cut in half: the frames after it are lost, the ones before it are still a measurement."""
        words = [sine(1000, 2000, 0.04)] + [[0.0] * 2000 for _ in range(15)]
        lines = capture_text(words, 2000)
        parts = lines[2].split(" ", 3)
        lines[2] = " ".join(parts[:3] + [parts[3][:len(parts[3]) // 2]])
        analysis = transport.analyse_capture(transport.captures("\n".join(lines))["ak4458audio-0.7"])
        self.assertGreater(analysis["frames"], 0)
        self.assertLess(analysis["frames"], 2000)
        self.assertEqual(analysis["declared_frames"], 2000)
        self.assertAlmostEqual(analysis["words"][0]["dominant_hz"], 1000.0, delta=1.0)

    def test_a_piece_with_a_trailing_junk_fragment_is_still_read(self):
        """Another writer's line appended to a piece must not cost the capture."""
        words = [sine(1000, 2000, 0.04)] + [[0.0] * 2000 for _ in range(15)]
        lines = capture_text(words, 2000)
        lines[1] = lines[1] + "SP16_DATA sto"
        analysis = transport.analyse_capture(transport.captures("\n".join(lines))["ak4458audio-0.7"])
        self.assertEqual(analysis["frames"], 2000)

    def test_another_writers_whole_line_between_pieces_costs_nothing(self):
        words = [sine(1000, 2000, 0.04)] + [[0.0] * 2000 for _ in range(15)]
        lines = capture_text(words, 2000)
        lines.insert(3, "[sp16-ui-stub] audio hw:ak4458audio,0 playback tid=236 rate=44100")
        analysis = transport.analyse_capture(transport.captures("\n".join(lines))["ak4458audio-0.7"])
        self.assertEqual(analysis["frames"], 2000)

    def test_a_capture_that_never_ended_is_not_used(self):
        """A capture still being printed has no `end` line; it is not a measurement yet."""
        words = [[0.0] * 100 for _ in range(16)]
        self.assertEqual(transport.captures("\n".join(capture_text(words, 100, pieces=False))), {})

    def test_two_captures_are_kept_apart(self):
        words = [[0.0] * 100 for _ in range(16)]
        text = "\n".join(capture_text(words, 100, name="ak4458audio-0.1")
                         + capture_text(words, 100, name="ak4458audio-0.2"))
        self.assertEqual(sorted(transport.captures(text)), ["ak4458audio-0.1", "ak4458audio-0.2"])

    def test_console_tail_starts_on_a_line_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "console.txt"
            path.write_text("one\ntwo\nthree\n")
            self.assertEqual(transport.console_tail(path, 6), "three\n")


class WordMeasureTest(unittest.TestCase):
    def test_a_tone_word_and_a_noise_word_separate_by_orders_of_magnitude(self):
        frames = 8192
        tone_dominant, tone_share = transport.band_share(sine(1000, frames, 0.5), 1000.0)
        noise_dominant, noise_share = transport.band_share(noise(frames), 1000.0)
        self.assertAlmostEqual(tone_dominant, 1000.0, delta=1.0)
        self.assertGreater(tone_share, 0.5)
        self.assertLess(noise_share, 0.02)
        self.assertGreater(tone_share / max(noise_share, 1e-9), 20)

    def test_a_full_scale_noise_word_is_not_the_stimulus(self):
        """The odd words carry a full-scale pattern: high level, but not the tone, so they must not count."""
        frames = 8192
        dominant, share = transport.band_share(noise(frames, seed=7), 1000.0)
        self.assertNotAlmostEqual(dominant, 1000.0, places=1)
        self.assertLess(share, 0.02)

    def test_silence_is_zero_not_a_perfect_match(self):
        self.assertEqual(transport.band_share([0.0] * 4096, 1000.0), (0.0, 0.0))

    def test_a_word_at_another_frequency_is_not_the_stimulus(self):
        dominant, share = transport.band_share(sine(4000, 8192, 0.5), 1000.0)
        self.assertAlmostEqual(dominant, 4000.0, delta=1.0)
        self.assertLess(share, 0.01)


class TableTest(unittest.TestCase):
    def analysis(self, levels):
        words = [{"word": w, "peak": levels.get(w, 0.0), "tone_share": 0.9 if levels.get(w, 0.0) else 0.0}
                 for w in range(16)]
        return {"frames": 100, "declared_frames": 100, "words": words}

    def test_a_mode_drives_the_words_it_lifted_clear_of_the_baseline(self):
        runs = [{"mode": "none", "analysis": self.analysis({})},
                {"mode": "MODE1", "analysis": self.analysis({0: 0.05, 2: 0.05, 6: 0.05})},
                {"mode": "MODE3", "analysis": self.analysis({12: 0.05, 14: 0.05})}]
        result = transport.table(runs)
        self.assertEqual(result["words"][0]["driven_by"], ["MODE1"])
        self.assertEqual(result["words"][2]["driven_by"], ["MODE1"])
        self.assertEqual(result["words"][12]["driven_by"], ["MODE3"])
        self.assertEqual(result["words"][6]["driven_by"], ["MODE1"])
        self.assertEqual(result["words"][1]["driven_by"], [])
        self.assertEqual([p["dac_slot"] for p in result["pairs"]], list(range(8)))
        self.assertTrue(result["pairs"][0]["left_only"])
        self.assertFalse(result["pairs"][0]["both_halves"])

    def test_a_word_the_baseline_already_held_is_not_credited_to_a_mode(self):
        runs = [{"mode": "none", "analysis": self.analysis({0: 0.04})},
                {"mode": "MODE1", "analysis": self.analysis({0: 0.05, 2: 0.05})}]
        result = transport.table(runs)
        self.assertEqual(result["words"][0]["driven_by"], [])
        self.assertEqual(result["words"][2]["driven_by"], ["MODE1"])

    def test_a_noisy_word_is_not_a_driven_word(self):
        runs = [{"mode": "none", "analysis": self.analysis({})},
                {"mode": "MODE1", "analysis": self.analysis({0: 0.05})}]
        runs[1]["analysis"]["words"][0]["tone_share"] = 0.01  # full scale, but not at the tone
        self.assertEqual(transport.table(runs)["words"][0]["driven_by"], [])

    def test_the_render_names_every_word_and_every_slot(self):
        runs = [{"mode": "none", "analysis": self.analysis({})},
                {"mode": "MODE1", "analysis": self.analysis({0: 0.05})}]
        result = transport.table(runs)
        result["runs"] = runs
        text = transport.render(result)
        for word in range(16):
            self.assertIn(f"{word:4d}", text)
        self.assertEqual(len(result["pairs"]), 8)


class PageGuardTest(unittest.TestCase):
    """`testmode_page_is` returns a verdict dict, which is truthy whether it passed or not: the guard must read the
    verdict's status, or the probe taps mode coordinates and labels captures on a page that never showed."""

    def setUp(self):
        self.taps = []
        self.session = mock.Mock()
        self.session.tap.side_effect = lambda *point: self.taps.append(point)

    def runner(self, *statuses):
        verdicts = [hwsuite.sub(f"stepped to page {transport.AUDIO_TEST_PAGE} (AUDIO TEST)", status == "PASS",
                                "" if status == "PASS" else "the page never showed") for status in statuses]
        self.assertEqual([v["status"] for v in verdicts], list(statuses))
        return mock.Mock(testmode_page_is=mock.Mock(side_effect=verdicts))

    def test_it_stops_when_the_page_never_showed(self):
        with self.assertRaises(SystemExit) as caught:
            transport.set_mode(self.session, self.runner("FAIL"), "MODE1")
        self.assertIn(f"not on test page {transport.AUDIO_TEST_PAGE}", str(caught.exception))
        self.assertEqual(self.taps, [])

    def test_it_stops_when_the_page_left_after_the_mode_tap(self):
        with mock.patch.object(transport.time, "sleep"):
            with self.assertRaises(SystemExit) as caught:
                transport.set_mode(self.session, self.runner("PASS", "FAIL"), "MODE1")
        self.assertIn(f"left test page {transport.AUDIO_TEST_PAGE}", str(caught.exception))
        self.assertEqual(self.taps, [transport.MODE_BUTTONS["MODE1"]])

    def test_none_taps_nothing_but_still_needs_the_page(self):
        runner = self.runner("FAIL")
        with self.assertRaises(SystemExit):
            transport.set_mode(self.session, runner, "none")
        self.assertEqual(self.taps, [])
        self.assertEqual(runner.testmode_page_is.call_count, 1)

    def test_a_page_that_shows_twice_taps_that_modes_coordinates(self):
        runner = self.runner("PASS", "PASS")
        with mock.patch.object(transport.time, "sleep"):
            transport.set_mode(self.session, runner, "MODE3")
        self.assertEqual(self.taps, [transport.MODE_BUTTONS["MODE3"]])
        self.assertEqual(runner.testmode_page_is.call_count, 2)


class ArgumentTest(unittest.TestCase):
    def test_impossible_thresholds_are_refused(self):
        cases = [["--floor", "0"], ["--floor", "1"], ["--purity", "-0.1"], ["--purity", "2"],
                 ["--tone", "10"], ["--tone", "30000"], ["--band", "0"], ["--band", "600"],
                 ["--settle", "1"], ["--timeout", "5"]]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(SystemExit), open(os.devnull, "w") as quiet:
                with contextlib.redirect_stderr(quiet):
                    transport.main(["run", "--build", "x"] + extra)


if __name__ == "__main__":
    unittest.main()
