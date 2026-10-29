"""The analog-only control laws: reference module properties and the shim's constants staying in step with it."""
from pathlib import Path
import re
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import analog_laws as laws  # noqa: E402


class AnalogLawTests(unittest.TestCase):
    def test_neutral_until_the_control_moves(self):
        self.assertEqual((laws.main_out_gain(laws.NEVER_MOVED), laws.input_level_gain(laws.NEVER_MOVED)), (1.0, 1.0))
        self.assertEqual(laws.lp_cutoff_hz(laws.NEVER_MOVED), 20000.0)
        self.assertEqual(laws.lp_damping(laws.NEVER_MOVED), 2.0)

    def test_endpoints_and_monotonic_steps(self):
        self.assertEqual((laws.main_out_gain(0), laws.main_out_gain(1000)), (0.0, 1.0))
        self.assertAlmostEqual(laws.input_level_gain(500), 1.0)
        self.assertAlmostEqual(laws.input_level_gain(1000), 4.0)
        self.assertAlmostEqual(laws.lp_cutoff_hz(0), 20.0)
        self.assertAlmostEqual(laws.lp_cutoff_hz(1000), 20000.0, delta=1)
        self.assertAlmostEqual(laws.lp_damping(1000), 0.1)
        for f in (laws.main_out_gain, laws.input_level_gain, laws.lp_cutoff_hz):
            values = [f(slot) for slot in range(0, 1001, 50)]
            self.assertEqual(values, sorted(values))
        damping = [laws.lp_damping(slot) for slot in range(0, 1001, 50)]
        self.assertEqual(damping, sorted(damping, reverse=True))

    def test_resonance_peaks_at_the_cutoff(self):
        for res, expected in ((0, 0.5), (500, 1 / 1.05), (1000, 10.0)):
            self.assertAlmostEqual(laws.lp_gain(laws.lp_cutoff_hz(500), 500, res), expected, places=3)

    def test_dbu_scale_and_limiter(self):
        self.assertAlmostEqual(laws.to_dbu(1.0), 16.0)
        self.assertAlmostEqual(laws.to_dbu(0.5), 16 - 6.0206, places=3)
        self.assertEqual(laws.to_dbu(0.0), -99.0)
        self.assertEqual(laws.limit(0.3), 0.3)
        self.assertEqual(laws.limit(-0.5), -0.5)
        self.assertAlmostEqual(laws.limit(1.0), 0.75)
        self.assertLess(laws.limit(4.0), 1.0)
        self.assertEqual(laws.limit(-1.0), -0.75)

    def test_shim_constants_match_the_reference(self):
        source = (TOOLS / "hardware_stub.c").read_text()
        self.assertIn("a.main_gain=a.main_set?pot(v[0])*pot(v[0]):1", source)
        self.assertIn("a.input_gain=v[1]!=0xffff?4*pot(v[1])*pot(v[1]):1", source)
        self.assertIn("20*bm_exp2(pot(v[2])*BM_DECADES3)", source)
        self.assertIn("a.lp_k=v[3]!=0xffff?2-1.9f*pot(v[3]):2", source)

    def test_every_law_has_a_grade(self):
        for name, law in laws.LAWS.items():
            self.assertTrue(law["grades"], name)
            self.assertTrue(all(re.match(r"[SIU]\b", g) for g in law["grades"].values()), name)


if __name__ == "__main__":
    unittest.main()
