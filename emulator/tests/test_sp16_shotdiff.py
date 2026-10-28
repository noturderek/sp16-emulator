"""The screenshot comparison of the hardware suite: tolerance, masks, regions, heat maps and the reference store."""
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import shotdiff

try:
    from PIL import Image
    import numpy  # noqa: F401
except ImportError:                                             # the suite itself needs both; so does this module
    Image = None


def solid(colour=(10, 20, 30), size=(100, 50)):
    return Image.new("RGB", size, colour)


@unittest.skipIf(Image is None, "needs pillow and numpy")
class CompareTests(unittest.TestCase):
    def test_identical_images_match(self):
        report = shotdiff.compare(solid(), solid())
        self.assertEqual((report["compared"], report["changed"], report["bbox"]), (5000, 0, None))

    def test_a_difference_within_the_tolerance_is_not_a_change(self):
        near = solid((10 + shotdiff.DEFAULT_TOLERANCE, 20, 30))
        self.assertEqual(shotdiff.compare(solid(), near)["changed"], 0)
        far = solid((10 + shotdiff.DEFAULT_TOLERANCE + 1, 20, 30))
        self.assertEqual(shotdiff.compare(solid(), far)["changed"], 5000)

    def test_a_changed_block_reports_its_count_and_box(self):
        actual = solid()
        actual.paste((200, 0, 0), (10, 5, 30, 15))
        report = shotdiff.compare(solid(), actual)
        self.assertEqual((report["changed"], report["bbox"]), (200, [10, 5, 20, 10]))
        self.assertAlmostEqual(report["fraction"], 200 / 5000)

    def test_a_mask_leaves_a_region_out(self):
        actual = solid()
        actual.paste((200, 0, 0), (10, 5, 30, 15))
        mask = shotdiff.Region(0.0, 0.0, 0.5, 0.5)             # pixels 0..50 x 0..25 cover the block
        report = shotdiff.compare(solid(), actual, masks=[mask])
        self.assertEqual(report["changed"], 0)
        self.assertEqual(report["compared"], 5000 - 50 * 25)

    def test_a_region_compares_only_inside_it(self):
        actual = solid()
        actual.paste((200, 0, 0), (10, 5, 30, 15))
        outside = shotdiff.Region(0.5, 0.0, 1.0, 1.0)
        self.assertEqual(shotdiff.compare(solid(), actual, region=outside)["changed"], 0)
        inside = shotdiff.Region(0.0, 0.0, 0.5, 1.0)
        self.assertEqual(shotdiff.compare(solid(), actual, region=inside)["changed"], 200)

    def test_images_of_different_sizes_cannot_be_compared(self):
        with self.assertRaises(shotdiff.ShotError):
            shotdiff.compare(solid(), solid(size=(100, 51)))

    def test_the_heat_map_marks_changes_red_and_masks_blue(self):
        actual = solid()
        actual.paste((200, 0, 0), (60, 5, 70, 10))
        mask = shotdiff.Region(0.0, 0.0, 0.5, 1.0)
        heat = shotdiff.heat_map(solid(), actual, masks=[mask])
        self.assertEqual(heat.getpixel((65, 7)), (255, 16, 64))
        red, green, blue = heat.getpixel((5, 5))
        self.assertGreater(blue, red)
        self.assertEqual(heat.getpixel((90, 40))[0], heat.getpixel((90, 40))[2])  # unchanged: grey


class RegionTests(unittest.TestCase):
    def test_parse_accepts_commas_and_spaces(self):
        self.assertEqual(shotdiff.Region.parse("0.1, 0.2, 0.3, 0.4"), shotdiff.Region(0.1, 0.2, 0.3, 0.4))
        self.assertEqual(shotdiff.Region.parse("0.1 0.2 0.3 0.4"), shotdiff.Region(0.1, 0.2, 0.3, 0.4))

    def test_parse_rejects_bad_regions(self):
        for text in ("0.1 0.2 0.3", "a b c d", "0.5 0 0.5 1", "0 0 1.5 1", "0.6 0 0.4 1"):
            with self.assertRaises(shotdiff.ShotError, msg=text):
                shotdiff.Region.parse(text)

    def test_a_mask_file_skips_comments_and_names_a_bad_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "home.mask"
            path.write_text("# meters\n0 0 0.5 0.5  # left\n\n0.5 0.5 1 1\n")
            self.assertEqual(len(shotdiff.read_masks(path)), 2)
            path.write_text("0 0 0.5 0.5\n1 1\n")
            with self.assertRaises(shotdiff.ShotError) as caught:
                shotdiff.read_masks(path)
            self.assertIn("home.mask line 2", str(caught.exception))
            self.assertEqual(shotdiff.read_masks(Path(folder) / "none.mask"), [])


@unittest.skipIf(Image is None, "needs pillow and numpy")
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.refs, self.out = Path(self.tmp.name) / "refs", Path(self.tmp.name) / "out"

    def store(self, mode="compare", **kw):
        return shotdiff.ShotStore(self.refs, self.out, mode, **kw)

    def record(self, name, image, promote=True):
        """One recording run: stage `image`, and promote it as if its check had passed."""
        store = self.store("record")
        store.check(name, image, owner="HW-52")
        store.promote({name} if promote else set())
        return store

    def test_recording_stages_and_only_a_passing_check_promotes(self):
        store = self.record("home", solid(), promote=False)
        self.assertFalse((self.refs / "home.png").exists())
        self.assertTrue((self.out / "recorded" / "home.png").exists())
        self.assertEqual(store.entries[0]["status"], "recorded")
        self.assertIn("not promoted", store.entries[0]["note"])
        store = self.record("home", solid())
        self.assertEqual(store.entries[0]["status"], "promoted")
        self.assertTrue((self.refs / "home.png").exists())

    def test_a_missing_reference_is_reported_not_recorded_when_comparing(self):
        entry = self.store().check("home", solid())
        self.assertEqual(entry["status"], "missing")
        self.assertFalse((self.refs / "home.png").exists())

    def test_a_recorded_capture_matches_the_next_run(self):
        self.record("home", solid())
        self.assertEqual(self.store().check("home", solid())["status"], "match")

    def test_a_changed_capture_fails_and_writes_a_heat_map(self):
        self.record("home", solid())
        store = self.store()
        changed = solid()
        changed.paste((255, 0, 0), (0, 0, 40, 20))
        entry = store.check("home", changed)
        self.assertEqual(entry["status"], "diff")
        self.assertEqual(entry["changed"], 800)
        self.assertTrue(Path(entry["heat_map"]).exists())
        self.assertEqual(store.failures(), [entry])
        self.assertIn("| home | diff | 800 |", store.render_report())

    def test_a_mask_sidecar_keeps_an_animated_region_out(self):
        self.record("home", solid())
        (self.refs / "home.mask").write_text("0 0 0.5 0.5\n")
        changed = solid()
        changed.paste((255, 0, 0), (0, 0, 40, 20))
        self.assertEqual(self.store().check("home", changed)["status"], "match")

    def test_recording_again_replaces_the_reference(self):
        self.record("home", solid())
        self.record("home", solid((200, 200, 200)))
        self.assertEqual(self.store().check("home", solid((200, 200, 200)))["status"], "match")

    def test_a_different_size_is_reported_not_raised(self):
        self.record("home", solid())
        entry = self.store().check("home", solid(size=(120, 50)))
        self.assertEqual(entry["status"], "size")

    def test_the_allowed_share_lets_small_changes_through(self):
        self.record("home", solid())
        changed = solid()
        changed.paste((255, 0, 0), (0, 0, 5, 5))
        self.assertEqual(self.store(max_fraction=0.01).check("home", changed)["status"], "match")

    def test_names_cannot_leave_the_folder(self):
        for name in ("../x", "a/b", "", ".hidden"):
            with self.assertRaises(shotdiff.ShotError, msg=name):
                self.store().check(name, solid())


if __name__ == "__main__":
    unittest.main()
