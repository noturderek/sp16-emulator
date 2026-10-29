"""MIDI thru from a hardware device into a running emulator.

Everything a hardware MIDI source sends (for example a physical SP-16 on USB, `PIONEER DJ TSP-16 MIDI`) is passed on
to the emulator's `SP-16 USB [<build>]` destination, the emulated unit's USB-B MIDI IN, or to `SP-16 DIN [<build>]`
with --din. macOS never connects a hardware source to a virtual destination by itself, so a running thru is what lets
the physical unit's PLAY, STOP and clock drive the emulated sequencer.

    python3 emulator/midi/thru.py --build pr-check                 # TSP-16 -> SP-16 USB [pr-check]
    python3 emulator/midi/thru.py --build pr-check --din           # ... -> SP-16 DIN [pr-check]
    python3 emulator/midi/thru.py --build pr-check --source "Launchpad"

It waits for both ends to appear, so it can start before the emulator (`./sp16 run --name NAME --midi`). It uses
the MIDI bridge's CoreMIDI helper (`run --in SOURCE --out DESTINATION`): each `in HEX` line it prints is written
back as `out HEX`. Stop it with Ctrl-C.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

QUIET = ("f8", "fe")   # clock and active sensing: forwarded, never printed


def target_name(build, din=False):
    return f"SP-16 {'DIN' if din else 'USB'} [{build}]"


def forward(line):
    """The helper's `in HEX` line -> the `out HEX` line to send, or None for anything else."""
    line = line.strip()
    if not line.startswith("in ") or not line[3:].strip():
        return None
    return "out " + line[3:]


def shown(line):
    """True when a forwarded message is worth printing (not clock or active sensing)."""
    payload = line.strip()[3:]
    return bool(payload) and payload[:2] not in QUIET


def endpoints_present(listing, source, target):
    """Whether the helper's `list` output has the source and the target destination."""
    lines = listing.splitlines()
    has_source = any(l.startswith("source ") and source.lower() in l.lower() for l in lines)
    has_target = any(l == f"destination {target}" for l in lines)
    return has_source, has_target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--build", required=True, help="the running emulator build, e.g. pr-check")
    parser.add_argument("--source", default="TSP-16", help="part of the hardware MIDI source's name (default TSP-16)")
    parser.add_argument("--din", action="store_true", help="send to the emulator's DIN endpoint instead of USB")
    parser.add_argument("--quiet", action="store_true", help="do not print each message")
    parser.add_argument("--build-dir", type=Path, help="where the builds are (default build/)")
    args = parser.parse_args(argv)
    from midi_bridge.midi import helper_binary
    helper = str(helper_binary(args.build_dir))
    target = target_name(args.build, args.din)
    told = None
    while True:
        listing = subprocess.run([helper, "list"], capture_output=True, text=True).stdout
        has_source, has_target = endpoints_present(listing, args.source, target)
        if has_source and has_target:
            break
        missing = args.source if not has_source else target
        if missing != told:
            hint = "connect the device" if not has_source else f"start the emulator: ./sp16 run --name {args.build} --midi"
            print(f"thru: waiting for {missing!r} ({hint})", flush=True)
            told = missing
        time.sleep(1)
    proc = subprocess.Popen([helper, "run", "--in", args.source, "--out", target],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for line in proc.stdout:
            if line.strip() == "ready":
                print(f"thru: {args.source} -> {target} (Ctrl-C to stop)", flush=True)
                continue
            out = forward(line)
            if out:
                proc.stdin.write(out + "\n")
                proc.stdin.flush()
                if not args.quiet and shown(line):
                    print(line.strip()[3:], flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            try:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
                proc.wait(timeout=3)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
