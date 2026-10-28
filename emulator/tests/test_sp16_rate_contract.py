"""Contract: every board PCM opens at 44.1 kHz only (tests/sp16_rate_contract.c, run under qemu-user in Docker)."""
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_contract  # noqa: E402


class RateContractTests(unittest.TestCase):
    def test_source_defaults_to_the_codec_rate(self):
        source = (arm_contract.TOOLS / "hardware_stub.c").read_text()
        self.assertIn("#define CODEC_RATE 44100u", source)
        self.assertNotIn(".rate=48000", source)

    @unittest.skipUnless(arm_contract.docker_works(), "docker not available")
    def test_all_three_streams_open_at_44100(self):
        with arm_contract.ArmContract() as arm:
            try:
                out = arm.run("sp16_rate_contract", [
                    "$PRE /tmp/run/t && echo PASS-fixed-rate",
                    "$PRE -E SP16_PCM_ANY_RATE=1 /tmp/run/t any && echo PASS-any-rate"])
            except subprocess.CalledProcessError as exc:
                self.fail((exc.stdout or "")[-2000:] + (exc.stderr or "")[-2000:])
            self.assertIn("PASS-fixed-rate", out)
            self.assertIn("PASS-any-rate", out)


if __name__ == "__main__":
    unittest.main()
