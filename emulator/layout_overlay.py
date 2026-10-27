"""Overlay panel-layout.json on a top-view render and print where each control sits, in mm.

The layout canvas is the chassis: its full width (canvas.width units) is the chassis width, 436.5 mm. The render is
scaled so that its pixel width covers the same canvas width, so one layout unit is 436.5 / canvas.width mm and the
same number of pixels. Offsets are measured from the chassis top-left corner (the canvas origin), and the vertical
scale is the horizontal one, so the layout is never stretched.

The render is private (firmware/TORAIZ-SP-16/references). It is read in place and never copied into the repo; the
overlay goes to build/, which git ignores.

  python3 emulator/layout_overlay.py [--image PATH] [--out PATH]
"""
import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
LAYOUT_PATH = TOOLS / "panel-layout.json"
DEFAULT_IMAGE = Path("/Users/shwanton/Projects/pioneer/internal/firmware/TORAIZ-SP-16/references/originals/pioneerdj"
                     "/SP16-005_toraiz-sp-16_5.png")
DEFAULT_OUT = REPO / "build" / "layout-overlay.png"
CHASSIS_WIDTH_MM = 436.5
KIND_COLOURS = {"pad": (0, 255, 255), "button": (255, 200, 0), "step": (255, 120, 255), "bar": (120, 255, 120),
                "knob": (255, 80, 80), "selector": (255, 80, 80), "analog": (255, 160, 60), "strip": (80, 160, 255),
                "lamp": (255, 255, 255)}


def unit_scale(layout, image_width):
    """(pixels per layout unit, mm per layout unit) for a render whose width is the chassis width."""
    canvas_width = layout["canvas"]["width"]
    return image_width / canvas_width, CHASSIS_WIDTH_MM / canvas_width


def control_offsets(layout, mm_per_unit):
    """[(id, name, x_mm, y_mm, distance_mm)] for every control, from the chassis top-left corner."""
    rows = []
    for control in layout["controls"]:
        x, y = control["cx"] * mm_per_unit, control["cy"] * mm_per_unit
        rows.append((control["id"], control["name"], x, y, math.hypot(x, y)))
    return rows


def draw_overlay(image, layout, px_per_unit):
    draw = ImageDraw.Draw(image)
    stroke = max(2, round(px_per_unit * 1.5))
    dot = max(3, round(px_per_unit * 2))
    font = ImageFont.load_default(size=max(10, round(px_per_unit * 11)))
    for control in layout["controls"]:
        cx, cy = control["cx"] * px_per_unit, control["cy"] * px_per_unit
        colour = KIND_COLOURS.get(control["kind"], (255, 255, 255))
        if control["shape"] == "circle":
            radius = control["r"] * px_per_unit
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=colour, width=stroke)
        else:
            half_w, half_h = control["w"] * px_per_unit / 2, control["h"] * px_per_unit / 2
            draw.rectangle((cx - half_w, cy - half_h, cx + half_w, cy + half_h), outline=colour, width=stroke)
        draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=colour)
        draw.text((cx + dot * 2, cy - dot * 2), control["name"], fill=colour, font=font)


def render_on_white(path):
    """The render has transparent corners: composite it over white so the overlay reads on any viewer."""
    with Image.open(path) as source:
        rgba = source.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, rgba).convert("RGB")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="top-view render (read in place)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="overlay PNG to write")
    args = parser.parse_args(argv)

    layout = json.loads(LAYOUT_PATH.read_text())
    image = render_on_white(args.image)
    px_per_unit, mm_per_unit = unit_scale(layout, image.width)
    draw_overlay(image, layout, px_per_unit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out)

    height_mm = image.height / px_per_unit * mm_per_unit
    layout_height_mm = layout["canvas"]["height"] * mm_per_unit
    print(f"render {image.width}x{image.height} px = {CHASSIS_WIDTH_MM} mm wide, {image.width / CHASSIS_WIDTH_MM:.3f} px/mm")
    print(f"layout canvas covers {layout_height_mm:.1f} mm of the render's {height_mm:.1f} mm height (top-left anchored)")
    print(f"overlay: {args.out}")
    print(f"{'id':>3} {'name':<14} {'x_mm':>8} {'y_mm':>8} {'dist_mm':>8}")
    rows = sorted(control_offsets(layout, mm_per_unit), key=lambda row: row[4], reverse=True)
    for control_id, name, x, y, distance in rows:
        print(f"{control_id:>3} {name:<14} {x:8.2f} {y:8.2f} {distance:8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
