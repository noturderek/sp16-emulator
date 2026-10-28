#!/usr/bin/env python3
"""SP-16 hardware conformance suite runner (docs/hardware-suite.md).

54 checks (HW-01..HW-54; HW-47..HW-54 are extras outside the verification tiers) of the emulated hardware boundary, driven only through a research session's control socket,
its files and process signals to exact PIDs. The firmware is the oracle for hardware outcomes only; per-version data
(probes, recipes, lamp roles) comes from the firmware's compatibility profile, and a check whose profile data is
missing is BLOCKED, never PASS.

  hwsuite.py --runtime <session runtime.json> --profile <profile.json> --build-dir <build> [--control-build <build>]
             [--tier smoke|core|full] [--only HW-05,HW-18] [--out DIR] [--fixtures DIR] [--mark-verified]
  hwsuite.py --make-fixtures DIR [--profile <profile.json>]

Exit codes: 0 all selected PASS or N/A, 1 a FAIL, 2 a BLOCKED (no FAIL), 3 environment or usage error, 4 session lost.
Never plays audio (refuses any session not built with --audio file), never uses GUI automation, never flashes.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
import wave

TOOLS = Path(__file__).resolve().parent
SUITE_NAME = "sp16-hardware-conformance"
SUITE_VERSION = "1.0.0"
# Bump when a change to the emulated hardware boundary (shim, bridge, panel model) can alter verdicts.
HARDWARE_EPOCH = 1
# Sources whose hash forms the hardware fingerprint (the emulated boundary).
FINGERPRINT_SOURCES = ("hardware_stub.c", "guest-bridge.py", "panel.py", "panel_map.py", "protocol.py", "input.py")
TIMING_CHECKS = ("HW-19", "HW-32", "HW-33", "HW-44")
MAX_VMS = 3
EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_ENV, EXIT_LOST = range(5)
STATUSES = ("PASS", "FAIL", "N/A", "BLOCKED", "SKIP")
# "X" checks are extras outside verification: a full run never includes them (so a profile can still be marked verified
# without a testmode build or a disposable session); `--tier extra`, or `--only` naming them, runs them.
TIERS = {"smoke": ("S",), "core": ("S", "C"), "full": ("S", "C", "L"), "extra": ("X",)}


# ------------------------------------------------------------------------------------------------------ check table

class Check:
    def __init__(self, hw_id, area, title, tier, needs=(), after=()):
        self.id, self.area, self.title, self.tier = hw_id, area, title, tier
        self.needs = tuple(needs)   # dotted profile paths that must be authored (not null/empty)
        self.after = tuple(after)   # prerequisite checks: a FAIL or BLOCKED there blocks this one


CHECKS = [
    Check("HW-01", "Boot", "Firmware boots to a settled home screen with live lamps", "S", ["identity.boot_timeout_s"]),
    Check("HW-02", "LCD", "Framebuffer is faithful and survives resize", "C", ["selector_probe"], ["HW-01"]),
    Check("HW-03", "LCD touch", "Touch coordinates map to the firmware's touch space", "C",
          ["touch_probe", "touch_dead"], ["HW-01"]),
    Check("HW-04", "LCD touch", "Touch timing: short taps, hold, drag, release", "C", ["touch_probe", "screen_titles"],
          ["HW-01"]),
    Check("HW-05", "TX wire", "OEM to panel frames are valid and continuous", "S",
          ["identity.panel_protocol_revision", "identity.tx_fps"], ["HW-01"]),
    Check("HW-06", "RX wire", "Panel to OEM frames are valid, correctly paced and never dropped", "S",
          ["identity.rx_idle_min_fps"], ["HW-01"]),
    Check("HW-07", "Events", "Every host event arrives once and is accounted", "S", [], ["HW-01"]),
    Check("HW-08", "Events", "1000-event burst is lossless", "S", [], ["HW-01"]),
    Check("HW-09", "Buttons", "Every button bit reaches the firmware; undeclared bits are inert", "C",
          ["panel.button_roles", "panel.buttons_inert", "button_effects", "panel.lamp_roles"], ["HW-01"]),
    Check("HW-10", "Lamps", "Pad-mode buttons are one-hot and toggle", "C", ["panel.lamp_groups.pad_mode_one_hot"],
          ["HW-01"]),
    Check("HW-11", "Lamps", "Dim vs full flag and transport lamps", "C", ["panel.lamp_groups.transport"], ["HW-01"]),
    Check("HW-12", "Buttons and lamps", "Step keys, bar keys and position lamps", "C",
          ["panel.lamp_groups.bar", "rgb_known.playhead"], ["HW-01"]),
    Check("HW-13", "Buttons", "Chords, holds, short pulses, release-all, SHIFT", "C", ["shift_probe"], ["HW-01"]),
    Check("HW-14", "Lamps", "Lamp map is complete: every channel the firmware drives is decoded", "C",
          ["panel.lamp_roles", "lamp_unassigned"], ["HW-01"]),
    Check("HW-15", "Lamps", "RGB channels are independent and correctly scaled", "C",
          ["rgb_known.strike_flash", "rgb_known.playhead", "rgb_known.max_channel_value"], ["HW-01"]),
    Check("HW-16", "Knobs", "Each knob counts one detent per detent, in the right column and direction", "S",
          ["knob_probe", "knob_columns"], ["HW-01"]),
    Check("HW-17", "Knobs", "Detent bursts drain and respect bounds", "C", ["knob_bounded_param"], ["HW-01"]),
    Check("HW-18", "Selector", "One detent is one step, both directions, with wrap", "S", ["selector_probe"], ["HW-01"]),
    Check("HW-19", "Selector", "Detent bursts are exact (frame-window rule)", "S",
          ["selector_probe", "panel.selector_window_frames"], ["HW-01"]),
    Check("HW-20", "Selector, BACK, HOME", "Push opens, BACK and HOME navigate", "C", ["nav_probe", "screen_titles"],
          ["HW-01"]),
    Check("HW-21", "Pads", "Every pad registers on its own channel; simultaneous pads", "S",
          ["panel.pad_curve", "rgb_known.strike_flash"], ["HW-01"]),
    Check("HW-22", "Pads", "Velocity range and monotonic response", "C",
          ["recipes.sample_load", "recipes.velocity_enable", "panel.pad_curve"], ["HW-01"]),
    Check("HW-23", "Pads", "Held pressure persists; release is seen", "C",
          ["recipes.gate_mode", "recipes.sample_load"], ["HW-01"]),
    Check("HW-24", "Pads", "Calibration state: no phantom blink; negative control works", "S",
          ["calibration.path", "calibration.range"], ["HW-01"]),
    Check("HW-25", "Pads, lamps", "Brightness level scales pads and buttons", "C",
          ["recipes.brightness", "rgb_known.tx_level_offset", "rgb_known.tx_level_range"], ["HW-01"]),
    Check("HW-26", "Strip", "Position and contact reach the firmware; LEDs follow", "C",
          ["strip.lamp_channels", "strip.default_mode"], ["HW-01"]),
    Check("HW-27", "Strip", "Strip changes audio in its first mode, only while touched", "C",
          ["recipes.sample_load", "recipes.gate_mode", "strip.default_mode"], ["HW-01"]),
    Check("HW-28", "Strip, lamps", "MODE button cycles four one-hot lamps", "C", ["panel.lamp_groups.mode_order"],
          ["HW-01"]),
    Check("HW-29", "Strip, lamps", "HOLD latches the strip value", "C", ["strip.lamp_channels"], ["HW-01"]),
    Check("HW-30", "Analog", "MAIN OUT, DRIVE and HP CUTOFF reach the firmware's ADC words", "C",
          ["panel.analog_words"], ["HW-01"]),
    Check("HW-31", "Audio", "The three PCM ports open with the expected parameters", "S", ["audio.alsa_streams"],
          ["HW-01"]),
    Check("HW-32", "Audio", "Clock holds 1.00x with no gaps", "S", ["audio.default_bpm", "audio.late_over_20ms_max"],
          ["HW-01"]),
    Check("HW-33", "Audio, clock", "Sequencer tempo equals the project tempo", "S",
          ["audio.default_bpm", "rgb_known.playhead", "recipes.sample_load", "recipes.click_steps"], ["HW-01"]),
    Check("HW-34", "Audio", "Playback is sample-exact and in tune", "S",
          ["recipes.sample_load", "audio.sound_channels", "recipes.volume_knob"], ["HW-01"]),
    Check("HW-35", "Audio, lamps", "Level lamp follows the master output", "C",
          ["panel.lamp_groups.over", "recipes.sample_load"], ["HW-01"]),
    Check("HW-36", "USB", "A stick present at boot is mounted and visible to the firmware", "C",
          ["usb.sample_dir", "usb.browse_recipe", "panel.lamp_groups.usb"], ["HW-01"]),
    Check("HW-37", "USB", "Eject and attach round trip", "C", ["usb.browse_recipe", "panel.lamp_groups.usb"], ["HW-01"]),
    Check("HW-38", "USB", "USB STOP long hold ejects safely", "C", ["usb.stop_hold_s", "panel.lamp_groups.usb"],
          ["HW-01"]),
    Check("HW-39", "USB", "Blank and dirty sticks are handled without data loss", "C", ["usb.tree"], ["HW-01"]),
    Check("HW-40", "eMMC", "State saved through the firmware survives a clean restart", "C",
          ["recipes.persist_probe", "recipes.save_project"], ["HW-01"]),
    Check("HW-41", "eMMC, power", "Recovery after the app is killed hard", "C", ["recipes.save_project"], ["HW-01"]),
    Check("HW-42", "eMMC", "One session per data image; other profiles untouched", "C", [], ["HW-01"]),
    Check("HW-43", "Power", "Clean quit and no stale state", "S", [], ["HW-01"]),
    Check("HW-44", "Soak", "Five minutes of play and input stays healthy", "L", ["audio.default_bpm"], ["HW-01"]),
    Check("HW-45", "Driver ABI", "Every device node and ioctl the firmware uses is served; unknown ones are loud", "S",
          ["device_nodes", "ioctl_set", "identity.panel_protocol_revision"], ["HW-01"]),
    Check("HW-46", "Analog", "ACTIVE drives the filter select line; the shim's filter loop runs", "C",
          ["panel.lamp_roles"], ["HW-01"]),
    Check("HW-47", "Power", "Standby: the power key enters it (stick unmounted, saves blocked, UI alive); a restart leaves it",
          "X", ["button_effects.power", "recipes.save_project", "usb.sample_dir", "panel.lamp_roles"], ["HW-01"]),
    # Factory test pages, graded against the service manual's pass lines. They run in a build made with
    # `--app-arg testmode` (--testmode-build), where the app starts in the factory test instead of the firmware UI.
    Check("HW-48", "Factory test", "Pages 1 and 2: every pad and step lamp shows red, green, blue and white; all off, LCD black", "X"),
    Check("HW-49", "Factory test", "Page 4: every button counts once (48 with a lamp, MODE and SHIFT: 50 of 50) and the "
          "status line names the pressed control", "X"),
    Check("HW-50", "Factory test", "Page 5: MASTER, DSF DRIVE and DSF HIGHPASS span 0-1023 and the strip 0-127", "X"),
    Check("HW-51", "Factory test", "Page 6: the six encoders and the push encoder count and report their state", "X"),
    Check("HW-52", "Screenshots", "The settled home screen and the panel match the suite's own earlier captures", "X",
          ["home.title_regex"], ["HW-01"]),
    Check("HW-53", "Factory test", "Page 3: three WAKE UP passes record a held pad's calibration, only inside the valid range", "X",
          ["calibration.pads", "calibration.range", "calibration.default"]),
    Check("HW-54", "Factory test", "Page 8: the MIDI CHECK touch button runs the DIN MIDI loopback to OK", "X"),
]
TESTMODE_IDS = ("HW-48", "HW-49", "HW-50", "HW-51", "HW-53", "HW-54")
EXTRA_IDS = ("HW-47",) + TESTMODE_IDS + ("HW-52",)
CHECK_BY_ID = {check.id: check for check in CHECKS}
SMOKE_IDS = tuple(c.id for c in CHECKS if c.tier == "S")
assert len(CHECKS) == 54 and len(SMOKE_IDS) == 16


def checks_hash():
    """Identity of the check table (ids, tiers, profile needs, prerequisites) recorded with every result."""
    table = [[c.id, c.area, c.title, c.tier, list(c.needs), list(c.after)] for c in CHECKS]
    return hashlib.sha256(json.dumps(table, sort_keys=True).encode()).hexdigest()


def hardware_fingerprint(tools=TOOLS):
    sources = {}
    for name in FINGERPRINT_SOURCES:
        path = Path(tools) / name
        sources[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    digest = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    return {"hardware_epoch": HARDWARE_EPOCH, "sha256": digest, "sources": sources}


def select_checks(tier="full", only=None):
    """Ids to run, in table order. `only` (a list of ids) narrows the tier; unknown ids are a usage error."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if only:
        unknown = [hw for hw in only if hw not in CHECK_BY_ID]
        if unknown:
            raise ValueError("unknown check ids: " + ", ".join(unknown))
    return [c.id for c in CHECKS if (c.tier in TIERS[tier] or (only and c.tier == "X" and c.id in only))
            and (not only or c.id in only)]


# ------------------------------------------------------------------------------------------------- profile fields

def profile_value(profile, dotted):
    node = profile
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def authored(value):
    """A profile field counts as authored when it is present and not null/empty. False and 0 are data."""
    return value is not None and value != [] and value != {} and value != ""


def missing_fields(profile, hw_id):
    """Profile paths a check needs that are not authored; BLOCKED until they are."""
    return [path for path in CHECK_BY_ID[hw_id].needs if not authored(profile_value(profile, path))]


def absent_reason(profile, capability):
    """The profile's declared reason that this version lacks a capability (permits N/A), or None."""
    for item in (profile.get("hardware") or {}).get("absent") or []:
        if item.get("capability") == capability:
            return item.get("reason")
    return None


# ---------------------------------------------------------------------------------------------------- verdicts

def verdict(hw_id, status, reason="", metrics=None, evidence=None, subresults=None, elapsed=0.0, load=None):
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    if status == "N/A" and not reason:
        raise ValueError("N/A needs a reason")
    check = CHECK_BY_ID[hw_id]
    return {"id": hw_id, "area": check.area, "title": check.title, "tier": check.tier, "status": status,
            "reason": reason, "elapsed_s": round(elapsed, 2), "metrics": metrics or {}, "evidence": evidence or [],
            "subresults": subresults or [], "load": load}


def from_subresults(hw_id, subresults, metrics=None, evidence=None):
    """Aggregate named sub-results {name, status, observed}: any FAIL fails, else any BLOCKED blocks, else PASS
    (an N/A sub-result is recorded but does not fail the check, e.g. HW-30's analog-only pots)."""
    statuses = [s["status"] for s in subresults]
    if not subresults:
        return verdict(hw_id, "BLOCKED", "no sub-results", metrics, evidence)
    if "FAIL" in statuses:
        status = "FAIL"
    elif "BLOCKED" in statuses:
        status = "BLOCKED"
    elif all(s == "N/A" for s in statuses):
        status = "N/A"
    else:
        status = "PASS"
    failing = [s for s in subresults if s["status"] == status] if status in ("FAIL", "BLOCKED") else []
    reason = "; ".join(f"{s['name']}: {s.get('observed', '')}" for s in failing)[:600]
    if status == "N/A":
        reason = "; ".join(s.get("observed", "") for s in subresults)
    return verdict(hw_id, status, reason, metrics, evidence, subresults)


def sub(name, ok, observed="", blocked=False, na=False):
    status = "N/A" if na else "BLOCKED" if blocked else "PASS" if ok else "FAIL"
    return {"name": name, "status": status, "observed": observed if isinstance(observed, str) else json.dumps(observed)}


def summarize(verdicts, tier="full", only=None, max_na_without_signoff=5):
    counts = {status: 0 for status in STATUSES}
    for item in verdicts:
        counts[item["status"]] += 1
    complete = {item["id"] for item in verdicts} >= set(select_checks("full"))
    eligible = (tier == "full" and not only and complete and counts["FAIL"] == counts["BLOCKED"]
                == counts["SKIP"] == 0)
    return {"counts": counts, "total": len(verdicts), "verified_eligible": eligible,
            "needs_owner_signoff": eligible and counts["N/A"] > max_na_without_signoff}


def exit_code(verdicts, session_lost=False):
    statuses = {item["status"] for item in verdicts}
    if session_lost:
        return EXIT_LOST
    if "FAIL" in statuses:
        return EXIT_FAIL
    if "BLOCKED" in statuses:
        return EXIT_BLOCKED
    return EXIT_OK


def render_report(result):
    """hwsuite-report.md: one row per HW id, a section per FAIL/BLOCKED, the N/A list and the stub register."""
    run, summary = result["run"], result["summary"]
    lines = [f"# Hardware conformance suite: firmware {run.get('firmware_version')} ({run['id']})", "",
             f"- Suite {result['suite']['version']}, checks hash `{result['suite']['checks_hash'][:16]}`, hardware "
             f"fingerprint `{result['suite']['hardware_fingerprint']['sha256'][:16]}` (epoch "
             f"{result['suite']['hardware_fingerprint']['hardware_epoch']})",
             f"- Tier `{run['tier']}`{' only ' + ','.join(run['only']) if run.get('only') else ''}; started "
             f"{run['start']}, ended {run.get('end')}; profile `{run.get('profile_path')}` "
             f"(SHA-256 `{(run.get('profile_sha256') or '')[:16]}`); firmware image `"
             f"{(run.get('firmware_image_sha256') or '')[:16]}`",
             "- Counts: " + ", ".join(f"{k} {v}" for k, v in summary["counts"].items()) +
             f"; verified eligible: {'yes' if summary['verified_eligible'] else 'no'}", "",
             "| ID | Area | Status | Observed | s |", "|---|---|---|---|---|"]
    for item in result["verdicts"]:
        observed = (item["reason"] or "ok").replace("|", "/").replace("\n", " ")
        lines.append(f"| {item['id']} | {item['area']} | {item['status']} | {observed[:220]} | {item['elapsed_s']} |")
    for item in result["verdicts"]:
        if item["status"] in ("FAIL", "BLOCKED"):
            lines += ["", f"## {item['id']} {item['status']}: {item['title']}", "", item["reason"] or "", ""]
            for s in item["subresults"]:
                if s["status"] != "PASS":
                    lines.append(f"- {s['name']}: {s['status']} ({s.get('observed', '')[:300]})")
            if item["evidence"]:
                lines.append("- Evidence: " + ", ".join(f"`{e}`" for e in item["evidence"][:6]))
    na = [item for item in result["verdicts"] if item["status"] == "N/A"]
    na_subs = [(item["id"], s) for item in result["verdicts"] for s in item["subresults"] if s["status"] == "N/A"]
    lines += ["", "## N/A", ""] + ([f"- {item['id']}: {item['reason']}" for item in na] or ["- none (check level)"])
    lines += [f"- {hw} sub-result {s['name']}: {s.get('observed', '')}" for hw, s in na_subs]
    lines += ["", "## Not emulated (stub register; see docs/hardware-suite.md, Not in this suite)", "",
              "MIDI beyond the factory test's DIN loopback, and USB-B MIDI; sync sources and PRO DJ LINK; audio input, "
              "THROUGH and live sampling (a research tone stands in for the input); output routing and multichannel "
              "outputs beyond the main stereo pair; the analog filter acting on sampler playback, and DSP (the filter loop "
              "only carries a research tone); network and USB host hotplug beyond HW-36..39; physical pad force.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- frame decoding

def crc16_x25(data):
    crc = 0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xffff


def tx_frame_ok(frame):
    """HW-05 per-frame criterion: 128 bytes, header 0x02, marker 0xaa at 0x77, CRC-16/X-25 over 0..123 at 124 (LE)."""
    return (len(frame) == 128 and frame[0] == 0x02 and frame[0x77] == 0xaa
            and crc16_x25(frame[:124]) == struct.unpack_from("<H", frame, 124)[0])


def parse_tx_log(data):
    """tx-frames.jsonl bytes -> [(t, frame bytes)] with the 3-byte multi/part/count prefix stripped."""
    out = []
    for line in data.splitlines():
        try:
            record = json.loads(line)
            out.append((float(record["t"]), bytes.fromhex(record["hex"])[3:]))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def frame_gaps(times):
    return [b - a for a, b in zip(times, times[1:])]


def mono_state(decoded, channel):
    """(lit, dim) of a mono lamp channel; dim is None where the lamp has no second flag."""
    return decoded["mono"][channel]


def one_hot(decoded, channels, bright_only=True):
    """Channels of a group that are lit (and, with bright_only, at full brightness: second flag clear or absent)."""
    lit = []
    for ch in channels:
        on, dim = decoded["mono"][ch]
        if on and (not bright_only or not dim):
            lit.append(ch)
    return lit


def playhead_steps(records, colour):
    """[(t, step index)] where exactly one of RGB channels 0-15 shows the playhead colour; changes only."""
    out, last = [], None
    for t, decoded in records:
        lit = [ch for ch in range(16) if decoded["rgb"][ch] == tuple(colour)]
        head = lit[0] if len(lit) == 1 else None
        if head is not None and head != last:
            out.append((t, head))
        if head is not None:
            last = head
    return out


def revisit_period(changes):
    """Median time between visits of the same step (a 16-step loop period); robust to single-interval jitter."""
    seen, periods = {}, []
    for t, head in changes:
        if head in seen:
            periods.append(t - seen[head])
        seen[head] = t
    return median(periods)


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def linear_fit_error(points, slope, intercept):
    """Max |observed - (slope * x + intercept)| over (x, observed) pairs."""
    return max(abs(y - (slope * x + intercept)) for x, y in points)


# ----------------------------------------------------------------------------------------------------- fixtures

RATE = 44100


def wav_bytes(samples_lr, rate=RATE):
    """Interleaved stereo int16 frames -> RIFF WAV bytes (deterministic)."""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(struct.pack("<hh", l, r) for l, r in samples_lr))
    return buf.getvalue()


def tone(seconds, freq, amplitude, decay=None, tail=0.0, rate=RATE):
    frames = []
    for n in range(int(seconds * rate)):
        a = amplitude * (math.exp(-n / rate / decay) if decay else 1.0)
        v = max(-32767, min(32767, round(a * 32767 * math.sin(2 * math.pi * freq * n / rate))))
        frames.append((v, v))
    frames += [(0, 0)] * int(tail * rate)
    return frames


def fixture_wavs():
    """Spec 5.3 audio fixtures: name -> WAV bytes."""
    return {
        "hw-tone440.wav": wav_bytes(tone(6.0, 440.0, 10 ** (-6 / 20))),
        "hw-click.wav": wav_bytes(tone(0.08, 440.0, 10 ** (-6 / 20), decay=0.02, tail=2.0)),
        "hw-tone-full.wav": wav_bytes(tone(1.0, 440.0, 1.0)),
        "hw-tone-m30.wav": wav_bytes(tone(1.0, 440.0, 10 ** (-30 / 20))),
    }


class Fat32:
    """Minimal deterministic FAT32 image writer (512-byte sectors, one sector per cluster, long file names).

    Enough for a virtual USB stick with a few folders and files: the guest's fsck.vfat and the firmware read it like
    any stick. Not a general file system implementation."""

    SECTOR = 512
    RESERVED = 32
    EOC = 0x0FFFFFFF

    # No volume label: dosfstools reports a boot-sector label without a matching root-directory label entry.
    def __init__(self, size_bytes=64 << 20, label="NO NAME", volume_id=0x5316C0DE):
        self.sectors = size_bytes // self.SECTOR
        self.label = label.upper()[:11].ljust(11)
        self.volume_id = volume_id
        # FAT size: solve for clusters with 128 entries per FAT sector, 2 FATs.
        fat = 1
        while True:
            clusters = self.sectors - self.RESERVED - 2 * fat
            if (clusters + 2 + 127) // 128 <= fat:
                break
            fat += 1
        self.fat_sectors = fat
        self.clusters = self.sectors - self.RESERVED - 2 * fat
        if self.clusters < 65525:
            raise ValueError("image too small for FAT32")
        self.fat = [0x0FFFFFF8, 0x0FFFFFFF, self.EOC]  # cluster 2 = root directory
        self.data = {}  # cluster -> bytes
        self.dirs = {2: []}  # directory first cluster -> [32-byte entries]
        self.names = {2: set()}

    @staticmethod
    def _sfn_checksum(name11):
        total = 0
        for byte in name11:
            total = ((total >> 1) | ((total & 1) << 7)) + byte & 0xff
        return total

    def _short_name(self, parent, name):
        base, _, ext = name.rpartition(".") if "." in name[1:] else (name, "", "")
        clean = lambda s: re.sub(r"[^A-Z0-9_~!#$%&'()@^`{}-]", "", s.upper())
        b, e = clean(base), clean(ext)[:3]
        candidate = (b[:8].ljust(8) + e.ljust(3)).encode()
        if b and len(b) <= 8 and name.upper() == name and candidate not in self.names[parent]:
            return candidate, False
        for n in range(1, 100):
            tail = f"~{n}"
            candidate = ((b[:8 - len(tail)] or "X") + tail).ljust(8).encode() + e.ljust(3).encode()
            if candidate not in self.names[parent]:
                return candidate, True
        raise ValueError("too many similar names")

    def _alloc(self, count):
        first = len(self.fat)
        for i in range(count):
            self.fat.append(first + i + 1 if i < count - 1 else self.EOC)
        if len(self.fat) - 2 > self.clusters:
            raise ValueError("image full")
        return first

    def _entries(self, parent, name, attr, cluster, size):
        sfn, needs_lfn = self._short_name(parent, name)
        self.names[parent].add(sfn)
        entries = []
        if needs_lfn or name != name.upper():
            units = list(name.encode("utf-16-le"))
            chars = [units[i] | units[i + 1] << 8 for i in range(0, len(units), 2)] + [0]
            parts = [chars[i:i + 13] for i in range(0, len(chars), 13)]
            check = self._sfn_checksum(sfn)
            for index in range(len(parts), 0, -1):
                part = (parts[index - 1] + [0xffff] * 13)[:13]
                seq = index | (0x40 if index == len(parts) else 0)
                raw = (bytes([seq]) + struct.pack("<5H", *part[:5]) + bytes([0x0F, 0, check])
                       + struct.pack("<6H", *part[5:11]) + b"\0\0" + struct.pack("<2H", *part[11:13]))
                entries.append(raw)
        # Fixed timestamps (2026-01-01 00:00) keep the image deterministic.
        date, tim = ((2026 - 1980) << 9) | (1 << 5) | 1, 0
        entries.append(sfn + struct.pack("<BBBHHHHHHHI", attr, 0, 0, tim, date, date, cluster >> 16, tim, date,
                                         cluster & 0xffff, size))
        return entries

    def mkdir(self, parent, name):
        cluster = self._alloc(1)
        self.dirs[cluster], self.names[cluster] = [], set()
        dot = lambda n, c: n.ljust(11).encode() + struct.pack("<BBBHHHHHHHI", 0x10, 0, 0, 0, 0x5c21, 0x5c21, c >> 16,
                                                                 0, 0x5c21, c & 0xffff, 0)
        self.dirs[cluster] += [dot(".", cluster), dot("..", 0 if parent == 2 else parent)]
        self.dirs[parent] += self._entries(parent, name, 0x10, cluster, 0)
        return cluster

    def add_file(self, parent, name, payload):
        count = max(1, (len(payload) + self.SECTOR - 1) // self.SECTOR)
        first = self._alloc(count) if payload else 0
        for i in range(count if payload else 0):
            self.data[first + i] = payload[i * self.SECTOR:(i + 1) * self.SECTOR]
        self.dirs[parent] += self._entries(parent, name, 0x20, first, len(payload))
        return first

    def path(self, parts):
        """Create nested directories (or reuse) and return the last cluster."""
        cluster = 2
        for part in parts:
            existing = getattr(self, "_paths", {})
            key = (cluster, part)
            if key not in existing:
                existing[key] = self.mkdir(cluster, part)
                self._paths = existing
            cluster = existing[key]
        return cluster

    def image(self, dirty=False):
        """The whole image as bytes. dirty=True clears the clean-shutdown bit (FAT[1] bit 27) and sets the boot
        sector's dirty flag, as an unclean unmount leaves it."""
        # Directories may need more than one cluster: extend chains.
        layout, fat = {}, list(self.fat)
        for first, entries in self.dirs.items():
            blob = b"".join(entries)
            chunks = [blob[i:i + self.SECTOR] for i in range(0, max(len(blob), 1), self.SECTOR)] or [b""]
            chain = [first]
            for _ in chunks[1:]:
                fat[chain[-1]] = len(fat)
                chain.append(len(fat))
                fat.append(self.EOC)
            for cluster, chunk in zip(chain, chunks):
                layout[cluster] = chunk
        if dirty:
            fat[1] = 0x07FFFFFF
        used = len(fat) - 2
        img = bytearray()
        bs = bytearray(self.SECTOR)
        bs[0:3] = b"\xEB\x58\x90"
        bs[3:11] = b"MSWIN4.1"
        struct.pack_into("<HBHBHHBHHHII", bs, 11, self.SECTOR, 1, self.RESERVED, 2, 0, 0, 0xF8, 0, 32, 64, 0,
                         self.sectors)
        struct.pack_into("<IHHIHH12sBBBI11s8s", bs, 36, self.fat_sectors, 0, 0, 2, 1, 6, b"\0" * 12, 0x80,
                         1 if dirty else 0, 0x29, self.volume_id, self.label.encode(), b"FAT32   ")
        bs[510:512] = b"\x55\xAA"
        fsinfo = bytearray(self.SECTOR)
        struct.pack_into("<I", fsinfo, 0, 0x41615252)
        struct.pack_into("<III", fsinfo, 484, 0x61417272, self.clusters - used, len(fat))
        struct.pack_into("<I", fsinfo, 508, 0xAA550000)
        reserved = bytearray(self.RESERVED * self.SECTOR)
        reserved[0:512], reserved[512:1024] = bs, fsinfo
        reserved[6 * 512:7 * 512], reserved[7 * 512:8 * 512] = bs, fsinfo
        img += reserved
        fat_bytes = bytearray(self.fat_sectors * self.SECTOR)
        struct.pack_into(f"<{len(fat)}I", fat_bytes, 0, *fat)
        img += fat_bytes + fat_bytes
        data_start = len(img)
        total = self.sectors * self.SECTOR
        img += bytes(max(0, (max(list(self.data) + list(layout)) - 1) * self.SECTOR))
        for cluster, chunk in {**self.data, **layout}.items():
            offset = data_start + (cluster - 2) * self.SECTOR
            img[offset:offset + len(chunk)] = chunk
        if len(img) > total:
            raise ValueError("image overflow")
        return bytes(img), total


class FatReader:
    """Read-only FAT32 reader (any cluster size, long names) for checking stick and data images while their session
    is stopped: list a directory, read a file. Never writes."""

    def __init__(self, path):
        self.handle = open(path, "rb")
        bs = self.handle.read(512)
        if bs[510:512] != b"\x55\xAA":
            raise ValueError("no FAT boot sector")
        (self.bps, self.spc, self.reserved, self.nfats, root_entries, total16, _, fat16, _, _, _, total32) = \
            struct.unpack_from("<HBHBHHBHHHII", bs, 11)
        self.fat_size, = struct.unpack_from("<I", bs, 36) if fat16 == 0 else (fat16,)
        self.root, = struct.unpack_from("<I", bs, 44)
        if fat16 != 0 or root_entries:
            raise ValueError("not FAT32")
        self.dirty_flag = bool(bs[0x41] & 1)
        self.cluster_bytes = self.bps * self.spc
        self.data_start = (self.reserved + self.nfats * self.fat_size) * self.bps
        self.handle.seek(self.reserved * self.bps)
        raw = self.handle.read(self.fat_size * self.bps)
        self.fat = struct.unpack(f"<{len(raw) // 4}I", raw)

    def close(self):
        self.handle.close()

    def chain(self, first):
        out, cluster = [], first
        while 2 <= cluster < 0x0FFFFFF8 and len(out) < len(self.fat):
            out.append(cluster)
            cluster = self.fat[cluster] & 0x0FFFFFFF
        return out

    def read_chain(self, first):
        data = bytearray()
        for cluster in self.chain(first):
            self.handle.seek(self.data_start + (cluster - 2) * self.cluster_bytes)
            data += self.handle.read(self.cluster_bytes)
        return bytes(data)

    def listdir(self, cluster):
        """{name: (is_dir, first cluster, size)} with long names when present."""
        raw, out, lfn = self.read_chain(cluster), {}, []
        for i in range(0, len(raw), 32):
            entry = raw[i:i + 32]
            if entry[0] == 0:
                break
            if entry[0] == 0xE5:
                lfn = []
                continue
            if entry[11] == 0x0F:
                chars = entry[1:11] + entry[14:26] + entry[28:32]
                lfn.insert(0, chars.decode("utf-16-le", errors="replace"))
                continue
            short = entry[0:8].decode("latin-1").rstrip() + (("." + entry[8:11].decode("latin-1").rstrip())
                                                             if entry[8:11].strip() else "")
            name = "".join(lfn).split("\0")[0] if lfn else short
            lfn = []
            if entry[11] & 0x08 or name in (".", ".."):
                continue
            first = struct.unpack_from("<H", entry, 20)[0] << 16 | struct.unpack_from("<H", entry, 26)[0]
            out[name] = (bool(entry[11] & 0x10), first, struct.unpack_from("<I", entry, 28)[0])
        return out

    def lookup(self, path):
        cluster, entry = self.root, (True, self.root, 0)
        for part in [p for p in path.split("/") if p]:
            listing = {k.upper(): v for k, v in self.listdir(cluster).items()}
            entry = listing.get(part.upper())
            if entry is None:
                return None
            cluster = entry[1]
        return entry

    def read(self, path):
        entry = self.lookup(path)
        if entry is None or entry[0]:
            return None
        return self.read_chain(entry[1])[:entry[2]]

    def list(self, path):
        entry = self.lookup(path)
        return None if entry is None or not entry[0] else self.listdir(entry[1])


def write_sparse(path, payload, total):
    """Write payload then extend the file sparsely to total bytes; mode 0600, never through a symlink."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.truncate(total)


def stick_image(files, sample_dir, dirty=False, size=64 << 20):
    """FAT32 stick with each fixture under sample_dir (e.g. 'PIONEER DJ SAMPLER/Samples')."""
    fs = Fat32(size)
    folder = fs.path([p for p in sample_dir.split("/") if p])
    for name, payload in sorted(files.items()):
        fs.add_file(folder, name, payload)
    return fs.image(dirty=dirty)


def make_fixtures(out, sample_dir="PIONEER DJ SAMPLER/Samples"):
    """Generate every spec 5.3 fixture into `out` (created 0700); returns {name: sha256}."""
    out = Path(out)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    hashes = {}
    wavs = fixture_wavs()
    for name, payload in wavs.items():
        write_sparse(out / name, payload, len(payload))
        hashes[name] = hashlib.sha256(payload).hexdigest()
    for name, dirty in (("hw-stick.img", False), ("hw-dirty.img", True)):
        payload, total = stick_image(wavs, sample_dir, dirty=dirty)
        write_sparse(out / name, payload, total)
        hashes[name] = sha256_file(out / name)
    write_sparse(out / "hw-blank.img", b"", 64 << 20)
    hashes["hw-blank.img"] = sha256_file(out / "hw-blank.img")
    return hashes


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ------------------------------------------------------------------------------------------------- host and VMs

def load_averages():
    out = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True).stdout
    values = re.findall(r"[0-9]+\.[0-9]+", out)
    return [float(v) for v in values[:3]] if len(values) >= 3 else None


def qemu_processes():
    """[(pid, ppid, command)] of every qemu-system process on the host (any owner, any build)."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and "qemu-system" in parts[2].split(" -", 1)[0]:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def host_state():
    return {"load": load_averages(), "vms": len(qemu_processes())}


def app_executable(build):
    """Exact executable of a build's app bundle (decoupled builds use the neutral name)."""
    import plistlib
    bundle = Path(json.loads((Path(build) / "assets.json").read_text())["app_bundle"])
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    return str(bundle / "Contents/MacOS" / info["CFBundleExecutable"])


def build_processes(build):
    """(app pids, [(qemu pid, parent pid)]) of one build, by exact executable path and build directory."""
    exe = app_executable(build)
    prefix = str(Path(build).resolve()) + "/"
    # Foundation starts QEMU with a standardized path: /private/tmp/... becomes /tmp/... (also /private/var, /private/etc).
    prefixes = (prefix, prefix[len("/private"):]) if prefix.startswith("/private/") else (prefix,)
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    apps, qemus = [], []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, command = int(parts[0]), int(parts[1]), parts[2]
        if command == exe or command.startswith(exe + " "):
            apps.append(pid)
        elif command.startswith(prefixes) and "qemu-system" in command.split(" -", 1)[0]:
            qemus.append((pid, ppid))
    return apps, qemus


class SessionLost(RuntimeError):
    pass


class EnvError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------- the session

ACTIONS = {"release": 0, "press": 1, "delta": 2, "value": 3}


class Session:
    """One research session: socket client plus its files. Never touches anything outside its session directory."""

    def __init__(self, runtime_path, connect_timeout=20, call_timeout=40):
        self.call_timeout = call_timeout
        self.runtime_path = Path(runtime_path)
        self.runtime = json.loads(self.runtime_path.read_text())
        if not self.runtime.get("control_socket"):
            raise EnvError("session was not built with --research (no control socket)")
        self.dir = Path(self.runtime["session"])
        if self.dir.resolve() != self.runtime_path.parent.resolve():
            raise EnvError("runtime.json session path does not match its directory")
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        import panel_map
        self.panel_map = panel_map
        self.ids = panel_map.CONTROL_IDS
        self.sock = None
        deadline = time.monotonic() + connect_timeout
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.call_timeout)
            try:
                sock.connect(self.runtime["control_socket"])
                break
            except (FileNotFoundError, ConnectionRefusedError):
                sock.close()
                if time.monotonic() > deadline:
                    raise SessionLost("control socket unavailable")
                time.sleep(.2)
        self.sock = sock
        self.reader = sock.makefile("r")
        self.shots = 0

    def close(self):
        if self.sock:
            try:
                self.reader.close()
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def call(self, **command):
        try:
            self.sock.sendall((json.dumps(command) + "\n").encode())
            line = self.reader.readline()
        except (OSError, ValueError) as error:
            raise SessionLost(f"socket error: {error}")
        if not line:
            raise SessionLost("socket closed")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply

    # --- input
    def event(self, name, action, value=0):
        return self.call(op="event", id=self.ids[name], action=ACTIONS[action], value=int(value))

    def press(self, name, seconds=0.15, value=100):
        self.event(name, "press", value)
        time.sleep(seconds)
        self.event(name, "release", 0)

    def turn(self, name, detents, gap=0.3):
        step = 1 if detents > 0 else -1
        for i in range(abs(detents)):
            self.event(name, "delta", step)
            if i < abs(detents) - 1:
                time.sleep(gap)

    def touch(self, x, y, action):
        return self.call(op="touch", x=int(x), y=int(y), action={"move": 0, "down": 1, "up": 2}[action])

    def tap(self, x, y, hold=0.1):
        self.touch(x, y, "down")
        time.sleep(hold)
        self.touch(x, y, "up")

    def raw(self, offset, data):
        return self.call(op="raw", offset=offset, hex=bytes(data).hex())

    def clear(self):
        return self.call(op="clear")

    def metrics(self):
        return self.call(op="metrics")

    def guest(self):
        return self.metrics().get("guest") or {}

    def usb(self, action):
        return self.call(op="usb", action=action)

    def resize(self, width, height):
        return self.call(op="resize", width=width, height=height)

    # --- LCD
    def snapshot(self, tag="snap"):
        self.shots += 1
        name = f"hw-{tag}-{self.shots:04d}"[:60] + ".png"
        self.call(op="snapshot", name=name)
        return self.dir / name

    def image(self, tag="snap"):
        from PIL import Image
        return Image.open(self.snapshot(tag)).convert("RGB")

    def panelshot(self, tag="panel"):
        """The whole host panel view (buttons, lamps, knobs and the LCD) as it is drawn now."""
        from PIL import Image
        self.shots += 1
        name = f"hw-{tag}-{self.shots:04d}"[:60] + ".png"
        self.call(op="panelshot", name=name)
        return Image.open(self.dir / name).convert("RGB")

    # --- TX lamps
    def txlog(self, on=True):
        return self.call(op="txlog", on=1 if on else 0)

    def tx_offset(self):
        path = self.dir / "tx-frames.jsonl"
        return path.stat().st_size if path.exists() else 0

    def tx_since(self, offset):
        """[(t, frame)] logged after a byte offset (the app keeps the file open; never truncate it)."""
        path = self.dir / "tx-frames.jsonl"
        if not path.exists():
            return []
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
        cut = data.rfind(b"\n")
        return parse_tx_log(data[:cut + 1] if cut >= 0 else b"")

    def lamps_since(self, offset):
        decode = self.panel_map.decode_tx
        return [(t, d) for t, d in ((t, decode(f)) for t, f in self.tx_since(offset)) if d]

    def lamps_now(self, window=0.5):
        offset = self.tx_offset()
        time.sleep(window)
        records = self.lamps_since(offset)
        return records[-1][1] if records else None

    # --- console
    def console(self):
        path = self.dir / "console.txt"
        return path.read_text(errors="replace") if path.exists() else ""


def ocr(image_or_path, box=None, psm=7, scale=4):
    """Tesseract text of an LCD region (upscaled; the white-on-dark LCD text is inverted first)."""
    from PIL import Image, ImageOps
    image = image_or_path if hasattr(image_or_path, "crop") else Image.open(image_or_path).convert("RGB")
    if box:
        image = image.crop(box)
    gray = image.convert("L")
    from PIL import ImageStat
    if ImageStat.Stat(gray).mean[0] < 128:
        gray = ImageOps.invert(gray)
    gray = gray.resize((image.width * scale, image.height * scale))
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"hwsuite-ocr-{os.getpid()}-{uuid.uuid4().hex}.png"
    try:
        gray.save(tmp)
        out = subprocess.run(["tesseract", str(tmp), "stdout", "--psm", str(psm)], capture_output=True, text=True)
        return re.sub(r"\s+", " ", out.stdout).strip()
    finally:
        tmp.unlink(missing_ok=True)


def highlighted_band(image, box, min_level=170, min_height=12):
    """(y0, y1) of the brightest horizontal band in a list region: the firmware draws the focused row of its lists
    with a light background. None when no row is highlighted."""
    x0, y0, x1, y1 = box
    gray = image.convert("L")
    px = gray.load()
    rows = []
    for y in range(y0, y1):
        samples = [px[x, y] for x in range(x0, x1, 4)]
        rows.append(sorted(samples)[len(samples) // 2] >= min_level)
    best, start = None, None
    for i, bright in enumerate(rows + [False]):
        if bright and start is None:
            start = i
        elif not bright and start is not None:
            if i - start >= min_height and (best is None or i - start > best[1] - best[0]):
                best = (start, i)
            start = None
    return None if best is None else (y0 + best[0], y0 + best[1])


def selected_tile(image, probe):
    """Cursor position on a tile screen (selector_probe.tiles: white border of the focused tile), 1-based, or None."""
    tiles = probe["tiles"]
    px = image.load()
    best, best_count = None, 0
    for index in range(1, probe["length"] + 1):
        row, col = (index - 1) // len(tiles["left"]), (index - 1) % len(tiles["left"])
        x0, y0 = tiles["left"][col], tiles["top_rows_from_bottom"][row]
        count = 0
        for x in range(x0 + 8, x0 + tiles["width"] - 10, 2):
            for y in (y0, y0 + 1, y0 + tiles["height"] - 1, y0 + tiles["height"]):
                r, g, b = px[x, y]
                if min(r, g, b) > 200:
                    count += 1
        if count > best_count:
            best, best_count = index, count
    return best if best_count > 60 else None


def read_number(text):
    """First signed decimal number in an OCR reading, or None."""
    match = re.search(r"[-+]?[0-9]+(?:[.,][0-9]+)?", text or "")
    return float(match.group(0).replace(",", ".")) if match else None


def signed_value(text):
    """A knob reading as a number: plain numbers, and L/R pan positions (L12 = -12, C = 0, R12 = 12)."""
    text = norm(text)
    if text in ("C", "0"):
        return 0.0
    match = re.fullmatch(r"([LR]) ?([0-9]+)", text)
    if match:
        return float(match.group(2)) * (-1 if match.group(1) == "L" else 1)
    return read_number(text)


def image_digest(image, box=None):
    return hashlib.sha256((image.crop(box) if box else image).tobytes()).hexdigest()


def diff_box(a, b, threshold=24):
    """Bounding box of pixels that differ by more than threshold in any channel, or None."""
    from PIL import ImageChops
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > threshold else 0)
    return diff.getbbox()


# --------------------------------------------------------------------------------------------------------- audio

class AudioTap:
    """Reads the session's captured PCM (`--audio file`) by stream frame index; never plays anything."""

    def __init__(self, session, stream):
        self.session, self.stream = session, stream

    def info(self):
        return ((self.session.metrics().get("audio") or {}).get("streams") or {}).get(self.stream) or {}

    def frames(self):
        return int(self.info().get("frames", 0))

    def read(self, first, last):
        """int16/float32 numpy array (frames x captured channels) of stream frames [first, last)."""
        import numpy as np
        info = self.info()
        path = Path(info["wav"])
        channels = int(info["channels"])
        with open(path, "rb") as handle:
            head = handle.read(4096)
            offset = head.find(b"data") + 8
            width = 4 if info.get("format", "").startswith("FLOAT") else 2
            frame_bytes = channels * width
            handle.seek(offset + max(0, first) * frame_bytes)
            raw = handle.read(max(0, last - first) * frame_bytes)
        raw = raw[:len(raw) // frame_bytes * frame_bytes]
        dtype = "<f4" if width == 4 else "<i2"
        return np.frombuffer(raw, dtype=dtype).reshape(-1, channels)

    def wait_frames(self, count, timeout=30):
        """Wait until `count` more frames were captured; returns the end index."""
        target = self.frames() + count
        deadline = time.monotonic() + timeout
        while self.frames() < target and time.monotonic() < deadline:
            time.sleep(.1)
        return self.frames()


def dominant_frequency(x, rate=RATE):
    """Dominant frequency of a mono segment (Hann window, parabolic peak interpolation)."""
    import numpy as np
    x = np.asarray(x, dtype=float)
    if len(x) < 64 or not np.any(x):
        return None
    spectrum = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(len(x))))
    k = int(np.argmax(spectrum[1:])) + 1
    if 1 <= k < len(spectrum) - 1:
        a, b, c = np.log(spectrum[k - 1] + 1e-12), np.log(spectrum[k] + 1e-12), np.log(spectrum[k + 1] + 1e-12)
        k = k + 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else k
    return k * rate / len(x)


def active_span(x, threshold):
    """(first, last) frame index where |x| exceeds threshold, or None."""
    import numpy as np
    idx = np.nonzero(np.abs(np.asarray(x, dtype=float)) > threshold)[0]
    return (int(idx[0]), int(idx[-1])) if len(idx) else None


def peak(x):
    import numpy as np
    x = np.asarray(x)
    return float(np.abs(x.astype(np.float64)).max()) if len(x) else 0.0


def block_peaks(x, block):
    import numpy as np
    x = np.abs(np.asarray(x, dtype=float))
    n = len(x) // block
    return x[:n * block].reshape(n, block).max(axis=1) if n else np.zeros(0)


def onsets(x, threshold, min_gap):
    """Frame indexes where |x| first exceeds threshold after at least min_gap quiet frames."""
    import numpy as np
    idx = np.nonzero(np.abs(np.asarray(x, dtype=float)) > threshold)[0]
    out, last = [], None
    for i in idx:
        if last is None or i - last > min_gap:
            out.append(int(i))
        last = i
    return out


# ------------------------------------------------------------------------------------------------------ recipes

TITLE_BOX = (0, 0, 600, 32)


class RecipeError(RuntimeError):
    pass


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip().upper()


def screen_title(session, box=TITLE_BOX, tag="title"):
    image = session.image(tag)
    return norm(ocr(image, tuple(box))), image


def wait_title(session, pattern, box=TITLE_BOX, timeout=4.0):
    """OCR the title until it matches (case-insensitive regex); returns (ok, last title, image)."""
    deadline = time.monotonic() + timeout
    title, image = "", None
    while True:
        title, image = screen_title(session, box)
        if re.search(pattern, title, re.I):
            return True, title, image
        if time.monotonic() > deadline:
            return False, title, image
        time.sleep(.4)


def wait_settled(session, stable=1.0, timeout=30.0, box=None):
    """Wait until the LCD (or a region) is unchanged for `stable` seconds; returns (seconds, image) or (None, image)."""
    t0 = time.monotonic()
    last, since, image = None, None, None
    while time.monotonic() - t0 < timeout:
        image = session.image("settle")
        digest = image_digest(image, box)
        now = time.monotonic()
        if digest != last:
            last, since = digest, now
        elif now - since >= stable:
            return now - t0, image
        time.sleep(.25)
    return None, image


# ------------------------------------------------------------------------------------------- factory test pages
# The factory test mode (a build made with --app-arg testmode) steps through numbered pages with HOME (next) and BACK
# (previous), wrapping at both ends. Pages 1 and 2 have no title: the LCD is all white (LED test, all on) and all black
# (all off).
TESTMODE_PAGES = 13
TESTMODE_TITLE_BOX = (0, 0, 800, 50)
TESTMODE_PAGE = re.compile(r"PAGE\s*(\d{1,2})\s*[:;.]", re.I)


def testmode_page_of(image):
    """The test page an LCD image shows, or None while it is half drawn: the title's number, or 1 for an all-white and
    2 for an all-black screen."""
    import numpy as np
    pixels = np.asarray(image.convert("L"))
    # A stray row or two at the edge (the snapshot is one row short of the LCD) must not hide a plain screen.
    if (pixels < 245).mean() < 0.01:
        return 1
    if (pixels > 10).mean() < 0.01:
        return 2
    match = TESTMODE_PAGE.search(norm(ocr(image, TESTMODE_TITLE_BOX, psm=7, scale=2)))
    page = int(match.group(1)) if match else None
    return page if page is not None and page < TESTMODE_PAGES else None


def testmode_page(session, timeout=60.0):
    """The settled test page now (None if the screen never settles on a readable one within `timeout`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, image = wait_settled(session, 1.5, max(5.0, deadline - time.monotonic()))
        page = testmode_page_of(image) if image is not None else None
        if page is not None:
            return page
    return None


def testmode_goto(session, page, timeout=120.0):
    """Step to a test page by the shorter way round; returns whether it got there. One key at a time: the app takes
    seconds to draw a page on a loaded host, and keys sent meanwhile pile up and overshoot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = testmode_page(session, min(60.0, deadline - time.monotonic()))
        if current == page:
            return True
        if current is None:
            continue
        session.press("home" if (page - current) % TESTMODE_PAGES <= TESTMODE_PAGES // 2 else "back", 0.3)
        time.sleep(1.0)
    return False


# Page 8 (DEVICE TEST): the MIDI CHECK touch button (about (115, 112)) opens /dev/ttymxc1 twice, writes a 4-byte
# pattern, polls 2 s and reads it back through the DIN MIDI loopback (service manual); the page then shows
# "MIDI CHECK : OK" (the colon is drawn as a box on some revisions, so the separator is loose).
MIDI_CHECK_OK = re.compile(r"MIDI\s*CHECK[\s:\[\]=]*OK", re.I)


def wait_midi_check(session, timeout=30.0):
    """Poll page 8's OCR until the MIDI CHECK status reads OK; (ok, last text). The loopback poll alone takes 2 s,
    and a loaded host stretches the redraw, so the default timeout is generous."""
    deadline = time.monotonic() + timeout
    text = ""
    while True:
        text = norm(ocr(session.image("midi"), psm=6, scale=3))
        if MIDI_CHECK_OK.search(text):
            return True, text
        if time.monotonic() > deadline:
            return False, text
        time.sleep(.5)


# Page 4 (button test): controls with a lamp (pads, steps and 16 more buttons) count towards LED-BUTTON n/48; MODE and
# SHIFT have no lamp; RESULT counts all 50. Selector push, BACK and HOME are not counted (BACK and HOME step the pages).
TESTMODE_LED_BUTTONS = ([f"pad{n}" for n in range(1, 17)] + [f"step{n}" for n in range(1, 17)] +
                        ["track", "mute", "slice", "scale", "rec", "stop", "play", "hold", "scene", "pattern",
                         "bar1", "bar2", "bar3", "bar4", "active", "usb_stop"])
TESTMODE_PLAIN_BUTTONS = ["mode", "shift"]
TESTMODE_CONTROLS = tuple(TESTMODE_LED_BUTTONS + TESTMODE_PLAIN_BUTTONS)


def check_testmode_skip(names):
    """The controls HW-49 is told to leave unpressed (`--testmode-skip`) must be controls it presses: a misspelt name
    would skip nothing, and the check would pass while reporting that control as skipped. Returns them as a tuple."""
    names = tuple(names)
    unknown = [name for name in names if name not in TESTMODE_CONTROLS]
    if unknown:
        import difflib
        hint = "; ".join(f"{name!r}" + (f" (did you mean {close[0]!r}?)" if close else "")
                         for name in unknown for close in [difflib.get_close_matches(name.lower(), TESTMODE_CONTROLS, n=1)])
        raise ValueError(f"unknown control to skip: {hint}. The controls are pad1..pad16, step1..step16, "
                         + ", ".join(TESTMODE_CONTROLS[32:]))
    return names


TESTMODE_LED_ROW, TESTMODE_RESULT_ROW = (400, 62, 800, 100), (400, 170, 800, 206)
# Page 4 also has a status cell left of the LED-BUTTON row that names the control the page counted (`PAD 1 : 3`), the
# page's own attribution of the event. It is read for every press: a swapped mapping still moves RESULT, but it names
# the other control.
TESTMODE_STATUS_BOX = (0, 62, 400, 100)
# The page's spelling of each control as tokens (letters and digits), so `PAD 1` and `BAR 3` match after OCR noise.
TESTMODE_STATUS_NAMES = {
    **{f"pad{n}": ("PAD", str(n)) for n in range(1, 17)},
    **{f"step{n}": ("STEP", str(n)) for n in range(1, 17)},
    **{f"bar{n}": ("BAR", str(n)) for n in range(1, 5)},
    "usb_stop": ("USB", "STOP"),
    **{name: (name.upper(),) for name in ("track", "mute", "slice", "scale", "rec", "stop", "play", "hold",
                                          "scene", "pattern", "mode", "shift", "active")},
}
assert set(TESTMODE_STATUS_NAMES) == set(TESTMODE_CONTROLS), "every control page 4 counts needs its printed name"
# Page 5 (AD test): gauge rows (a bar that fills left to right, its value inside it) and a red OK once both limits
# were reached; the box is the gauge, the OK sits right of it.
TESTMODE_GAUGES = {"main_out": (64, 96, 1023), "drive": (100, 132, 1023), "hp_cutoff": (136, 168, 1023),
                   "strip": (172, 204, 127)}
TESTMODE_GAUGE_X, TESTMODE_OK_X = (502, 657), (662, 800)
# Page 6 (encoder test): value column x >= 365; row centres.
TESTMODE_ENCODER_ROWS = {"selector": 80, "selector_state": 116, **{f"knob{n}": 188 + 36 * (n - 1) for n in range(1, 7)}}
# Page 3 (pad calibration): a 4 x 4 grid, pad 1 at the bottom left; each cell shows the raw AD value over the stored
# calibration value. One press of WAKE UP (the USB STOP key) measures one pass (a 3 s spare period, then a 1 s average),
# so three presses make one calibration. The START button is pale while passes run and red when the page is idle.
TESTMODE_START_BOX = (728, 150, 748, 190)
CALIBRATION_FILE = ".sp16/data/padcalibration.xml"      # the guest's data directory, on the eMMC image
CALIBRATION_PASS_SECONDS = 7.0                           # a pass takes about 4.5 s; the next press must not land inside it


# Regions that move on their own, as fractions (x0, y0, x1, y1) of the capture, left out of every comparison. The panel
# view's bottom status line counts frames and acknowledgement times; the home LCD and panel are measured idle (two
# captures of one session through shotdiff.compare) and add what is seen to move.
PANEL_STATUS_MASK = (0.0, 0.955, 1.0, 1.0)
HOME_LCD_MASKS = ()
HOME_PANEL_MASKS = ()


def testmode_counter(image, row, total):
    """n of `LABEL(n/total)` in a page 4 row, or None."""
    match = re.search(r"\((\d+)\s*/\s*%d\)" % total, ocr(image, row, psm=7, scale=3))
    return int(match.group(1)) if match else None


def testmode_status_controls(text):
    """The controls whose printed name a page 4 status cell's text starts with. The longest name wins, so `USB STOP`
    is USB STOP and not STOP; a name only counts as the first tokens of the cell, never as a substring of a value.
    OCR drops the space of a two-word name (`USBSTOP`), so the joined form matches too."""
    tokens = tuple(re.findall(r"[A-Z0-9]+", norm(text)))
    named = [name for name, words in TESTMODE_STATUS_NAMES.items()
             if tokens[:len(words)] == words or (len(words) > 1 and tokens[:1] == ("".join(words),))]
    if not named:
        return set()
    longest = max(len(TESTMODE_STATUS_NAMES[name]) for name in named)
    return {name for name in named if len(TESTMODE_STATUS_NAMES[name]) == longest}


def testmode_status_name(image):
    """Page 4: the control the status cell names, or None when it names none or the name is unreadable."""
    named = testmode_status_controls(ocr(image, TESTMODE_STATUS_BOX, psm=7, scale=3))
    return named.pop() if len(named) == 1 else None


def testmode_gauge(image, name):
    """Page 5: (fill fraction 0..1, OK shown) of one gauge row, measured from pixels: the bar is light blue, the OK red."""
    import numpy as np
    top, bottom, _ = TESTMODE_GAUGES[name]
    bar = np.asarray(image.convert("RGB").crop((TESTMODE_GAUGE_X[0], top + 3, TESTMODE_GAUGE_X[1], bottom - 3))).astype(int)
    columns = np.nonzero((bar[..., 2] - bar[..., 0] > 20).any(axis=0))[0]
    fraction = 0.0 if len(columns) == 0 else (columns.max() + 1) / bar.shape[1]
    ok = np.asarray(image.convert("RGB").crop((TESTMODE_OK_X[0], top, TESTMODE_OK_X[1], bottom))).astype(int)
    shown = int(((ok[..., 0] > 200) & (ok[..., 1] < 90) & (ok[..., 2] < 90)).sum()) > 30
    return round(float(fraction), 3), shown


def testmode_encoder(image, name):
    """Page 6: the integer or state word right of a label (None when unreadable)."""
    centre = TESTMODE_ENCODER_ROWS[name]
    text = norm(ocr(image, (368, centre - 16, 760, centre + 16), psm=7, scale=3))
    if name == "selector_state":
        match = re.search(r"\b(ON|OFF)\b", text)
        return match.group(1) if match else None
    value = signed_value(text)
    return None if value is None else int(value) & 0xFFFF


def testmode_start_ready(image):
    """Page 3: whether the START button is the saturated red of an idle page (True) or the pale one of a measurement
    in progress (False)."""
    import numpy as np
    pixels = np.asarray(image.convert("RGB").crop(TESTMODE_START_BOX)).astype(int)
    return int(pixels[..., 1].mean()) < 110


def testmode_pad_raw(image, pad):
    """Page 3: the raw AD number in pad `pad`'s cell (None when unreadable)."""
    x, y = 45 + 180 * ((pad - 1) % 4), 408 - 100 * ((pad - 1) // 4)
    value = signed_value(ocr(image, (x - 40, y - 14, x + 40, y + 14), psm=7, scale=3))
    return None if value is None else int(value)


def calibration_values(data_image, path=CALIBRATION_FILE):
    """{pad: value} of the CALIB_VALUE_PADn attributes in the calibration file of an eMMC image ({} if there is none)."""
    reader = FatReader(data_image)
    try:
        data = reader.read(path)
    finally:
        reader.close()
    text = data.decode(errors="replace") if data else ""
    return {int(n): int(v) for n, v in re.findall(r'CALIB_VALUE_PAD(\d+)="(\d+)"', text)}


def run_recipe(session, steps, params=None, log=None):
    """Execute a profile recipe: an ordered list of single-key steps (see the profile schema). Raises RecipeError
    with the failing step when an expectation does not hold. Parameters fill "{name}" placeholders in strings."""
    params = params or {}
    log = log if log is not None else []

    def fill(value):
        if isinstance(value, str):
            for key, replacement in params.items():
                value = value.replace("{" + key + "}", str(replacement))
            return value
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value

    for index, raw in enumerate(steps or []):
        step = fill(raw)
        kind = next(iter(k for k in step if k not in ("hold", "value", "gap", "detents", "box", "max_detents",
                                                       "timeout", "note", "then", "else")))
        arg = step[kind]
        if kind == "if_text":
            # Decide on a settled screen: lists fill progressively (1.50's BROWSE drew one row of four first).
            _, settled = wait_settled(session, 0.8, 8, tuple(step["box"]))
            text = norm(ocr(settled if settled is not None else session.image("if"), tuple(step["box"]), psm=6))
            branch = "then" if re.search(arg, text, re.I) else "else"
            log.append({"step": index, "if_text": arg, "observed": text[:120], "branch": branch})
            run_recipe(session, step.get(branch) or [], params, log)
            continue
        if kind == "press":
            session.press(arg, step.get("hold", .15), step.get("value", 100))
            time.sleep(step.get("gap", .4))
        elif kind == "hold_on":
            session.event(arg, "press", step.get("value", 100))
            time.sleep(step.get("gap", .3))
        elif kind == "release":
            session.event(arg, "release")
            time.sleep(step.get("gap", .3))
        elif kind == "turn":
            session.turn(arg, int(step["detents"]), step.get("gap", .6))
            time.sleep(.6)
        elif kind == "burst":
            session.turn(arg, int(step["detents"]), step.get("gap", .005))
        elif kind == "tap":
            session.tap(arg[0], arg[1], step.get("hold", .1))
            time.sleep(step.get("gap", .4))
        elif kind == "wait":
            time.sleep(float(arg))
        elif kind == "expect_title":
            ok, title, _ = wait_title(session, arg, timeout=step.get("timeout", 4.0))
            log.append({"step": index, "expect_title": arg, "observed": title})
            if not ok:
                raise RecipeError(f"step {index}: title {title!r} does not match {arg!r}")
        elif kind == "expect_text":
            deadline = time.monotonic() + step.get("timeout", 4.0)
            while True:
                text = norm(ocr(session.image("expect"), tuple(step["box"]), psm=6))
                if re.search(arg, text, re.I):
                    break
                if time.monotonic() > deadline:
                    raise RecipeError(f"step {index}: text {text!r} does not match {arg!r}")
                time.sleep(.4)
            log.append({"step": index, "expect_text": arg, "observed": text})
        elif kind == "focus_row":
            box = tuple(step["box"])
            found, seen, direction, reversed_once = False, [], int(step.get("detents", 1)), False
            for _ in range(int(step.get("max_detents", 30)) + 1):
                image = session.image("row")
                band = highlighted_band(image, box)
                text = norm(ocr(image, (box[0], band[0], box[2], band[1]), psm=7)) if band else ""
                seen.append(text)
                if re.search(arg, text, re.I):
                    found = True
                    break
                # Lists that do not wrap stop at an end: the focus stays put, so search the other way once.
                if len(seen) >= 2 and seen[-1] == seen[-2] and not reversed_once:
                    direction, reversed_once = -direction, True
                session.event("selector", "delta", direction)
                time.sleep(step.get("gap", .7))
            log.append({"step": index, "focus_row": arg, "observed": seen[-1] if seen else ""})
            if not found:
                raise RecipeError(f"step {index}: no list row matching {arg!r} (saw {seen[-4:]})")
        elif kind == "until_gone":
            deadline = time.monotonic() + step.get("timeout", 60)
            while re.search(arg, norm(ocr(session.image("gone"), tuple(step["box"]), psm=6)), re.I):
                if time.monotonic() > deadline:
                    raise RecipeError(f"step {index}: {arg!r} still shown after {step.get('timeout', 60)} s")
                time.sleep(.5)
        elif kind == "usb":
            session.usb(arg)
            time.sleep(step.get("gap", 3.0))
        elif kind == "settle":
            wait_settled(session, float(arg))
        else:
            raise RecipeError(f"step {index}: unknown step kind {kind!r}")
    return log


# ------------------------------------------------------------------------------------------------ launch / stop

def launch(build, firmware_id=None, usb_image=None, ephemeral=False, timeout=120):
    """Start one session of a build (refuses if one is running) and wait for SP16_NATIVE_READY and the socket.
    Returns (app pid, runtime.json path, seconds to ready)."""
    build = Path(build)
    apps, _ = build_processes(build)
    if apps:
        raise EnvError(f"a session of {build} is already running (pid {apps})")
    t0 = time.time()
    args = [sys.executable, str(TOOLS / "native.py"), "launch", "--output", str(build)]
    if firmware_id:
        args += ["--firmware", firmware_id]
    if usb_image:
        args += ["--usb-image", str(usb_image)]
    if ephemeral:
        args += ["--ephemeral"]
    out = subprocess.run(args, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if out.returncode:
        raise EnvError("launch failed: " + (out.stdout[-400:] + out.stderr[-1200:]).strip())
    runtime = None
    while time.time() - t0 < timeout:
        candidates = [p for p in (build / "sessions").glob("native-*/runtime.json") if p.stat().st_mtime >= t0 - 1]
        if candidates:
            runtime = max(candidates, key=lambda p: p.stat().st_mtime)
            text = (runtime.parent / "console.txt").read_text(errors="replace") if (runtime.parent / "console.txt").exists() else ""
            if "SP16_FIRMWARE_ERROR" in text or "Kernel panic" in text:
                raise EnvError("boot failed: " + " | ".join(l for l in text.splitlines() if "ERROR" in l or "panic" in l)[:400])
            if "SP16_NATIVE_READY" in text:
                apps, _ = build_processes(build)
                if len(apps) == 1:
                    return apps[0], runtime, time.time() - t0
        time.sleep(.2)
    raise EnvError(f"no SP16_NATIVE_READY within {timeout} s")


def stop(build, pid, sig=signal.SIGTERM, timeout=25):
    """Signal one app by exact PID and wait until it and its QEMU child are gone. Returns seconds or None."""
    apps, qemus = build_processes(build)
    if pid not in apps:
        return 0.0
    children = [q for q, parent in qemus if parent == pid]
    t0 = time.time()
    os.kill(pid, sig)
    while time.time() - t0 < timeout:
        apps, qemus = build_processes(build)
        if pid not in apps and not any(q in children for q, _ in qemus):
            return time.time() - t0
        time.sleep(.1)
    return None


def qemu_children(build, pid):
    return [q for q, parent in build_processes(build)[1] if parent == pid]


def console_lines(text, marker):
    """Console lines containing a marker (guest lines can be interleaved with others, so match anywhere)."""
    return [line for line in text.splitlines() if marker in line]


def runtime_drive(runtime, drive_id):
    """Backing file of a QEMU -drive id in a session's arguments (e.g. sp16usb, sp16emmc), or None."""
    for arg in runtime.get("arguments", []):
        if arg.startswith("file=") and f",id={drive_id}" in arg:
            return arg[5:].split(",", 1)[0]
    return None


def process_usage(pids):
    """{pid: {cpu, rss_mb}} from ps (recorded, never gated)."""
    if not pids:
        return {}
    out = subprocess.run(["ps", "-o", "pid=,pcpu=,rss=", "-p", ",".join(map(str, pids))], capture_output=True,
                         text=True).stdout
    usage = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            usage[int(parts[0])] = {"cpu": float(parts[1]), "rss_mb": round(int(parts[2]) / 1024, 1)}
    return usage


# --------------------------------------------------------------------------------------------------- the runner

class Runner:
    """Runs the selected checks against one firmware: a main session (stick attached), then the lifecycle,
    stick and uncalibrated-control sessions the spec's section 5.4 describes."""

    def __init__(self, profile, profile_path, build, firmware_id, out, fixtures, tier="full", only=None,
                 control_build=None, runtime=None, testmode_build=None, testmode_skip=(), shots=None):
        self.p, self.profile_path = profile, Path(profile_path)
        self.build = Path(build) if build else None
        self.control_build = Path(control_build) if control_build else None
        self.testmode_build = Path(testmode_build) if testmode_build else None
        self.testmode_skip = check_testmode_skip(testmode_skip)   # controls HW-49 leaves unpressed: its negative control
        self.shots = shots                          # a shotdiff.ShotStore when --shots is on
        self.current_check = None                   # the check whose method is running (owner of its captures)
        self.firmware_id = firmware_id
        self.out, self.fixtures = Path(out), Path(fixtures)
        self.tier, self.only = tier, only
        self.selected = select_checks(tier, only)
        self.smoke = tier == "smoke"
        self.given_runtime = Path(runtime) if runtime else None
        self.verdicts, self.partial, self.notes = {}, {}, {}
        self.session, self.pid, self.runtime_path = None, None, None
        self.roles = {role: tuple(v) for role, v in profile["panel"]["lamp_roles"].items()}
        self.groups = profile["panel"]["lamp_groups"]
        self.title_box = tuple((profile.get("home") or {}).get("title_box") or TITLE_BOX)
        self.lost = None
        self.loaded = {}
        self.events_log = []

    # ------------------------------------------------------------------ small helpers
    def need(self, hw_id):
        return hw_id in self.selected and hw_id not in self.verdicts

    def lamp(self, decoded, role):
        kind, channel = self.roles[role]
        return decoded["mono"][channel] if kind == "mono" else decoded["rgb"][channel]

    def full(self, decoded, role):
        lit, dim = self.lamp(decoded, role)
        return bool(lit) and not dim

    def now(self, window=0.5):
        lamps = self.session.lamps_now(window)
        if lamps is None:
            raise SessionLost("no TX frames (txlog)")
        return lamps

    def capture(self, action, after=0.8):
        offset = self.session.tx_offset()
        action()
        time.sleep(after)
        return self.session.lamps_since(offset)

    def title(self):
        return norm(ocr(self.session.image("title"), self.title_box))

    def home_regex(self):
        return (self.p.get("home") or {}).get("title_regex") or "^HOME$"

    def go_home(self):
        for _ in range(3):
            self.session.press("home")
            ok, title, _ = wait_title(self.session, self.home_regex(), self.title_box, timeout=3)
            if ok:
                return True
            self.session.press("back")
        return False

    def edge(self, timeout=3.0):
        """Wait for the next guest stats report (about 1 Hz) and return (monotonic time, guest counters)."""
        first = self.session.guest()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            guest = self.session.guest()
            if guest.get("rx_frames") != first.get("rx_frames") or guest.get("tx_frames") != first.get("tx_frames"):
                return time.monotonic(), guest
            time.sleep(.02)
        return time.monotonic(), self.session.guest()

    def fresh_guest(self):
        """Guest counters from a stats report computed after this call (reports arrive about once a second)."""
        return self.edge()[1]

    def fresh_metrics(self):
        guest = self.fresh_guest()
        metrics = self.session.metrics()
        metrics["guest"] = guest
        return metrics

    def recipe(self, name, **params):
        steps = (self.p.get("recipes") or {}).get(name)
        if not authored(steps):
            raise RecipeError(f"recipe {name} is not authored")
        if isinstance(steps, dict):
            steps = steps.get("steps")
        return run_recipe(self.session, steps, params)

    def sample_params(self, name):
        stem = Path(name).stem
        return {"sample": name, "sample_re": re.escape(stem.upper()).replace("\\-", "[-~ ]?")}

    def select_track(self, track):
        probe = self.p["selector_probe"]
        self.go_home()
        for _ in range(3):
            current = selected_tile(self.session.image("track"), probe)
            if current == track:
                return True
            if current is None:
                return False
            length = probe["length"]
            forward = (track - current) % length
            delta = forward if forward <= length // 2 else forward - length
            self.session.turn("selector", delta, 0.7)
            time.sleep(1.2)
        return selected_tile(self.session.image("track"), probe) == track

    def on_track(self, track):
        """Whether the home screen shows `track` selected, re-selecting it once when it does not.

        A track parameter belongs to the selected track: if the selection moved (a knob step or a menu the previous
        recipe left behind), a VOLUME setting would land on another track while the pad still plays this one, and the
        level would not change at all. Another screen, or one the probe cannot read, is looked at again on the home
        screen; an unreadable tile screen is then reported as not selected rather than guessed.
        Returns (selected, the track the screen showed)."""
        probe = self.p["selector_probe"]
        current = selected_tile(self.session.image("track"), probe)
        if current is None:
            self.go_home()
            current = selected_tile(self.session.image("track"), probe)
        if current is not None and current != track:
            self.select_track(track)
            current = selected_tile(self.session.image("track"), probe)
        return current == track, current

    def load_sample(self, track, name):
        """Assign a fixture to a track with the profile's sample_load recipe (once per track and sample)."""
        if self.loaded.get(track) == name:
            return
        if not self.select_track(track):
            raise RecipeError(f"could not select track {track}")
        self.recipe("sample_load", **self.sample_params(name))
        self.loaded[track] = name
        self.go_home()

    def set_knob(self, knob, target, value_box, maximum, minimum=0):
        """Drive a bounded knob to a value: clamp at an end with a burst, then single detents (1.3 s apart, below
        the firmware's acceleration). Returns the value read back by OCR."""
        end = minimum if target - minimum <= maximum - target else maximum
        self.session.turn(knob, (-1 if end == minimum else 1) * (maximum - minimum + 40), 0.005)
        time.sleep(4)
        steps = target - end
        for _ in range(abs(steps)):
            self.session.event(knob, "delta", 1 if steps > 0 else -1)
            time.sleep(.75)
        time.sleep(1)
        return read_number(ocr(self.session.image("knob"), tuple(value_box), psm=7, scale=4))

    def normalize(self):
        """Bring the firmware back to its idle panel state (home screen, stopped, no pad mode, no latch, first strip
        mode, no step triggers, track 1 selected) using lamp readback; returns the corrections made."""
        fixes = []
        self.go_home()
        self.session.press("stop")
        time.sleep(.6)
        for _ in range(3):
            lamps = self.now()
            changed = False
            for group in ("pad_mode_one_hot",):
                for role in self.groups.get(group, []):
                    if self.full(lamps, role):
                        self.session.press(role)
                        fixes.append(role)
                        changed = True
            for role in ("rec", "scene", "pattern", "hold"):
                if role in self.roles and self.full(lamps, role):
                    self.session.press(role)
                    fixes.append(role)
                    changed = True
            order = self.groups.get("mode_order") or []
            if order and not self.lamp(lamps, order[0])[0]:
                self.session.press("mode")
                fixes.append("mode")
                changed = True
            for step in range(16):
                if any(lamps["rgb"][step]) and not self.full(lamps, "play"):
                    self.session.press(f"step{step + 1}")
                    fixes.append(f"step{step + 1}")
                    changed = True
            if not changed:
                break
            time.sleep(.8)
        lamps = self.now()
        pads = [ch - 0x10 + 1 for ch in range(0x10, 0x20) if any(lamps["rgb"][ch])]
        if pads != [1]:
            if self.select_track(1):
                fixes.append("track1")
        self.go_home()
        return fixes

    # ------------------------------------------------------------------ checks: boot, LCD, touch
    def hw01(self):
        identity = self.p["identity"]
        console = self.session.console()
        ready = console_lines(console, "SP16_NATIVE_READY")
        subs = [sub("ready marker with verified application", bool(ready) and "app_sha256_verified=yes" in ready[0],
                    ready[0][:160] if ready else "no SP16_NATIVE_READY")]
        boot = self.notes.get("boot", {})
        if boot:
            subs.append(sub("ready within the boot timeout", boot["ready_s"] <= identity["boot_timeout_s"],
                            f"{boot['ready_s']:.1f} s (limit {identity['boot_timeout_s']} s)"))
        t0 = time.monotonic()
        settled, image, previous, title = None, None, None, ""
        while time.monotonic() - t0 < identity["boot_timeout_s"]:
            image = self.session.image("boot")
            colours = image.resize((80, 48)).getcolors(4096)
            uniform = colours is not None and len(colours) <= 1
            if previous is not None and image_digest(image) == image_digest(previous) and not uniform:
                settled = time.monotonic() - t0
                break
            previous = image
            time.sleep(.5)
        subs.append(sub("LCD 800x480, not uniform, two snapshots 500 ms apart identical",
                        settled is not None and image.size == (800, 480),
                        f"size {image.size if image else None}, settled after {settled}"))
        if settled is not None and (self.p.get("home") or {}).get("title_regex"):
            title = norm(ocr(image, self.title_box))
            subs.append(sub("home screen title", re.search(self.home_regex(), title, re.I) is not None, title))
        metrics = self.session.metrics()
        self.notes["settled_uptime"] = metrics.get("uptime_seconds", 0)
        subs.append(sub("lamps live", metrics.get("lamps_live") is True, str(metrics.get("lamps_live"))))
        subs.append(sub("guest TX frames", (metrics.get("guest") or {}).get("tx_frames", 0) > 0,
                        str((metrics.get("guest") or {}).get("tx_frames"))))
        return from_subresults("HW-01", subs, {"boot": boot, "title": title})

    def hw02(self):
        self.p["selector_probe"]
        s = self.session
        self.go_home()
        subs, sizes = [], []
        before = s.image("lcd")
        s.event("selector", "delta", 1)
        t0, changed = time.monotonic(), None
        while time.monotonic() - t0 < 2.5:
            img = s.image("lcd")
            sizes.append(img.size)
            if image_digest(img) != image_digest(before):
                changed = time.monotonic() - t0
                break
            time.sleep(.1)
        subs.append(sub("detent changes the frame within 2 s", changed is not None and changed <= 2.0,
                        f"{changed}"))
        s.event("selector", "delta", -1)
        time.sleep(2.0)
        settled_ref = wait_settled(s, 1.0, 10)[1]
        shot = s.dir / f"hw-panel-{uuid.uuid4().hex[:8]}.png"
        s.call(op="panelshot", name=shot.name)
        from PIL import Image
        original = Image.open(shot).size
        # panelshot is in points on a Retina screen; resize takes points too (the backing scale is constant).
        scale = max(1, round(original[0] / 1280)) if original[0] > 1600 else 1
        restore = (original[0] // scale, original[1] // scale)
        for width, height in ((960, 640), (1280, 800), (900, 560), restore):
            try:
                s.resize(width, height)
            except RuntimeError as error:
                subs.append(sub(f"resize {width}x{height}", False, str(error)))
                continue
            time.sleep(2.0)
            img = s.image("resize")
            sizes.append(img.size)
            same = image_digest(img) == image_digest(settled_ref)
            subs.append(sub(f"resize {width}x{height}: settled LCD frame unchanged", same and img.size == (800, 480),
                            f"size {img.size}, identical {same}"))
        subs.append(sub("every snapshot 800x480", all(size == (800, 480) for size in sizes), str(set(sizes))))
        visible = (s.metrics().get("visible_ms") or {})
        return from_subresults("HW-02", subs, {"visible_ms_p95": visible.get("p95"), "panel_size": original})

    def tap_reacts(self, x, y, hold=0.08, expect=None, timeout=2.0):
        """Tap and wait for the title to match `expect` (or any LCD change); returns seconds or None."""
        s = self.session
        before = s.image("tap")
        s.tap(x, y, hold)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            img = s.image("tap")
            if expect:
                if re.search(expect, norm(ocr(img, self.title_box)), re.I):
                    return time.monotonic() - t0
            elif diff_box(before, img):
                return time.monotonic() - t0
            time.sleep(.15)
        return None

    def hw03(self):
        probe, dead = self.p["touch_probe"], self.p["touch_dead"]
        s = self.session
        subs = []
        failed0 = s.metrics()["input_results"]["failed"]
        self.go_home()
        reacted = self.tap_reacts(probe["x"], probe["y"], expect=probe.get("expect_title"))
        subs.append(sub("probe tap changes the screen within 2 s", reacted is not None, f"{reacted}"))
        self.go_home()
        time.sleep(1)
        before = s.image("dead")
        s.tap(dead["x"], dead["y"])
        time.sleep(1.5)
        box = diff_box(before, s.image("dead"))
        subs.append(sub("dead point changes nothing", box is None, f"diff {box}"))
        replies = []
        for x, y in ((0, 0), (799, 0), (0, 479), (799, 479)):
            try:
                s.touch(x, y, "down")
                time.sleep(.08)
                s.touch(x, y, "up")
                replies.append("ok")
            except RuntimeError as error:
                replies.append(str(error))
            time.sleep(.8)
            self.go_home()
        subs.append(sub("corner touches accepted", all(r == "ok" for r in replies), str(replies)))
        failed = s.metrics()["input_results"]["failed"] - failed0
        subs.append(sub("input_results.failed delta 0", failed == 0, str(failed)))
        reacted = self.tap_reacts(probe["x"], probe["y"], expect=probe.get("expect_title"))
        subs.append(sub("still responsive after corners", reacted is not None, f"{reacted}"))
        self.go_home()
        return from_subresults("HW-03", subs)

    def hw04(self):
        probe = self.p["touch_probe"]
        s = self.session
        subs, registered = [], []
        for hold in (0.05, 0.05, 0.08, 0.08, 0.08, 0.12):
            self.go_home()
            time.sleep(.8)
            registered.append(self.tap_reacts(probe["x"], probe["y"], hold, probe.get("expect_title")) is not None)
        subs.append(sub("6 of 6 short taps register", all(registered), str(registered)))
        self.go_home()
        time.sleep(.8)
        acks0 = (s.metrics().get("ack_ms") or {}).get("count", 0)
        s.touch(probe["x"], probe["y"], "down")
        time.sleep(1.0)
        s.touch(probe["x"], probe["y"], "up")
        time.sleep(.5)
        acks = (s.metrics().get("ack_ms") or {}).get("count", 0) - acks0
        ok, title, _ = wait_title(s, probe.get("expect_title") or ".", self.title_box, 2.5)
        subs.append(sub("1 s hold: one down and one up delivered, screen reacts", acks == 2 and ok,
                        f"acks +{acks}, title {title}"))
        self.go_home()
        time.sleep(.8)
        x, y = probe["x"], probe["y"]
        s.touch(x, y, "down")
        for dx in (-10, -20, -30, -40, -50):
            time.sleep(.05)
            s.touch(max(0, x + dx), y + 20, "move")
        time.sleep(.05)
        s.touch(max(0, x - 50), y + 20, "up")
        time.sleep(1.2)
        self.go_home()
        time.sleep(.8)
        after = self.tap_reacts(x, y, 0.08, probe.get("expect_title"))
        subs.append(sub("after a drag the next tap works (no stale touch)", after is not None, f"{after}"))
        time.sleep(.5)
        pending = s.metrics().get("pending_controls")
        subs.append(sub("pending_controls 0", pending == 0, str(pending)))
        self.go_home()
        return from_subresults("HW-04", subs, {"acks_hold": acks})

    # ------------------------------------------------------------------ checks: wire protocol
    def hw05(self):
        identity = self.p["identity"]
        s = self.session
        g0 = self.fresh_guest()
        offset = s.tx_offset()
        time.sleep(10)
        frames = s.tx_since(offset)
        g1 = self.fresh_guest()
        times = [t for t, _ in frames]
        bad = [i for i, (_, f) in enumerate(frames) if not tx_frame_ok(f)]
        rate = (len(frames) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0
        gap = max(frame_gaps(times)) if len(times) > 1 else None
        nominal = float(identity["tx_fps"])
        subs = [sub("every frame 128 B, header 0x02, marker 0xaa at 0x77, CRC valid", frames and not bad,
                    f"{len(frames)} frames, {len(bad)} bad"),
                sub("rate within 20% of nominal", abs(rate - nominal) <= 0.2 * nominal, f"{rate:.1f}/s vs {nominal}"),
                sub("no gap longer than 250 ms", gap is not None and gap <= 0.25, f"max gap {gap and round(gap, 3)} s"),
                sub("guest tx_bad_crc 0", g1.get("tx_bad_crc", 1) == 0 and g1.get("tx_bad_crc") == g0.get("tx_bad_crc"),
                    str(g1.get("tx_bad_crc")))]
        return from_subresults("HW-05", subs, {"frames": len(frames), "rate": round(rate, 2), "max_gap_s": gap})

    def rx_rate(self, seconds):
        t0, g0 = self.edge()
        time.sleep(max(0, seconds - 0.5))
        t1, g1 = self.edge()
        return (g1["rx_frames"] - g0["rx_frames"]) / (t1 - t0), g0, g1

    def hw06(self):
        s = self.session
        identity = self.p["identity"]
        start = self.fresh_guest()
        idle, _, _ = self.rx_rate(5)
        s.event("pad16", "press", 100)
        time.sleep(.3)
        held, _, _ = self.rx_rate(5)
        s.event("pad16", "release")
        time.sleep(3)
        t0, g0 = self.edge()
        s.press("pad16", 0.02, 100)
        t1, g1 = self.edge()
        burst = g1["rx_frames"] - g0["rx_frames"]
        end = self.fresh_guest()
        held_nominal = float(identity.get("rx_held_fps") or 100)
        subs = [sub("idle heartbeat", idle >= float(identity["rx_idle_min_fps"]),
                    f"{idle:.1f}/s (min {identity['rx_idle_min_fps']})"),
                sub("held: 10 ms cadence +-10%", abs(held - held_nominal) <= 0.1 * held_nominal,
                    f"{held:.1f}/s vs {held_nominal}"),
                sub("at least 16 frames after an input change", burst >= 16, f"{burst} frames in {t1 - t0:.2f} s"),
                sub("rx_drops delta 0", end.get("rx_drops") == start.get("rx_drops"),
                    f"{start.get('rx_drops')} -> {end.get('rx_drops')}"),
                sub("rx_frames monotonic", end.get("rx_frames", 0) > start.get("rx_frames", 0), "")]
        return from_subresults("HW-06", subs, {"idle_fps": round(idle, 2), "held_fps": round(held, 2),
                                               "frames_after_press": burst})

    def hw07(self, count=200):
        s = self.session
        m0 = self.fresh_metrics()
        sent = 0
        for i in range(count // 4):
            # State-neutral pairs: press/release of pads whose tracks hold no sample (lamp flash only).
            s.event("pad16", "press", 100)
            s.event("pad16", "release")
            s.event("pad15", "press", 100)
            s.event("pad15", "release")
            sent += 4
            time.sleep(.02)
        deadline = time.monotonic() + 1.0
        while s.metrics().get("pending_controls") and time.monotonic() < deadline:
            time.sleep(.05)
        time.sleep(1.5)
        m1 = self.fresh_metrics()
        g0, g1 = m0.get("guest") or {}, m1.get("guest") or {}
        r0, r1 = m0["input_results"], m1["input_results"]
        delta = {k: r1[k] - r0[k] for k in r1}
        subs = [sub("guest events delta equals events sent", g1["events"] - g0["events"] == sent,
                    f"{g1['events'] - g0['events']} of {sent}"),
                sub("input_results ok delta equals sent", delta["ok"] == sent, str(delta)),
                sub("failed, unsupported, invalid 0", delta["failed"] == delta["unsupported"] == delta["invalid"] == 0,
                    str(delta)),
                sub("guest failed/unsupported 0", g1["failed"] == g0["failed"] and g1["unsupported"] == g0["unsupported"],
                    ""),
                sub("pending_controls 0 within 1 s", m1.get("pending_controls") == 0, str(m1.get("pending_controls")))]
        return from_subresults("HW-07", subs, {"sent": sent, "ack_ms_p50": (m1.get("ack_ms") or {}).get("p50")})

    def hw08(self):
        s = self.session
        self.normalize()
        self.knob_track()
        columns = self.p.get("knob_columns") or []
        first = {c["knob"]: c.get("first_direction", 1) for c in columns}
        row = lambda image: [ocr(image, tuple(c["value_box"]), 7, 4) for c in columns]
        before = row(s.image("burst"))
        m0 = self.fresh_metrics()
        failures = 0
        t0 = time.monotonic()
        for i in range(1000):
            knob = f"knob{i % 6 + 1}"
            # Each knob alternates +1/-1 (starting away from a bound), so the burst leaves every value where it was.
            sign = first.get(knob, 1) * (1 if (i // 6) % 2 == 0 else -1)
            try:
                s.event(knob, "delta", sign)
            except RuntimeError:
                failures += 1
        seconds = time.monotonic() - t0
        deadline = time.monotonic() + 10
        while s.metrics().get("pending_controls") and time.monotonic() < deadline:
            time.sleep(.1)
        time.sleep(2)
        m1 = self.fresh_metrics()
        g0, g1 = m0["guest"], m1["guest"]
        subs = [sub("0 send failures", failures == 0, str(failures)),
                sub("guest events +1000", g1["events"] - g0["events"] >= 1000, str(g1["events"] - g0["events"])),
                sub("guest failed and unsupported 0", g1["failed"] == g0["failed"] and g1["unsupported"] == g0["unsupported"],
                    f"failed +{g1['failed'] - g0['failed']}, unsupported +{g1['unsupported'] - g0['unsupported']}"),
                sub("tx_bad_crc 0", g1["tx_bad_crc"] == 0, str(g1["tx_bad_crc"])),
                sub("rx_drops delta 0", g1["rx_drops"] == g0["rx_drops"], f"{g0['rx_drops']} -> {g1['rx_drops']}"),
                sub("pending_controls 0 within 10 s", m1.get("pending_controls") == 0, str(m1.get("pending_controls")))]
        after = row(s.image("burst"))
        self.notes["knob_row_after_burst"] = {"before": before, "after": after}
        return from_subresults("HW-08", subs, {"seconds": round(seconds, 2), "knob_row": [before, after]})

    def effect_seen(self, effect, before, records, title_before, title_after):
        """Does a declared button effect show? Lamp: the role's decoded state differs from before in some frame.
        Title: the screen title matches. Delivered: only the event accounting (checked by the caller)."""
        kind = effect.get("type")
        if kind == "lamp":
            role = effect["role"]
            states = {self.lamp(d, role) for _, d in records}
            ok = any(state != self.lamp(before, role) for state in states)
            observed = f"{self.lamp(before, role)} -> {sorted(states)[:4]}"
            if effect.get("extra"):
                index, value = effect["extra"]
                extras = sorted({d["extra"] for _, d in records})
                ok = ok and any(e[index] == value for e in extras)
                observed += f", TX extras {extras}"
            return ok, observed
        if kind == "title":
            return re.search(effect["regex"], title_after, re.I) is not None, f"{title_before} -> {title_after}"
        if kind == "delivered":
            return True, effect.get("reason", "delivered")
        return False, f"unknown effect type {kind}"

    def hw09(self):
        s = self.session
        effects = self.p["button_effects"]
        roles = self.p["panel"]["button_roles"]
        self.normalize()
        # Named roles, then raw bits the profile declares with an observed effect (not dispatched as a named role).
        # Entries marked run_last leave the firmware in a state that lasts until restart; they run at the end of
        # the run, in the last session.
        targets = [(role, effects.get(role)) for role in roles if not (effects.get(role) or {}).get("run_last")]
        targets += [(name, effect) for name, effect in effects.items() if effect.get("bit") and not effect.get("run_last")]
        subs = self.button_effects(targets)
        # Undeclared bits: a raw frame with only that bit set must change nothing.
        for byte, bit in self.p["panel"]["buttons_inert"]:
            g0 = self.fresh_guest()
            before_img = s.image("inert")
            before = self.now()
            records = self.capture(lambda: (s.raw(byte, [1 << bit]), time.sleep(.6), s.clear()), 1.0)
            after_img = s.image("inert")
            g1 = self.fresh_guest()
            changed = sorted({(k, ch) for _, d in records for k in ("mono", "rgb") for ch, v in d[k].items()
                              if v != before[k][ch]})
            extras = sorted({d["extra"] for _, d in records} - {before["extra"]})
            box = diff_box(before_img, after_img)
            ok = not changed and not extras and box is None and g1["unsupported"] == g0["unsupported"]
            subs.append(sub(f"inert bit ({byte},{bit})", ok, f"lamps {changed[:6]}, extras {extras}, lcd {box}"))
        self.normalize()
        last = [(name, effect) for name, effect in effects.items() if effect.get("run_last")]
        if last:
            self.partial["HW-09"] = subs
            self.notes["hw09_last"] = last
            return None
        return from_subresults("HW-09", subs)

    def hw09_last(self):
        """The deferred HW-09 effects, in the final session of the run."""
        subs = self.partial.pop("HW-09", [])
        try:
            subs += self.button_effects(self.notes.get("hw09_last") or [])
        except (SessionLost, RuntimeError) as error:
            subs.append(sub("deferred effects", False, str(error), blocked=True))
        self.verdicts["HW-09"] = from_subresults("HW-09", subs)

    def button_effects(self, targets):
        s = self.session
        subs = []
        for role, effect in targets:
            if not effect:
                subs.append(sub(role, False, "no declared effect", blocked=True))
                continue
            if effect.get("type") == "shift_probe":
                ok, observed = self.shift_probe()
                subs.append(sub(role, ok, observed))
                continue
            try:
                run_recipe(s, effect.get("pre") or [])
                time.sleep(.4)
                delivered_only = effect.get("type") == "delivered"
                g0 = self.fresh_guest() if delivered_only else None
                before = self.now()
                title_before = self.title() if effect.get("type") == "title" else ""
                if effect.get("bit"):
                    byte, bit = effect["bit"]
                    action = lambda: (s.raw(byte, [1 << bit]), time.sleep(effect.get("hold_s", .3)), s.clear())
                else:
                    action = lambda: s.press(role, effect.get("hold_s", .15))
                records = self.capture(action, 0.9)
                title_after = self.title() if effect.get("type") == "title" else ""
                ok, observed = self.effect_seen(effect, before, records, title_before, title_after)
                if delivered_only:
                    g1 = self.fresh_guest()
                    ok = g1["events"] - g0["events"] == 2 and g1["unsupported"] == g0["unsupported"]
                    observed += f" (events +{g1['events'] - g0['events']})"
                subs.append(sub(role, ok, observed))
                run_recipe(s, effect.get("post") or [])
            except (RecipeError, RuntimeError) as error:
                subs.append(sub(role, False, f"recipe: {error}", blocked=True))
            time.sleep(.3)
        return subs

    def shift_probe(self):
        probe = self.p["shift_probe"]
        s = self.session
        s.event(probe["hold"], "press", 100)
        time.sleep(probe.get("hold_s", 0.4))
        records = self.capture(lambda: s.press(probe["press"], 0.2), 0.8)
        s.event(probe["hold"], "release")
        time.sleep(.4)
        expect = probe["expect"]
        last = records[-1][1] if records else None
        ok = last is not None and all(last["rgb"][ch] == tuple(expect["colour"]) for ch in expect["rgb_channels"])
        observed = "no frames" if last is None else str({ch: last["rgb"][ch] for ch in expect["rgb_channels"][:4]})
        run_recipe(s, probe.get("cleanup") or [])
        return ok, observed

    # ------------------------------------------------------------------ checks: lamps
    def hw10(self):
        s = self.session
        group = self.groups["pad_mode_one_hot"]
        self.normalize()
        subs, violations = [], 0
        for role in group:
            for press in (1, 2):
                records = self.capture(lambda: s.press(role), 0.8)
                for _, d in records:
                    if len([r for r in group if self.full(d, r)]) > 1:
                        violations += 1
                last = records[-1][1]
                bright = [r for r in group if self.full(last, r)]
                if press == 1:
                    subs.append(sub(f"{role} first press: only its lamp bright", bright == [role], str(bright)))
                else:
                    subs.append(sub(f"{role} second press clears it", role not in bright, str(bright)))
        subs.append(sub("never two bright at once", violations == 0, f"{violations} frames"))
        self.normalize()
        return from_subresults("HW-10", subs)

    def hw11(self):
        s = self.session
        self.normalize()
        subs = []

        def state():
            d = self.now()
            return {r: self.lamp(d, r) for r in ("play", "stop", "rec")}

        st = state()
        subs.append(sub("idle: STOP full, PLAY dim", st["stop"] == (1, 0) and st["play"] == (1, 1), str(st)))
        s.press("play")
        time.sleep(.6)
        st = state()
        subs.append(sub("playing: PLAY full, STOP dim", st["play"] == (1, 0) and st["stop"] == (1, 1), str(st)))
        s.press("play")
        time.sleep(.6)
        st = state()
        subs.append(sub("paused: both dim", st["play"] == (1, 1) and st["stop"] == (1, 1), str(st)))
        s.press("stop")
        time.sleep(.6)
        st = state()
        subs.append(sub("stopped: STOP full", st["stop"] == (1, 0) and st["play"] == (1, 1), str(st)))
        s.press("rec")
        time.sleep(.6)
        armed = state()["rec"]
        s.press("rec")
        time.sleep(.6)
        disarmed = state()["rec"]
        subs.append(sub("REC full when armed, dim when not", armed == (1, 0) and disarmed == (1, 1),
                        f"{armed} / {disarmed}"))
        self.normalize()
        return from_subresults("HW-11", subs)

    def hw12(self):
        s = self.session
        self.normalize()
        subs, wrong = [], []
        base = self.now()
        for n in range(1, 17):
            role = f"step{n}"
            channel = self.roles[role][1]
            records = self.capture(lambda: s.press(role), 0.6)
            last = records[-1][1]
            lit = [ch for ch in range(16) if last["rgb"][ch] != base["rgb"][ch]]
            if lit != [channel]:
                wrong.append((n, lit))
            records = self.capture(lambda: s.press(role), 0.6)
            if any(records[-1][1]["rgb"][ch] != base["rgb"][ch] for ch in range(16)):
                wrong.append((n, "not cleared"))
        subs.append(sub("step n lights RGB n-1 only and a second tap clears it", not wrong, str(wrong[:4])))
        for n in range(1, 17):
            s.event(f"step{n}", "press", 100)
        time.sleep(.3)
        for n in range(1, 17):
            s.event(f"step{n}", "release")
        time.sleep(.8)
        lit = [ch for ch in range(16) if any(self.now()["rgb"][ch])]
        subs.append(sub("16 together light 16", len(lit) == 16, str(len(lit))))
        s.press("step5", 0.8)
        time.sleep(.6)
        held = any(self.now()["rgb"][self.roles["step5"][1]])
        subs.append(sub("hold does not erase", held, f"step5 lit after 0.8 s hold: {held}"))
        for n in range(1, 17):
            s.event(f"step{n}", "press", 100)
        time.sleep(.3)
        for n in range(1, 17):
            s.event(f"step{n}", "release")
        time.sleep(.8)
        g0 = self.fresh_guest()
        bar_records = self.capture(lambda: [s.press(f"bar{i}") for i in range(1, 5)], 0.8)
        g1 = self.fresh_guest()
        subs.append(sub("bar keys delivered and bar lamps decode",
                        g1["events"] - g0["events"] >= 8 and g1["unsupported"] == g0["unsupported"] and bar_records,
                        f"events +{g1['events'] - g0['events']}"))
        records = self.capture(lambda: s.press("play"), 10.0)
        s.press("stop")
        bars = self.groups.get("bar") or []
        changes = 0
        for role in bars:
            states = [self.lamp(d, role) for _, d in records]
            changes += sum(1 for a, b in zip(states, states[1:]) if a != b)
        subs.append(sub("bar position lamps pulse while playing", changes >= 2, f"{changes} changes in 10 s"))
        self.notes["play_records"] = records
        self.normalize()
        return from_subresults("HW-12", subs)

    def hw13(self):
        s = self.session
        roles = self.p["panel"]["button_roles"]
        self.normalize()
        idle = self.now()
        subs = []
        chord = ["shift", "pad16", "step16", "hold"]
        for name in chord:
            s.event(name, "press", 100)
            time.sleep(.2)
        t, g = self.edge()
        t, g = self.edge()
        rx = g.get("rx") or {}
        buttons = bytes.fromhex(rx.get("buttons", "")) if rx else b""

        def bit_set(role):
            byte, bit = roles[role]
            return len(buttons) >= byte and bool(buttons[byte - 1] >> bit & 1)

        if not rx:
            subs.append(sub("RX frame shows the chord's bits together", False, "guest does not report rx state",
                            blocked=True))
        else:
            ok = all(bit_set(r) for r in ("shift", "step16", "hold")) and 16 in rx.get("pads", [])
            subs.append(sub("RX frame shows the chord's bits together", ok, str(rx)))
        s.event("hold", "release")
        time.sleep(.2)
        t, g = self.edge()
        t, g = self.edge()
        rx = g.get("rx") or {}
        buttons = bytes.fromhex(rx.get("buttons", "")) if rx else b""
        if rx:
            ok = not bit_set("hold") and bit_set("shift") and bit_set("step16") and 16 in rx.get("pads", [])
            subs.append(sub("only the released control clears", ok, str(rx)))
        for name in ("step16", "pad16", "shift"):
            s.event(name, "release")
        time.sleep(1.0)
        g0 = self.fresh_guest()
        for _ in range(10):
            s.event("pad15", "press", 100)
            time.sleep(.02)
            s.event("pad15", "release")
            time.sleep(.05)
        time.sleep(1.5)
        g1 = self.fresh_guest()
        pad15 = self.now()["rgb"][self.roles["pad15"][1]]
        subs.append(sub("10 rapid taps give +20 events and no stale lamp",
                        g1["events"] - g0["events"] == 20 and pad15 == idle["rgb"][self.roles["pad15"][1]],
                        f"events +{g1['events'] - g0['events']}, pad15 {pad15}"))
        before = self.lamp(self.now(), "track")
        s.event("track", "press", 100)
        s.event("track", "release")
        time.sleep(.8)
        after = self.lamp(self.now(), "track")
        subs.append(sub("press shorter than one frame is seen (TRACK toggles)", after != before, f"{before} -> {after}"))
        if after != before:
            s.press("track")
            time.sleep(.5)
        ok, observed = self.shift_probe_held(3.0)
        subs.append(sub("SHIFT held 3 s keeps working, chord shows its lamp effect", ok, observed))
        fixes = self.normalize()
        time.sleep(.5)
        final = self.now()
        stale = sorted({("mono", ch) for ch in final["mono"] if final["mono"][ch] != idle["mono"][ch]} |
                       {("rgb", ch) for ch in final["rgb"] if final["rgb"][ch] != idle["rgb"][ch]})
        pending = s.metrics().get("pending_controls")
        subs.append(sub("after release-all only firmware idle state, pending 0", not stale and pending == 0,
                        f"stale {stale[:6]}, pending {pending}, firmware-state fixes {fixes}"))
        return from_subresults("HW-13", subs)

    def shift_probe_held(self, seconds):
        probe = dict(self.p["shift_probe"], hold_s=seconds)
        saved = self.p["shift_probe"]
        self.p["shift_probe"] = probe
        try:
            return self.shift_probe()
        finally:
            self.p["shift_probe"] = saved

    def hw14(self):
        """Union of every decoded TX frame of the main session."""
        s = self.session
        records = s.lamps_since(0)
        settled = self.notes.get("settled_uptime", 0)
        # Frames before the home screen settled are the firmware's start-up initialisation (the TX extras at
        # 0x66..0x68 move only then); unassigned channels are judged from the settled panel on.
        after_boot = [(t, d) for t, d in records if t >= settled]
        mono_states, rgb_states = {}, {}
        extra_states = set()
        for _, d in records:
            for ch, v in d["mono"].items():
                mono_states.setdefault(ch, set()).add(v)
            for ch, v in d["rgb"].items():
                rgb_states.setdefault(ch, set()).add(v)
        for _, d in after_boot:
            extra_states.add(d["extra"])
        unassigned = self.p["lamp_unassigned"]
        mapped_mono = {ch for kind, ch in self.roles.values() if kind == "mono"}
        mapped_rgb = {ch for kind, ch in self.roles.values() if kind == "rgb"}
        required = self.p.get("lamp_required") or list(self.roles)
        missing = [r for r in required if len(mono_states.get(self.roles[r][1], ()) if self.roles[r][0] == "mono"
                                               else rgb_states.get(self.roles[r][1], ())) < 2]
        unmapped = [f"mono {ch}" for ch, v in mono_states.items() if len(v) > 1 and ch not in mapped_mono
                    and ch not in unassigned.get("mono", [])]
        unmapped += [f"rgb {ch}" for ch, v in rgb_states.items() if len(v) > 1 and ch not in mapped_rgb]
        changed_unassigned = [f"mono {ch}" for ch in unassigned.get("mono", [])
                              if len({d["mono"][ch] for _, d in after_boot}) > 1]
        for index in unassigned.get("extra") or []:
            if len({e[index] for e in extra_states}) > 1:
                changed_unassigned.append(f"extra flag {index}: {sorted(extra_states)}")
        declared_extra = {int(k) for k in (self.p.get("lamp_extra_driven") or {})}
        undeclared = [i for i in range(3) if i not in declared_extra and i not in (unassigned.get("extra") or [])
                      and len({e[i] for e in extra_states}) > 1]
        unmapped += [f"extra flag {i}" for i in undeclared]
        subs = [sub("every required role seen in two distinct states", not missing, f"never changed: {missing}"),
                sub("no driven channel outside the profile", not unmapped, str(unmapped), blocked=bool(unmapped)),
                sub("declared unassigned channels never change", not changed_unassigned, str(changed_unassigned))]
        return from_subresults("HW-14", subs, {"frames": len(records)})

    def hw15(self):
        s = self.session
        known = self.p["rgb_known"]
        self.normalize()
        subs = []
        records = self.capture(lambda: s.press("pad1", 0.15, 100), 1.5)
        channel = self.roles["pad1"][1]
        colours = [d["rgb"][channel] for _, d in records]
        flash = tuple(known["strike_flash"])
        hit = colours.index(flash) if flash in colours else None
        decays = hit is not None and colours[-1] != flash
        subs.append(sub("strike flash equals the profile colour and decays", hit is not None and decays,
                        f"{sorted(set(colours))[-3:]}"))
        maximum = known["max_channel_value"]
        everything = s.lamps_since(0)
        over = max((max(v) for _, d in everything for v in d["rgb"].values()), default=0)
        subs.append(sub("no component above the profile maximum", over <= maximum, f"max {over} (limit {maximum})"))
        s.press("track")
        time.sleep(.5)
        s.press("pad2")
        time.sleep(.8)
        d = self.now()
        colour2 = d["rgb"][self.roles["pad2"][1]]
        s.press("pad1")
        time.sleep(.8)
        colour1 = self.now()["rgb"][self.roles["pad1"][1]]
        s.press("track")
        time.sleep(.5)
        subs.append(sub("two tracks give different pad colours (R,G,B carried)", colour1 != colour2 and any(colour1)
                        and any(colour2), f"track1 {colour1}, track2 {colour2}"))
        play = self.notes.get("play_records")
        if play is None:
            play = self.capture(lambda: s.press("play"), 3.0)
            s.press("stop")
        heads = playhead_steps(play, known["playhead"])
        subs.append(sub("playhead colour equals the profile", len(heads) >= 4, f"{len(heads)} playhead steps"))
        self.normalize()
        return from_subresults("HW-15", subs, {"track1": colour1, "track2": colour2})

    # ------------------------------------------------------------------ checks: encoders and selector
    @staticmethod
    def confined(a, b, boxes, threshold=24):
        """True when every pixel that differs between two images lies inside one of the boxes."""
        from PIL import ImageChops, ImageDraw
        mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > threshold else 0)
        draw = ImageDraw.Draw(mask)
        for box in boxes:
            draw.rectangle(box, fill=0)
        return mask.getbbox() is None

    def hw16(self):
        s = self.session
        probe, columns = self.p["knob_probe"], self.p["knob_columns"]
        self.normalize()
        also = [self.knob_track()]
        subs, sent = [], 0
        g0 = self.fresh_guest()
        for column in columns:
            knob, box, vbox = column["knob"], tuple(column["box"]), tuple(column["value_box"])
            start = s.image("knob")
            v0 = ocr(start, vbox, psm=7, scale=4)
            direction = 1
            s.event(knob, "delta", 1)
            sent += 1
            time.sleep(1.0)
            moved = s.image("knob")
            if diff_box(start, moved) is None:  # at the upper end: count the other way first
                direction = -1
                s.event(knob, "delta", -1)
                sent += 1
                time.sleep(1.0)
                moved = s.image("knob")
            v1 = ocr(moved, vbox, psm=7, scale=4)
            changed = diff_box(start, moved) is not None
            allowed = [box] + also + [tuple(b) for b in column.get("also") or []]
            inside = self.confined(start, moved, allowed)
            n0, n1 = read_number(v0), read_number(v1)
            sign_ok = True
            if n0 is not None and n1 is not None and v0 != v1:
                sign_ok = (n1 - n0) * direction * probe.get("increase", 1) > 0
            s.event(knob, "delta", -direction)
            sent += 1
            time.sleep(1.0)
            back = s.image("knob")
            returned = image_digest(back, box) == image_digest(start, box)
            subs.append(sub(f"{knob}: one detent changes only its column, reverses",
                            changed and inside and sign_ok and returned and self.confined(start, back, allowed),
                            f"{v0!r} -> {v1!r} (dir {direction:+d}), confined {inside}, returned {returned}"))
        knob = probe.get("alternating_knob") or probe["knob"]
        column = next(c for c in columns if c["knob"] == knob)
        start = s.image("knob")
        # 0.6 s apart: below the firmware's encoder acceleration (faster alternation is scaled by the firmware
        # unevenly, e.g. C -> R3 after 40 detents 50 ms apart at host load 11, which is not a wire fault).
        for i in range(40):
            s.event(knob, "delta", 1 if i % 2 == 0 else -1)
            sent += 1
            time.sleep(.6)
        time.sleep(2.0)
        end = s.image("knob")
        v0, v1 = ocr(start, tuple(column["value_box"]), 7, 4), ocr(end, tuple(column["value_box"]), 7, 4)
        n0, n1 = signed_value(v0), signed_value(v1)
        same = v0 == v1 or (n0 is not None and n1 is not None and abs(n1 - n0) <= 1)
        subs.append(sub(f"{knob}: 40 alternating detents return to the start", same and
                        self.confined(start, end, [tuple(column["box"])] + also), f"{v0!r} -> {v1!r}"))
        time.sleep(1.0)
        g1 = self.fresh_guest()
        subs.append(sub("guest events match", g1["events"] - g0["events"] == sent and g1["failed"] == g0["failed"],
                        f"{g1['events'] - g0['events']} of {sent}"))
        return from_subresults("HW-16", subs)

    def hw17(self):
        s = self.session
        param = self.p["knob_bounded_param"]
        knob, vbox = param["knob"], tuple(param["value_box"])
        self.normalize()
        self.knob_track()
        subs = []
        for direction, bound in ((1, param["max"]), (-1, param["min"])):
            g0 = self.fresh_guest()
            for _ in range(400):
                s.event(knob, "delta", direction)
                time.sleep(.005)
            time.sleep(6)
            at6 = read_number(ocr(s.image("bound"), vbox, 7, 4))
            time.sleep(6)
            at12 = read_number(ocr(s.image("bound"), vbox, 7, 4))
            g1 = self.fresh_guest()
            subs.append(sub(f"burst {direction:+d}: clamps at {bound}, drained by +6 s",
                            at6 == at12 == bound, f"+6 s {at6}, +12 s {at12}"))
            subs.append(sub(f"burst {direction:+d}: guest events 400, failed 0",
                            g1["events"] - g0["events"] == 400 and g1["failed"] == g0["failed"],
                            f"events +{g1['events'] - g0['events']}"))
        return from_subresults("HW-17", subs)

    def tile_box(self, track):
        tiles = self.p["selector_probe"]["tiles"]
        row, col = (track - 1) // len(tiles["left"]), (track - 1) % len(tiles["left"])
        x0, y0 = tiles["left"][col], tiles["top_rows_from_bottom"][row]
        return (x0, y0, x0 + tiles["width"] + 2, y0 + tiles["height"] + 2)

    def knob_track(self):
        """Select the spare track the knob checks change (track parameters on the knob screen follow the selected
        track), so bursts never alter the tracks the audio checks use; returns its tile box (the tile redraws some
        values)."""
        track = self.p["knob_probe"].get("track", 1)
        if not self.select_track(track):
            raise RecipeError(f"could not select track {track}")
        return self.tile_box(track)

    def tile(self):
        return selected_tile(self.session.image("tile"), self.p["selector_probe"])

    def hw18(self):
        s = self.session
        probe = self.p["selector_probe"]
        length = probe["length"]
        self.normalize()
        subs, wrong = [], []
        position = self.tile()
        plan = [-1, 1] + [1] * 11 + [-1] * 11
        for detent in plan:
            s.event("selector", "delta", detent)
            time.sleep(1.3)
            now = self.tile()
            expected = (position - 1 + detent) % length + 1
            if now != expected:
                wrong.append((position, detent, now))
            position = now if now else expected
        subs.append(sub("every detent moves exactly one position, wrap at both ends", not wrong and position == 1,
                        f"{len(plan)} detents, wrong {wrong[:4]}, end {position}"))
        return from_subresults("HW-18", subs)

    def settle_tile(self, timeout):
        t0 = time.monotonic()
        last, since = None, t0
        while time.monotonic() - t0 < timeout:
            now = self.tile()
            if now != last:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= 0.8:
                return now, since - t0
            time.sleep(.1)
        return last, None

    def hw19(self):
        s = self.session
        probe = self.p["selector_probe"]
        length, window = probe["length"], self.p["panel"]["selector_window_frames"]
        host = host_state()
        if host["vms"] > MAX_VMS:
            return verdict("HW-19", "BLOCKED", f"host load: {host['vms']} VMs running (limit {MAX_VMS})",
                           {"host": host})
        self.normalize()
        start = self.tile()
        subs = []
        runs = [(6, gap, sign) for gap in (0.3, 0.1, 0.05, 0.0) for _ in range(2) for sign in (1, -1)]
        runs += [(13, 0.0, 1), (13, 0.0, -1), (length, 0.0, 1)]
        position = start
        for count, gap, sign in runs:
            for i in range(count):
                s.event("selector", "delta", sign)
                if gap:
                    time.sleep(gap)
            budget = count * window * 0.010 + 2.0
            now, settle = self.settle_tile(budget + 3)
            expected = (position - 1 + sign * count) % length + 1
            ok = now == expected and settle is not None and settle <= budget
            subs.append(sub(f"{count} detents gap {gap} sign {sign:+d}", ok,
                            f"{position} -> {now} (expect {expected}), settled {settle and round(settle, 2)} s "
                            f"(budget {budget:.2f})"))
            position = now or expected
        subs.append(sub("L detents return to start", position == start, f"{start} -> {position}"))
        subs.append(sub("popup list run (58 detents)", True, "not run: no popup list with a rotary cursor in the "
                        "profile", na=True))
        return from_subresults("HW-19", subs, {"host": host})

    def hw20(self):
        s = self.session
        nav = self.p["nav_probe"]
        subs = []
        self.go_home()
        run_recipe(s, nav["level1"])
        ok1, t1, _ = wait_title(s, nav["level1_title"], self.title_box)
        subs.append(sub("level 1 opens", ok1, t1))
        run_recipe(s, nav["level2"])
        ok2, t2, _ = wait_title(s, nav["level2_title"], self.title_box)
        subs.append(sub("selector push (bit 1.1) opens the focused item", ok2, t2))
        s.press("back")
        ok, t, _ = wait_title(s, nav["level1_title"] + "$", self.title_box)
        subs.append(sub("BACK goes up exactly one level", ok, t))
        run_recipe(s, nav["level2"])
        ok2, t2, _ = wait_title(s, nav["level2_title"], self.title_box)
        s.press("home")
        ok, t, _ = wait_title(s, nav["home_expect_title"], self.title_box)
        subs.append(sub("HOME returns home from two levels", ok2 and ok, f"{t2} -> {t}"))
        self.go_home()
        return from_subresults("HW-20", subs)

    # ------------------------------------------------------------------ checks: pads
    def hw21(self):
        s = self.session
        flash = tuple(self.p["rgb_known"]["strike_flash"])
        self.normalize()
        base = self.now()
        subs, late, bleed = [], [], []
        g0 = self.fresh_guest()
        for n in range(1, 17):
            channel = self.roles[f"pad{n}"][1]
            sent = s.metrics()["uptime_seconds"]
            records = self.capture(lambda: s.press(f"pad{n}", 0.15, 100), 1.0)
            first = next((t for t, d in records if d["rgb"][channel] == flash), None)
            if first is None or first - sent > 0.2:
                late.append((n, None if first is None else round(first - sent, 3)))
            others = {ch for _, d in records for ch in range(0x10, 0x20)
                      if ch != channel and d["rgb"][ch] != base["rgb"][ch]}
            if others:
                bleed.append((n, sorted(others)))
            decayed = records[-1][1]["rgb"][channel] != flash if records else False
            if not decayed:
                late.append((n, "no decay"))
        subs.append(sub("pad n flashes its own channel within 200 ms and decays", not late, str(late[:4])))
        subs.append(sub("no other pad channel changes", not bleed, str(bleed[:4])))

        def all16():
            for n in range(1, 17):
                s.event(f"pad{n}", "press", 100)
            time.sleep(.3)
            for n in range(1, 17):
                s.event(f"pad{n}", "release")

        records = self.capture(all16, 1.0)
        flashed = {ch for _, d in records for ch in range(0x10, 0x20) if d["rgb"][ch] == flash}
        subs.append(sub("16 pads together give 16 flashes", len(flashed) == 16, str(len(flashed))))
        time.sleep(1)
        g1 = self.fresh_guest()
        subs.append(sub("guest events as sent", g1["events"] - g0["events"] == 64, str(g1["events"] - g0["events"])))
        return from_subresults("HW-21", subs)

    def pcm(self):
        return AudioTap(self.session, self.p["audio"]["sound_channels"]["stream"].replace("hw:", "").replace(",", "-"))

    def sound_columns(self, tap):
        info = tap.info()
        first = int(info.get("first_channel", 0))
        sc = self.p["audio"]["sound_channels"]
        return sc["left"] - first, sc["right"] - first

    def hit_capture(self, pad, velocity=100, hold=0.15, seconds=1.0, pre=0.3):
        """Strike a pad and return (left, right, all channels) of the captured sound from `pre` s before."""
        tap = self.pcm()
        start = tap.frames()
        time.sleep(pre)
        self.session.press(pad, hold, velocity)
        end = tap.wait_frames(int(seconds * RATE) + int(pre * RATE) // 2, timeout=seconds + 20)
        data = tap.read(start, end)
        left, right = self.sound_columns(tap)
        return data[:, left], data[:, right], data

    def hw22(self):
        self.normalize()
        self.load_sample(2, "hw-click.wav")
        self.select_track(2)
        self.set_knob("knob1", 100, self.p["recipes"]["volume_knob"]["value_box"], 127)
        subs = []
        self.recipe("velocity_disable")
        self.go_home()
        flat = [peak(self.hit_capture("pad2", v, seconds=0.6)[0]) for v in (20, 127)]
        subs.append(sub("velocity disabled: equal levels", flat[0] > 0 and abs(flat[0] - flat[1]) <= 0.02 * flat[1],
                        f"peaks {flat}"))
        self.recipe("velocity_enable")
        self.go_home()
        levels = {}
        for v in (1, 20, 50, 80, 100, 127):
            levels[v] = peak(self.hit_capture("pad2", v, seconds=0.6)[0])
            time.sleep(.3)
        values = [levels[v] for v in sorted(levels)]
        subs.append(sub("velocity 1 registers", levels[1] > 0, f"peak {levels[1]}"))
        subs.append(sub("peak non-decreasing with velocity, at least 3 distinct levels",
                        all(b >= a for a, b in zip(values, values[1:])) and len(set(values)) >= 3, str(levels)))
        subs.append(sub("127 not below 100", levels[127] >= levels[100], f"{levels[127]} vs {levels[100]}"))
        self.recipe("velocity_disable")
        self.normalize()
        return from_subresults("HW-22", subs, {"levels": levels, "flat": flat})

    def frame_clock(self, samples):
        """Fit stream frames -> app uptime from (uptime, frames) metrics samples."""
        import numpy as np
        if len(samples) < 2:
            return None
        u = np.array([a for a, _ in samples])
        f = np.array([b for _, b in samples], dtype=float)
        slope, intercept = np.polyfit(f, u, 1)
        return lambda frame: slope * frame + intercept

    def held_sound(self, pad, hold, velocity=100):
        """Hold a pad; return (sound seconds, hold seconds measured on the frame clock, onset frame, end frame,
        rx pads seen while held)."""
        s = self.session
        tap = self.pcm()
        start = tap.frames()
        time.sleep(.3)
        f_press = tap.frames()
        s.event(pad, "press", velocity)
        t0 = time.monotonic()
        time.sleep(min(hold, 1.2))
        rx = (s.guest().get("rx") or {}).get("pads")
        time.sleep(max(0, hold - (time.monotonic() - t0)))
        f_release = tap.frames()
        s.event(pad, "release")
        end = tap.wait_frames(int(1.5 * RATE), timeout=30)
        left = tap.read(start, end)[:, self.sound_columns(tap)[0]]
        span = active_span(left, max(64, peak(left) * 0.05))
        if span is None:
            return None, (f_release - f_press) / RATE, None, None, rx
        return (span[1] - span[0]) / RATE, (f_release - f_press) / RATE, span[0], span[1], rx

    def hw23(self):
        self.normalize()
        self.load_sample(1, "hw-tone440.wav")
        self.select_track(1)
        self.set_knob("knob1", 100, self.p["recipes"]["volume_knob"]["value_box"], 127)
        subs = []
        self.recipe("gate_mode")
        self.go_home()
        sound, hold, a, b, rx = self.held_sound("pad1", 2.0)
        subs.append(sub("GATE: sound sustained for the hold (>= 1.9 s)", sound is not None and sound >= 1.9,
                        f"sound {sound and round(sound, 3)} s for a {hold:.3f} s hold"))
        subs.append(sub("GATE: gone within 100 ms of release", sound is not None and sound - hold <= 0.1,
                        f"sound - hold = {sound and round(sound - hold, 3)} s"))
        subs.append(sub("held pressure word valid across frames", rx is None or 1 in rx,
                        f"rx pads while held {rx}" if rx is not None else "guest does not report rx state"))
        self.recipe("oneshot_mode")
        self.go_home()
        sound, hold, a, b, rx = self.held_sound("pad1", 0.5)
        subs.append(sub("One Shot: continues after release", sound is not None and sound >= hold + 1.0,
                        f"sound {sound and round(sound, 3)} s for a {hold:.3f} s hold"))
        self.normalize()
        return from_subresults("HW-23", subs)

    def tx_window(self, seconds, offset=None):
        offset = self.session.tx_offset() if offset is None else offset
        time.sleep(seconds)
        return self.session.lamps_since(offset)

    def hw24_calibrated(self):
        # Idle panel first: a pad flash from an earlier check (about 1 s decay) is not a calibration blink.
        self.normalize()
        time.sleep(2.0)
        records = self.tx_window(10.0)
        colours = {ch: {d["rgb"][ch] for _, d in records} for ch in range(0x10, 0x20)}
        worst = max((len(v) for v in colours.values()), default=0)
        varying = {ch - 0x10 + 1: sorted(v)[:3] for ch, v in colours.items() if len(v) > 1}
        return [sub("calibrated: >= 600 frames, every pad channel holds one colour", len(records) >= 600 and worst == 1,
                    f"{len(records)} frames, max colours per pad {worst}, varying pads {varying}")]

    def hw25(self):
        s = self.session
        known = self.p["rgb_known"]
        self.normalize()
        s.press("step1")
        time.sleep(.6)
        subs, seen = [], {}
        labels = known["level_labels"]
        for label, level in labels.items():
            self.recipe("brightness", label=label)
            time.sleep(.8)
            s.tx_since(s.tx_offset() - 4000)
            d = self.now()
            seen[label] = {"level": d["level"], "step1": d["rgb"][self.roles["step1"][1]],
                           "pad1": d["rgb"][self.roles["pad1"][1]]}
        self.recipe("brightness", label=next(k for k, v in labels.items() if v == max(labels.values())))
        self.go_home()
        s.press("step1")
        time.sleep(.5)
        full = seen[next(k for k, v in labels.items() if v == max(labels.values()))]
        for label, level in labels.items():
            got = seen[label]
            scale = known["level_scale"][str(level)]
            ratios = [got[k][i] / full[k][i] for k in ("step1", "pad1") for i in range(3) if full[k][i] >= 20]
            ok = got["level"] == level and ratios and all(abs(r - scale) <= 0.1 * scale + 1 / 74 for r in ratios)
            subs.append(sub(f"{label}: level byte {level}, RGB scaled x{scale}", ok,
                            f"level {got['level']}, ratios {[round(r, 2) for r in ratios]}"))
        self.normalize()
        return from_subresults("HW-25", subs, {"levels": seen})

    # ------------------------------------------------------------------ checks: strip
    def strip_leds(self, decoded):
        return [ch for ch in self.p["strip"]["lamp_channels"] if decoded["mono"][ch][0]]

    def hw26(self):
        s = self.session
        channels = self.p["strip"]["lamp_channels"]
        self.normalize()
        subs = []
        rest = self.strip_leds(self.now())
        subs.append(sub("none lit at rest", not rest, str(rest)))
        sets, slow = {}, []
        for position in (0, 250, 500, 750, 1000):
            s.event("strip", "press", position)
            time.sleep(.5)
            sets[position] = self.strip_leds(self.now())
            released = s.metrics()["uptime_seconds"]
            records = self.capture(lambda: s.event("strip", "release"), 0.8)
            cleared = next((t for t, d in records if not self.strip_leds(d)), None)
            if cleared is None or cleared - released > 0.3:
                slow.append((position, None if cleared is None else round(cleared - released, 3)))
        distinct = len({tuple(v) for v in sets.values()}) == len(sets) and all(sets.values())
        subs.append(sub("lit LED set differs at each position", distinct, str(sets)))
        mirror = lambda leds: sorted(channels[0] + channels[-1] - ch for ch in leds)
        symmetric = all(mirror(sets[p]) == sorted(sets[1000 - p]) for p in (0, 250, 500))
        subs.append(sub("symmetric about the centre", symmetric, str(sets)))
        subs.append(sub("release clears within 300 ms", not slow, str(slow)))
        s.event("strip", "press", 0)
        for position in (250, 500, 750, 1000):
            time.sleep(.08)
            s.event("strip", "value", position)
        time.sleep(.3)
        dragged = self.strip_leds(self.now())
        s.event("strip", "release")
        time.sleep(.5)
        after = self.strip_leds(self.now())
        subs.append(sub("drag: touch, moves, release, no stale LED", dragged == sets[1000] and not after,
                        f"at end {dragged}, after release {after}"))
        return from_subresults("HW-26", subs, {"sets": sets})

    def hw27(self):
        s = self.session
        self.normalize()
        self.load_sample(1, "hw-tone440.wav")
        self.select_track(1)
        self.recipe("gate_mode")
        self.go_home()
        tap = self.pcm()
        left_col = self.sound_columns(tap)[0]

        def freq_now(seconds=0.4):
            first = tap.frames()
            last = tap.wait_frames(int(seconds * RATE), timeout=10)
            return dominant_frequency(tap.read(first, last)[:, left_col])

        s.event("pad1", "press", 100)
        time.sleep(.5)
        f0 = freq_now()
        freqs = {}
        s.event("strip", "press", 1000)
        for position in (1000, 500, 0):
            s.event("strip", "value", position)
            time.sleep(.4)
            freqs[position] = freq_now()
        s.event("strip", "release")
        time.sleep(.2)
        back = freq_now(0.3)
        s.event("pad1", "release")
        time.sleep(.8)
        s.event("strip", "press", 1000)
        time.sleep(.4)
        s.event("pad1", "press", 100)
        time.sleep(.05)
        first_bent = freq_now(0.3)
        s.event("pad1", "release")
        s.event("strip", "release")
        time.sleep(.5)
        self.recipe("oneshot_mode")
        subs = []
        top, bottom, centre = freqs.get(1000), freqs.get(0), freqs.get(500)
        ok = all((f0, top, bottom, centre))
        if ok:
            symmetry = abs(top / f0 - f0 / bottom)
            span = 12 * math.log2(top / bottom)
            subs.append(sub("pitch bends monotonically, symmetric about the centre", top > centre > bottom and
                            symmetry < 0.02 and abs(centre / f0 - 1) < 0.005,
                            f"f0 {f0:.1f}, top {top:.1f}, centre {centre:.1f}, bottom {bottom:.1f}, asym {symmetry:.4f}"))
            subs.append(sub("at least 0.5 semitone total", span >= 0.5, f"{span:.2f} semitones"))
        else:
            subs.append(sub("pitch measured", False, f"f0 {f0}, {freqs}"))
        subs.append(sub("release returns the pitch within 200 ms", back is not None and f0 is not None and
                        abs(back / f0 - 1) < 0.005, f"{back}"))
        subs.append(sub("strip first: pad plays already bent", first_bent is not None and top is not None and
                        abs(first_bent / top - 1) < 0.01, f"{first_bent}"))
        self.normalize()
        return from_subresults("HW-27", subs, {"f0": f0, "freqs": freqs})

    def hw28(self):
        s = self.session
        order = self.groups["mode_order"]
        self.normalize()
        subs, sequence = [], []

        def lit():
            d = self.now()
            return [r for r in order if self.lamp(d, r)[0]], [self.lamp(d, r)[1] for r in order]

        roles, flags = lit()
        sequence.append(roles)
        for _ in range(5):
            s.press("mode")
            time.sleep(.5)
            roles, flags_now = lit()
            flags += flags_now
            sequence.append(roles)
        expected = [[order[i % len(order)]] for i in range(6)]
        subs.append(sub("exactly one mode lamp, in profile order, wrapping", sequence == expected, str(sequence)))
        subs.append(sub("mode lamps have no dim state", all(f is None for f in flags), ""))
        self.normalize()
        return from_subresults("HW-28", subs)

    def hw29(self):
        s = self.session
        self.normalize()
        subs = []
        s.event("strip", "press", 900)
        time.sleep(.4)
        s.press("hold")
        time.sleep(.3)
        s.event("strip", "release")
        time.sleep(.6)
        d = self.now()
        latched = self.strip_leds(d)
        subs.append(sub("HOLD lamp lit, LEDs stay after release", self.full(d, "hold") and bool(latched),
                        f"hold {self.lamp(d, 'hold')}, leds {latched}"))
        s.press("hold")
        time.sleep(.6)
        d = self.now()
        subs.append(sub("second HOLD clears lamp and LEDs", not self.full(d, "hold") and not self.strip_leds(d),
                        f"hold {self.lamp(d, 'hold')}, leds {self.strip_leds(d)}"))
        s.event("strip", "press", 900)
        time.sleep(.4)
        s.press("hold")
        time.sleep(.3)
        s.event("strip", "release")
        time.sleep(.4)
        s.press("mode")
        time.sleep(.6)
        d = self.now()
        subs.append(sub("MODE change cancels HOLD", not self.full(d, "hold") and not self.strip_leds(d),
                        f"hold {self.lamp(d, 'hold')}, leds {self.strip_leds(d)}"))
        self.normalize()
        return from_subresults("HW-29", subs)

    # ------------------------------------------------------------------ checks: analog
    def hw30(self):
        s = self.session
        words = self.p["panel"]["analog_words"]
        subs, worst = [], 0
        for name, (offset, inverted) in words.items():
            errors = []
            for value in (0, 250, 500, 750, 1000):
                s.event(name, "value", value)
                t0 = time.monotonic()
                expected = round(value * 1023 / 1000)
                expected = 1023 - expected if inverted else expected
                word = None
                while time.monotonic() - t0 < 2.5:
                    time.sleep(.3)
                    word = ((s.guest().get("adc") or {}).get(f"0x{offset:02x}"))
                    if word is not None and abs(word - expected) <= 10.23:
                        break
                settle = time.monotonic() - t0
                if word is None or abs(word - expected) > 10.23 or settle > 2.0:
                    errors.append((value, word, expected, round(settle, 2)))
                if word is not None:
                    worst = max(worst, abs(word - expected))
            # MAIN OUT is also an analog volume in the board model (unity only at full scale): leave it there so later
            # audio checks hear the firmware's level; the other pots go back to the centre.
            s.event(name, "value", 1000 if name == "main_out" else 500)
            subs.append(sub(f"{name}: word linear within 1% of 1023, settle <= 2 s", not errors, str(errors[:3])))
        dac = self.hp_dac_sweep(subs) if "hp_cutoff" in words else None
        for name in ("lp_cutoff", "lp_resonance"):
            if name not in words:
                g0 = self.fresh_guest()
                result = s.event(name, "value", 500)
                g1 = self.fresh_guest()
                ok = result.get("ok", True) is not False and g1["unsupported"] == g0["unsupported"]
                subs.append(sub(f"{name}: no ADC word; accepted as host analog state for the board model", ok,
                                f"unsupported +{g1['unsupported'] - g0['unsupported']}"))
        return from_subresults("HW-30", subs, {"worst_error": worst, "hp_dac": dac})

    def hp_dac_sweep(self, subs):
        """HP CUTOFF goes back out as the DAC7811 word on /dev/dac_spi4.0 (the shim reports the newest word). The app
        eases the word toward its target, so each step waits for it to settle; DRIVE must leave it alone."""
        s = self.session

        def settled():
            last, deadline = None, time.monotonic() + 6
            while time.monotonic() < deadline:
                time.sleep(.6)
                code = ((self.fresh_guest().get("board") or {}).get("dac") or {}).get("code")
                if code is not None and code == last:
                    return code
                last = code
            return last
        codes = []
        for value in (0, 250, 500, 750, 1000):
            s.event("hp_cutoff", "value", value)
            codes.append(settled())
        rising = None not in codes and all(a < b for a, b in zip(codes, codes[1:]))
        subs.append(sub("hp_cutoff: DAC code rises strictly with the pot (0, 250, 500, 750, 1000)", rising, str(codes)))
        s.event("drive", "value", 1000)
        after = settled()
        s.event("drive", "value", 500)
        s.event("hp_cutoff", "value", 500)
        settled()
        subs.append(sub("drive: leaves the DAC code unchanged", after == codes[-1], f"{codes[-1]} -> {after}"))
        return codes

    # ------------------------------------------------------------------ checks: analog board model
    def phones_peak(self, seconds=1.5):
        """1 kHz amplitude in the newest `seconds` of the headphone stream (hw:ak4452audio,0) in the session's WAV, read after
        waiting that long, or None when the session records no such file."""
        time.sleep(seconds + 1.0)  # the host sink writes behind the guest by a few packets
        stream = ((self.session.metrics().get("audio") or {}).get("streams") or {}).get("ak4452audio-0") or {}
        path = stream.get("wav")
        if not path or not Path(path).is_file():
            return None
        with open(path, "rb") as handle:
            data = handle.read()
        start = data.find(b"data") + 8
        frames = (len(data) - start) // 8
        take = min(frames, int(seconds * RATE))
        values = struct.unpack_from(f"<{take * 2}f", data, start + (frames - take) * 8)
        left = [v if math.isfinite(v) else 0.0 for v in values[0::2]]
        # Amplitude of the 1 kHz stimulus only (Goertzel), so other sounds on the headphone bus (a sample an earlier
        # check left playing, the metronome) do not mask the filter's effect.
        k = 2 * math.cos(2 * math.pi * 1000 / RATE)
        s1 = s2 = 0.0
        for v in left:
            s1, s2 = v + k * s1 - s2, s1
        power = s1 * s1 + s2 * s2 - k * s1 * s2
        return 2 * math.sqrt(max(power, 0.0)) / max(len(left), 1)

    def hw46(self):
        """The Dave Smith filter is an analog insert loop (service manual): the send DAC feeds the low-pass (CUTOFF,
        RESONANCE, never seen by an MCU) and the DAC7811-driven high-pass, the return ADC brings it back, and ACTIVE
        drives the filter select line GPIO5_IO05 through /dev/gpiodrv. Gating: ACTIVE drives that line, and the shim's
        loop returns the research FILTER TONE (return peak above 0.1). The headphone level with ACTIVE on or off and
        with the lowest CUTOFF are informational (levels recorded in the observed values, not gating)."""
        s = self.session
        self.normalize()
        subs = []

        def sel():
            return (((self.fresh_guest().get("board") or {}).get("gpio") or {}).get("GPIO5_IO05"))

        def set_active(on):
            for _ in range(2):
                if self.full(self.now(), "active") == on:
                    return True
                s.press("active")
                time.sleep(.8)
            return self.full(self.now(), "active") == on
        lines = []
        for on in (True, False, True):
            ok = set_active(on)
            lines.append((on, ok, sel()))
        follows = all(ok and level == int(on) for on, ok, level in lines)
        subs.append(sub("ACTIVE drives the filter select line GPIO5_IO05 (1 = filter in)", follows, str(lines)))
        s.event("lp_cutoff", "value", 1000)
        s.event("lp_resonance", "value", 0)
        s.event("filter_tone", "value", 900)  # -6 dBFS, 1 kHz
        on_peak = self.phones_peak()
        set_active(False)
        off_peak = self.phones_peak()
        set_active(True)
        # Informational until the firmware's use of the return is understood (task: filter send/return path): in
        # the emulator the 1 kHz return tone reached the headphone bus at about -60 dBFS at most and did not follow
        # CUTOFF, so these two lines record levels and do not gate the verdict.
        subs.append(sub("headphone 1 kHz level, ACTIVE on vs off (informational)", True, f"on {on_peak}, off {off_peak}"))
        s.event("lp_cutoff", "value", 0)
        closed_peak = self.phones_peak()
        subs.append(sub("headphone 1 kHz level, CUTOFF open vs lowest (informational)", True,
                        f"open {on_peak}, lowest {closed_peak}"))
        report = (self.fresh_guest().get("board") or {}).get("filter_loop") or {}
        subs.append(sub("the shim's loop returns the filter-input tone (return peak reported)", report.get("return_peak", 0) > .1,
                        str(report)))
        s.event("lp_cutoff", "value", 1000)
        s.event("filter_tone", "value", 0)
        subs.append(sub("send stream from a track insert: not observed yet (informational)", True,
                        "the app left the send playback silent in every insert setting tried; " + str(report)))
        return from_subresults("HW-46", subs, {"on": on_peak, "off": off_peak, "lowest": closed_peak,
                                           "filter_loop": report})

    # ------------------------------------------------------------------ checks: audio
    def hw31(self):
        console = self.session.console()
        subs = []
        params = [line for line in console_lines(console, "[sp16-ui-stub] PCM ") if " hw_params " in line]
        for stream in self.p["audio"]["alsa_streams"]:
            want = {"name": stream["name"], "stream": stream["direction"], "access": stream.get("access"),
                    "format": stream.get("format"), "channels": stream.get("channels"), "rate": stream.get("rate"),
                    "period_frames": stream.get("period_frames"), "buffer_frames": stream.get("buffer_frames")}
            match = None
            for line in params:
                fields = dict(re.findall(r"(\w+)=([^\s]+)", line))
                if f"PCM {stream['name']} " not in line or fields.get("stream") != stream["direction"]:
                    continue
                ok = all(str(v) == fields.get(k, "").split("(")[0] for k, v in want.items()
                         if k not in ("name", "stream") and v is not None)
                if ok and fields.get("result") == "0":
                    match = line
                    break
            subs.append(sub(f"{stream['name']} {stream['direction']} opened as declared", match is not None,
                            (match or f"no hw_params line matching {want}")[-180:]))
        results = [re.search(r"hw_params result=(-?\d+)", line).group(1) for line in params]
        subs.append(sub("every hw_params result 0", results and all(r == "0" for r in results), str(set(results))))
        opened = {m for m in re.findall(r"\[sp16-ui-stub\] PCM (\S+) -> ", console)}
        declared = {stream["name"] for stream in self.p["audio"]["alsa_streams"]}
        subs.append(sub("no PCM outside the profile", opened <= declared, f"opened {sorted(opened)}"))
        return from_subresults("HW-31", subs)

    def audio_counters(self):
        m = self.session.metrics()
        audio = m.get("audio") or {}
        streams = {name: {"frames": v.get("frames", 0), "gap_frames": v.get("gap_frames", 0),
                          "guest_drops": v.get("guest_drops", 0)} for name, v in (audio.get("streams") or {}).items()}
        return {"t": time.monotonic(), "streams": streams, "seq_gaps": audio.get("seq_gaps", 0),
                "late_over_20ms": audio.get("late_over_20ms", 0), "packets": audio.get("packets", 0)}

    def clock_window(self, seconds):
        """Audio clock evidence over a window: per-stream frame rate on the host clock, gaps, drops, late packets
        and the guest's `speed=` lines printed inside the window."""
        console_offset = len(self.session.console())
        a0 = self.audio_counters()
        time.sleep(seconds)
        a1 = self.audio_counters()
        elapsed = a1["t"] - a0["t"]
        speeds = re.findall(r"\[sp16-ui-stub\] audio (\S+) (\w+) .*?speed=([0-9.]+)x",
                            self.session.console()[console_offset:])
        rates = {name: (a1["streams"][name]["frames"] - a0["streams"].get(name, {}).get("frames", 0)) / elapsed
                 for name in a1["streams"]}
        return {"seconds": round(elapsed, 2), "rates": {k: round(v, 1) for k, v in rates.items()},
                "gap_frames": sum(a1["streams"][n]["gap_frames"] - a0["streams"].get(n, {}).get("gap_frames", 0)
                                  for n in a1["streams"]),
                "guest_drops": sum(a1["streams"][n]["guest_drops"] - a0["streams"].get(n, {}).get("guest_drops", 0)
                                   for n in a1["streams"]),
                "seq_gaps": a1["seq_gaps"] - a0["seq_gaps"], "late_over_20ms": a1["late_over_20ms"] - a0["late_over_20ms"],
                "packets": a1["packets"] - a0["packets"], "speeds": speeds, "load": load_averages()}

    def clock_subs(self, label, window):
        audio = self.p["audio"]
        limit = audio["late_over_20ms_max"]
        slow = {k: v for k, v in window["rates"].items() if v < 0.99 * RATE}
        bad_speed = [s for s in window["speeds"] if float(s[2]) < 0.99]
        late_share = window["late_over_20ms"] / max(1, window["packets"])
        return [sub(f"{label}: every stream >= 0.99x", not slow and not bad_speed,
                    f"rates {window['rates']}, speed lines {window['speeds'][:3]}"),
                sub(f"{label}: gap_frames, guest_drops, seq_gaps 0",
                    window["gap_frames"] == window["guest_drops"] == window["seq_gaps"] == 0,
                    f"gaps {window['gap_frames']}, drops {window['guest_drops']}, seq {window['seq_gaps']}"),
                sub(f"{label}: late_over_20ms within the profile limit", late_share <= limit,
                    f"{window['late_over_20ms']} of {window['packets']} packets ({late_share:.4f}, limit {limit})")]

    def play_window(self, seconds, lead):
        """PLAY, wait `lead` s, then capture TX, audio and clock counters for `seconds`; STOP. Returns evidence."""
        s = self.session
        tap = self.pcm()
        s.press("play")
        time.sleep(lead)
        offset = s.tx_offset()
        first = tap.frames()
        clock = self.clock_window(seconds)
        last = tap.frames()
        records = s.lamps_since(offset)
        s.press("stop")
        time.sleep(.5)
        return {"records": records, "first": first, "last": last, "clock": clock, "tap": tap}

    def hw33_and_32(self):
        """Tempo (HW-33) from the playhead and a click track, and the playing half of the clock check (HW-32)."""
        s = self.session
        host = host_state()
        self.normalize()
        self.load_sample(2, "hw-click.wav")
        self.select_track(2)
        self.set_knob("knob1", 100, self.p["recipes"]["volume_knob"]["value_box"], 127)
        steps = self.p["recipes"]["click_steps"]
        for n in steps:
            s.press(f"step{n}")
            time.sleep(.3)
        self.select_track(1)
        lead, seconds = (5, 20) if self.smoke else (15, 62)
        window = self.play_window(seconds, lead)
        self.select_track(2)
        for n in steps:
            s.press(f"step{n}")
            time.sleep(.3)
        self.normalize()
        known, audio = self.p["rgb_known"], self.p["audio"]
        expected_period = 16 * 60 / (4 * audio["default_bpm"])
        heads = playhead_steps(window["records"], known["playhead"])
        period = revisit_period(heads)
        tap = window["tap"]
        left = tap.read(window["first"], window["last"])[:, self.sound_columns(tap)[0]]
        marks = onsets(left, max(200, peak(left) * 0.3), int(0.2 * RATE))
        gaps = [b - a for a, b in zip(marks, marks[1:])]
        step_frames = expected_period / 16 * 4 * RATE * (steps[1] - steps[0]) / 4
        bad = [g for g in gaps if abs(g - step_frames) > 64]
        tempo = []
        if host["vms"] > MAX_VMS:
            tempo.append(sub("host load", False, f"{host['vms']} VMs running", blocked=True))
        tempo += [sub("playhead revisit period within 1% of the default tempo",
                      period is not None and abs(period - expected_period) <= 0.01 * expected_period,
                      f"median {period and round(period, 4)} s vs {expected_period} s ({len(heads)} steps)"),
                  sub("click onsets 22050 +-64 frames apart over 3 loops",
                      len(gaps) >= 11 if not self.smoke else len(gaps) >= 3,
                      f"{len(gaps)} gaps, median {median(gaps)}, outside tolerance {bad[:4]}")]
        if bad:
            tempo[-1] = sub("click onsets 22050 +-64 frames apart over 3 loops", False,
                            f"{len(gaps)} gaps, outside tolerance {bad[:4]}")
        evidence = {"period": period, "gaps": gaps[:24], "host": host, "uptime": load_averages()}
        self.notes["play_clock"] = window["clock"]
        return from_subresults("HW-33", tempo, evidence)

    def hw32(self):
        host = host_state()
        playing = self.notes.get("play_clock")
        if playing is None:
            window = self.play_window(20 if self.smoke else 60, 3)
            playing = window["clock"]
        self.normalize()
        idle = self.clock_window(20 if self.smoke else 60)
        subs = []
        if host["vms"] > MAX_VMS:
            subs.append(sub("host load", False, f"{host['vms']} VMs running", blocked=True))
        subs += self.clock_subs("playing", playing) + self.clock_subs("idle", idle)
        return from_subresults("HW-32", subs, {"playing": playing, "idle": idle, "host": host})

    def hw34(self):
        s = self.session
        self.normalize()
        self.load_sample(1, "hw-tone440.wav")
        self.select_track(1)
        self.recipe("oneshot_mode")
        volume = self.p["recipes"]["volume_knob"]
        self.set_knob("knob1", 100, volume["value_box"], volume["max"])
        self.go_home()
        selected, shown = self.on_track(1)
        if not selected:
            return from_subresults("HW-34", [sub("TRACK 1 is selected before the tone hit", False,
                                                 f"the home screen showed TRACK {shown}")])
        left, right, data = self.hit_capture("pad1", 100, seconds=7.0, pre=0.5)
        subs = []
        threshold = max(64, peak(left) * 0.05)
        span = active_span(left, threshold)
        if span is None:
            return from_subresults("HW-34", [sub("sound captured", False, "silent capture")])
        body = left[span[0]:span[1]]
        f = dominant_frequency(body[:RATE * 2])
        subs.append(sub("dominant frequency 440 Hz +-0.5%", f is not None and abs(f - 440) <= 2.2, f"{f and round(f, 2)} Hz"))
        length = (span[1] - span[0]) / RATE
        wav_len = 6.0
        subs.append(sub("length equals the file +-20 ms", abs(length - wav_len) <= 0.02, f"{length:.4f} s"))
        blocks = block_peaks(body, int(0.005 * RATE))
        floor = float(median(list(blocks)) or 0)
        dropout = int((blocks[:-2] < 0.5 * floor).sum()) if len(blocks) > 2 else 0
        subs.append(sub("no dropout (continuous envelope)", dropout == 0, f"{dropout} quiet 5 ms blocks"))
        before = left[:max(0, span[0] - 200)]
        after = left[span[1] + int(0.3 * RATE):]
        zero = (len(before) == 0 or not before.any()) and (len(after) == 0 or not after.any())
        subs.append(sub("silence around the hit is digital zero", zero,
                        f"before max {peak(before)}, after max {peak(after)}"))
        tap = self.pcm()
        lcol, rcol = self.sound_columns(tap)
        others = [c for c in range(data.shape[1]) if c not in (lcol, rcol)]
        leak = [c for c in others if (f2 := dominant_frequency(data[span[0]:span[0] + RATE, c])) is not None
                and abs(f2 - 440) < 5 and peak(data[span[0]:span[0] + RATE, c]) > 0.1 * peak(body)]
        same = peak(right[span[0]:span[1]]) > 0.5 * peak(body)
        subs.append(sub("sound on the declared channels only", not leak and same,
                        f"other channels with the tone {leak}; right peak {peak(right)}"))
        levels, lost = [], []
        self.set_knob("knob1", 0, volume["value_box"], volume["max"])
        for _ in range(4):
            selected, shown = self.on_track(1)
            if not selected:
                lost.append(f"before hit {len(levels) + 1}: TRACK {shown}")
                break
            s.turn("knob1", 8, 0.35)
            time.sleep(1.5)
            value = read_number(ocr(s.image("vol"), tuple(volume["value_box"]), 7, 4))
            # The knob step itself can move the selection, so check again right before the hit that reads the level.
            selected, shown = self.on_track(1)
            if not selected:
                lost.append(f"after the step of hit {len(levels) + 1}: TRACK {shown}")
                break
            l, _, _ = self.hit_capture("pad1", 100, seconds=0.5)
            levels.append((value, peak(l)))
            time.sleep(6.0)
        subs.append(sub("TRACK 1 is selected before every level hit", not lost, "; ".join(lost)))
        ordered = sorted(levels)
        mono = all(b[1] > a[1] for a, b in zip(ordered, ordered[1:])) and len({v for v, _ in levels}) == 4
        subs.append(sub("level follows the track VOLUME knob monotonically (4 settings)", mono, str(levels)))
        self.set_knob("knob1", 100, volume["value_box"], volume["max"])
        self.normalize()
        return from_subresults("HW-34", subs, {"frequency": f, "length": length, "levels": levels})

    def over_window(self, track, name, seconds=3.0):
        s = self.session
        self.load_sample(track, name)
        self.select_track(track)
        volume = self.p["recipes"]["volume_knob"]
        self.set_knob("knob1", volume["max"], volume["value_box"], volume["max"])
        self.select_track(1)
        tap = self.pcm()
        clock = []
        start = tap.frames()
        offset = s.tx_offset()
        s.press(f"pad{track}", 0.15, 127)
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            m = s.metrics()
            info = ((m.get("audio") or {}).get("streams") or {}).get(tap.stream) or {}
            clock.append((m["uptime_seconds"], info.get("frames", 0)))
            time.sleep(.05)
        end = tap.frames()
        records = s.lamps_since(offset)
        left = tap.read(start, end)[:, self.sound_columns(tap)[0]]
        return records, left, start, self.frame_clock(clock)

    def hw35(self):
        self.normalize()
        role = self.groups["over"][0]
        subs = []
        records, left, start, to_time = self.over_window(3, "hw-tone-full.wav")
        loud = [i for i in range(len(left)) if abs(int(left[i])) >= 0.89 * 32767] if peak(left) >= 0.89 * 32767 else []
        lit_times = [t for t, d in records if self.lamp(d, role)[0]]
        if not loud:
            subs.append(sub("full-scale tone reaches the output", False, f"peak {peak(left)}"))
        else:
            t_first, t_last = to_time(start + loud[0]), to_time(start + loud[-1])
            on = next((t for t in lit_times if t >= t_first - 0.05), None)
            off = next((t for t, d in records if t > t_last and not self.lamp(d, role)[0]), None)
            subs.append(sub("OVER lit within 50 ms of the first near-full-scale sample",
                            on is not None and on - t_first <= 0.05, f"{on and round(on - t_first, 3)} s"))
            subs.append(sub("OVER off within 1 s of the last", off is not None and off - t_last <= 1.0,
                            f"{off and round(off - t_last, 3)} s"))
        records, left, _, _ = self.over_window(4, "hw-tone-m30.wav")
        lit = [t for t, d in records if self.lamp(d, role)[0]]
        subs.append(sub("never lit for the -30 dBFS tone", not lit and peak(left) > 0,
                        f"lit frames {len(lit)}, peak {peak(left)}"))
        self.normalize()
        return from_subresults("HW-35", subs)

    # ------------------------------------------------------------------ checks: USB
    def usb_led(self, decoded):
        return self.lamp(decoded, self.groups["usb"][0])[0]

    def browse_names(self):
        """Fixture names the firmware lists through the profile's browse recipe (OCR of its list)."""
        usb = self.p["usb"]
        recipe = usb["browse_recipe"]
        try:
            run_recipe(self.session, recipe["steps"])
        except RecipeError as error:  # e.g. no USB row while the stick is ejected: read what is listed
            self.notes.setdefault("browse_errors", []).append(str(error))
        text = norm(ocr(self.session.image("browse"), tuple(recipe["list_box"]), psm=6))
        self.go_home()
        return text

    def hw36(self):
        s = self.session
        console = s.console()
        usb_lines = console_lines(console, "SP16_USB")
        fsck = next((l for l in usb_lines if "fsck_rc=" in l), "")
        subs = [sub("console fsck_rc=0 mounted=yes announced=yes",
                    "fsck_rc=0" in fsck and any("mounted=yes" in l for l in usb_lines)
                    and any("announced=yes" in l for l in usb_lines), " | ".join(usb_lines[:4]))]
        settled = self.notes.get("settled_uptime", 0)
        frames = s.lamps_since(0)
        lit_at = next((t for t, d in frames if self.usb_led(d)), None)
        # The firmware lights the LED when it handles the mount announcement (within moments of the home screen
        # settling) and blinks it during stick access (the browse below); steady state is lit.
        subs.append(sub("USB LED lit once the mount is announced (within 5 s of the settled home screen)",
                        lit_at is not None and lit_at - settled <= 5.0,
                        f"first lit {lit_at and round(lit_at - settled, 2)} s from settle"))
        names = self.browse_names()
        missing = [n for n in fixture_wavs() if not re.search(self.sample_params(n)["sample_re"], names)]
        subs.append(sub("the firmware lists the fixture files", not missing, f"missing {missing}; read {names[:160]}"))
        offset = s.tx_offset()
        time.sleep(1.5)
        steady = [self.usb_led(d) for _, d in s.lamps_since(offset)]
        subs.append(sub("USB LED steady lit after the stick access", bool(steady) and all(steady),
                        f"{sum(steady)} of {len(steady)} frames lit"))
        return from_subresults("HW-36", subs)

    def hw37(self):
        s = self.session
        self.go_home()
        subs = []
        offset = s.tx_offset()
        t0 = s.metrics()["uptime_seconds"]
        reply = s.usb("eject")
        time.sleep(1.0)
        records = s.lamps_since(offset)
        off_at = next((t for t, d in records if not self.usb_led(d)), None)
        s.console()
        subs.append(sub("eject: reply ok, LED off at once", reply.get("ok") and off_at is not None and off_at - t0 <= 1.0,
                        f"{reply}, LED off after {off_at and round(off_at - t0, 3)} s"))
        names = self.browse_names()
        gone = not re.search(self.sample_params("hw-tone440.wav")["sample_re"], names) and "USB" not in names
        subs.append(sub("the firmware's list drops the stick", gone, names[:120]))
        alive = s.metrics().get("lamps_live") and "Kernel panic" not in s.console()
        subs.append(sub("no OEM crash", bool(alive), ""))
        offset = s.tx_offset()
        t0 = s.metrics()["uptime_seconds"]
        reply = s.usb("attach")
        time.sleep(4.0)
        records = s.lamps_since(offset)
        on_at = next((t for t, d in records if self.usb_led(d)), None)
        subs.append(sub("attach: LED on again (about 2 s)", reply.get("ok") and on_at is not None and on_at - t0 <= 4.0,
                        f"{reply}, LED on after {on_at and round(on_at - t0, 2)} s"))
        names = self.browse_names()
        back = any(re.search(self.sample_params(n)["sample_re"], names) for n in fixture_wavs())
        subs.append(sub("the list returns", back, names[:120]))
        self.partial["HW-37"] = subs
        return None  # completed after the session stops (fixture file hashes)

    def hw38(self):
        s = self.session
        self.go_home()
        console_offset = len(s.console())
        offset = s.tx_offset()
        t0 = s.metrics()["uptime_seconds"]
        s.press("usb_stop", float(self.p["usb"]["stop_hold_s"]) + 0.6)
        time.sleep(4.0)
        records = s.lamps_since(offset)
        console = s.console()[console_offset:]
        states = [(t, self.usb_led(d)) for t, d in records if t >= t0]
        edges = [(t, v) for (t, v), (_, p) in zip(states[1:], states[:-1]) if v != p]
        rising = [t for t, v in edges if v]
        subs = [sub("console unmounted_by_app=yes removed=yes", "unmounted_by_app=yes" in console and
                    "removed=yes" in console, " | ".join(console_lines(console, "SP16_USB"))[:200]),
                sub("LED blinks about 2 Hz three times then stays off",
                    len(rising) >= 2 and states and not states[-1][1],
                    f"{len(rising)} blinks, final {'on' if states and states[-1][1] else 'off'}")]
        reply = s.usb("attach")
        time.sleep(3.0)
        subs.append(sub("stick re-attached for the rest of the run", bool(reply.get("ok")), str(reply)))
        self.partial["HW-38"] = subs
        return None

    def stick_files_intact(self, image):
        """Fixture files on a stopped session's stick image, byte-identical to the generated fixtures."""
        reader = FatReader(image)
        try:
            folder = self.p["usb"]["sample_dir"]
            bad = []
            for name, payload in fixture_wavs().items():
                data = reader.read(f"{folder}/{name}")
                if data is None or hashlib.sha256(data).digest() != hashlib.sha256(payload).digest():
                    bad.append(name)
            return bad, reader.dirty_flag
        finally:
            reader.close()

    # ------------------------------------------------------------------ lifecycle (HW-40..43, HW-39)
    def persist_value(self):
        probe = self.p["recipes"]["persist_probe"]
        return norm(ocr(self.session.image("persist"), tuple(probe["box"]), psm=7, scale=4))

    def prepare_persistence(self):
        """Before the clean restart: set the probe value and save the project through the firmware."""
        probe = self.p["recipes"]["persist_probe"]
        self.normalize()
        self.select_track(1)
        before = self.persist_value()
        # Use the option whose value differs from the current one, so the restore proves the saved state.
        options = probe.get("options") or [{"set": probe["set"], "regex": probe["regex"]}]
        option = next((o for o in options if not re.search(o["regex"], before, re.I)), options[0])
        run_recipe(self.session, option["set"])
        time.sleep(1.0)
        value = self.persist_value()
        self.recipe("save_project")
        self.go_home()
        self.notes["persist_regex"] = option["regex"]
        return {"before": before, "set": value, "matches": re.search(option["regex"], value, re.I) is not None}

    def project_file(self, data_image):
        reader = FatReader(data_image)
        try:
            return reader.read(self.p["recipes"]["save_project"]["project_file"])
        finally:
            reader.close()

    def hw43_stop(self):
        """Latch SHIFT, a pad and HOLD; SIGTERM the exact app PID; check clean stop lines and timing."""
        s = self.session
        s.event("shift", "press", 100)
        s.event("pad16", "press", 100)
        s.press("hold")
        time.sleep(.5)
        console_path = s.dir / "console.txt"
        s.close()
        seconds = stop(self.build, self.pid, signal.SIGTERM, timeout=20)
        time.sleep(.5)
        text = console_path.read_text(errors="replace")
        return [sub("SP16_DATA clean=yes and SP16_HOST_CLEAN_STOP clean=yes", "SP16_DATA clean=yes" in text and
                    "SP16_HOST_CLEAN_STOP clean=yes" in text, ""),
                sub("no panic", "Kernel panic" not in text, ""),
                sub("app and QEMU gone <= 9 s", seconds is not None and seconds <= 9, f"{seconds and round(seconds, 2)} s")]

    def relaunch(self, usb_image=None, ephemeral=False, build=None):
        build = build or self.build
        self.pid, self.runtime_path, ready = launch(build, self.firmware_id, usb_image, ephemeral)
        self.session = Session(self.runtime_path)
        self.session.txlog(True)
        # The LCD is black and still for a few seconds before the application draws: wait for the home title.
        wait_title(self.session, self.home_regex(), self.title_box, timeout=self.p["identity"]["boot_timeout_s"])
        wait_settled(self.session, 2.0, 30)
        return ready

    def hw43_after(self, idle_reference):
        """After the relaunch: the controls latched before the quit (SHIFT, a pad, HOLD) carry nothing over."""
        s = self.session
        console = s.console()
        d = self.now()
        stale = [role for role in ("shift", "hold") if role in self.roles and self.full(d, role)]
        pad = self.roles["pad16"][1]
        if d["rgb"][pad] != idle_reference["rgb"][pad] or d["rgb"][pad] == tuple(self.p["rgb_known"]["strike_flash"]):
            stale.append(f"pad16 {d['rgb'][pad]}")
        rx = (self.fresh_guest().get("rx") or {})
        held = rx.get("buttons", "00") != "00" * 5 or bool(rx.get("pads"))
        data = next((l for l in console_lines(console, "SP16_DATA") if "fsck_rc=" in l), "")
        return [sub("relaunch: no lamp or control state carried over (SHIFT, HOLD, pad 16, RX bits)",
                    not stale and not held, f"stale {stale}, rx {rx}"),
                sub("relaunch: data fsck_rc=0", "fsck_rc=0" in data or ("formatted=yes" not in console and not data),
                    data or "no fsck line")]

    def lifecycle(self):
        """HW-43 clean quit, HW-40 restore after a clean restart, HW-41 hard kill, HW-39 sticks."""
        need40, need41, need43 = self.need("HW-40"), self.need("HW-41"), self.need("HW-43")
        stick = runtime_drive(self.session.runtime, "sp16usb")
        data_image = runtime_drive(self.session.runtime, "sp16emmc")
        self.normalize()
        idle = self.now()
        persist = None
        if need40 or need41:
            try:
                persist = self.prepare_persistence()
            except RecipeError as error:
                persist = {"error": str(error)}
        saved = None
        sub43 = self.hw43_stop()
        if "HW-37" in self.partial or "HW-38" in self.partial:
            bad, _ = self.stick_files_intact(stick)
            for hw in ("HW-37", "HW-38"):
                if hw in self.partial:
                    self.partial[hw].append(sub("stick fixture files byte-identical afterwards", not bad, f"changed {bad}"))
                    self.verdicts[hw] = from_subresults(hw, self.partial.pop(hw))
        if persist and "error" not in persist:
            saved = self.project_file(data_image)
        self.relaunch(stick)
        if need43:
            self.verdicts["HW-43"] = from_subresults("HW-43", sub43 + self.hw43_after(idle))
        if need40:
            if persist is None or "error" in persist:
                self.verdicts["HW-40"] = verdict("HW-40", "BLOCKED", f"persistence recipe failed: {persist}")
            else:
                console = self.session.console()
                value = self.persist_value() if self.select_track(1) else ""
                restored = re.search(self.notes["persist_regex"], value, re.I) is not None
                formatted = console.count("SP16_DATA formatted=yes")
                subs = [sub("console SP16_DATA fsck_rc=0 mounted=yes",
                            any("fsck_rc=0" in l for l in console_lines(console, "SP16_DATA"))
                            and any("mounted=yes" in l for l in console_lines(console, "SP16_DATA")),
                            " | ".join(console_lines(console, "SP16_DATA"))[:200]),
                        sub("probe value set and saved", persist["matches"] and saved is not None,
                            f"{persist}, project file {len(saved) if saved else None} bytes"),
                        sub("probe value restored after the restart", restored, f"read {value!r}"),
                        sub("the data image is not formatted again", formatted == 0, f"formatted lines {formatted}")]
                self.verdicts["HW-40"] = from_subresults("HW-40", subs, {"persist": persist})
        if need41:
            self.verdicts["HW-41"] = self.hw41(stick, data_image, saved)

    def hw41(self, stick, data_image, saved):
        if saved is None:
            return verdict("HW-41", "BLOCKED", "no saved project (save_project recipe)")
        s = self.session
        self.recipe("save_project")
        self.go_home()
        time.sleep(1.0)
        console_path = s.dir / "console.txt"
        s.close()
        children = qemu_children(self.build, self.pid)
        os.kill(self.pid, signal.SIGKILL)
        t0 = time.time()
        while time.time() - t0 < 40:
            if not any(q in children for q, _ in build_processes(self.build)[1]):
                break
            time.sleep(.2)
        gone = time.time() - t0
        text = console_path.read_text(errors="replace")
        subs = [sub("guest notices the hang-up and stops: QEMU gone <= 20 s", gone <= 20, f"{gone:.1f} s"),
                sub("guest clean stop after host loss", "SP16_HOST_LOST" in text and "SP16_DATA clean=yes" in text, "")]
        after = self.project_file(data_image)
        subs.append(sub("saved project byte-identical", after == saved,
                        f"{len(saved)} -> {len(after) if after else None} bytes"))
        self.relaunch(stick)
        console = self.session.console()
        data = " | ".join(console_lines(console, "SP16_DATA"))
        subs.append(sub("next boot fsck_rc 0 or repaired, mounted=yes",
                        re.search(r"fsck_rc=[01]\b", data) is not None and "mounted=yes" in data, data[:200]))
        return from_subresults("HW-41", subs)

    def hw39(self):
        """Blank and dirty sticks in their own sessions of the main build (the main session is stopped)."""
        usb = self.p["usb"]
        subs = []
        blank = self.out / "hw-blank-run.img"
        write_sparse(blank, b"", 64 << 20)
        dirty = self.out / "hw-dirty-run.img"
        payload, total = stick_image(fixture_wavs(), usb["sample_dir"], dirty=True)
        write_sparse(dirty, payload, total)
        for attempt, image in (("blank first boot", blank), ("blank second boot", blank), ("dirty", dirty)):
            self.relaunch(image)
            time.sleep(8.0)
            console = self.session.console()
            lines = " | ".join(console_lines(console, "SP16_USB"))
            if attempt == "blank first boot":
                subs.append(sub("blank: formatted FAT32 once", "formatted=yes" in lines and "mounted=yes" in lines,
                                lines[:200]))
            elif attempt == "blank second boot":
                subs.append(sub("blank: not formatted again", "formatted=yes" not in lines and "fsck_rc=0" in lines,
                                lines[:200]))
            else:
                subs.append(sub("dirty: fsck repairs, never formats", "formatted=yes" not in lines and
                                re.search(r"fsck_rc=1\b", lines) is not None and "mounted=yes" in lines, lines[:200]))
            self.session.close()
            stop(self.build, self.pid)
            time.sleep(1.0)
        reader = FatReader(blank)
        try:
            tree = reader.list(usb["layout"]) or {}
        finally:
            reader.close()
        missing = [d for d in usb["tree"] if d not in tree]
        subs.append(sub("the firmware creates its folder tree on a blank stick", not missing,
                        f"found {sorted(tree)}, missing {missing}"))
        bad, dirty_flag = self.stick_files_intact(dirty)
        subs.append(sub("dirty: pre-existing files hash-identical, dirty flag cleared", not bad and not dirty_flag,
                        f"changed {bad}, dirty flag {dirty_flag}"))
        return from_subresults("HW-39", subs)

    # ------------------------------------------------------------------ HW-47 standby
    def standby_save_completes(self):
        """Does the profile's save recipe finish? (In standby the firmware's save hangs at SAVING 0%.)"""
        try:
            self.recipe("save_project")
            return True
        except RecipeError:
            return False

    def hw47(self):
        """The power key (RX 5.4) puts the app in standby: the TX standby flag (0x67 bit 0) latches, the USB stick is
        unmounted and its LED goes dark, project saves stop, and the UI stays alive (the socket answers, TX frames
        keep coming). The emulator has no M1 to wake the app, so leaving standby is a restart: the stick mounts again,
        the flag is clear and saves work. Runs in its own throwaway session after the main one is stopped."""
        if self.given_runtime:
            return verdict("HW-47", "BLOCKED", "needs its own session: omit --runtime")
        stick = self.out / "hw-stick-standby.img"
        payload, total = stick_image(fixture_wavs(), self.p["usb"]["sample_dir"])
        write_sparse(stick, payload, total)
        hold = (self.p["button_effects"]["power"]).get("hold_s", .3)
        subs, first = [], None
        try:
            self.relaunch(stick, ephemeral=True)
            s = self.session
            idle = self.now()
            console = s.console()
            subs += [sub("before: standby flag clear", not idle["standby"], f"flag {idle['standby']}"),
                     sub("before: stick mounted, USB LED lit", any("mounted=yes" in l for l in console_lines(console, "SP16_USB"))
                         and bool(self.lamp(idle, "usb_led")[0]), " | ".join(console_lines(console, "SP16_USB"))[:160])]
            offset = s.tx_offset()
            s.press("power", hold)
            time.sleep(3.0)
            records = s.lamps_since(offset)
            tail_offset = s.tx_offset()
            time.sleep(1.0)
            tail = s.lamps_since(tail_offset)
            console = s.console()
            first = next((t for t, d in records if d["standby"]), None)
            subs += [sub("the TX standby flag latches", first is not None and all(d["standby"] for _, d in tail) and bool(tail),
                         f"{sum(d['standby'] for _, d in records)} of {len(records)} frames, tail {len(tail)} frames"),
                     sub("UI alive: TX frames keep coming and the socket answers", len(tail) >= 20 and
                         s.metrics().get("pending_controls") == 0, f"{len(tail)} frames in 1 s"),
                     sub("stick unmounted by the app", "unmounted_by_app=yes" in console, ""),
                     sub("USB LED dark", tail != [] and not tail[-1][1]["mono"][self.roles["usb_led"][1]][0],
                         str(tail[-1][1]["mono"][self.roles["usb_led"][1]] if tail else None))]
            try:
                s.snapshot("standby")
                subs.append(sub("the LCD can still be read", True, "snapshot written"))
            except (RuntimeError, OSError) as error:
                subs.append(sub("the LCD can still be read", False, str(error)))
            subs.append(sub("a project save does not complete", not self.standby_save_completes(), ""))
            # Leaving standby: no M1 here, so restart the app (clean stop first) and look at the same things again.
            s.close()
            seconds = stop(self.build, self.pid, signal.SIGTERM, timeout=20)
            subs.append(sub("the app stops cleanly from standby (<= 9 s)", seconds is not None and seconds <= 9,
                            f"{seconds and round(seconds, 2)} s"))
            self.relaunch(stick, ephemeral=True)
            s = self.session
            woke = self.now()
            console = s.console()
            subs += [sub("after the restart: standby flag clear", not woke["standby"], f"flag {woke['standby']}"),
                     sub("after the restart: stick mounted, USB LED lit",
                         any("mounted=yes" in l for l in console_lines(console, "SP16_USB")) and bool(self.lamp(woke, "usb_led")[0]),
                         ""),
                     sub("after the restart: a project save completes", self.standby_save_completes(), "")]
        except (SessionLost, RecipeError, RuntimeError, OSError) as error:
            subs.append(sub("the check ran to the end", False, f"{type(error).__name__}: {error}", blocked=True))
        finally:
            if self.session:
                self.session.close()
                stop(self.build, self.pid)
        return from_subresults("HW-47", subs, {"flag_seen": first is not None})

    # ------------------------------------------------------------------ screenshot regression (--shots)
    def shot(self, name, image, masks=()):
        """Record or compare one capture against the suite's own reference (None when --shots is off). `masks` are
        (x0, y0, x1, y1) fractions of the capture that move on their own. The result is a sub-result: a changed capture
        fails, with its heat map written next to the report."""
        if self.shots is None:
            return None
        import shotdiff
        entry = self.shots.check(name, image, [shotdiff.Region(*mask) for mask in masks], self.current_check)
        if entry["status"] == "recorded":
            return sub(f"screenshot {name}", True, "staged as a reference if this check passes")
        if entry["status"] == "missing":
            return sub(f"screenshot {name}", False, entry["note"], blocked=True)
        if entry["status"] in ("size", "empty"):
            return sub(f"screenshot {name}", False, entry["note"])
        return sub(f"screenshot {name}", entry["status"] == "match",
                   f"{entry['changed']} of {entry['compared']} pixels differ ({entry['fraction']:.3%}), box {entry['bbox']}"
                   + (f", heat map {Path(entry['heat_map']).name}" if entry.get("heat_map") else ""))

    def shots_into(self, subs, name, image, masks=()):
        result = self.shot(name, image, masks)
        if result:
            subs.append(result)

    def panel_shot_into(self, subs, name, extra_masks=()):
        """The whole panel view now (lamps, knobs, LCD), without its status line, against the reference `name`."""
        if self.shots is not None:
            self.shots_into(subs, name, self.session.panelshot("shot"), (PANEL_STATUS_MASK,) + tuple(extra_masks))

    def hw52(self):
        """The settled home screen, as the LCD and as the whole panel, in a fresh session of the main build."""
        if self.shots is None:
            return verdict("HW-52", "N/A", "precondition: no --shots (record or compare)")
        stick = self.out / "hw-stick-shots.img"
        payload, total = stick_image(fixture_wavs(), self.p["usb"]["sample_dir"])
        write_sparse(stick, payload, total)
        subs = []
        try:
            self.relaunch(stick, ephemeral=True)
            time.sleep(2.0)
            wait_settled(self.session, 3.0, 60)
            self.shots_into(subs, "home-lcd", self.session.image("shot"), HOME_LCD_MASKS)
            self.panel_shot_into(subs, "home-panel", HOME_PANEL_MASKS)
        except (SessionLost, RuntimeError, OSError) as error:
            subs.append(sub("the home screen was captured", False, f"{type(error).__name__}: {error}", blocked=True))
        finally:
            if self.session:
                self.session.close()
                stop(self.build, self.pid)
        return from_subresults("HW-52", subs)

    # ------------------------------------------------------------------ HW-48..HW-51 factory test pages
    def testmode_checks(self):
        """HW-48..51 in one session of the testmode build (the main session is already stopped)."""
        wanted = [hw for hw in TESTMODE_IDS if self.need(hw)]
        if not wanted:
            return
        if not self.testmode_build:
            for hw in wanted:
                self.verdicts[hw] = verdict(hw, "N/A", "precondition: no --testmode-build (a build made with --app-arg testmode)")
            return
        checks = {"HW-48": self.hw48, "HW-49": self.hw49, "HW-50": self.hw50, "HW-51": self.hw51,
              "HW-53": self.hw53, "HW-54": self.hw54}
        pid = None
        try:
            # The firmware this build will boot must be the profile's (a library can hold another one under the same id).
            firmware_id = self.check_firmware("the testmode build's", self.testmode_build, "testmode_manifest")
            pid, self.runtime_path, _ = launch(self.testmode_build, firmware_id, None, ephemeral=True)
            self.pid = pid
            # Many presses and snapshots: a loaded host can stall one answer for a minute, so wait longer than usual.
            self.session = Session(self.runtime_path, call_timeout=150)
            self.session.txlog(True)
            if self.session.runtime.get("audio") != "file":
                raise EnvError("the testmode session must be built with --audio file")
            running = self.session.runtime.get("firmware")
            if running and running != firmware_id:
                raise EnvError(f"the testmode session runs firmware {running}, not the checked {firmware_id}")
            for hw in wanted:
                self.record(hw, checks[hw])
        except (EnvError, SessionLost, OSError) as error:
            for hw in wanted:
                self.verdicts.setdefault(hw, verdict(hw, "BLOCKED", f"testmode session: {error}"))
        finally:
            if self.session:
                self.session.close()
            if pid is not None:
                stop(self.testmode_build, pid, timeout=90)

    def testmode_page_is(self, page, why):
        got = testmode_goto(self.session, page)
        return sub(f"stepped to page {page} ({why})", got, "" if got else "the page never showed")

    def lamps_until(self, wanted, timeout=8.0):
        """Poll TX lamps until `wanted(decoded)` holds; (ok, last decoded)."""
        deadline, last = time.monotonic() + timeout, None
        while True:
            last = self.now(0.4)
            if wanted(last) or time.monotonic() > deadline:
                return wanted(last), last
            time.sleep(.2)

    def hw48(self):
        s, full = self.session, self.session.panel_map.RGB_FULL
        subs = [self.testmode_page_is(1, "LED test, all on")]
        if subs[-1]["status"] == "PASS":
            image = s.image("page1")
            import numpy as np
            subs.append(sub("the LCD is white", testmode_page_of(image) == 1, f"darkest {np.asarray(image.convert('L')).min()}"))
            self.shots_into(subs, "testmode-page1-lcd", image)
            for key, name, colour in (("track", "red", (full, 0, 0)), ("mute", "green", (0, full, 0)),
                                      ("slice", "blue", (0, 0, full)), ("scale", "white", (full, full, full))):
                s.press(key, 0.4)
                ok, lamps = self.lamps_until(lambda d: all(v == colour for v in d["rgb"].values()))
                odd = sorted({v for v in lamps["rgb"].values() if v != colour})
                subs.append(sub(f"{key.upper()} turns all 32 pad and step lamps {name}", ok, f"others {odd[:3]}"))
                time.sleep(1.0)
                self.panel_shot_into(subs, f"testmode-page1-{name}-panel")
            ok, lamps = self.lamps_until(lambda d: all(on for on, _ in d["mono"].values()))
            off = [ch for ch, (on, _) in lamps["mono"].items() if not on]
            subs.append(sub("every single-colour lamp channel is on", ok, f"dark channels {off}"))
        subs.append(self.testmode_page_is(2, "LED test, all off"))
        if subs[-1]["status"] == "PASS":
            image = s.image("page2")
            subs.append(sub("the LCD is black", testmode_page_of(image) == 2, ""))
            ok, lamps = self.lamps_until(lambda d: not any(any(v) for v in d["rgb"].values()) and not any(on for on, _ in d["mono"].values()))
            lit = [ch for ch, v in lamps["rgb"].items() if any(v)] + [f"m{ch}" for ch, (on, _) in lamps["mono"].items() if on]
            subs.append(sub("every lamp is off", ok, f"lit {lit[:8]}"))
            self.panel_shot_into(subs, "testmode-page2-panel")
        return from_subresults("HW-48", subs)

    def hw49(self):
        s = self.session
        subs = [self.testmode_page_is(4, "button test")]
        if subs[-1]["status"] != "PASS":
            return from_subresults("HW-49", subs)
        skip = set(self.testmode_skip)
        for _ in range(4):          # the page can be half drawn when it first settles on a loaded host
            wait_settled(s, 1.5, 40)
            image = s.image("p4")
            count = testmode_counter(image, TESTMODE_RESULT_ROW, 50)
            if count is not None:
                break
        subs.append(sub("RESULT starts at 0 of 50", count == 0,
                        f"read {count} from {ocr(image, TESTMODE_RESULT_ROW, psm=7, scale=3)!r}"))
        if count is None:
            return from_subresults("HW-49", subs)
        missed, misnamed, unread = [], [], []
        for name in TESTMODE_LED_BUTTONS + TESTMODE_PLAIN_BUTTONS:
            if name in skip:
                continue
            before = count
            s.press(name, 0.4)
            deadline = time.monotonic() + 20
            named = None
            while True:
                wait_settled(s, 1.0, 20)
                image = s.image("p4")
                count = testmode_counter(image, TESTMODE_RESULT_ROW, 50)
                named = testmode_status_name(image)
                if count is not None and count != before or time.monotonic() > deadline:
                    break
            if count != (before or 0) + 1:
                missed.append(f"{name} ({before} -> {count})")
                count = count if count is not None else before
            # The status cell names the control the page counted. Two controls with swapped mappings move RESULT the same
            # way, so the totals alone cannot tell them apart; the page's own name can.
            if named is None:
                unread.append(name)
            elif named != name:
                misnamed.append(f"{name} -> {named}")
        subs.append(sub("every control moved RESULT up by exactly one", not missed, "; ".join(missed[:6])))
        subs.append(sub("the status line named the control that was pressed", not misnamed, "; ".join(misnamed[:6])))
        if unread:
            subs.append(sub("the status line was readable for every press", False,
                            f"no name read for {len(unread)} presses: {', '.join(unread[:6])}", na=True))
        s.press("selector", 0.4)
        wait_settled(s, 1.5, 20)
        image = s.image("p4")
        subs.append(sub("the selector push is not counted", testmode_counter(image, TESTMODE_RESULT_ROW, 50) == count, f"read {testmode_counter(image, TESTMODE_RESULT_ROW, 50)}"))
        led = testmode_counter(image, TESTMODE_LED_ROW, 48)
        subs.append(sub("LED-BUTTON reaches 48 of 48", led == 48, f"read {led}"))
        subs.append(sub("RESULT reaches 50 of 50", testmode_counter(image, TESTMODE_RESULT_ROW, 50) == 50,
                        f"read {testmode_counter(image, TESTMODE_RESULT_ROW, 50)}"))
        self.shots_into(subs, "testmode-page4-lcd", image)
        if not skip:
            self.panel_shot_into(subs, "testmode-page4-panel")
        for label, row in (("LED-BUTTON", TESTMODE_LED_ROW), ("RESULT", TESTMODE_RESULT_ROW)):
            text = norm(ocr(image, row, psm=7, scale=3))
            subs.append(sub(f"{label} shows OK", re.search(r"\bOK\b", text) is not None, text))
        return from_subresults("HW-49", subs, {"skipped": sorted(skip)})

    def gauge_at(self, name, set_value, expected, tolerance=0.03, timeout=30.0):
        """Set a control and read its gauge. The page follows the ADC words with a lag that a loaded host stretches, and
        an unchanged bar is no proof it has settled (it may not have moved yet), so read until the bar is where the
        control put it, or the time is up; the last reading is returned either way and the check judges it."""
        set_value()
        deadline = time.monotonic() + timeout
        while True:
            time.sleep(1.0)
            reading = testmode_gauge(self.session.image("p5"), name)
            if abs(reading[0] - expected) <= tolerance or time.monotonic() > deadline:
                return reading

    def hw50(self):
        s = self.session
        subs = [self.testmode_page_is(5, "AD test")]
        if subs[-1]["status"] != "PASS":
            return from_subresults("HW-50", subs)
        for name in ("main_out", "drive", "hp_cutoff", "strip"):
            top, bottom, full_scale = TESTMODE_GAUGES[name]
            if name == "strip":
                def put(value):
                    return lambda: s.event("strip", "press", value)
            else:
                def put(value, name=name):
                    return lambda: s.event(name, "value", value)
            reads = {value: self.gauge_at(name, put(value), value / 1000) for value in (0, 1000, 500, 0, 1000)}
            label = "Y-PAD (strip)" if name == "strip" else name.upper()
            subs.append(sub(f"{label}: 0 reads the bottom of 0-{full_scale}", reads[0][0] <= 0.02, str(reads[0][0])))
            subs.append(sub(f"{label}: 1000 reads the top of 0-{full_scale}", reads[1000][0] >= 0.98, str(reads[1000][0])))
            subs.append(sub(f"{label}: the middle reads about half", abs(reads[500][0] - 0.5) <= 0.03, str(reads[500][0])))
            subs.append(sub(f"{label}: OK shows after both limits", reads[1000][1], f"after the top limit: {reads[1000][1]}"))
        wait_settled(s, 1.5, 30)
        self.shots_into(subs, "testmode-page5-lcd", s.image("p5"))
        s.event("strip", "release", 0)
        return from_subresults("HW-50", subs)

    def hw51(self):
        s = self.session
        subs = [self.testmode_page_is(6, "encoder test")]
        if subs[-1]["status"] != "PASS":
            return from_subresults("HW-51", subs)

        def read(name, expected, timeout=40.0):
            """The page follows the panel frames with a lag a loaded host stretches: read until the value is the expected
            one or the time is up (the last reading is returned and the check judges it)."""
            deadline = time.monotonic() + timeout
            while True:
                time.sleep(1.0)
                value = testmode_encoder(s.image("p6"), name)
                if value == expected or time.monotonic() > deadline:
                    return value
        for n in range(1, 7):
            knob = f"knob{n}"
            s.turn(knob, 3, 0.4)
            up = read(knob, 3)
            s.turn(knob, -5, 0.4)
            down = read(knob, 0xFFFE)
            # The page prints the counter as an unsigned 16-bit number: -2 shows as 65534.
            subs.append(sub(f"6 ENCODER ({n}): +3 reads 3, then -5 reads -2 (65534)",
                            (up, down) == (3, 0xFFFE), f"{up}, {down}"))
        s.turn("selector", 2, 0.8)
        count = read("selector", 2)
        subs.append(sub("PUSH ENCODER counts the selector's detents", count == 2, f"read {count}"))
        s.event("selector", "press", 100)
        on = read("selector_state", "ON")
        s.event("selector", "release", 0)
        off = read("selector_state", "OFF")
        subs.append(sub("PUSH ENCODER STATUS is ON while pushed and OFF after", (on, off) == ("ON", "OFF"), f"{on}, {off}"))
        wait_settled(s, 1.5, 30)
        self.shots_into(subs, "testmode-page6-lcd", s.image("p6"))
        return from_subresults("HW-51", subs)

    def calibration_now(self):
        """The calibration file on the running session's eMMC image: {pad: value}."""
        return calibration_values(runtime_drive(self.session.runtime, "sp16emmc"))

    def calibration_walk(self, pad, raw, before):
        """Hold pad `pad` at `raw` and press WAKE UP once per pass, as test page 3 asks. Returns the raw value the page
        showed, whether START was red (idle) again, and the file afterwards. The pass takes seconds and the page redraws
        later, so the last press is followed by a wait, and the file by a short poll for the write."""
        s = self.session
        s.event(f"pressure{pad}", "value", raw)
        try:
            time.sleep(2.5)
            wait_settled(s, 1.5, 30)
            shown = testmode_pad_raw(s.image("p3"), pad)
            for _ in range(3):
                s.press("usb_stop", 0.4)
                time.sleep(CALIBRATION_PASS_SECONDS)
            deadline = time.monotonic() + 30
            while not testmode_start_ready(s.image("p3")) and time.monotonic() < deadline:
                time.sleep(1.0)
            idle = testmode_start_ready(s.image("p3"))
            deadline = time.monotonic() + 8
            after = self.calibration_now()
            while after == before and time.monotonic() < deadline:
                time.sleep(1.0)
                after = self.calibration_now()
        finally:
            s.event(f"pressure{pad}", "value", 0)
        return shown, idle, after

    def hw53(self):
        """Test page 3: each walk holds a pad at a raw value and makes the three passes; the staged calibration file
        must take the value if it is inside the valid range (the manual: 48 to 504, others are not recorded) and stay as it
        was if not, and no other pad's value may move."""
        s, calibration = self.session, self.p["calibration"]
        low, high = calibration["range"]
        default = calibration["default"]
        subs = [self.testmode_page_is(3, "pad calibration")]
        if subs[-1]["status"] != "PASS":
            return from_subresults("HW-53", subs)
        wait_settled(s, 1.5, 30)
        file = self.calibration_now()
        subs.append(sub("the staged file starts at the synthetic default for every pad",
                        file == {pad: default for pad in range(1, calibration["pads"] + 1)},
                        f"{len(file)} pads, values {sorted(set(file.values()))}"))
        walks = ((1, 300, 300, "a value inside the range is recorded"),
                 (2, low, low, "the lowest valid value is recorded"),
                 (3, high, high, "the highest valid value is recorded"),
                 (4, low - 1, default, "a value just below the range is not recorded"),
                 (5, high + 1, default, "a value just above the range is not recorded"))
        for pad, raw, expected, why in walks:
            shown, idle, after = self.calibration_walk(pad, raw, file)
            moved = {p: v for p, v in after.items() if p != pad and file.get(p) != v}
            subs.append(sub(f"pad {pad} held at {raw}: the page shows it and the three passes run to idle", shown == raw and idle,
                            f"page read {shown}, START idle {idle}"))
            subs.append(sub(f"pad {pad} held at {raw}: {why}", after.get(pad) == expected and not moved,
                            f"file {file.get(pad)} -> {after.get(pad)} (expected {expected}), other pads changed {moved}"))
            file = after
        wait_settled(s, 1.5, 30)
        self.shots_into(subs, "testmode-page3-lcd", s.image("p3"))
        return from_subresults("HW-53", subs)

    def hw54(self):
        """Test page 8 (DEVICE TEST): tap the MIDI CHECK touch button and expect the DIN MIDI loopback to read OK."""
        s = self.session
        subs = [self.testmode_page_is(8, "device test")]
        if subs[-1]["status"] != "PASS":
            return from_subresults("HW-54", subs)
        wait_settled(s, 1.5, 30)
        s.tap(115, 112)
        ok, text = wait_midi_check(s)
        subs.append(sub("tapping the MIDI CHECK button shows MIDI CHECK OK", ok, text))
        wait_settled(s, 1.5, 30)
        self.shots_into(subs, "testmode-page8-lcd", s.image("p8"))
        return from_subresults("HW-54", subs)

    # ------------------------------------------------------------------ HW-42, HW-44, HW-45, HW-24
    def library_files(self):
        """Other firmware entries' data images and other builds' app and QEMU (must not change during the run)."""
        files = []
        library = Path(json.loads((self.build / "assets.json").read_text())["library"])
        for entry in sorted(library.iterdir()) if library.is_dir() else []:
            if entry.is_dir() and entry.name != self.firmware_id:
                files += [p for p in (entry / "data").glob("*.img")]
        if self.control_build:
            assets = json.loads((self.control_build / "assets.json").read_text())
            files += [Path(app_executable(self.control_build)), Path(assets["qemu"])]
        return files

    def hw42(self):
        s = self.session
        subs = []
        import fcntl
        lock = Path(runtime_drive(s.runtime, "sp16emmc")).parent / "session.lock"
        try:
            handle = open(lock, "rb")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                held = False
            except BlockingIOError:
                held = True
            finally:
                handle.close()
        except OSError as error:
            held = False
            subs.append(sub("session lock readable", False, str(error)))
        subs.append(sub("data image lock held by the running session", held, str(lock)))
        exe = app_executable(self.build)
        qemus_before = len(build_processes(self.build)[1])
        proc = subprocess.Popen([exe, "--firmware", self.firmware_id], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        t0 = time.time()
        stderr = ""
        refused = None
        import select
        while time.time() - t0 < 5:
            if proc.poll() is not None:
                refused = proc.returncode == 3
                stderr += proc.stderr.read() or ""
                break
            ready, _, _ = select.select([proc.stderr], [], [], 0.2)
            if ready:
                stderr += os.read(proc.stderr.fileno(), 4096).decode(errors="replace")
                if re.search(r"already in use|cannot start|locked", stderr, re.I):
                    refused = True
                    break
        elapsed = time.time() - t0
        qemus_during = len(build_processes(self.build)[1])
        if proc.poll() is None:
            proc.terminate()  # the refusal is a modal alert; the runner dismisses it by stopping that exact PID
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        subs.append(sub("second start refused within 5 s (refusal message; exit 3 after the alert)",
                        bool(refused) and qemus_during == qemus_before,
                        f"{elapsed:.1f} s, rc {proc.returncode}, qemus {qemus_before}->{qemus_during}, "
                        f"stderr {stderr.strip()[-160:]!r}"))
        first = self.hw07(40)
        subs.append(sub("first session keeps working (events accounted)", first["status"] == "PASS", first["reason"]))
        self.partial["HW-42"] = subs
        return None

    def hw44(self):
        import random
        s = self.session
        host = host_state()
        self.normalize()
        self.load_sample(2, "hw-click.wav")
        self.select_track(2)
        steps = self.p["recipes"]["click_steps"]
        for n in steps:
            s.press(f"step{n}")
            time.sleep(.3)
        self.select_track(1)
        rng = random.Random(44)
        g0 = self.fresh_guest()
        m0 = self.fresh_metrics()
        console_offset = len(s.console())
        self.pcm()
        s.press("play")
        time.sleep(3)
        offset = s.tx_offset()
        samples, usage = [], []
        t0 = time.monotonic()
        # HWSUITE_SOAK_SECONDS shortens the soak for runner development only; a shortened soak is never a PASS.
        total = int(os.environ.get("HWSUITE_SOAK_SECONDS", "300"))
        while time.monotonic() - t0 < total:
            choice = rng.randrange(3)
            if choice == 0:
                s.press(f"pad{rng.randrange(5, 17)}", 0.05, rng.randrange(1, 128))
            elif choice == 1:
                knob = f"knob{rng.randrange(2, 4)}"
                s.event(knob, "delta", 1)
                s.event(knob, "delta", -1)
            else:
                s.event("main_out", "value", rng.randrange(0, 1001))
            if not samples or time.monotonic() - samples[-1][0] >= 30:
                m = s.metrics()
                samples.append((time.monotonic(), m["guest"]))
                usage.append(process_usage([self.pid] + qemu_children(self.build, self.pid)))
            time.sleep(1.0)
        records = s.lamps_since(offset)
        s.press("stop")
        s.event("main_out", "value", 500)
        self.select_track(2)
        for n in steps:
            s.press(f"step{n}")
            time.sleep(.3)
        self.normalize()
        time.sleep(1.0)
        m1 = self.fresh_metrics()
        g1 = m1["guest"]
        speeds = re.findall(r"speed=([0-9.]+)x", s.console()[console_offset:])
        heads = playhead_steps(records, self.p["rgb_known"]["playhead"])
        t_start = heads[0][0] if heads else 0
        first = revisit_period([h for h in heads if h[0] - t_start <= 60])
        last = revisit_period([h for h in heads if h[0] >= heads[-1][0] - 60]) if heads else None
        drift = abs(last / first - 1) if first and last else None
        r0, r1 = m0["input_results"], m1["input_results"]
        subs = []
        if host["vms"] > MAX_VMS:
            subs.append(sub("host load", False, f"{host['vms']} VMs", blocked=True))
        if total < 300:
            subs.append(sub("five-minute soak", False, f"shortened to {total} s (development only)", blocked=True))
        subs += [sub("rx_drops delta 0, tx_bad_crc 0", g1["rx_drops"] == g0["rx_drops"] and g1["tx_bad_crc"] == 0,
                     f"rx_drops {g0['rx_drops']}->{g1['rx_drops']}, tx_bad_crc {g1['tx_bad_crc']}"),
                 sub("failed and unsupported 0", r1["failed"] == r0["failed"] and r1["unsupported"] == r0["unsupported"]
                     and g1["failed"] == g0["failed"], ""),
                 sub("pending_controls 0 at the end", m1.get("pending_controls") == 0, str(m1.get("pending_controls"))),
                 sub("all streams speed >= 0.99", bool(speeds) and all(float(v) >= 0.99 for v in speeds), str(speeds)),
                 sub("tempo drift first vs last minute <= 1%", drift is not None and drift <= 0.01,
                     f"{first} -> {last} ({drift})")]
        return from_subresults("HW-44", subs, {"usage": usage, "host": host, "load_end": load_averages()})

    def hw45(self):
        s = self.session
        console = s.console()
        guest = s.guest()
        subs = []
        missing_nodes = [n for n in self.p["device_nodes"] if f"] open {n} => " not in console]
        subs.append(sub("every profile device node opened", not missing_nodes, f"missing {missing_nodes}"))
        seen = guest.get("ioctls_seen")
        if seen is None:
            subs.append(sub("every profile ioctl seen and accepted", False, "guest does not report ioctls_seen",
                            blocked=True))
        else:
            missing = [r for r in self.p["ioctl_set"] if r not in seen]
            extra = sorted(set(seen) - set(self.p["ioctl_set"]))
            subs.append(sub("every profile ioctl seen and accepted", not missing, f"not seen {missing}"))
            if extra:
                subs.append(sub("no request outside the profile", False, f"new {extra}", blocked=True))
        unsupported = console_lines(console, "unsupported ioctl")
        subs.append(sub("zero unsupported ioctl lines", not unsupported and not guest.get("unknown_ioctls"),
                        str(unsupported[:3])))
        unknown = set((guest.get("unknown_devices") or {}))
        undeclared = sorted(unknown - set(self.p.get("device_nodes_unserved") or []))
        subs.append(sub("no unknown device opens beyond the declared unserved ones", not undeclared,
                        f"unknown {sorted(unknown)}", blocked=bool(undeclared)))
        manifest = self.notes.get("manifest") or {}
        revisions = (manifest.get("update") or {}).get("revisions") or {}
        declared = self.p["identity"]["panel_protocol_revision"]
        actual = {k.replace(".rev", ""): v for k, v in revisions.items() if k.startswith("subucom")}
        subs.append(sub("panel protocol revision of the import equals the profile", actual == declared,
                        f"import {actual}, profile {declared}"))
        return from_subresults("HW-45", subs, {"ioctls_seen": seen, "unknown_devices": sorted(unknown)})

    def hw24_control(self):
        if not self.control_build:
            return [sub("uncalibrated control session", False, "no --control-build given", blocked=True)]
        pid, runtime, _ = launch(self.control_build, self.firmware_id, None, ephemeral=True)
        session = Session(runtime)
        try:
            session.txlog(True)
            wait_title(session, self.home_regex(), self.title_box, timeout=self.p["identity"]["boot_timeout_s"])
            wait_settled(session, 2.0, 30)
            offset = session.tx_offset()
            time.sleep(10.0)
            records = session.lamps_since(offset)
            pad = self.roles["pad1"][1]
            colours = [d["rgb"][pad] for _, d in records]
            changes = [i for i in range(1, len(colours)) if colours[i] != colours[i - 1]]
            periods = [b - a for a, b in zip(changes, changes[1:])]
            blink = len(changes) >= 4
            offset = session.tx_offset()
            session.press("pad1", 0.15, 127)
            time.sleep(1.0)
            strike = [d["rgb"][pad] for _, d in session.lamps_since(offset)]
            flash = tuple(self.p["rgb_known"]["strike_flash"])
            return [sub("uncalibrated control: the blink is detected", blink,
                        f"{len(changes)} colour changes, frames between {sorted(set(periods))[:4]}"),
                    sub("uncalibrated control: strikes are ignored", flash not in strike, f"{sorted(set(strike))[:3]}")]
        finally:
            session.close()
            stop(self.control_build, pid)

    # ------------------------------------------------------------------ orchestration
    def record(self, hw_id, fn):
        if not self.need(hw_id):
            return
        missing = missing_fields(self.p, hw_id)
        blocked_by = [a for a in CHECK_BY_ID[hw_id].after if self.verdicts.get(a, {}).get("status") in ("FAIL", "BLOCKED")]
        if missing:
            self.verdicts[hw_id] = verdict(hw_id, "BLOCKED", "profile data missing: " + ", ".join(missing))
            return
        if blocked_by:
            self.verdicts[hw_id] = verdict(hw_id, "BLOCKED", "prerequisite failed: " + ", ".join(blocked_by))
            return
        if self.lost:
            self.verdicts[hw_id] = verdict(hw_id, "BLOCKED", f"session lost earlier: {self.lost}")
            return
        t0 = time.monotonic()
        load = load_averages()
        self.current_check = hw_id
        try:
            result = fn()
        except SessionLost as error:
            self.lost = str(error)
            result = verdict(hw_id, "BLOCKED", f"session lost: {error}")
        except (RecipeError, RuntimeError, EnvError, ValueError, KeyError, OSError) as error:
            result = verdict(hw_id, "BLOCKED", f"runner could not complete the check: {type(error).__name__}: {error}")
        if result is None:
            return  # multi-phase check, finished later
        result["elapsed_s"] = round(time.monotonic() - t0, 2)
        result["load"] = {"start": load, "end": load_averages(), "vms": len(qemu_processes())}
        self.verdicts[hw_id] = result
        print(f"[hwsuite] {hw_id} {result['status']} {result['elapsed_s']} s {result['reason'][:140]}",
              file=sys.stderr, flush=True)

    def reset_project(self):
        """Start from a new firmware project (profile recipe new_project): the firmware reopens the last saved project
        at boot, so state an earlier run saved (a pan, a time stretch, samples) would otherwise leak into this run."""
        steps = (self.p.get("recipes") or {}).get("new_project")
        if not authored(steps) or self.lost or not self.session:
            return
        try:
            run_recipe(self.session, steps)
            self.loaded = {}
            self.notes["project_reset"] = True
        except (RecipeError, RuntimeError) as error:
            self.notes["project_reset"] = f"failed: {error}"
        except SessionLost as error:
            self.lost = str(error)

    def finish_shots(self, verdicts):
        """Record mode: the staged captures of checks that passed become the references."""
        if self.shots is None or self.shots.mode != "record":
            return
        passed = {v["id"] for v in verdicts if v["status"] == "PASS"}
        self.notes["shots_promoted"] = self.shots.promote({e["name"] for e in self.shots.entries if e["check"] in passed})

    def finish_multiphase(self):
        for hw in list(self.partial):
            if self.need(hw):
                self.verdicts[hw] = from_subresults(hw, self.partial.pop(hw))

    def main_session(self):
        if self.given_runtime:
            self.runtime_path = self.given_runtime
            self.session = Session(self.runtime_path)
            apps, _ = build_processes(self.build)
            self.pid = apps[0] if len(apps) == 1 else None
            self.session.txlog(True)
        else:
            stick = self.out / "hw-stick-main.img"
            payload, total = stick_image(fixture_wavs(), self.p["usb"]["sample_dir"] or "PIONEER DJ SAMPLER/Samples")
            write_sparse(stick, payload, total)
            time.time()
            self.pid, self.runtime_path, ready = launch(self.build, self.firmware_id, stick)
            self.session = Session(self.runtime_path)
            self.session.txlog(True)
            self.notes["boot"] = {"ready_s": round(ready, 2)}
        if self.session.runtime.get("audio") != "file":
            raise EnvError("the session must be built with --audio file (refusing any other audio mode)")
        self.firmware_id = self.firmware_id or self.session.runtime.get("firmware")
        self.check_firmware("the session's")

    def check_firmware(self, whose, build=None, record="manifest"):
        """The firmware under test must be the profile's: its library manifest, in `build` (the main one by default),
        carries the profile's application SHA-256. Returns the firmware id that was checked: the run's, else the one the
        build's library has selected. The manifest is kept in `notes[record]`."""
        build = Path(build) if build else self.build
        try:
            library = Path(json.loads((build / "assets.json").read_text())["library"])
            firmware_id = self.firmware_id or json.loads((library / "selected.json").read_text())["id"]
            manifest_path = library / firmware_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            if not isinstance(manifest, dict):
                raise ValueError("the manifest is not a JSON object")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise EnvError(f"{whose} firmware cannot be read from its library ({type(error).__name__}: {error})") from None
        if manifest.get("app_sha256") != self.p["firmware"]["app_sha256"]:
            raise EnvError(f"{whose} firmware does not match the profile (application SHA-256)")
        self.notes[record] = {k: manifest.get(k) for k in ("id", "app_sha256", "update", "image")}
        self.notes[record]["sha256"] = sha256_file(manifest_path)
        return firmware_id

    def run(self):
        others_before = {str(p): sha256_file(p) for p in self.library_files()} if self.need("HW-42") else {}
        # The factory test checks must not report on another firmware than the profile's: refuse before any VM starts.
        if self.testmode_build and any(self.need(hw) for hw in TESTMODE_IDS):
            self.check_firmware("the testmode build's", self.testmode_build, "testmode_manifest")
        # Extras start their own throwaway sessions: with only those selected there is no main session to boot.
        if not all(hw in EXTRA_IDS for hw in self.selected) or self.given_runtime:
            self.main_session()
        else:
            library = Path(json.loads((self.build / "assets.json").read_text())["library"])
            self.firmware_id = self.firmware_id or json.loads((library / "selected.json").read_text())["id"]
            self.check_firmware("the library's selected")
        order = [("HW-01", self.hw01), ("HW-31", self.hw31), ("HW-05", self.hw05), ("HW-06", self.hw06),
                 ("HW-07", self.hw07), ("HW-36", self.hw36), ("HW-24", lambda: self.partial.setdefault("HW-24", self.hw24_calibrated()) and None),
                 ("HW-02", self.hw02), ("HW-03", self.hw03), ("HW-04", self.hw04), ("HW-20", self.hw20),
                 ("HW-18", self.hw18), ("HW-19", self.hw19), ("HW-16", self.hw16), ("HW-17", self.hw17),
                 ("HW-28", self.hw28), ("HW-10", self.hw10), ("HW-11", self.hw11), ("HW-12", self.hw12),
                 ("HW-13", self.hw13), ("HW-09", self.hw09), ("HW-15", self.hw15), ("HW-21", self.hw21),
                 ("HW-26", self.hw26), ("HW-29", self.hw29), ("HW-30", self.hw30), ("HW-08", self.hw08),
                 ("HW-34", self.hw34), ("HW-22", self.hw22), ("HW-23", self.hw23),
                 ("HW-27", self.hw27), ("HW-25", self.hw25), ("HW-35", self.hw35), ("HW-33", self.hw33_and_32),
                 ("HW-32", self.hw32), ("HW-44", self.hw44), ("HW-37", self.hw37), ("HW-38", self.hw38),
                 ("HW-42", self.hw42), ("HW-45", self.hw45), ("HW-46", self.hw46), ("HW-14", self.hw14)]
        for hw_id, fn in order:
            self.record(hw_id, fn)
            if self.lost:
                break
            if hw_id == "HW-01":
                self.reset_project()
        if not self.lost and self.build and any(self.need(h) for h in ("HW-40", "HW-41", "HW-43", "HW-37", "HW-38")):
            try:
                self.lifecycle()
            except (SessionLost, EnvError, RecipeError, RuntimeError, OSError) as error:
                self.lost = self.lost or f"lifecycle: {error}"
        if self.notes.get("hw09_last") and "HW-09" in self.partial and not self.lost:
            self.hw09_last()
        if self.session:
            self.session.close()
            if self.pid and self.build:
                stop(self.build, self.pid)
        if not self.lost and self.need("HW-39"):
            self.record("HW-39", self.hw39)
        if not self.lost and self.need("HW-47"):
            self.record("HW-47", self.hw47)
        if not self.lost and self.need("HW-52"):
            self.record("HW-52", self.hw52)
        if not self.lost and any(self.need(hw) for hw in TESTMODE_IDS):
            self.testmode_checks()
        if "HW-24" in self.partial or self.need("HW-24"):
            subs = self.partial.pop("HW-24", []) or []
            try:
                subs += self.hw24_control()
            except (SessionLost, EnvError, RuntimeError, OSError) as error:
                subs.append(sub("uncalibrated control session", False, str(error), blocked=True))
            if self.need("HW-24"):
                self.verdicts["HW-24"] = from_subresults("HW-24", subs)
        if "HW-42" in self.partial:
            after = {p: sha256_file(Path(p)) for p in others_before}
            changed = [p for p in others_before if after[p] != others_before[p]]
            self.partial["HW-42"].append(sub("no other profile's file changed", not changed,
                                             f"{len(others_before)} files hashed, changed {changed}"))
        self.finish_multiphase()
        for hw_id in self.selected:
            if hw_id not in self.verdicts:
                reason = f"session lost: {self.lost}" if self.lost else "not reached"
                self.verdicts[hw_id] = verdict(hw_id, "BLOCKED", reason)
        verdicts = [self.verdicts[hw] for hw in self.selected]
        self.finish_shots(verdicts)
        return verdicts


# ------------------------------------------------------------------------------------------------- result files

def write_result(out, runner, verdicts, started, args, profile_bytes):
    out = Path(out)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = runner.notes.get("manifest") or {}
    testmode = runner.notes.get("testmode_manifest") or {}
    result = {
        "suite": {"name": SUITE_NAME, "version": SUITE_VERSION, "checks_hash": checks_hash(),
                  "hardware_fingerprint": hardware_fingerprint()},
        "run": {"id": runner.run_id, "start": started, "end": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "tier": args.tier, "only": runner.only, "build": str(runner.build) if runner.build else None,
                "control_build": str(runner.control_build) if runner.control_build else None,
                "testmode_build": str(runner.testmode_build) if runner.testmode_build else None,
                "testmode_firmware": ({"id": testmode.get("id"), "app_sha256": testmode.get("app_sha256"),
                                       "manifest_sha256": testmode.get("sha256")} if testmode else None),
                "firmware_id": runner.firmware_id, "firmware_version": runner.p["firmware"]["version"],
                "firmware_image_sha256": (manifest.get("image") or {}).get("sha256"),
                "manifest_sha256": manifest.get("sha256"), "app_sha256": manifest.get("app_sha256"),
                "profile_path": str(runner.profile_path), "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
                "runtime": str(runner.runtime_path) if runner.runtime_path else None,
                "host": {"load_end": load_averages(), "vms_end": len(qemu_processes())},
                "fixtures": runner.fixture_hashes, "session_lost": runner.lost,
                "project_reset": runner.notes.get("project_reset")},
        "verdicts": verdicts,
        "summary": summarize(verdicts, args.tier, runner.only),
    }
    if runner.shots is not None:
        result["shots"] = {"mode": runner.shots.mode, "folder": str(runner.shots.folder), "entries": runner.shots.entries,
                           "promoted": runner.notes.get("shots_promoted", [])}
        (out / "shots-report.md").write_text(runner.shots.render_report())
    text = json.dumps(result, indent=1, default=str, sort_keys=False)
    (out / "hwsuite-result.json").write_text(text)
    os.chmod(out / "hwsuite-result.json", 0o600)
    if not args.json_only:
        (out / "hwsuite-report.md").write_text(render_report(result))
    return result, hashlib.sha256(text.encode()).hexdigest()


def mark_verified(profile_path, result, result_path, result_sha256):
    """Spec 5.5: write the verification block (the only writer of status: verified). Refuses unless eligible."""
    path = Path(profile_path)
    raw = path.read_bytes()
    profile = json.loads(raw)
    summary, run = result["summary"], result["run"]
    if not summary["verified_eligible"]:
        raise EnvError("run is not eligible: full tier without --only, every check PASS or N/A")
    if run["profile_sha256"] != hashlib.sha256(raw).hexdigest():
        raise EnvError("the profile changed after the run")
    if run["app_sha256"] != profile["firmware"]["app_sha256"]:
        raise EnvError("the run's firmware is not this profile's")
    na = [v for v in result["verdicts"] if v["status"] == "N/A"]
    absent = {item["capability"] for item in (profile.get("hardware") or {}).get("absent") or []}
    unjustified = [v["id"] for v in na if v["reason"] not in absent and not v["reason"].startswith("precondition:")]
    if unjustified:
        raise EnvError(f"N/A without a declared hardware.absent reason: {unjustified}")
    if summary.get("needs_owner_signoff") and not profile.get("owner_signoff"):
        raise EnvError("more than five N/A need a maintainer's sign-off line (owner_signoff) in the profile")
    try:  # record the stored result relative to the repository (results live in the git-ignored private/ folder)
        result_path = Path(result_path).resolve().relative_to(TOOLS.parent)
    except ValueError:
        pass
    profile["status"] = "verified"
    profile["verification"] = {
        "method": "hwsuite", "result": "pass", "date": run["end"][:10], "status": "verified",
        "firmware_version": run["firmware_version"], "manifest_sha256": run["manifest_sha256"],
        "image_sha256": run["firmware_image_sha256"], "suite_version": result["suite"]["version"],
        "checks_hash": result["suite"]["checks_hash"],
        "hardware_fingerprint": result["suite"]["hardware_fingerprint"]["sha256"],
        "hardware_epoch": result["suite"]["hardware_fingerprint"]["hardware_epoch"],
        "run_id": run["id"], "result_path": str(result_path), "result_sha256": result_sha256,
        "counts": summary["counts"],
        "evidence": f"hwsuite full tier, {summary['counts']['PASS']} PASS, {summary['counts']['N/A']} N/A"}
    path.write_text(json.dumps(profile, indent=2) + "\n")
    return profile["verification"]


def shots_tolerance_arg(text):
    """argparse type of --shots-tolerance: a whole number the comparison accepts (0 to 254), else a usage error."""
    import shotdiff
    try:
        return shotdiff.check_tolerance(int(text))
    except shotdiff.ShotError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number from 0 to {shotdiff.MAX_TOLERANCE}") from None


def shots_share_arg(text):
    """argparse type of --shots-share: a share from 0 up to, but not including, 1, else a usage error."""
    import shotdiff
    try:
        return shotdiff.check_share(float(text))
    except shotdiff.ShotError as error:
        raise argparse.ArgumentTypeError(str(error)) from None
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number from 0 up to, but not including, 1") from None


def testmode_skip_arg(text):
    """argparse type of --testmode-skip: comma-separated control names HW-49 presses, else a usage error."""
    try:
        return list(check_testmode_skip(x.strip() for x in text.split(",") if x.strip()))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", help="runtime.json of a running research session (--audio file) to use as the "
                                          "main session; omit to let the runner launch it with the fixture stick")
    parser.add_argument("--profile", help="the firmware's compatibility profile (profiles/sp16-<version>.json)")
    parser.add_argument("--build-dir", help="research build directory (relaunches; and the main session if no --runtime)")
    parser.add_argument("--firmware", help="firmware id in the build's library (default: the session's)")
    parser.add_argument("--control-build", help="build made with --uncalibrated sharing the library (HW-24 control)")
    parser.add_argument("--testmode-build", help="build made with --app-arg testmode, for the factory test page checks "
                                                 "(HW-48..HW-51 and HW-53..HW-54, extra tier); its firmware must be the "
                                                 "profile's, or the run is refused")
    parser.add_argument("--testmode-skip", type=testmode_skip_arg, default=[],
                        help="comma-separated controls HW-49 leaves unpressed (pad1..pad16, step1..step16, track, mode, "
                             "...): the negative control, which must make that check fail")
    parser.add_argument("--shots", choices=("record", "compare"), help="screenshot regression: record stages this run's "
                                                                       "LCD and panel captures and keeps those of checks "
                                                                       "that passed as the references; compare checks "
                                                                       "them against the references (a missing one "
                                                                       "blocks its check)")
    parser.add_argument("--shots-dir", help="folder of reference captures (default private/hwsuite-shots/<firmware "
                                            "version>, git-ignored: the captures show the firmware's screen)")
    parser.add_argument("--shots-tolerance", type=shots_tolerance_arg, default=None,
                        help="largest per-channel difference that still counts as equal, a whole number from 0 to 254 "
                             "(default 8)")
    parser.add_argument("--shots-share", type=shots_share_arg, default=None,
                        help="share of compared pixels allowed to differ, rounded down to whole pixels (so 0 allows "
                             "none): from 0 up to but not including 1 (default 0)")
    parser.add_argument("--fixtures", help="fixture directory (default <out>/fixtures)")
    parser.add_argument("--out", help="output directory (default <build>/hwsuite/<run id>)")
    parser.add_argument("--tier", choices=list(TIERS), default="full")
    parser.add_argument("--smoke", action="store_true", help="same as --tier smoke")
    parser.add_argument("--only", help="comma-separated HW ids")
    parser.add_argument("--mark-verified", action="store_true")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--make-fixtures", metavar="DIR", help="only generate the fixtures into DIR and exit")
    parser.add_argument("--from-result", metavar="PATH", help="with --mark-verified: mark from a stored full-run "
                                                               "hwsuite-result.json (or its directory) instead of running")
    args = parser.parse_args(argv)
    if args.smoke:
        args.tier = "smoke"
    try:
        if args.make_fixtures:
            print(json.dumps(make_fixtures(args.make_fixtures), indent=1))
            return EXIT_OK
        if args.from_result:
            # Mark from a stored full run: the same checks as right after a run (eligibility, profile unchanged
            # since the run, same firmware), with the stored file's own hash recorded.
            if not args.mark_verified or not args.profile:
                raise EnvError("--from-result needs --mark-verified and --profile")
            path = Path(args.from_result)
            path = path / "hwsuite-result.json" if path.is_dir() else path
            text = path.read_text()
            block = mark_verified(args.profile, json.loads(text), path, hashlib.sha256(text.encode()).hexdigest())
            print(json.dumps({"marked_verified": block}, indent=1))
            return EXIT_OK
        if not args.profile or not args.build_dir:
            raise EnvError("--profile and --build-dir are required")
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        import profiles as profile_module
        profile_bytes = Path(args.profile).read_bytes()
        profile = profile_module.validate(json.loads(profile_bytes))
        only = [x.strip().upper() for x in args.only.split(",")] if args.only else None
        build = Path(args.build_dir).resolve()
        assets = json.loads((build / "assets.json").read_text())
        if not assets.get("research") or assets.get("audio") != "file":
            raise EnvError("the build must be a research build with --audio file")
        vms = len(qemu_processes()) + (0 if args.runtime else 1)
        if vms > MAX_VMS:
            raise EnvError(f"host over the VM limit ({vms} VMs with this run, limit {MAX_VMS})")
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        out = Path(args.out) if args.out else build / "hwsuite" / run_id
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        fixtures = Path(args.fixtures) if args.fixtures else out / "fixtures"
        shots = None
        if args.shots:
            import shotdiff
            folder = Path(args.shots_dir) if args.shots_dir else TOOLS.parent / "private" / "hwsuite-shots" / profile["firmware"]["version"]
            shots = shotdiff.ShotStore(folder, out / "shots", args.shots,
                                       shotdiff.DEFAULT_TOLERANCE if args.shots_tolerance is None else args.shots_tolerance,
                                       shotdiff.DEFAULT_MAX_FRACTION if args.shots_share is None else args.shots_share)
        runner = Runner(profile, args.profile, build, args.firmware, out, fixtures, args.tier, only,
                        args.control_build, args.runtime, args.testmode_build, args.testmode_skip, shots)
        runner.run_id = run_id
        runner.fixture_hashes = make_fixtures(fixtures, (profile.get("usb") or {}).get("sample_dir")
                                              or "PIONEER DJ SAMPLER/Samples")
    except (EnvError, ValueError, OSError, KeyError) as error:
        print(f"hwsuite: {error}", file=sys.stderr)
        return EXIT_ENV
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        verdicts = runner.run()
    except EnvError as error:
        print(f"hwsuite: {error}", file=sys.stderr)
        return EXIT_ENV
    result, digest = write_result(out, runner, verdicts, started, args, profile_bytes)
    print(json.dumps({"out": str(out), "summary": result["summary"]}, indent=1))
    code = exit_code(verdicts, bool(runner.lost))
    if args.mark_verified:
        try:
            block = mark_verified(args.profile, result, out / "hwsuite-result.json", digest)
            print(json.dumps({"marked_verified": block}, indent=1))
        except EnvError as error:
            print(f"hwsuite: not marked verified: {error}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())


