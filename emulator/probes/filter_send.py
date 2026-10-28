#!/usr/bin/env python3
"""Probe the analog-filter send in ordinary playback: does the send stream carry anything, with ACTIVE on and off?

The firmware builds a track send by copying the track's audio pair into its send pair, applying the insert FX or not
(Pre FX / Post FX) and passing it through the send mute, and the board's send DAC plays it on `hw:ak4452audio,1` into
the analog filter. So the first thing a silent send can be is "the send PCM never carried a sample", which is
measurable without reading firmware memory: the host sink records every playback stream, and the shim reports the
filter loop's own peaks.

    python3 emulator/probes/filter_send.py run --build NAME [--track 3] [--sample hw-tone440.wav]

It plays a known tone on a track, samples the send stream and the shim's loop counters while the pad sounds, then
presses ACTIVE and does it again, and prints what each pass saw. It says nothing about the firmware's send enum, its
mute mode or the selected object's byte 0x39: those are in the app's memory and need a debugger (see the report).

The send is assigned in the app's own UI (assign the analog filter to a track, choose Pre FX or Post FX, hold ACTIVE);
this tool only presses ACTIVE, so a recipe that needs the assignment first should pass `--setup-recipe`.
"""
import argparse
import json
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

SEND_STREAM = "hw:ak4452audio,1"
MASTER_STREAM = "hw:ak4458audio,0"


def stream_figures(metrics, name):
    """One playback stream's counters out of a metrics reply (None where the sink does not report it)."""
    key = name.replace("hw:", "").replace(",", "-")
    stream = ((metrics.get("audio") or {}).get("streams") or {}).get(key) or {}
    return {"peak": stream.get("peak"), "nonzero_frames": stream.get("nonzero_frames"), "frames": stream.get("frames"),
            "wav": stream.get("wav")}


def loop_figures(metrics):
    """The shim's analog filter loop counters (send and return peaks, underruns, drops)."""
    return ((metrics.get("guest") or {}).get("board") or {}).get("filter_loop") or {}


def measure(metrics):
    """One reading of every figure a pass watches, from a single metrics reply so they belong to the same moment."""
    send, master, loop = stream_figures(metrics, SEND_STREAM), stream_figures(metrics, MASTER_STREAM), loop_figures(metrics)
    return {"send_peak": float(send.get("peak") or 0.0), "nonzero_send_frames": int(send.get("nonzero_frames") or 0),
            "master_peak": float(master.get("peak") or 0.0), "loop_send_peak": float(loop.get("send_peak") or 0.0),
            "loop_return_peak": float(loop.get("return_peak") or 0.0),
            "loop_underruns": int(loop.get("underruns") or 0)}


def sample(session, seconds=2.5):
    """The highest send and loop figures seen while the pad sounds, and the last master figures."""
    best = measure({})
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        reading = measure(session.metrics())
        for key, value in reading.items():
            best[key] = max(best[key], value)
        time.sleep(0.2)
    return best


def active(session, runner):
    """Whether the ACTIVE lamp is lit now (the filter's select line follows it)."""
    return bool(runner.full(runner.now(), "active"))


def run(args):
    import hwsuite
    import profiles
    build = Path(args.build if "/" in args.build else TOOLS.parent / "build" / args.build).resolve()
    profile_path = Path(args.profile) if args.profile else TOOLS / "profiles" / "sp16-1.60.json"
    profile = profiles.validate(json.loads(profile_path.read_bytes()))
    out = build / "filter-send" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    fixtures = out / "fixtures"
    sample_dir = (profile.get("usb") or {}).get("sample_dir") or "PIONEER DJ SAMPLER/Samples"
    hwsuite.make_fixtures(fixtures, sample_dir)
    stick = out / "probe-stick.img"
    payload, total = hwsuite.stick_image(hwsuite.fixture_wavs(), sample_dir)
    hwsuite.write_sparse(stick, payload, total)
    pid, runtime, ready = hwsuite.launch(build, None, stick, ephemeral=True)
    session = None
    try:
        runner = hwsuite.Runner(profile, profile_path, build, None, out, fixtures, "smoke", None, None, runtime)
        runner.session = hwsuite.Session(runtime)
        session = runner.session
        session.txlog(True)  # the ACTIVE lamp is read back from the TX log
        if args.setup_recipe:
            runner.recipe(args.setup_recipe)
        runner.normalize()
        runner.load_sample(args.track, args.sample)
        runner.select_track(args.track)
        volume = profile["recipes"]["volume_knob"]
        level = runner.set_knob("knob1", volume["max"], volume["value_box"], volume["max"])
        runner.select_track(1)
        passes = []
        for wanted in (False, True):
            if active(session, runner) != wanted:
                session.press("active")
                time.sleep(1.0)
            session.press(f"pad{args.track}", 0.2, 127)
            seen = sample(session, args.seconds)
            seen.update(active=active(session, runner), volume=level)
            passes.append(seen)
            print(f"ACTIVE {wanted}: send_peak={seen['send_peak']} nonzero_send_frames={seen['nonzero_send_frames']} "
                  f"loop_send_peak={seen['loop_send_peak']} loop_return_peak={seen['loop_return_peak']} "
                  f"master_peak={seen['master_peak']}", flush=True)
            time.sleep(1.0)
        report = {"build": str(build), "track": args.track, "sample": args.sample, "volume": level,
                  "firmware": profile["identity"]["version"], "launch_s": round(ready, 1),
                  "streams": {"send": SEND_STREAM, "master": MASTER_STREAM},
                  "passes": {"inactive": passes[0], "active": passes[1]}}
        (out / "filter-send.json").write_text(json.dumps(report, indent=1) + "\n")
        silent = all(p["send_peak"] == 0 and p["nonzero_send_frames"] == 0 for p in passes)
        print(f"\nsend stream {'is silent' if silent else 'carried samples'} with ACTIVE on and off; "
              f"wrote {out / 'filter-send.json'}")
        return report
    finally:
        if session:
            session.close()
        if pid:
            hwsuite.stop(build, pid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="play a tone with ACTIVE off and on and report the send stream and filter loop")
    r.add_argument("--build", required=True, help="build name or folder of a research build with --audio file")
    r.add_argument("--profile", help="profile file (default: profiles/sp16-1.60.json)")
    r.add_argument("--track", type=int, default=3, help="track to play the tone on (default 3)")
    r.add_argument("--sample", default="hw-tone440.wav", help="fixture sample name (default hw-tone440.wav)")
    r.add_argument("--seconds", type=float, default=2.5, help="seconds to sample per pass (default 2.5)")
    r.add_argument("--setup-recipe", help="run this profile recipe before playing (needed when the send has to be "
                                          "assigned to the track first)")
    args = parser.parse_args(argv)
    if not 1 <= args.track <= 16:
        parser.error("--track must be between 1 and 16")
    if not 0.5 <= args.seconds <= 30.0:
        parser.error("--seconds must be between 0.5 and 30")
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
