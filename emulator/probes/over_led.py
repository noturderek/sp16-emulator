#!/usr/bin/env python3
"""Probe the OVER lamp (mono 34) against the master output level: does the firmware raise LED_CLIP at all?

HW-35 asserts that the OVER lamp follows the digital master peak. The lamp is mono channel 34 (service manual
`LED_CLIP`, an M1 output the app drives; 2026-10-24 findings), so the question a failing run leaves open is which of
three things is wrong: the lamp role, the check, or the level path. This probe answers it with measurements instead of
an assumption:

    python3 emulator/probes/over_led.py run --build NAME [--track 3] [--volumes 127,110,102,96]

For each track volume it plays a full-scale tone from a pad strike at velocity 127 and records, over the same window,
the master stream's peak (the sink WAV, so a real sample value) and every TX frame's mono lamp set. The last pass
plays the -30 dBFS fixture as the control that must stay dark. The report says per pass whether mono 34 ever lit and
which mono channels did, so a role error (the wrong channel), a level error (a peak that never reaches the threshold)
and a firmware that simply never raises its clip flag each look different.

The reference for the expected behaviour is the 2026-10-12 observation (E14, graded O): with a 0 dBFS tone on a track
the lamp rose 2 ms after the first loud sample, stayed lit while the tone played and fell about 0.5 s after it ended,
with the threshold near -0.9 dBFS (S16 peak about 29,500).
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

OVER_ROLE = "over_led"
OVER_CHANNEL = 34
NEAR_FULL_SCALE = 0.89  # the check's own threshold: peak at or above this of 32767 is -1 dBFS


def lit_channels(decoded):
    """Every mono channel lit in one TX frame."""
    return sorted(channel for channel, (lit, _) in decoded["mono"].items() if lit)


def window(runner, track, sample, seconds=3.0):
    """Strike `track`'s pad and report the window: TX records with their times, the master-left samples, the first
    sample index of the window and a frame->app-uptime clock."""
    session = runner.session
    runner.load_sample(track, sample)
    if not runner.select_track(track):
        raise RuntimeError(f"could not select track {track}")
    tap = runner.pcm()
    clock = []
    start = tap.frames()
    offset = session.tx_offset()
    session.press(f"pad{track}", 0.15, 127)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        metrics = session.metrics()
        stream = ((metrics.get("audio") or {}).get("streams") or {}).get(tap.stream) or {}
        clock.append((metrics["uptime_seconds"], stream.get("frames", 0)))
        time.sleep(.05)
    end = tap.frames()
    left = tap.read(start, end)[:, runner.sound_columns(tap)[0]]
    return {"records": session.lamps_since(offset), "left": left, "start": start,
            "to_time": runner.frame_clock(clock), "seconds": seconds}


def pass_figures(seen):
    """One pass's figures: the master peak, whether the OVER channel ever lit, when, and which mono channels did."""
    left = seen["left"]
    peak_value = int(max((abs(int(sample)) for sample in left), default=0))
    loud = [index for index, sample in enumerate(left) if abs(int(sample)) >= NEAR_FULL_SCALE * 32767]
    over = [time_point for time_point, decoded in seen["records"] if decoded["mono"][OVER_CHANNEL][0]]
    every = sorted({channel for _, decoded in seen["records"] for channel in lit_channels(decoded)})
    first_loud = None if not loud or not seen["to_time"] else seen["to_time"](seen["start"] + loud[0])
    return {"peak": peak_value, "window_samples": int(len(left)),
            "nonzero_samples": int(sum(1 for sample in left if int(sample) != 0)),
            "peak_dbfs": round(20 * math.log10(peak_value / 32767), 2) if peak_value else None,
            "near_full_scale_samples": len(loud),
            "over_lit": bool(over),
            "over_frames": len(over),
            "over_first_s_after_loud_sample": (None if first_loud is None or not over
                                               else round(over[0] - first_loud, 3)),
            "mono_channels_lit": every,
            "tx_frames": len(seen["records"])}


def run(args):
    import hwsuite
    import profiles
    build = Path(args.build if "/" in args.build else TOOLS.parent / "build" / args.build).resolve()
    profile_path = Path(args.profile) if args.profile else TOOLS / "profiles" / "sp16-1.60.json"
    profile = profiles.validate(json.loads(profile_path.read_bytes()))
    out = build / "over-led" / time.strftime("%Y%m%d-%H%M%S")
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
        session.txlog(True)  # every lamp state is read back from the TX log
        runner.normalize()
        volume = profile["recipes"]["volume_knob"]
        passes, level = {}, None
        for wanted in args.volumes:
            level = runner.set_knob(volume["knob"], wanted, volume["value_box"], volume["max"])
            seen = window(runner, args.track, "hw-tone-full.wav", args.seconds)
            figures = pass_figures(seen)
            figures.update(wanted=wanted, read_back=level)
            passes[f"full@{wanted}"] = figures
            print(f"volume {wanted} (read back {level}): peak={figures['peak']} ({figures['peak_dbfs']} dBFS) "
                  f"near-full samples={figures['near_full_scale_samples']} over_lit={figures['over_lit']} "
                  f"over_frames={figures['over_frames']} mono lit={figures['mono_channels_lit']}", flush=True)
            time.sleep(1.0)
        seen = window(runner, args.track, "hw-tone-m30.wav", args.seconds)
        control = pass_figures(seen)
        passes["m30"] = control
        print(f"-30 dBFS control: peak={control['peak']} over_lit={control['over_lit']} "
              f"mono lit={control['mono_channels_lit']}", flush=True)
        report = {"build": str(build), "track": args.track, "firmware": profile["identity"]["version"],
                  "launch_s": round(ready, 1), "over_channel": OVER_CHANNEL,
                  "over_role": profile["panel"]["lamp_roles"].get(OVER_ROLE),
                  "passes": passes}
        (out / "over-led.json").write_text(json.dumps(report, indent=1) + "\n")
        loud = [name for name, figures in passes.items() if name != "m30" and figures["near_full_scale_samples"]]
        lit = [name for name in loud if passes[name]["over_lit"]]
        if not loud:
            verdict = "no pass reached near-full-scale output: the level path never got loud enough to judge the lamp"
        elif lit:
            verdict = f"mono {OVER_CHANNEL} lit at {', '.join(lit)}: the lamp role and the level path agree"
        else:
            verdict = (f"the master reached near-full scale at {', '.join(loud)} and mono {OVER_CHANNEL} never lit: "
                       f"the firmware does not raise its clip flag here")
        print(f"\n{verdict}; wrote {out / 'over-led.json'}")
        return report
    finally:
        if session:
            session.close()
        if pid:
            hwsuite.stop(build, pid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="sweep the track volume and report the master peak against the OVER lamp")
    r.add_argument("--build", required=True, help="build name or folder of a research build with --audio file")
    r.add_argument("--profile", help="profile file (default: profiles/sp16-1.60.json)")
    r.add_argument("--track", type=int, default=3, help="track to play the tone on (default 3)")
    r.add_argument("--volumes", default="127,110,102,96",
                   help="track volumes to try, loudest first (default 127,110,102,96: E14 bracketed the trigger "
                        "between 100 and 102)")
    r.add_argument("--seconds", type=float, default=3.0, help="seconds to sample per pass (default 3)")
    args = parser.parse_args(argv)
    if not 1 <= args.track <= 16:
        parser.error("--track must be between 1 and 16")
    if not 0.5 <= args.seconds <= 30.0:
        parser.error("--seconds must be between 0.5 and 30")
    try:
        args.volumes = [int(value) for value in args.volumes.split(",") if value.strip()]
    except ValueError:
        parser.error("--volumes must be a comma-separated list of track volumes")
    if not args.volumes or any(not 0 <= value <= 127 for value in args.volumes):
        parser.error("--volumes: every value must be between 0 and 127")
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
