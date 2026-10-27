"""Contract: INPUT LEVEL gain, the input limiter and the analog level metrics.

Runs tests/sp16_input_chain_contract.c against hardware_stub.c under qemu-user in Docker (skipped without Docker), plus
host-side checks of the bridge's parsing of the shim's level line.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analog_laws  # noqa: E402
import service_stimulus  # noqa: E402
import arm_contract  # noqa: E402

LEVELS = ("[sp16-ui-stub] analog levels limiter=1 in_peak=0.9770 in_dbu=15.8 out_peak=0.2500 out_dbu=4.0 limited=412 "
          "main_out_gain=0.2500 input_gain=1.0000 source=tone")


def load_board_line():
    source = (arm_contract.TOOLS / "guest-bridge.py").read_text()
    head = source[source.index("UNKNOWN_IOCTL = "):source.index("def main():")]
    scope = {}
    exec("import re\n" + head, scope)
    return scope["board_line"]


class InputChainTests(unittest.TestCase):
    def test_bridge_reports_the_levels_and_the_limiter(self):
        key, value = load_board_line()(LEVELS)
        self.assertEqual(key, "analog_levels")
        self.assertIs(value["limiter_active"], True)
        self.assertEqual((value["in_dbu"], value["out_dbu"], value["limited_samples"], value["source"]), (15.8, 4.0, 412, "tone"))
        self.assertIs(load_board_line()(LEVELS.replace("limiter=1", "limiter=0"))[1]["limiter_active"], False)
        self.assertIsNone(load_board_line()("[sp16-ui-stub] analog levels garbage"))

    def test_service_stimulus_preset(self):
        self.assertEqual(service_stimulus.events(-10.0), [("main_out", 1000), ("input_level", 1000), ("input_tone", 604)])
        self.assertAlmostEqual(service_stimulus.dbv_to_dbfs(-10.0), -23.79, places=1)
        self.assertEqual(service_stimulus.tone_slot(0.0), 1000)
        with self.assertRaises(ValueError):
            service_stimulus.tone_slot(-61.0)

    def test_shim_constants_match_the_reference(self):
        source = (arm_contract.TOOLS / "hardware_stub.c").read_text()
        self.assertIn("#define FS_DBU %s" % analog_laws.FULL_SCALE_DBU, source.replace("16.0f", "16.0"))
        self.assertIn("#define LIMITER_THRESHOLD %sf" % analog_laws.LIMITER_THRESHOLD, source)

    @unittest.skipUnless(arm_contract.docker_works(), "docker not available")
    def test_input_chain_contract(self):
        with arm_contract.ArmContract() as arm:
            try:
                out = arm.run("sp16_input_chain_contract", [
                    "rm -f /tmp/run/analog",
                    "$PRE -E SP16_ANALOG=/tmp/run/analog /tmp/run/t && echo PASS-input-chain"])
            except subprocess.CalledProcessError as exc:
                self.fail((exc.stdout or "")[-2500:] + (exc.stderr or "")[-1500:])
            self.assertIn("PASS-input-chain", out)
            self.assertIn("analog levels limiter=1", out)
            self.assertIn("analog levels limiter=0", out)
            self.assertIn("source=tone", out)


if __name__ == "__main__":
    unittest.main()
