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

    def test_compare_raises_when_nothing_is_left_to_compare(self):
        self.assertTrue(issubclass(shotdiff.EmptyComparison, shotdiff.ShotError))
        with self.assertRaises(shotdiff.EmptyComparison) as caught:
            shotdiff.compare(solid(), solid(), masks=[shotdiff.Region(0.0, 0.0, 1.0, 1.0)])
        self.assertIn("mask leaves nothing to compare", str(caught.exception))
        with self.assertRaises(shotdiff.EmptyComparison):              # a region too thin to hold one pixel
            shotdiff.compare(solid(), solid(), region=shotdiff.Region(0.0, 0.0, 0.001, 0.001))

    def test_the_fraction_is_exact_not_rounded(self):
        actual = solid(size=(2560, 1600))
        actual.putpixel((5, 5), (255, 255, 255))
        report = shotdiff.compare(solid(size=(2560, 1600)), actual)
        self.assertEqual((report["compared"], report["changed"]), (4096000, 1))
        self.assertEqual(report["fraction"], 1 / 4096000)

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


class ThresholdTests(unittest.TestCase):
    """A threshold outside its range would switch the comparison off without a word, so it is an error."""

    def test_tolerance_is_a_whole_number_from_0_to_254(self):
        for good in (0, 1, shotdiff.DEFAULT_TOLERANCE, 254):
            self.assertEqual(shotdiff.check_tolerance(good), good)
        for bad in (-1, 255, 256, 1000, 8.5, "8", None, True, float("nan")):
            with self.assertRaises(shotdiff.ShotError, msg=repr(bad)) as caught:
                shotdiff.check_tolerance(bad)
            self.assertIn("0 to 254", str(caught.exception))

    def test_share_is_a_number_from_0_up_to_but_not_including_1(self):
        for good in (0, 0.0, 0.001, 0.5, 0.999999):
            self.assertEqual(shotdiff.check_share(good), good)
        for bad in (-0.1, 1, 1.0, 1.5, 2, float("nan"), float("inf"), "0.5", None, True):
            with self.assertRaises(shotdiff.ShotError, msg=repr(bad)) as caught:
                shotdiff.check_share(bad)
            self.assertIn("not including, 1", str(caught.exception))

    @unittest.skipIf(Image is None, "needs pillow and numpy")
    def test_no_entry_point_takes_a_tolerance_that_calls_every_pixel_equal(self):
        black, white = solid((0, 0, 0)), solid((255, 255, 255))
        # The largest difference two 8-bit channels can have is 255: a tolerance of 254 still sees it, 255 would not.
        self.assertEqual(shotdiff.compare(black, white, tolerance=254)["changed"], 5000)
        for tolerance in (255, 256):
            with self.assertRaises(shotdiff.ShotError, msg=f"compare {tolerance}"):
                shotdiff.compare(black, white, tolerance=tolerance)
            with self.assertRaises(shotdiff.ShotError, msg=f"heat_map {tolerance}"):
                shotdiff.heat_map(black, white, tolerance=tolerance)
            with self.assertRaises(shotdiff.ShotError, msg=f"ShotStore {tolerance}"):
                shotdiff.ShotStore("refs", "out", "compare", tolerance=tolerance)

    def test_the_store_refuses_a_share_that_would_let_a_changed_capture_pass(self):
        for share in (1, 1.0, 3):
            with self.assertRaises(shotdiff.ShotError, msg=repr(share)):
                shotdiff.ShotStore("refs", "out", "compare", max_fraction=share)
        store = shotdiff.ShotStore("refs", "out", "compare", tolerance=0, max_fraction=0.5)
        self.assertEqual((store.tolerance, store.max_fraction), (0, 0.5))


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


class AllowedChangesTests(unittest.TestCase):
    """The pass mark is a whole number of pixels, floor(share * compared), read from the decimal the share was written as."""

    def test_the_most_changed_pixels_a_capture_may_have(self):
        for compared, share, expected in ((3911680, 0, 0), (3911680, 0.0, 0), (5000, 0.01, 50), (10, 0.3, 3), (7, 0.5, 3),
                                          (1, 0.999, 0), (4096000, 1e-7, 0), (4096000, 1e-6, 4), (99, 0.999999, 98),
                                          (100, 0.29, 29)):   # 100 * 0.29 is 28.999999999999996 in binary floating point
            self.assertEqual(shotdiff.allowed_changes(compared, share), expected, (compared, share))

    def test_a_share_below_1_never_lets_a_completely_changed_capture_through(self):
        for compared in (1, 2, 100, 3911680):
            self.assertLess(shotdiff.allowed_changes(compared, 0.999999999), compared)


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

    def test_an_identical_capture_still_matches_at_the_default_share(self):
        self.record("home", solid())
        entry = self.store().check("home", solid())
        self.assertEqual((entry["status"], entry["changed"], entry["allowed"], entry["compared"]), ("match", 0, 0, 5000))

    def test_one_changed_pixel_in_a_full_size_capture_fails_at_the_default_share(self):
        size = (2560, 1600)                  # a panel capture: 4,096,000 pixels, so one pixel is 0.0000244% of them
        self.record("home", solid(size=size))
        changed = solid(size=size)
        changed.putpixel((1280, 800), (255, 255, 255))
        store = self.store()
        entry = store.check("home", changed)
        self.assertEqual((entry["status"], entry["changed"], entry["allowed"], entry["compared"]), ("diff", 1, 0, 4096000))
        self.assertEqual(round(entry["fraction"], 6), 0.0)   # the old rounded fraction: it called this capture a match
        self.assertEqual(store.failures(), [entry])
        self.assertTrue(Path(entry["heat_map"]).exists())

    def test_a_change_just_inside_and_just_over_the_allowed_share(self):
        def changed(count, size=(100, 50)):
            image = solid(size=size)
            for i in range(count):
                image.putpixel((i % size[0], i // size[0]), (255, 0, 0))
            return image
        self.record("home", solid())                         # 5000 pixels
        store = self.store(max_fraction=0.01)                # floor(0.01 * 5000) = 50
        self.assertEqual([store.check("home", changed(n))["status"] for n in (49, 50, 51)], ["match", "match", "diff"])
        self.record("small", solid(size=(10, 10)))           # 100 pixels
        store = self.store(max_fraction=0.29)                # 29, as the decimal 0.29 says (not 28)
        self.assertEqual([store.check("small", changed(n, (10, 10)))["status"] for n in (28, 29, 30)],
                         ["match", "match", "diff"])
        self.record("panel", solid(size=(2560, 1600)))       # 4,096,000 pixels
        store = self.store(max_fraction=1e-6)                # floor(4.096) = 4; rounded to 6 places, 4 and 5 both look like 1e-6
        self.assertEqual([store.check("panel", changed(n, (2560, 1600)))["status"] for n in (4, 5)], ["match", "diff"])

    def test_masks_that_cover_the_whole_capture_fail_instead_of_matching(self):
        self.record("home", solid())
        changed = solid()
        changed.paste((255, 0, 0), (0, 0, 40, 20))
        store = self.store()
        entry = store.check("home", changed, masks=[shotdiff.Region(0.0, 0.0, 1.0, 1.0)])
        self.assertEqual(entry["status"], "empty")
        self.assertIn("mask leaves nothing to compare", entry["note"])
        self.assertNotIn("changed", entry)                   # nothing was compared, so there is no zero to read as a pass
        self.assertEqual(store.failures(), [entry])
        self.assertIn("| home | empty |", store.render_report())
        self.assertIn("mask leaves nothing to compare", store.render_report())

    def test_a_sidecar_mask_over_everything_fails_even_an_identical_capture(self):
        self.record("home", solid())
        (self.refs / "home.mask").write_text("0 0 1 1\n")
        self.assertEqual(self.store().check("home", solid())["status"], "empty")

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
