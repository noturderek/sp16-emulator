#!/usr/bin/env python3
"""Service-stimulus preset for a running research session: a sine (the shim's 1 kHz test tone) on the input jack at a stated level, INPUT LEVEL and
MAIN OUT at maximum, the way the service manual's check points are driven (-10 dBV in, both knobs at the top).

    python3 emulator/service_stimulus.py --build NAME [--dbv -10] [--dry-run]

The level is turned into the host tone slot with the converter scale of analog_laws.py (full scale = +16 dBu, grade I)
and sent with `./sp16 ctl event`. Then read metrics.json (guest.board.analog_levels: in_dbu, out_dbu, limiter_active)
to compare against the manual's check points. The scale is an assumption until measured on a unit.
"""
import argparse
import math
from pathlib import Path
import subprocess
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import analog_laws as laws  # noqa: E402

DBU_PER_DBV = 20 * math.log10(1 / 0.7746)  # 0 dBV = 1 V rms = +2.21 dBu


def dbv_to_dbfs(dbv):
    return dbv + DBU_PER_DBV - laws.FULL_SCALE_DBU


def tone_slot(dbfs):
    """Host tone slot (0..1000) for a level in dBFS; the shim maps the slot to -60 .. 0 dBFS linearly in dB."""
    if not -60 < dbfs <= 0:
        raise ValueError(f"{dbfs:.1f} dBFS is outside the tone range (-60 .. 0 dBFS)")
    return round((dbfs + 60) / 60 * laws.SLOT_MAX)


def events(dbv):
    return [("main_out", 1000), ("input_level", 1000), ("input_tone", tone_slot(dbv_to_dbfs(dbv)))]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--build", default=None, help="the research build whose newest session to drive")
    parser.add_argument("--dbv", type=float, default=-10.0, help="input level in dBV (default -10, the manual's stimulus)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    base = [str(TOOLS.parent / "sp16"), "ctl"] + (["--build", args.build] if args.build else [])
    for name, value in events(args.dbv):
        command = base + ["event", name, "value", str(value)]
        print(" ".join(command))
        if not args.dry_run and subprocess.run(command).returncode:
            return 1
    print(f"input {args.dbv:+.1f} dBV = {dbv_to_dbfs(args.dbv):+.1f} dBFS (assumed scale); expect in_dbu near "
          f"{args.dbv + DBU_PER_DBV:+.1f} before INPUT LEVEL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
