#!/usr/bin/env python3
"""Measure which of the sixteen AK4458 transport words carry audio, per AUDIO TEST mode.

The master stream `hw:ak4458audio,0` is sixteen S16_LE channels per frame, so the only way to see all sixteen at once
is the shim's research PCM tap (`--trace pcmtap`, which prints each finished capture to the session console). This
tool drives the factory test's AUDIO TEST page (page 11), selects a mode, waits for a capture and prints one row per
transport word:

    python3 emulator/probes/ak4458_transport.py run --build NAME      # needs --research --app-arg testmode --trace pcmtap
    python3 emulator/probes/ak4458_transport.py show FILE             # re-print a saved run's table

A word carries the stimulus when its peak is at least `--floor` (default 0.01, -40 dBFS) *and* at least `--purity`
(default 0.5) of its energy lies within `--band` Hz of the stimulus frequency (`--tone`, default 1 kHz). Measured on
the captures this tool takes, a word carrying the tone scores 0.90 and a word full of full-scale noise scores 0.005,
so the test separates them by nearly two hundred times: a padded word can have the same peak without the same
spectrum. The
table also prints each word's dominant frequency. The untouched page is captured first as the silence baseline, and
the tap only arms on a stream's first non-zero frame, so a mode that leaves the master stream silent is reported as
such instead of as a missing measurement.

A sixteen-word capture is about 5.6 MB raw and reaches the host as gzip+base64 lines on the serial console, where the
kernel cuts long writes and other writers' lines land in between, so a whole capture often arrives damaged. The reader
here takes the longest undamaged prefix of each capture and analyses the frames it holds; every row of the report
says how many of the capture's declared frames were usable. (`pcm-tap-analyze.py` is the strict offline reader for a
quiet console; this one is deliberately tolerant because the console it reads is shared.)

Nothing here says which codec pin or jack a word reaches. Three separate things decide that: the app decides which
words it writes, the codec's TDM-to-DAC assignment decides which channel word leaves which pin, and the board decides
which jack that pin feeds. Only the first is measured here.
"""
import argparse
import base64
import importlib.util
import json
import math
import re
import sys
import time
import zlib
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

RATE = 44100
STREAM = "ak4458audio-0"
WORDS = 16
HEADER = 64
AUDIO_TEST_PAGE = 11
# The AUDIO TEST page's mode cells, in LCD pixels (the points audio-jack-map.json records).
MODE_BUTTONS = {"MODE1": (272, 145), "MODE2": (400, 145), "MODE3": (528, 145), "LIMIT": (656, 145)}
MODES = ("none", "MODE1", "MODE2", "MODE3")
INPUT_TONE_SLOT = 700  # the shim's input tone: 0..1000 over -60..0 dBFS, so about -18 dBFS, under the limiter
TAIL_BYTES = 4_000_000  # the console tail a capture is read from (a 4 s sixteen-word capture is about 750 kB of text)
DUMP = re.compile(r"SP16_PCM_DUMP (\S+) (begin|end|\d+)(?: (.*))?$")
BASE64 = re.compile(r"[A-Za-z0-9+/=]+")
PIECE_CHARS = 2000  # the guest prints a capture 2000 base64 characters at a time
PIECE_BYTES = PIECE_CHARS // 4 * 3


def tap_module():
    """emulator/pcm-tap-analyze.py under a name that can be imported (the file name has a hyphen)."""
    path = TOOLS / "pcm-tap-analyze.py"
    spec = importlib.util.spec_from_file_location("sp16_pcm_tap_analyze", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------------------------- capturing

def base64_piece(text):
    """One console piece as bytes; ValueError when the line damaged it.

    Another writer's line can be glued onto the end with no separator, and its characters are base64 ones often enough
    (`SP16_DATA` is), so a piece is cut back to the length the guest prints before it is decoded."""
    text = text[:PIECE_CHARS]
    text = text[:len(text) - len(text) % 4]
    try:
        return base64.b64decode(text, validate=True)
    except Exception as error:  # binascii.Error is not always a ValueError subclass
        raise ValueError(str(error))


def inflate(data):
    """As much of a zlib stream as it really holds, fed a piece at a time so damage late in it costs nothing before."""
    engine, out = zlib.decompressobj(), b""
    for start in range(0, len(data), PIECE_BYTES):
        try:
            out += engine.decompress(data[start:start + PIECE_BYTES])
        except zlib.error:
            break
    return out


def captures(text):
    """Every capture the console text carries, as {name: raw}.

    A capture is the 64-byte header followed by a zlib stream, base64d 2000 characters at a time. Pieces are decoded
    and inflated one at a time and only the longest undamaged prefix of each capture is used, so a missing or
    corrupted piece costs the frames after it and nothing before it."""
    pieces, out = {}, {}
    for line in text.splitlines():
        match = DUMP.search(line)
        if not match:
            continue
        name, tag, rest = match.group(1), match.group(2), match.group(3) or ""
        if tag == "begin":
            pieces[name] = {}
        elif tag == "end":
            good, body, index = pieces.pop(name, {}), b"", 0
            while index in good and good[index] is not None:
                body += good[index]
                index += 1
            if len(body) >= HEADER:
                try:
                    out[name] = body[:HEADER] + inflate(body[HEADER:])
                except zlib.error:
                    continue
        elif name in pieces and tag.isdigit():
            token = BASE64.search(rest)
            if token:
                try:
                    pieces[name][int(tag)] = base64_piece(token.group(0))
                except ValueError:
                    pieces[name][int(tag)] = None
    return out


def console_tail(path, size=TAIL_BYTES):
    """The last `size` bytes of a console log as text, starting at a line boundary."""
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        start = max(0, end - size)
        handle.seek(start)
        data = handle.read()
        if start:  # the cut landed mid-line unless the byte before it is a newline
            handle.seek(start - 1)
            if handle.read(1) != b"\n":
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else b""
    return data.decode("utf-8", "replace")


def latest_serial(session):
    """The highest capture serial the console currently carries for the master stream (0 when it carries none)."""
    serials = []
    for name in captures(console_tail(session.dir / "console.txt")):
        prefix, _, serial = name.rpartition(".")
        if prefix == STREAM and serial.isdigit():
            serials.append(int(serial))
    return max(serials, default=0)


def newest_capture(session, newer_than, timeout, allow_none=False):
    """The newest capture of the master stream whose serial is above `newer_than`, as (raw, name, serial).

    The guest prints every finished capture again, so the serial only ever grows; the console is read from its tail
    because a session that keeps capturing grows it without bound. A capture whose damaged pieces cut it short is
    still used — the reader reports how much of it survived.

    The tap arms on the stream's first non-zero frame, so a master stream that carries nothing produces no capture at
    all. With `allow_none` that is reported as a silent stream rather than as a failure."""
    deadline = time.monotonic() + timeout
    while True:
        found = captures(console_tail(session.dir / "console.txt"))
        serials = {}
        for name in found:
            prefix, _, serial = name.rpartition(".")
            if prefix == STREAM and serial.isdigit():
                serials[name] = int(serial)
        if serials:
            name = max(serials, key=serials.get)
            if serials[name] > newer_than:
                return found[name], name, serials[name]
        if time.monotonic() >= deadline:
            if allow_none:
                return None, None, newer_than
            raise SystemExit(f"no capture of {STREAM} newer than #{newer_than} within {timeout:.0f} s: the tap never "
                             "armed, so the stream stayed all-zero (or this is not a --trace pcmtap build)")
        time.sleep(3.0)


# ----------------------------------------------------------------------------------------------- measuring

def word_samples(raw):
    """The capture as sixteen per-word sample lists in -1..1, its header, and the frames that survived."""
    tap = tap_module()
    header = tap.parse(raw)
    spec = tap.FORMATS.get(header["format"])
    if not spec:
        raise ValueError(f"unsupported ALSA format {header['format']}")
    code, width, scale, label = spec
    if header["channels"] != WORDS:
        raise ValueError(f"{header['channels']} channels per frame, expected {WORDS}")
    frames = min(header["captured"], (len(raw) - HEADER) // header["bpf"])
    if frames < 2:
        raise ValueError(f"only {frames} usable frame(s) of {header['captured']} in the capture")
    samples = tap.decode(code, label, raw[HEADER:HEADER + frames * WORDS * width], frames * WORDS)
    return [[v / scale for v in samples[c::WORDS]] for c in range(WORDS)], header, frames


def band_share(column, tone_hz, band_hz=150.0, rate=RATE):
    """(dominant frequency, share of the word's energy within `band_hz` of `tone_hz`) for one word.

    A tone gives nearly 1 in the default 300 Hz window and a noise-filled or padded word about 300 / 22050 = 0.014, so
    the two are more than two orders of magnitude apart. The frequency is the interpolated FFT peak, which says which signal the word carries.
    Silence returns (0.0, 0.0), never a perfect match."""
    import numpy as np
    x = np.asarray(column, dtype=float)
    if not x.size or not x.any():
        return 0.0, 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    total = float(spec.sum())
    if not total:
        return 0.0, 0.0
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    peak = int(np.argmax(spec))
    offset = 0.0
    if 0 < peak < len(spec) - 1:  # parabolic interpolation of the peak between bins
        a, b, c = (np.log(spec[peak - 1] + 1e-30), np.log(spec[peak] + 1e-30), np.log(spec[peak + 1] + 1e-30))
        if a - 2 * b + c:
            offset = 0.5 * (a - c) / (a - 2 * b + c)
    dominant = float(freqs[peak] + offset * (freqs[1] - freqs[0]))
    inside = float(spec[np.abs(freqs - tone_hz) <= band_hz].sum())
    return round(dominant, 2), round(inside / total, 4)


def silent_capture():
    """A stream the tap never armed on: every word at zero, which is what an unarmed tap proves."""
    return {"frames": 0, "declared_frames": 0, "rate": RATE, "format": None, "first_frame": 0, "stream_silent": True,
            "words": [{"word": i, "dac_slot": i // 2, "slot_half": "LR"[i % 2], "peak": 0.0, "rms": 0.0,
                       "dominant_hz": 0.0, "tone_share": 0.0} for i in range(WORDS)]}


def analyse_capture(raw, tone_hz=1000.0, band_hz=150.0):
    """Per-word peak, RMS, dominant frequency and tone share of one capture."""
    words, header, frames = word_samples(raw)
    rows = []
    for index, column in enumerate(words):
        peak = max((abs(v) for v in column), default=0.0)
        mean_square = sum(v * v for v in column) / len(column)
        dominant, share = band_share(column, tone_hz, band_hz, header["rate"])
        rows.append({"word": index, "dac_slot": index // 2, "slot_half": "LR"[index % 2],
                     "peak": round(peak, 6), "rms": round(math.sqrt(mean_square), 6),
                     "dominant_hz": dominant, "tone_share": share})
    return {"frames": frames, "declared_frames": header["captured"], "rate": header["rate"],
            "format": header["format"], "first_frame": header["first"], "stream_silent": False, "words": rows}


def classify(analysis, floor=0.01, purity=0.5):
    """Words that carry the stimulus: at or above the level floor and mostly at the stimulus frequency."""
    return [row["word"] for row in analysis["words"] if row["peak"] >= floor and row["tone_share"] >= purity]


def table(runs, floor=0.01, purity=0.5):
    """The word table over several modes: one row per transport word, then one row per pair of words."""
    modes = [r["mode"] for r in runs]
    rows = []
    for index in range(WORDS):
        row = {"word": index, "dac_slot": index // 2, "slot_half": "LR"[index % 2], "levels": {}, "shares": {}}
        for run in runs:
            entry = run["analysis"]["words"][index]
            row["levels"][run["mode"]] = entry["peak"]
            row["shares"][run["mode"]] = entry["tone_share"]
        baseline = row["levels"].get("none")
        # A mode drives a word when the word carries the stimulus in that mode and the mode lifted it clear of the
        # untouched page (four times the baseline level, so a word an earlier mode left ringing does not count).
        row["driven_by"] = [m for m in modes if m != "none"
                            and row["levels"][m] >= floor and row["shares"][m] >= purity
                            and (not baseline or row["levels"][m] > 4 * baseline)]
        rows.append(row)
    pairs = []
    for slot in range(WORDS // 2):
        left, right = rows[2 * slot], rows[2 * slot + 1]
        pairs.append({"dac_slot": slot, "words": [2 * slot, 2 * slot + 1],
                      "driven_by": sorted(set(left["driven_by"]) | set(right["driven_by"])),
                      "left_only": bool(left["driven_by"]) and not right["driven_by"],
                      "both_halves": bool(left["driven_by"]) and bool(right["driven_by"])})
    return {"modes": modes, "floor": floor, "purity": purity, "words": rows, "pairs": pairs}


def render(result):
    """The table as plain lines."""
    modes = result["modes"]
    lines = ["word  DAC slot  half  " + "  ".join(m.rjust(8) for m in modes) + "   driven by"]
    for row in result["words"]:
        cells = "  ".join(f"{row['levels'].get(m, 0.0):8.4f}" for m in modes)
        lines.append(f"{row['word']:4d}  {row['dac_slot']:8d}  {row['slot_half']:>4}  {cells}   "
                     + (",".join(row["driven_by"]) or "-"))
    lines.append("")
    lines.append("DAC slot  words  driven by            left half only  both halves")
    for entry in result["pairs"]:
        lines.append(f"{entry['dac_slot']:8d}  {str(entry['words']):5}  {(','.join(entry['driven_by']) or '-'):19} "
                     f"{str(entry['left_only']):15}  {entry['both_halves']}")
    lines.append("")
    for run in result.get("runs", []):
        analysis = run["analysis"]
        lines.append(f"{run['mode']:<6} capture={(run.get('capture') or '-'):<20} "
                     f"frames={analysis['frames']}/{analysis['declared_frames']} "
                     f"main_pair_peak={run.get('main_pair_peak')} input_peak={run.get('input_peak')}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------- the live run

def on_audio_test_page(runner):
    """Whether the session is showing the AUDIO TEST page now.

    `Runner.testmode_page_is` returns a verdict dict (hwsuite.sub), which is truthy whether it passed or not, so the
    guard has to read the verdict's status - the test hwsuite's own callers make - or it never fires and the probe taps
    mode coordinates and labels captures on a page that never showed."""
    return runner.testmode_page_is(AUDIO_TEST_PAGE, "AUDIO TEST")["status"] == "PASS"


def set_mode(session, runner, mode):
    """Settle on the AUDIO TEST page and select a mode; `none` leaves the page untouched, which is its OFF state."""
    if not on_audio_test_page(runner):
        raise SystemExit(f"the session is not on test page {AUDIO_TEST_PAGE}")
    if mode != "none":
        session.tap(*MODE_BUTTONS[mode])
        time.sleep(1.0)
    if not on_audio_test_page(runner):
        raise SystemExit(f"the session left test page {AUDIO_TEST_PAGE} after selecting {mode}")


def run(args):
    import hwsuite
    import profiles
    build = Path(args.build if "/" in args.build else TOOLS.parent / "build" / args.build).resolve()
    profile_path = Path(args.profile) if args.profile else TOOLS / "profiles" / "sp16-1.60.json"
    profile = profiles.validate(json.loads(profile_path.read_bytes()))
    out = build / "ak4458-transport" / time.strftime("%Y%m%d-%H%M%S")
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
        modes = args.mode or list(MODES)
        # The stimulus: the shim's own tone on the input jack. The knobs stay at their neutral defaults so the limiter
        # cannot colour the signal the table is about.
        session.event("input_tone", "value", INPUT_TONE_SLOT)
        runs, serial = [], 0
        for mode in modes:
            set_mode(session, runner, mode)
            time.sleep(args.settle)  # a capture already running when the mode changed has to finish first
            # Only a capture that began after the settle counts: the one that was in flight when the mode changed can
            # hold the tail of the previous mode.
            settled_at = latest_serial(session)
            # Cross-check independent of the tap: the shim meters the MAIN pair (channels 12 and 14) after MAIN OUT.
            levels = (session.metrics().get("guest") or {}).get("board", {}).get("analog_levels") or {}
            raw, name, serial = newest_capture(session, settled_at, args.timeout, allow_none=True)
            if raw is None:
                analysis = silent_capture()
            else:
                try:
                    analysis = analyse_capture(raw, args.tone, args.band)
                except ValueError as error:
                    raise SystemExit(f"{name}: {error}")
            runs.append({"mode": mode, "capture": name, "analysis": analysis,
                         "main_pair_peak": levels.get("out_peak"), "input_peak": levels.get("in_peak"),
                         "limiter_active": levels.get("limiter_active")})
            print(f"[{mode}] capture={name} frames={analysis['frames']}/{analysis['declared_frames']} "
                  f"carries={classify(analysis, args.floor, args.purity)} "
                  f"tone_of_word0={analysis['words'][0]['dominant_hz']}Hz "
                  f"main_pair_peak={levels.get('out_peak')} input_peak={levels.get('in_peak')}", flush=True)
        if all(run_["analysis"]["stream_silent"] for run_ in runs):
            raise SystemExit("no capture in any mode: the tap never armed, so check that the build is --trace pcmtap "
                             "and that the point the stimulus enters reaches the master stream")
        session.event("input_tone", "value", 0)
        result = table(runs, args.floor, args.purity)
        result.update(build=str(build), tone_hz=args.tone, firmware=profile["identity"]["version"],
                      launch_s=round(ready, 1), runs=runs)
        (out / "ak4458-transport.json").write_text(json.dumps(result, indent=1) + "\n")
        print(render(result))
        print(f"\nwrote {out / 'ak4458-transport.json'}")
        return result
    finally:
        if session:
            session.close()
        if pid:
            hwsuite.stop(build, pid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="drive the factory test's AUDIO TEST page and tabulate the sixteen transport words")
    r.add_argument("--build", required=True, help="build name or folder (needs --app-arg testmode and --trace pcmtap)")
    r.add_argument("--profile", help="profile file (default: profiles/sp16-1.60.json)")
    r.add_argument("--mode", action="append", choices=MODES, help="only this mode (repeatable; default all four)")
    r.add_argument("--tone", type=float, default=1000.0, help="stimulus frequency in Hz (default 1000)")
    r.add_argument("--band", type=float, default=150.0,
                   help="half-width of the tone's band in Hz (default 150: the analog loop returns it about 35 Hz high)")
    r.add_argument("--floor", type=float, default=0.01, help="level a word must reach to count (default 0.01)")
    r.add_argument("--purity", type=float, default=0.5, help="share of the word's energy at the tone (default 0.5)")
    r.add_argument("--settle", type=float, default=10.0, help="seconds to wait after a mode change (default 10)")
    r.add_argument("--timeout", type=float, default=50.0,
                   help="seconds to wait for a capture after a mode change (default 50)")
    s = sub.add_parser("show", help="re-print a saved run's table")
    s.add_argument("file", help="the ak4458-transport.json a run wrote")
    args = parser.parse_args(argv)
    if args.command == "show":
        print(render(json.loads(Path(args.file).read_text())))
        return 0
    if not 0.0 < args.floor < 1.0:
        parser.error("--floor must be greater than 0 and less than 1")
    if not 0.0 <= args.purity <= 1.0:
        parser.error("--purity must be between 0 and 1")
    if not 20.0 <= args.tone <= RATE / 2:
        parser.error(f"--tone must be between 20 and {RATE // 2} Hz")
    if not 1.0 <= args.band <= args.tone / 2:
        parser.error("--band must be at least 1 Hz and at most half the tone")
    if args.settle < 5.0:
        parser.error("--settle must be at least 5 s: a capture already running when the mode changed is 4 s long")
    if args.timeout < 10.0:
        parser.error("--timeout must be at least 10 s")
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
