"""Pixel comparison of two captures of one screen, for the hardware suite's screenshot regression layer.

A pixel differs when any colour channel differs by more than the tolerance. The tolerance (a whole number from 0 to 254)
and the share of changed pixels a capture may have (from 0 up to, but not including, 1) are checked: a value outside
them would switch the comparison off, so it is an error. A capture matches when no more pixels changed than
floor(share * compared pixels), decided on whole pixels: a share of 0 allows none. When the masks leave no pixel to
compare, that is an error, never a match. Regions that always move (the level meters, the clock, a playhead) are masked
out; a mask is a rectangle in fractions of the image, so it survives a window resize. The references are the suite's own
earlier captures, kept in a folder outside the repository (they show the firmware's screen, which is never committed);
nothing here knows or reads any manufacturer image.

  Region      a rectangle as fractions (x0, y0, x1, y1) of the image, 0..1, from the top-left corner
  read_masks  a `.mask` sidecar: one `x0 y0 x1 y1` per line, `#` starts a comment
  compare     reference + actual -> a report {compared, changed, fraction, bbox} (EmptyComparison if nothing is compared)
  heat_map    the picture of the difference: actual in grey, changed pixels red, masked pixels tinted blue
  ShotStore   a folder of `<name>.png` references; stage, promote, compare and the diff files of one run

The design follows the pixel comparison of the open CDJ-3000 emulator's control crate (tolerance, masks, region, a heat
map), written again here for Pillow and numpy.
"""
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
import re

DEFAULT_TOLERANCE = 8          # per channel, 0..254: absorbs resampling and colour-management noise, not a recolour
DEFAULT_MAX_FRACTION = 0.0     # share of compared pixels allowed to differ (rounded down to whole pixels: 0 allows none)
# Two 8-bit channels differ by at most 255, and a pixel is changed only when its difference is above the tolerance, so a
# tolerance of 255 or more would call every pixel equal and switch the comparison off without a word.
MAX_TOLERANCE = 254
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ShotError(ValueError):
    """A capture, reference or setting that cannot be used: a bad name, a bad mask line, two sizes that differ, a
    threshold outside its range."""


class EmptyComparison(ShotError):
    """No pixel is left to compare (the masks and the region cover the whole capture): nothing was checked, and that must
    never read as a match."""


def check_tolerance(value):
    """`value` as a per-channel tolerance: a whole number from 0 to 254 (see MAX_TOLERANCE). Returns it."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOLERANCE:
        raise ShotError(f"tolerance {value!r} is not a whole number from 0 to {MAX_TOLERANCE}: 255 and above would count "
                        f"every pixel as equal")
    return value


def allowed_changes(compared, share):
    """The most changed pixels a capture of `compared` compared pixels may have and still match: floor(share * compared).
    It is decided on whole pixels, so a share of 0 allows none however large the capture, and no rounding of a fraction can
    let a changed capture through. The share is read as the decimal it was written as (0.29 of 100 pixels is 29)."""
    return int((Decimal(str(float(share))) * compared).to_integral_value(rounding=ROUND_FLOOR))


def check_share(value):
    """`value` as the allowed share of changed pixels: a number from 0 up to, but not including, 1. Returns it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1:
        raise ShotError(f"allowed share {value!r} is not a number from 0 up to, but not including, 1: a share of 1 would "
                        f"let a completely changed capture pass")
    return value


class Region:
    def __init__(self, x0, y0, x1, y1):
        values = (x0, y0, x1, y1)
        if not all(0.0 <= v <= 1.0 for v in values) or x0 >= x1 or y0 >= y1:
            raise ShotError(f"{values} is not a region: x0 < x1 and y0 < y1, all from 0 to 1")
        self.x0, self.y0, self.x1, self.y1 = values

    @classmethod
    def parse(cls, text):
        try:
            values = [float(word) for word in re.split(r"[,\s]+", text.strip()) if word]
        except ValueError:
            raise ShotError(f"{text!r} is not four numbers") from None
        if len(values) != 4:
            raise ShotError(f"{text!r} is not four numbers")
        return cls(*values)

    def pixels(self, width, height):
        """(left, top, right, bottom) in pixels: every pixel whose centre lies inside the region."""
        left, right = round(self.x0 * width), round(self.x1 * width)
        top, bottom = round(self.y0 * height), round(self.y1 * height)
        return left, top, right, bottom

    def __eq__(self, other):
        return isinstance(other, Region) and vars(self) == vars(other)

    def __repr__(self):
        return f"Region({self.x0}, {self.y0}, {self.x1}, {self.y1})"


def read_masks(path):
    """The regions of a `.mask` file (a missing file means none)."""
    path = Path(path)
    if not path.is_file():
        return []
    regions = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if line:
            try:
                regions.append(Region.parse(line))
            except ShotError as error:
                raise ShotError(f"{path.name} line {number}: {error}") from None
    return regions


def keep_mask(shape, masks, region=None):
    """Boolean array, True where a pixel is compared: not under a mask and, when `region` is given, inside it."""
    import numpy as np
    height, width = shape[:2]
    keep = np.ones((height, width), dtype=bool)
    if region is not None:
        left, top, right, bottom = region.pixels(width, height)
        inside = np.zeros_like(keep)
        inside[top:bottom, left:right] = True
        keep &= inside
    for mask in masks:
        left, top, right, bottom = mask.pixels(width, height)
        keep[top:bottom, left:right] = False
    return keep


def as_array(image):
    import numpy as np
    return np.asarray(image.convert("RGB"), dtype=np.int16)


def changed_pixels(reference, actual, tolerance=DEFAULT_TOLERANCE, masks=(), region=None):
    """(changed boolean array, keep boolean array) of two same-size PIL images."""
    check_tolerance(tolerance)
    import numpy as np
    if reference.size != actual.size:
        raise ShotError(f"the sizes differ: reference {reference.size[0]}x{reference.size[1]}, "
                        f"capture {actual.size[0]}x{actual.size[1]}")
    a, b = as_array(reference), as_array(actual)
    keep = keep_mask(a.shape, masks, region)
    changed = (np.abs(a - b).max(axis=2) > tolerance) & keep
    return changed, keep


def compare(reference, actual, tolerance=DEFAULT_TOLERANCE, masks=(), region=None):
    """The report of one comparison: pixels compared and changed, the changed share (exact: round it only to show it) and
    the box around the changes. Raises EmptyComparison when the masks and region leave no pixel to compare."""
    import numpy as np
    changed, keep = changed_pixels(reference, actual, tolerance, masks, region)
    compared, count = int(keep.sum()), int(changed.sum())
    if not compared:
        raise EmptyComparison("the mask leaves nothing to compare: every pixel is masked or outside the region")
    box = None
    if count:
        ys, xs = np.nonzero(changed)
        box = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
    return {"width": actual.size[0], "height": actual.size[1], "compared": compared, "changed": count,
            "fraction": count / compared, "bbox": box}


def heat_map(reference, actual, tolerance=DEFAULT_TOLERANCE, masks=(), region=None):
    """A PIL image of the difference: the capture dimmed to grey, changed pixels red, masked pixels tinted blue."""
    import numpy as np
    from PIL import Image
    changed, keep = changed_pixels(reference, actual, tolerance, masks, region)
    a = as_array(actual)
    grey = ((a[..., 0] * 3 + a[..., 1] * 6 + a[..., 2]) // 30).astype(np.uint8)
    out = np.stack([grey, grey, grey], axis=2)
    out[~keep] = np.stack([grey[~keep] // 2, grey[~keep] // 2, np.minimum(grey[~keep] // 2 + 96, 255)], axis=1)
    out[changed] = (255, 16, 64)
    return Image.fromarray(out, "RGB")


class ShotStore:
    """A folder of reference captures and the captures of one run.

    "record" stages each capture in `<out>/recorded/` and writes nothing into the reference folder until `promote` is
    told which captures belong to checks that passed: a reference taken from a failing run would make the next good run
    look like a regression. "compare" checks each capture against its reference and writes the heat map of a difference
    into `<out>`; a capture with no reference yet is "missing" (record one first).

    A status is "recorded" (staged), "match", "diff" (more changed pixels than `allowed_changes` lets through: with the
    default share of 0, any one), "size" (the capture's size differs from the reference's), "empty" (the masks leave
    nothing to compare) or "missing"."""

    def __init__(self, folder, out, mode="compare", tolerance=DEFAULT_TOLERANCE, max_fraction=DEFAULT_MAX_FRACTION):
        if mode not in ("record", "compare"):
            raise ShotError(f"unknown mode {mode!r}")
        self.folder, self.out, self.mode = Path(folder), Path(out), mode
        self.tolerance, self.max_fraction = check_tolerance(tolerance), check_share(max_fraction)
        self.entries = []

    def reference(self, name):
        return self.folder / f"{name}.png"

    def staged(self, name):
        return self.out / "recorded" / f"{name}.png"

    def check(self, name, image, masks=(), owner=None):
        if not NAME_PATTERN.match(name):
            raise ShotError(f"bad capture name {name!r}")
        from PIL import Image
        reference = self.reference(name)
        masks = list(masks) + read_masks(reference.with_suffix(".mask"))
        entry = {"name": name, "mode": self.mode, "masks": len(masks), "check": owner}
        if self.mode == "record":
            path = self.staged(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            image.convert("RGB").save(path)
            entry.update(status="recorded", note="staged until its check passes")
        elif not reference.exists():
            entry.update(status="missing", note="no reference yet: run once with --shots record")
        else:
            with Image.open(reference) as handle:
                expected = handle.convert("RGB")
            try:
                report = compare(expected, image, self.tolerance, masks)
            except EmptyComparison as error:
                entry.update(status="empty", note=str(error))
            except ShotError as error:
                entry.update(status="size", note=str(error))
            else:
                allowed = allowed_changes(report["compared"], self.max_fraction)
                entry.update(report, allowed=allowed)
                entry["status"] = "diff" if report["changed"] > allowed else "match"
                if entry["status"] == "diff":
                    self.out.mkdir(parents=True, exist_ok=True)
                    path = self.out / f"{name}-diff.png"
                    heat_map(expected, image, self.tolerance, masks).save(path)
                    entry["heat_map"] = str(path)
        self.entries.append(entry)
        return entry

    def promote(self, names):
        """Move the staged captures called `names` into the reference folder (replacing a reference); returns the names
        promoted. Entries of the others stay staged under `<out>/recorded/` and say so in the report."""
        import shutil
        promoted = []
        for entry in self.entries:
            if entry["status"] == "recorded" and entry["name"] in names:
                self.folder.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.staged(entry["name"]), self.reference(entry["name"]))
                entry["status"], entry["note"] = "promoted", "now the reference"
                promoted.append(entry["name"])
        for entry in self.entries:
            if entry["status"] == "recorded":
                entry["note"] = "not promoted: its check did not pass"
        return promoted

    def failures(self):
        return [e for e in self.entries if e["status"] in ("diff", "size", "empty")]

    def render_report(self):
        """shots-report.md: one row per capture, the changed share and the box, and the heat map file of each diff."""
        lines = [f"# Screenshot regression ({self.mode}, tolerance {self.tolerance}, allowed share {self.max_fraction:g})",
                 "", "| Capture | Status | Changed | Share | Box | Heat map |", "|---|---|---|---|---|---|"]
        for e in self.entries:
            lines.append("| {name} | {status} | {changed} | {share} | {box} | {heat} |".format(
                name=e["name"], status=e["status"], changed=e.get("changed", ""),
                share=f"{e['fraction']:.4%}" if "fraction" in e else e.get("note", ""),
                box=e.get("bbox") or "", heat=Path(e["heat_map"]).name if e.get("heat_map") else ""))
        return "\n".join(lines) + "\n"
