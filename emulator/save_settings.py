"""Make the firmware save its Utility settings now, by a short trip through standby.

The SP-16 firmware writes its settings (/root/data/Toraiz_SP16.dat on the data image) only when it goes into standby
after a setting changed, so a value set in UTILITY is lost if the emulator quits first. This presses the power key
into standby, waits for the standby flag and a moment for the save, then wakes the unit again:

    python3 emulator/save_settings.py --build pr-check

Research builds only (it drives `./sp16 ctl`).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def ctl(build, *words):
    """Run `./sp16 ctl --build BUILD WORDS...` and return its JSON reply (or raise with its output)."""
    result = subprocess.run([str(REPO / "sp16"), "ctl", "--build", build, *words], capture_output=True, text=True,
                            cwd=REPO)
    text = result.stdout.strip()
    if result.returncode:
        raise SystemExit(f"./sp16 ctl {' '.join(words)} failed: {(text + result.stderr).strip()[-300:]}")
    try:
        return json.loads(text[text.index("{"):]) if "{" in text else {}
    except ValueError:
        return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--build", required=True, help="the running research build, e.g. pr-check")
    parser.add_argument("--settle", type=float, default=4.0, help="seconds in standby before waking (default 4)")
    args = parser.parse_args(argv)
    reply = ctl(args.build, "power")
    if reply.get("standby") is not True:
        raise SystemExit(f"the unit did not enter standby: {reply}")
    print("standby: settings saved by the firmware; waking in", args.settle, "s", flush=True)
    time.sleep(args.settle)
    reply = ctl(args.build, "standby", "yes")
    print("awake" if reply.get("standby") is not True else f"still in standby: {reply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
