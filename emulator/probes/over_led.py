#!/usr/bin/env python3
"""Probe the OVER lamp (mono 34) against the master output level: does the firmware raise LED_CLIP at all?

HW-35 asserts that the OVER lamp follows the digital master peak. The lamp is mono channel 34 (service manual
`LED_CLIP`, an M1 output the app drives; 2026-10-24 findings), so the question a failing run leaves open is which of
three things is wrong: the lamp role, the check, or the level path. This probe answers it with measurements instead of
an assumption:

    python3 emulator/probes/over_led.py run --build NAME [--track 3] [--volumes 127,110,102,96]

For each track volume it plays a full-scale tone from a pad strike at velocity 127 and records, over the same window,
the master stream's peak (the sink WAV, so a real sample value) and every TX frame's mono lamp set. A discarded
warm-up strike runs first, because the first strike of a run is silently dropped. The last pass plays the -30 dBFS
fixture as the control that must stay dark, labelled by the level it actually measured. Each pass also records the shim's board-model evidence
(the MAIN OUT law's gain as both bounds, `main_out_gain_min` and `main_out_gain_max`, plus its own MAIN-pair output
peak) so the report can say where a level shortfall is: a neutral
`main_out_gain_min` of 1.0 equal to `main_out_gain_max` and a shim peak equal to the WAV peak mean the shim copied the
firmware's samples byte-for-byte
and the level is the firmware's own track-volume -> master path, not the emulator's MAIN OUT law, limiter, 16-channel
send or S24 conversion. The report says per pass whether mono 34 ever lit and which mono channels did, so a role error
(the wrong channel), a level error (a peak that never reaches the threshold) and a firmware that simply never raises
its clip flag each look different.

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
S16_FULL = 32768.0      # one full-scale S16 sample on the shim meter's linear scale; one LSB is one sample step
SHIM_TOLERANCE = 2.0    # S16 samples; see shim_matches for why the 4-decimal printing spans +/-1.64
NO_SHIM_EVIDENCE = "no shim evidence"


def lit_channels(decoded):
    """Every mono channel lit in one TX frame."""
    return sorted(channel for channel, (lit, _) in decoded["mono"].items() if lit)


def board_evidence(board, carried=None):
    """The shim's own view of the level path over one window, from the board model's `analog levels` metrics.

    `main_out_gain_min` and `main_out_gain_max` are the two bounds of the gain the MAIN OUT law applied over the
    window (1.0 = neutral, i.e. the control was never moved; both printed as one label by `gain_label`, and `n/a`
    when the window carried no gain reading); `shim_out_peak` is the peak the shim's output meter saw on the MAIN
    pair, over the same samples the sink WAV holds. A neutral pair of bounds and a `shim_out_peak` equal to the WAV
    peak mean the shim copied the firmware's samples byte-for-byte, so any level shortfall is upstream of the shim
    (the firmware's own track-volume -> master path), not in the emulator's board model. Bounds below 1.0, or an
    out_peak below the WAV peak, point at the shim.

    The shim's output meter is a peak-hold the bridge reports every ~2 s, so a window can open on the previous
    report's carried-over peak, not this window's. Readings equal to `carried` (the peak read just before the
    strike, or failing that the opening reading) are discarded, so the returned peak belongs to this window only;
    a window whose meter never reports a fresh value has no shim evidence.

    Both bounds of the gain are kept, because the gain is read repeatedly and one attenuated reading in the window
    is a real dip that `max` would hide. Readings with no gain (the block reports `None` between updates) are
    skipped, not turned into a value."""
    if carried is None:
        carried = board[0][1] if board else None
    fresh = [(gain, peak) for gain, peak in board if peak is not None and (carried is None or peak != carried)]
    gains = [gain for gain, _ in fresh if gain is not None]
    peaks = [peak for _, peak in fresh if peak is not None]
    return {"main_out_gain_min": min(gains) if gains else None,
            "main_out_gain_max": max(gains) if gains else None,
            "shim_out_peak": max(peaks) if peaks else None}


def window(runner, track, sample, seconds=3.0):
    """Strike `track`'s pad and report the window: TX records with their times, the master-left samples, the first
    sample index of the window, a frame->app-uptime clock, the shim's board-model evidence (MAIN OUT gain and the
    shim's own output peak) and the output-meter peak carried over from before the strike."""
    session = runner.session
    runner.load_sample(track, sample)
    if not runner.select_track(track):
        raise RuntimeError(f"could not select track {track}")
    tap = runner.pcm()
    clock = []
    board = []
    start = tap.frames()
    offset = session.tx_offset()
    carried = (((session.metrics().get("guest") or {}).get("board") or {}).get("analog_levels") or {}).get("out_peak")
    session.press(f"pad{track}", 0.15, 127)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        metrics = session.metrics()
        stream = ((metrics.get("audio") or {}).get("streams") or {}).get(tap.stream) or {}
        clock.append((metrics["uptime_seconds"], stream.get("frames", 0)))
        levels = ((metrics.get("guest") or {}).get("board") or {}).get("analog_levels") or {}
        board.append((levels.get("main_out_gain"), levels.get("out_peak")))
        time.sleep(.05)
    end = tap.frames()
    left = tap.read(start, end)[:, runner.sound_columns(tap)[0]]
    return {"records": session.lamps_since(offset), "left": left, "start": start,
            "to_time": runner.frame_clock(clock), "seconds": seconds, "board": board, "carried": carried}


def warm_up(runner, track, sample="hw-tone-full.wav", seconds=1.5):
    """Strike `track` once and throw the window away. The first strike of a run is deterministically silent (the voice
    is not audible until it has been allocated once), so measuring it would report a silent volume-127 pass and
    leave the sweep's brightest pass unjudged."""
    runner.load_sample(track, sample)
    if not runner.select_track(track):
        raise RuntimeError(f"could not select track {track}")
    runner.session.press(f"pad{track}", 0.15, 127)
    time.sleep(seconds)


def pass_figures(seen):
    """One pass's figures: the master peak, whether the OVER channel ever lit, when, which mono channels did, and
    the shim's board-model evidence (MAIN OUT gain and its own output peak) that says where any level shortfall is."""
    left = seen["left"]
    peak_value = int(max((abs(int(sample)) for sample in left), default=0))
    loud = [index for index, sample in enumerate(left) if abs(int(sample)) >= NEAR_FULL_SCALE * 32767]
    over = [time_point for time_point, decoded in seen["records"] if decoded["mono"][OVER_CHANNEL][0]]
    every = sorted({channel for _, decoded in seen["records"] for channel in lit_channels(decoded)})
    first_loud = None if not loud or not seen["to_time"] else seen["to_time"](seen["start"] + loud[0])
    evidence = board_evidence(seen.get("board", []), seen.get("carried"))
    return {"peak": peak_value, "window_samples": int(len(left)),
            "nonzero_samples": int(sum(1 for sample in left if int(sample) != 0)),
            "peak_dbfs": round(20 * math.log10(peak_value / 32767), 2) if peak_value else None,
            "near_full_scale_samples": len(loud),
            "over_lit": bool(over),
            "over_frames": len(over),
            "over_first_s_after_loud_sample": (None if first_loud is None or not over
                                               else round(over[0] - first_loud, 3)),
            "mono_channels_lit": every,
            "tx_frames": len(seen["records"]),
            "main_out_gain_min": evidence["main_out_gain_min"],
            "main_out_gain_max": evidence["main_out_gain_max"],
            "shim_out_peak": evidence["shim_out_peak"]}


def shim_matches(peak, wanted):
    """Whether the shim's output peak (linear 0..1) matches the WAV peak within the 4-decimal printing's own step.

    The shim prints `out_peak` to four decimal places, so the value the probe reads is quantized to 1e-4 of full
    scale, and on the S16 scale 1e-4 * 32768 = 3.2768 samples: a printed value spans +/-1.6384 samples around the
    true peak. SHIM_TOLERANCE is 2 samples, which covers that half-step plus the float/JSON round-trip noise, and
    is compared before any rounding so a real shortfall cannot be rounded away."""
    return abs(peak * S16_FULL - wanted) <= SHIM_TOLERANCE


def gain_label(low, high):
    """One MAIN OUT gain label for the report: `n/a` when the window carried no gain reading at all (nothing was
    measured, which is not a gain of None), one value when the two bounds agree, a range when the window dipped."""
    if low is None and high is None:
        return "MAIN OUT gain n/a"
    return f"MAIN OUT gain {low}" if low == high else f"MAIN OUT gain {low}..{high}"


def level_label(dbfs):
    """One measured level for the report: `-36.72 dBFS`, or `silent` for a window whose peak was 0. A silent window
    measured zero, so the label says so instead of printing `None`, which reads as a missing reading."""
    return "silent" if dbfs is None else f"{dbfs} dBFS"


def divergent_passes(passes):
    """Each pass whose shim evidence fails to prove the shim copied the firmware byte-for-byte, as a list of
    (pass name, why). A pass proves it when the MAIN OUT gain stayed neutral 1.0 across the whole window and the
    shim's output peak equals that pass's own WAV peak within the printing's +/-2 samples.

    The control pass (`control` in its figures) is gain-checked like every other pass - an attenuated control would
    weaken the sweep as much as an attenuated full-scale pass - but its peak match is not judged, because it plays
    the -30 dBFS fixture and is there to stay dark. A pass with no fresh meter reading is reported as
    `no shim evidence`, which is not a divergence: nothing was measured, so nothing diverged."""
    diverged = []
    for name, figures in passes.items():
        low = figures.get("main_out_gain_min")
        high = figures.get("main_out_gain_max")
        peak = figures.get("shim_out_peak")
        wanted = figures.get("peak")
        if low is None or high is None or peak is None:
            diverged.append((name, NO_SHIM_EVIDENCE))
        elif low != 1.0 or high != 1.0:
            diverged.append((name, gain_label(low, high)))
        elif not figures.get("control") and not shim_matches(peak, wanted):
            diverged.append((name, f"shim peak {peak} vs WAV peak {wanted}"))
    return diverged


def shim_verdict(passes):
    """The report's sentence about the shim's own evidence: what diverged, what had no meter reading at all, or
    that every pass proved the shim copied the firmware's samples."""
    judged = divergent_passes(passes)
    unproven = [name for name, why in judged if why == NO_SHIM_EVIDENCE]
    others = [(name, why) for name, why in judged if why != NO_SHIM_EVIDENCE]
    parts = []
    if others:
        parts.append("the shim diverged from a byte-for-byte copy on " +
                     ", ".join(f"{name} ({why})" for name, why in others))
    if unproven:
        parts.append("no shim evidence for " + ", ".join(unproven) +
                     " (no fresh output-meter reading in the window)")
    if parts:
        return "; ".join(parts)
    return ("the shim's MAIN OUT gain was neutral (1.0) and its output peak matched each pass's WAV peak within "
            f"+/-{SHIM_TOLERANCE:g} samples, so the measured level is the firmware's")


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
        warm_up(runner, args.track)
        print("warm-up strike (discarded): the first strike of a run is silent", flush=True)
        passes, level = {}, None
        for wanted in args.volumes:
            level = runner.set_knob(volume["knob"], wanted, volume["value_box"], volume["max"])
            seen = window(runner, args.track, "hw-tone-full.wav", args.seconds)
            figures = pass_figures(seen)
            figures.update(wanted=wanted, read_back=level)
            passes[f"full@{wanted}"] = figures
            print(f"volume {wanted} (read back {level}): peak={figures['peak']} ({level_label(figures['peak_dbfs'])}) "
                  f"near-full samples={figures['near_full_scale_samples']} over_lit={figures['over_lit']} "
                  f"over_frames={figures['over_frames']} mono lit={figures['mono_channels_lit']} "
                  f"main_out_gain={gain_label(figures['main_out_gain_min'], figures['main_out_gain_max'])} "
                  f"shim_out_peak={figures['shim_out_peak']}", flush=True)
            time.sleep(1.0)
        seen = window(runner, args.track, "hw-tone-m30.wav", args.seconds)
        control = pass_figures(seen)
        # the fixture is -30 dBFS on paper but the measured level is what the report must name
        measured = control["peak_dbfs"]
        control.update(wanted=None, read_back=level, control=True,
                       label=f"-30 dBFS fixture control (measured {level_label(measured)})")
        passes["control"] = control
        print(f"{control['label']}: peak={control['peak']} over_lit={control['over_lit']} "
              f"mono lit={control['mono_channels_lit']} "
              f"main_out_gain={gain_label(control['main_out_gain_min'], control['main_out_gain_max'])} "
              f"shim_out_peak={control['shim_out_peak']}", flush=True)
        report = {"build": str(build), "track": args.track, "firmware": profile["identity"]["version"],
                  "launch_s": round(ready, 1), "over_channel": OVER_CHANNEL,
                  "over_role": profile["panel"]["lamp_roles"].get(OVER_ROLE),
                  "passes": passes}
        (out / "over-led.json").write_text(json.dumps(report, indent=1) + "\n")
        loud = [name for name, figures in passes.items() if not figures.get("control") and
                figures["near_full_scale_samples"]]
        lit = [name for name in loud if passes[name]["over_lit"]]
        if not loud:
            verdict = "no pass reached near-full-scale output: the level path never got loud enough to judge the lamp"
        elif lit:
            verdict = f"mono {OVER_CHANNEL} lit at {', '.join(lit)}: the lamp role and the level path agree"
        else:
            verdict = (f"the master reached near-full scale at {', '.join(loud)} and mono {OVER_CHANNEL} never lit: "
                       f"the firmware does not raise its clip flag here")
        verdict += "; " + shim_verdict(passes)
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
