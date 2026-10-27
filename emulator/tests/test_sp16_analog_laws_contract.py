"""The shim's MAIN OUT, CUTOFF and RESONANCE against analog_laws.py (tests/sp16_analog_laws_contract.c, Docker)."""
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analog_laws as laws  # noqa: E402
import arm_contract  # noqa: E402


class AnalogLawContractTests(unittest.TestCase):
    @unittest.skipUnless(arm_contract.docker_works(), "docker not available")
    def test_shim_follows_the_reference_laws(self):
        fc = 1000
        args = "%.6f %.6f %.6f" % (laws.main_out_gain(500), laws.lp_gain(fc, 500, 0), laws.lp_gain(fc, 500, 500))
        with arm_contract.ArmContract() as arm:
            try:
                out = arm.run("sp16_analog_laws_contract", [
                    "rm -f /tmp/run/analog /tmp/run/port; : > /tmp/run/port",
                    "$PRE -E SP16_ANALOG=/tmp/run/analog -E SP16_PCM_PORT=/tmp/run/port /tmp/run/t %s && echo PASS-laws" % args])
            except subprocess.CalledProcessError as exc:
                self.fail("\n".join(l for l in ((exc.stdout or "") + (exc.stderr or "")).splitlines() if "Assertion" in l or "error" in l or "first=" in l or "analog" in l)[-1500:])
            self.assertIn("PASS-laws", out)


if __name__ == "__main__":
    unittest.main()
