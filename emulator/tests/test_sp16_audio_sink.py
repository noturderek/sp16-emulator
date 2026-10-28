"""File sink: the 16-channel master stream keeps its 4-channel WAV, and the output monitor table is selectable.

Compiles audio-selftest.swift (never part of the app) and checks the sink scenarios; no audio device is opened.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("swiftc"), "swiftc not available")
class AudioSinkTests(unittest.TestCase):
    def test_wav_slice_and_monitor_table(self):
        with tempfile.TemporaryDirectory() as work:
            binary = Path(work) / "selftest"
            build = subprocess.run(
                ["swiftc", "-O", "-parse-as-library", "-framework", "AVFoundation", "-framework", "CoreAudio",
                 str(TOOLS / "audio-selftest.swift"), str(TOOLS / "audio-output.swift"), str(TOOLS / "audio-sink.swift"),
                 "-o", str(binary)], capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, build.stderr[-2000:])
            run = subprocess.run([str(binary), "test"], capture_output=True, text=True, timeout=300)
            for name in ("sink-wav-slice", "sink-default-channels", "monitor-jack"):
                self.assertIn("PASS " + name, run.stdout, run.stdout[-2000:])


if __name__ == "__main__":
    unittest.main()
