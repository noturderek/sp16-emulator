#!/usr/bin/env python3
"""Timing probe for a running research session: step jitter from the audio WAV and panel-frame (TX) stalls.

    python3 emulator/timing_probe.py run --build NAME [--seconds 60]   # set up, play, measure, report
    python3 emulator/timing_probe.py wav FILE [--bpm 120]              # onset jitter of a recorded WAV
    python3 emulator/timing_probe.py tx FILE_OR_SESSION_DIR            # TX period and stalls from tx-frames.jsonl

`run` puts the hardware suite's click sample on every one of the 16 steps of a track, plays the pattern and takes two
measurements over the same window:

* Audio: a click lands on every step, so the onsets in the recorded WAV (sample accurate, the first sample above a
  threshold) give the step interval and its jitter in samples and milliseconds, and a straight-line fit of onset
  against step index gives the tempo and the residual jitter.
* TX stalls: the intervals between the firmware's panel (lamp) frames as the host received them. A stall is a gap
  above the limit (40 ms by default); the maximum is the number to watch.

The audio clock and the panel link are independent paths: the jitter is the emulated sampler's own timing, the stalls
are what the host sees of the panel. Both depend on host load, so the report carries the load average and the number
of VMs running; compare runs taken under similar load. Targets: onset sigma below 1 ms, TX stall maximum below 40 ms,
over 60 s.

A run on a host that cannot keep up is not a measurement of the emulator: when the host stops draining the audio port
for longer than the guest's ring holds (about 0.7 s), the guest drops frames and the recorded WAV has holes, which look
like missing or early clicks. The report therefore carries the frames the guest dropped during the window
(`gap_frames_in_window`, `guest_drops_in_window`) and a robust jitter figure that ignores a few outliers, and
`targets_met.audio_window_clean` is false when any frame was lost.
"""
import argparse
import json
import math
from pathlib import Path
import statistics
import struct
import sys
import time

TOOLS = Path(__file__).resolve().parent
RATE = 44100
STALL_MS = 40.0


# --------------------------------------------------------------------------------------------------- measurements

def find_onsets(samples, threshold=None, refractory=None):
    """Sample indexes where |x| first exceeds the threshold after a quiet stretch (default: 30 % of the peak, 60 % of
    the nominal step as the quiet stretch)."""
    import numpy as np
    x = np.abs(np.asarray(samples, dtype=float))
    if not x.size:
        return []
    threshold = float(threshold if threshold is not None else 0.3 * x.max())
    refractory = int(refractory if refractory is not None else 0.1 * RATE)
    above = np.nonzero(x > threshold)[0]
    out, last = [], None
    for i in above:
        if last is None or i - last > refractory:
            out.append(int(i))
        last = int(i)
    return out


PREROLL = int(0.3 * RATE)  # read this much before the window so a click that is already sounding is seen whole


def onsets_in_window(samples, preroll, refractory):
    """Onsets inside a window that starts `preroll` samples into `samples` (the rest is look-back). A click that began before
    the window is found at its true start, in the look-back, and dropped; without the look-back it would show up as a late
    onset at the window's first sample (the fixture click rings for about 80 ms)."""
    return [o - preroll for o in find_onsets(samples, refractory=refractory) if o >= preroll]


def robust_sigma(values):
    """Standard deviation estimated from the median absolute deviation: a few outliers (a dropout) do not move it."""
    centre = statistics.median(values)
    return 1.4826 * statistics.median(abs(v - centre) for v in values)


def onset_jitter(onsets, step_samples):
    """Jitter report for onsets that should be one step apart: interval statistics, and the residual of a straight-line
    fit of onset against step number (which absorbs a constant tempo error)."""
    if len(onsets) < 4:
        return {"onsets": len(onsets), "error": "too few onsets"}
    gaps = [b - a for a, b in zip(onsets, onsets[1:])]
    # step numbers: a missing click shows as a gap of about two steps, so round the gap to whole steps
    numbers, n = [0], 0
    for gap in gaps:
        n += max(1, round(gap / step_samples))
        numbers.append(n)
    mean_x, mean_y = statistics.fmean(numbers), statistics.fmean(onsets)
    sxx = sum((x - mean_x) ** 2 for x in numbers)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(numbers, onsets)) / sxx
    intercept = mean_y - slope * mean_x
    residual = [y - (slope * x + intercept) for x, y in zip(numbers, onsets)]
    single = [g for g in gaps if abs(g - step_samples) < 0.5 * step_samples]
    return {
        "onsets": len(onsets), "missing_steps": numbers[-1] + 1 - len(onsets),
        "step_samples_nominal": round(step_samples, 2), "step_samples_fitted": round(slope, 3),
        "tempo_bpm": round(60 * RATE / (4 * slope), 3),
        "interval_sigma_samples": round(statistics.pstdev(single), 2) if len(single) > 1 else None,
        "interval_sigma_ms": round(statistics.pstdev(single) / RATE * 1000, 3) if len(single) > 1 else None,
        "interval_min_ms": round(min(gaps) / RATE * 1000, 2), "interval_max_ms": round(max(gaps) / RATE * 1000, 2),
        "jitter_sigma_samples": round(statistics.pstdev(residual), 2),
        "jitter_sigma_ms": round(statistics.pstdev(residual) / RATE * 1000, 3),
        "jitter_sigma_robust_ms": round(robust_sigma(residual) / RATE * 1000, 3),
        "jitter_max_samples": round(max(abs(r) for r in residual), 1),
        "jitter_max_ms": round(max(abs(r) for r in residual) / RATE * 1000, 3),
        "onsets_off_by_over_1ms": sum(1 for r in residual if abs(r) > 0.001 * RATE),
        "short_intervals": sum(1 for g in gaps if g < 0.9 * step_samples),
        "long_intervals": sum(1 for g in gaps if g > 1.1 * step_samples),
    }


def tx_stalls(times, stall_ms=STALL_MS):
    """Period statistics of panel frames from their arrival times (seconds)."""
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    if len(gaps) < 2:
        return {"frames": len(times), "error": "too few frames"}
    ordered = sorted(gaps)
    return {
        "frames": len(times), "period_mean_ms": round(statistics.fmean(gaps), 2),
        "period_sigma_ms": round(statistics.pstdev(gaps), 2), "period_p99_ms": round(ordered[int(0.99 * (len(ordered) - 1))], 2),
        "stall_max_ms": round(max(gaps), 1), "stalls_over_limit": sum(1 for g in gaps if g > stall_ms), "limit_ms": stall_ms,
        "span_s": round(times[-1] - times[0], 1)}


def read_wav_channel(path, first=0, last=None, channel=0):
    """One channel of a recorded WAV (S16 or float32) between two frame indexes, as floats in -1..1."""
    import numpy as np
    with open(path, "rb") as handle:
        head = handle.read(4096)
        offset = head.find(b"data") + 8
        channels, = struct.unpack_from("<H", head, 22)
        tag, = struct.unpack_from("<H", head, 20)
        bits, = struct.unpack_from("<H", head, 34)
        width = bits // 8
        frame = channels * width
        handle.seek(offset + first * frame)
        raw = handle.read(-1 if last is None else (last - first) * frame)
    raw = raw[:len(raw) // frame * frame]
    if tag == 3 or (tag == 0xFFFE and bits == 32):
        data = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)
    else:
        data = np.frombuffer(raw, dtype="<i2" if width == 2 else "<i4").reshape(-1, channels) / float(1 << (bits - 1))
    return data[:, channel]


def read_tx_times(path, offset=0):
    """Arrival times of the lamp frames in tx-frames.jsonl, from a byte offset."""
    path = Path(path)
    if path.is_dir():
        path = path / "tx-frames.jsonl"
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    data = data[:data.rfind(b"\n") + 1]
    times = []
    for line in data.splitlines():
        try:
            times.append(float(json.loads(line)["t"]))
        except (ValueError, KeyError, TypeError):
            continue
    return times


# --------------------------------------------------------------------------------------------------- the live run

def host_load():
    import os
    import subprocess
    listing = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True).stdout
    return {"load_average": [round(x, 1) for x in os.getloadavg()],
            "qemu_processes": sum(1 for line in listing.splitlines() if "qemu-system" in line.split(" ")[0])}


def audio_counters(session):
    """Frames recorded and frames the guest dropped on the master stream so far (metrics of the host sink)."""
    stream = ((session.metrics().get("audio") or {}).get("streams") or {}).get("ak4458audio-0") or {}
    return {k: int(stream.get(k, 0)) for k in ("frames", "gap_frames", "guest_drops")}


def bridge_timing(session):
    """The bridge's own link counters from a fresh guest report (None for a build whose bridge does not report them)."""
    first = (session.metrics().get("guest") or {}).get("rx_frames")
    for _ in range(40):  # reports arrive about once a second
        guest = session.metrics().get("guest") or {}
        if guest.get("rx_frames") != first or first is None:
            break
        time.sleep(.1)
    return (guest.get("board") or {}).get("bridge_timing")


def run(args):
    sys.path.insert(0, str(TOOLS))
    import hwsuite
    import profiles
    build = Path(args.build if "/" in args.build else TOOLS.parent / "build" / args.build).resolve()
    profile_path = Path(args.profile) if args.profile else TOOLS / "profiles" / "sp16-1.60.json"
    profile = profiles.validate(json.loads(profile_path.read_bytes()))
    out = build / "timing-probe" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    fixtures = out / "fixtures"
    hwsuite.make_fixtures(fixtures, (profile.get("usb") or {}).get("sample_dir") or "PIONEER DJ SAMPLER/Samples")
    pid = None
    if args.runtime:
        runtime = Path(args.runtime)  # a session whose stick holds the suite's hw-click.wav
    else:  # a fresh ephemeral session with the suite's fixture stick; stopped by its exact process id at the end
        stick = out / "probe-stick.img"
        payload, total = hwsuite.stick_image(hwsuite.fixture_wavs(), (profile.get("usb") or {}).get("sample_dir")
                                             or "PIONEER DJ SAMPLER/Samples")
        hwsuite.write_sparse(stick, payload, total)
        pid, runtime, _ = hwsuite.launch(build, None, stick, ephemeral=True)
    session = None
    try:
        runner = hwsuite.Runner(profile, profile_path, build, None, out, fixtures, "smoke", None, None, runtime)
        runner.session = hwsuite.Session(runtime)
        session = runner.session
        session.txlog(True)
        bpm = profile["audio"]["default_bpm"]
        step = 60 / bpm / 4 * RATE
        if not args.no_setup:
            runner.normalize()
            runner.load_sample(2, "hw-click.wav")
            runner.select_track(2)
            runner.set_knob("knob1", 100, profile["recipes"]["volume_knob"]["value_box"], 127)
            for n in range(1, 17):
                session.press(f"step{n}")
                time.sleep(.3)
            runner.select_track(1)
        stream = session.metrics()["audio"]["streams"]["ak4458audio-0"]
        wav, first_channel = Path(stream["wav"]), 0
        session.press("play")
        time.sleep(args.lead)
        counters_start = audio_counters(session)
        frames_start = counters_start["frames"]
        tx_start = session.tx_offset()
        load_before = host_load()
        link_before = bridge_timing(session)
        time.sleep(args.seconds)
        counters_end = audio_counters(session)
        frames_end = counters_end["frames"]
        load_after = host_load()
        link_after = bridge_timing(session)
        session.press("stop")
        time.sleep(.5)
        first = max(0, frames_start - PREROLL)
        samples = read_wav_channel(wav, first, frames_end, first_channel)
        report = {
            "build": str(build), "seconds": args.seconds, "bpm": bpm, "host": {"before": load_before, "after": load_after},
            "audio": {**onset_jitter(onsets_in_window(samples, frames_start - first, int(0.5 * step)), step),
                      "window_s": round((frames_end - frames_start) / RATE, 1),
                      "gap_frames_in_window": counters_end["gap_frames"] - counters_start["gap_frames"],
                      "guest_drops_in_window": counters_end["guest_drops"] - counters_start["guest_drops"]},
            "tx": tx_stalls(read_tx_times(session.dir, tx_start), args.stall_ms),
            "bridge_timing": {"before": link_before, "after": link_after}}
        if not args.no_setup:
            runner.select_track(2)
            for n in range(1, 17):
                session.press(f"step{n}")
                time.sleep(.3)
            runner.normalize()
        return report
    finally:
        if session:
            session.close()
        if pid:
            hwsuite.stop(build, pid)


def below(value, limit):
    """True when value is a number under limit. Zero is a measurement (a perfect clock, no stall); None, which is what a
    report without that figure holds, is not."""
    return value is not None and value < limit


def verdict(report):
    audio, tx = report.get("audio", {}), report.get("tx", {})
    return {"onset_sigma_below_1ms": below(audio.get("jitter_sigma_ms"), 1.0),
            "tx_stall_max_below_40ms": below(tx.get("stall_max_ms"), 40.0),
            "audio_window_clean": audio.get("gap_frames_in_window") == 0 and audio.get("guest_drops_in_window") == 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="set up a click on every step, play, measure")
    r.add_argument("--build", required=True, help="build name or folder of the research build")
    r.add_argument("--runtime", help="use this running session's runtime.json (its stick must hold the suite's hw-click.wav) "
                                     "instead of launching a fresh ephemeral one")
    r.add_argument("--profile", help="profile file (default: profiles/sp16-1.60.json)")
    r.add_argument("--seconds", type=float, default=60.0, help="measurement window (default 60)")
    r.add_argument("--lead", type=float, default=8.0, help="seconds of PLAY before the window opens (default 8)")
    r.add_argument("--stall-ms", type=float, default=STALL_MS)
    r.add_argument("--no-setup", action="store_true", help="the click pattern is already on track 1")
    w = sub.add_parser("wav", help="onset jitter of a WAV")
    w.add_argument("file")
    w.add_argument("--bpm", type=float, default=120.0)
    w.add_argument("--channel", type=int, default=0)
    w.add_argument("--first", type=int, default=0, help="first frame")
    w.add_argument("--last", type=int, default=None, help="end frame")
    t = sub.add_parser("tx", help="TX period and stalls from tx-frames.jsonl")
    t.add_argument("file")
    t.add_argument("--offset", type=int, default=0, help="byte offset to start from")
    t.add_argument("--stall-ms", type=float, default=STALL_MS)
    args = parser.parse_args(argv)
    if args.command == "wav":
        report = onset_jitter(find_onsets(read_wav_channel(args.file, args.first, args.last, args.channel),
                                          refractory=int(0.5 * 60 / args.bpm / 4 * RATE)), 60 / args.bpm / 4 * RATE)
    elif args.command == "tx":
        report = tx_stalls(read_tx_times(args.file, args.offset), args.stall_ms)
    else:
        report = run(args)
        report["targets_met"] = verdict(report)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
