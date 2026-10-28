"""Menu recipes for the SP-16's UTILITY sync/MIDI settings (OEM firmware 1.60).

Each recipe is an ordered list of `run_recipe` steps (emulator/hwsuite.py). It drives the firmware by LCD OCR:
open UTILITY, focus a setting row, push the selector to edit, focus the value, push to confirm, return HOME. The
layout was discovered on a live 1.60 boot (see the lane's changes file):

* UTILITY opens from the HOME screen by tapping (748, 58); its title reads `HOME > UTILITY`.
* The setting list is the left column (box 20,145,390,450); the focused row is drawn with a light background.
* The focused setting's value is edited in the right column (box 395,145,765,450).
* The focus position in the list is remembered across UTILITY visits, so `focus_row` searches from wherever it
  starts (it reverses at the end of the non-wrapping list).
* SYNC SOURCE SELECT's values are INTERNAL / PRO DJ LINK / DIN MIDI / USB MIDI (firmware setting id 0x2d).
* MIDI OUT MODE (USB)'s values are OUT / OUT + MIDI IN (setting id 0x2b).

Run one against a live research session:

    python3 emulator/menu/sync_source.py --build lk1-live sync-source-pdl
    python3 emulator/menu/sync_source.py --runtime PATH/runtime.json usb-out
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import hwsuite  # noqa: E402

LEFT = [20, 145, 390, 450]     # UTILITY: the setting list (left column)
RIGHT = [395, 145, 765, 450]   # UTILITY: the focused setting's value (right column)
UTILITY_TAP = [748, 58]        # the UTILITY icon on the HOME screen


def set_row(setting, value, value_detents=1):
    """Steps that open UTILITY, focus `setting`, push, focus `value`, push, and return HOME.

    `value_detents` is the number of selector detents from the setting's current value to `value` (the recipe's
    `focus_row` then confirms the label by OCR rather than trusting the count)."""
    return [
        {"settle": 1.0},
        {"press": "home"},
        {"expect_title": "HOME"},
        {"tap": UTILITY_TAP},
        {"expect_title": "UTILITY"},
        {"focus_row": setting, "box": LEFT, "max_detents": 60},
        {"press": "selector"},
        {"wait": 0.8},
        {"focus_row": value, "box": RIGHT, "max_detents": value_detents + 4, "detents": 1},
        {"press": "selector"},
        {"wait": 0.5},
        {"press": "home"},
        {"expect_title": "HOME"},
    ]


#: SYNC SOURCE SELECT = PRO DJ LINK (follow a Pro DJ Link master; firmware setting id 0x2d -> 0xf1).
SYNC_SOURCE_PDL = set_row("SYNC SOURCE SELECT", "PRO DJ LINK", value_detents=1)

#: MIDI OUT MODE (USB) = OUT (generated USB-B note/CC output; firmware setting id 0x2b -> 0x120).
USB_OUT = set_row("MIDI OUT MODE (USB)", "OUT$", value_detents=0)


def _usb_note_out_steps():
    """USB-B note output: MIDI OUT MODE (USB) = OUT and the project's MIDI OUT NOTE/CC = ENABLE, in one UTILITY visit.

    The list has two NOTE/CC rows (MIDI IN at the top, MIDI OUT lower); the recipe reaches the MIDI OUT one by
    focusing the unique MIDI OUT MODE (USB) row first and then searching upward, so the first NOTE/CC it finds is the
    MIDI OUT row (project key 0x11000102), not the MIDI IN one.
    """
    return [
        {"settle": 1.0},
        {"press": "home"},
        {"expect_title": "HOME"},
        {"tap": UTILITY_TAP},
        {"expect_title": "UTILITY"},
        {"focus_row": "MIDI OUT MODE (USB)", "box": LEFT, "max_detents": 60},
        {"press": "selector"},
        {"wait": 0.8},
        {"focus_row": "OUT$", "box": RIGHT, "max_detents": 6, "detents": 1},
        {"press": "selector"},
        {"wait": 0.5},
        {"focus_row": "NOTE/CC", "box": LEFT, "max_detents": 12, "detents": -1},
        {"press": "selector"},
        {"wait": 0.8},
        {"focus_row": "ENABLE", "box": RIGHT, "max_detents": 6, "detents": 1},
        {"press": "selector"},
        {"wait": 0.5},
        {"press": "home"},
        {"expect_title": "HOME"},
    ]


#: MIDI OUT MODE (USB) = OUT and MIDI OUT NOTE/CC = ENABLE: firmware-generated USB-B notes on a pad hit.
USB_NOTE_OUT = _usb_note_out_steps()

RECIPES = {
    "sync-source-pdl": ("SYNC SOURCE SELECT = PRO DJ LINK", SYNC_SOURCE_PDL),
    "usb-out": ("MIDI OUT MODE (USB) = OUT", USB_OUT),
    "usb-note-out": ("MIDI OUT MODE (USB) = OUT, MIDI OUT NOTE/CC = ENABLE", USB_NOTE_OUT),
}


def newest_session(build):
    """The newest running session's runtime.json for a research build, or None."""
    sessions = Path(build) / "sessions"
    if not sessions.is_dir():
        return None
    runtimes = sorted(sessions.glob("native-*/runtime.json"), key=lambda p: p.stat().st_mtime)
    return runtimes[-1] if runtimes else None


def run(session, steps, log=None):
    """Run one recipe's steps and return the step log (raises hwsuite.RecipeError on a failed expectation)."""
    return hwsuite.run_recipe(session, steps, log=log)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("recipe", choices=sorted(RECIPES), help="which recipe to run")
    parser.add_argument("--build", help="research build folder (newest running session)")
    parser.add_argument("--runtime", help="a session's runtime.json (instead of --build)")
    args = parser.parse_args(argv)
    if args.runtime:
        runtime = Path(args.runtime)
    elif args.build:
        build = Path(args.build if "/" in args.build else TOOLS.parent / "build" / args.build).resolve()
        runtime = newest_session(build)
        if runtime is None:
            raise SystemExit(f"no running session under {build}/sessions")
    else:
        raise SystemExit("give --build NAME or --runtime FILE")
    title, steps = RECIPES[args.recipe]
    session = hwsuite.Session(runtime)
    try:
        print(f"recipe {args.recipe}: {title}", flush=True)
        for step in run(session, steps):
            if isinstance(step, dict) and any(k in step for k in ("expect_title", "expect_text", "focus_row", "if_text")):
                print(f"  {step}", flush=True)
        print("done", flush=True)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
