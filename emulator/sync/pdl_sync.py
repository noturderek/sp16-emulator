"""Pro DJ Link tempo -> MIDI clock and Ableton Link (Carabiner) bridge.

The SP-16 is a Pro DJ Link tempo follower; the bridge takes the master's tempo/phase (from its 0x28 beat packets and
0x0a status packets on the `--net mcast` segment, or any other source of the same packets) and drives two followers:

* MIDI clock: `midi/clock.py`'s ClockGenerator, fed the effective BPM (base BPM times the actual pitch), exactly as
  python-prodj-link's midiclock.py does (`clock = bpm * actual_pitch`).
* Ableton Link: a minimal Carabiner client (TCP 127.0.0.1:17000) that sends `set-tempo` on a change and
  `force-beat-at-time` to align the downbeat. Carabiner (external/Deep-Symmetry_carabiner, design reference) then
  shares the tempo with every Link peer. Ableton Link itself is a design reference only (external/Ableton_link is GPL).

All packet layouts follow the public Pro DJ Link write-up; the offsets are the same ones net/segment_watch.py decodes
and net/fake_master.py builds. Stdlib only.
"""
from __future__ import annotations

import socket
import struct

from midi import clock

MAGIC = b"Qspt1WmJOL"
PITCH_SCALE = 0x100000  # the "actual pitch" factor: 0x100000 = +0 %


def effective_bpm(bpm: float, pitch_frac: float) -> float:
    """The BPM a follower runs at: base BPM times the actual pitch (1 + pitch). `pitch_frac` is +0.02 for +2 %."""
    return bpm * (1.0 + pitch_frac)


def pitch_fraction(scaled: int) -> float:
    """The pitch as a fraction (+0.02) from the wire's scaled factor (0x100000 = +0 %)."""
    return scaled / PITCH_SCALE - 1.0


def parse_beat_packet(payload: bytes) -> dict:
    """The master's 0x28 beat packet: pitch factor, base BPM (x100), and beat in bar (1-based)."""
    if len(payload) < 0x5d or payload[:10] != MAGIC or payload[10] != 0x28:
        raise ValueError("not a 0x28 beat packet")
    scaled_pitch, _, bpm_x100, beat_in_bar = struct.unpack(">IHHB", payload[0x54:0x5d])
    return {"pitch_frac": pitch_fraction(scaled_pitch), "bpm": bpm_x100 / 100.0, "beat_in_bar": beat_in_bar}


def parse_status_packet(payload: bytes) -> dict:
    """The master's 0x0a status packet: pitch factor, base BPM (x100), beat counter, beat in bar and the play/master
    flags (offset 0x89 bits, as net/fake_master.py sets them)."""
    if len(payload) < 0x93 or payload[:10] != MAGIC or payload[10] != 0x0a:
        raise ValueError("not a 0x0a status packet")
    scaled_pitch = struct.unpack(">I", payload[0x8c:0x90])[0]
    bpm_x100 = struct.unpack(">H", payload[0x92:0x94])[0]
    beat_count = struct.unpack(">I", payload[0xa0:0xa4])[0]
    beat_in_bar = payload[0xa6]
    flags = payload[0x89]
    return {"pitch_frac": pitch_fraction(scaled_pitch), "bpm": bpm_x100 / 100.0,
            "beat_count": beat_count, "beat_in_bar": beat_in_bar,
            "playing": bool(flags & 0x40), "master": bool(flags & 0x20)}


class TempoTracker:
    """The master's tempo and phase, updated from beat/status packets stamped with host monotonic time.

    `effective_bpm` is the follower's tempo. A beat-in-bar of 1 records the bar (downbeat) time; `bar_period` is four
    beats at the current tempo, so `next_downbeat(after)` predicts when a bar starts, for phase alignment.
    """

    def __init__(self):
        self.bpm = 0.0
        self.pitch_frac = 0.0
        self.last_downbeat: float | None = None

    @property
    def effective_bpm(self) -> float:
        return effective_bpm(self.bpm, self.pitch_frac)

    @property
    def bar_period(self) -> float:
        if self.effective_bpm <= 0:
            raise ValueError("no tempo yet")
        return 4.0 * 60.0 / self.effective_bpm

    def update(self, bpm: float, pitch_frac: float, beat_in_bar: int, t: float) -> None:
        self.bpm = bpm
        self.pitch_frac = pitch_frac
        if beat_in_bar == 1:
            self.last_downbeat = t

    def next_downbeat(self, after: float) -> float:
        """The first downbeat at or after `after`, from the last observed bar start plus whole bar periods."""
        if self.last_downbeat is None or self.effective_bpm <= 0:
            raise ValueError("no downbeat yet")
        period = self.bar_period
        delta = after - self.last_downbeat
        bars = int(delta // period) + (1 if delta % period > 1e-9 else 0)
        return self.last_downbeat + bars * period


class CarabinerLink:
    """A minimal Carabiner client: `set-tempo` on tempo change and `force-beat-at-time` for phase. Carabiner listens on
    TCP 127.0.0.1:17000 and answers `status` with `{ :peers N :bpm X :start T :beat B }` (T in microseconds)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 17000):
        self.host, self.port = host, port
        self.sock: socket.socket | None = None

    @staticmethod
    def set_tempo_message(bpm: float) -> bytes:
        return f"set-tempo {bpm:.6f}\n".encode()

    @staticmethod
    def force_beat_at_time_message(beat: float, when_micros: int, quantum: int = 4) -> bytes:
        return f"force-beat-at-time {beat:.6f} {when_micros} {quantum}\n".encode()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=2.0)

    def send(self, message: bytes) -> None:
        if self.sock is None:
            self.connect()
        self.sock.sendall(message)

    def set_tempo(self, bpm: float) -> None:
        self.send(self.set_tempo_message(bpm))

    def force_beat_at_time(self, beat: float, when_micros: int, quantum: int = 4) -> None:
        self.send(self.force_beat_at_time_message(beat, when_micros, quantum))

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class PdlClockBridge:
    """Consumes master packets, keeps a MIDI ClockGenerator at the effective BPM, and pushes tempo/phase to Link.

    `feed_beat(payload, t)` and `feed_status(payload, t)` accept packets (with host monotonic timestamps); on a tempo
    change the MIDI generator is retuned and `set-tempo` is sent to Carabiner (when `link` is set).
    """

    def __init__(self, generator: clock.ClockGenerator | None = None, link: CarabinerLink | None = None,
                 sample_rate: float = clock.SAMPLE_RATE):
        self.tracker = TempoTracker()
        self.generator = generator or clock.ClockGenerator(sample_rate=sample_rate)
        self.link = link
        self._sent_bpm: float | None = None

    def _on_tempo(self, bpm: float) -> None:
        if self._sent_bpm is not None and abs(self._sent_bpm - bpm) < 1e-6:
            return
        self.generator.set_bpm(bpm)
        if self.link is not None:
            self.link.set_tempo(bpm)
        self._sent_bpm = bpm

    def feed_beat(self, payload: bytes, t: float) -> dict:
        beat = parse_beat_packet(payload)
        self.tracker.update(beat["bpm"], beat["pitch_frac"], beat["beat_in_bar"], t)
        self._on_tempo(self.tracker.effective_bpm)
        return beat

    def feed_status(self, payload: bytes, t: float) -> dict:
        status = parse_status_packet(payload)
        self.tracker.update(status["bpm"], status["pitch_frac"], status["beat_in_bar"], t)
        self._on_tempo(self.tracker.effective_bpm)
        return status
