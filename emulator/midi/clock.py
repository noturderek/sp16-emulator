"""MIDI clock timing: a second-order DLL for clock recovery and a sample-position clock generator.

MIDI clock is 24 pulses per quarter note (0xF8). The engine derives outbound clock from the audio frame position (the
CoreAudio mHostTime/mSampleTime pair, scheduled ahead) and filters inbound clock through a second-order delay-locked
loop (Fons Adriaensen, "Using a DLL to filter time") so a jittered external clock is recovered as a smooth BPM and
period. One host time domain is shared by the DIN and USB MIDI paths: every event is stamped with host monotonic time
at the edge and released to the app at stamp + a fixed latency (see RELEASE_LATENCY).

The DLL update, on each clock event at host time t::

    e = t - (centre + period)     # phase error: arrival vs. the prediction
    centre += period + b * e      # the loop's output (centre) time
    period += c * e               # the period estimate

with b = 2*zeta*wn*T0 and c = (wn*T0)**2 for a second-order loop of natural frequency wn (rad/s), damping zeta, and
nominal period T0. b and c are the proportional (phase) and integral (period) gains of a PI clock-recovery loop.
"""

from __future__ import annotations

import math

PPQN = 24.0
SAMPLE_RATE = 44100.0
RELEASE_LATENCY = 0.003  # seconds: inbound events reach the app this long after their edge timestamp
ZETA = 1.0 / math.sqrt(2.0)  # damping 0.707: small overshoot with fast settling


def clock_period_seconds(bpm: float) -> float:
    """The period between two MIDI clock pulses at `bpm` (60 / (bpm * 24))."""
    return 60.0 / (bpm * PPQN)


def bpm_from_period(period: float) -> float:
    """The BPM a clock period represents (60 / (period * 24))."""
    return 60.0 / (period * PPQN)


class ClockDLL:
    """Second-order DLL that recovers the period and BPM of a jittered MIDI clock stream.

    Feed it each clock event's host timestamp (seconds, monotonic); read `period`/`bpm` after it has locked. The
    first event sets the centre (tick returns None); `locked` is true once enough events have
    arrived to trust the estimate.
    """

    def __init__(self, nominal_bpm: float = 120.0, loop_hz: float = 2.0):
        if nominal_bpm <= 0 or loop_hz <= 0:
            raise ValueError("nominal_bpm and loop_hz must be positive")
        self.nominal_bpm = float(nominal_bpm)
        self.loop_hz = float(loop_hz)
        self.period = clock_period_seconds(nominal_bpm)
        self.centre: float | None = None
        self.events = 0
        wn = 2.0 * math.pi * loop_hz  # natural frequency, rad/s
        t0 = self.period
        self.b = 2.0 * ZETA * wn * t0  # phase (proportional) gain
        self.c = (wn * t0) ** 2.0  # period (integral) gain

    @property
    def bpm(self) -> float:
        return bpm_from_period(self.period)

    @property
    def locked(self) -> bool:
        return self.events >= 8

    def tick(self, t: float) -> None:
        """Feed one clock event's host timestamp (seconds, monotonic)."""
        self.events += 1
        if self.centre is None:
            self.centre = t
            return
        e = t - (self.centre + self.period)
        self.centre += self.period + self.b * e
        self.period += self.c * e

    def release_time(self, t: float) -> float:
        """When an event stamped at `t` reaches the app (edge timestamp + a fixed latency)."""
        return t + RELEASE_LATENCY


class ClockGenerator:
    """Derives outbound MIDI clock (0xF8) from the audio sample position, not a timer.

    Model on the sample-position accumulator of a host MIDI clock: each output sample advances an accumulator by
    clocks-per-sample; every time it crosses a whole pulse the sample offset of a 0xF8 is emitted. A BPM change
    keeps the fractional accumulator across BPM changes for rate continuity (no timer).
    """

    def __init__(self, bpm: float = 120.0, sample_rate: float = SAMPLE_RATE):
        if bpm <= 0 or sample_rate <= 0:
            raise ValueError("bpm and sample_rate must be positive")
        self.sample_rate = float(sample_rate)
        self.ppqn = PPQN
        self.accumulator = 0.0
        self.set_bpm(bpm)

    def set_bpm(self, bpm: float) -> None:
        if bpm <= 0:
            raise ValueError("bpm must be positive")
        self.bpm = float(bpm)
        self.clocks_per_sample = (bpm / 60.0) * self.ppqn / self.sample_rate

    def pulses(self, sample_count: int) -> list[int]:
        """The sample offsets (0 .. sample_count-1) at which a 0xF8 pulse falls, in order."""
        ticks: list[int] = []
        step = self.clocks_per_sample
        acc = self.accumulator
        for i in range(sample_count):
            acc += step
            if acc >= 1.0:
                acc -= 1.0
                ticks.append(i)
        self.accumulator = acc
        return ticks

    def pulses_until(self, sample_count: int, emit) -> None:
        """Call `emit(sample_offset)` for each pulse in the next `sample_count` samples (avoids a list)."""
        step = self.clocks_per_sample
        acc = self.accumulator
        for i in range(sample_count):
            acc += step
            if acc >= 1.0:
                acc -= 1.0
                emit(i)
        self.accumulator = acc
