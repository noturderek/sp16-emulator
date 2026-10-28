#!/usr/bin/env python3
"""Host-side laws of the four analog-only controls: MAIN OUT, INPUT LEVEL, LPF CUTOFF and RESONANCE.

The firmware never sees these controls as audio (MAIN OUT also reaches it as an ADC word, which this model leaves
untouched). hardware_stub.c applies them to the audio the host hears or records; this module is the reference the
shim's laws are tested against (tests/sp16_analog_laws_contract.c, test_sp16_analog_laws.py) and the one place that
says how far each law is known. A control position is the 0..1000 slot the bridge shares with the shim (panel_map
HOST_ANALOG); 0xffff means "never moved", and the shim then keeps the control neutral.

Grades: S static (manual or code), I inferred, U unknown. No law below was measured on a unit, so each curve is
an assumption (grade U) that only its endpoint is anchored to something stated (grade I or S).
"""
import json
import math

NEVER_MOVED = 0xFFFF
SLOT_MAX = 1000
FULL_SCALE_DBU = 16.0  # converter full scale = the jacks' rated maximum level, +16 dBu (manual specification, S; the mapping to digital full scale is I)
LIMITER_THRESHOLD = 0.5  # fraction of full scale where the input limiter starts (U)
DECADES_20_TO_20K = math.log2(1000)  # 20 Hz .. 20 kHz over the pot range

LAWS = {
    "main_out": {
        "formula": "gain = pot^2", "unit": "linear gain on the master jacks only",
        "grades": {"shape": "U (audio taper assumed)", "endpoints": "I (unity at full, silence at zero)"},
        "applies_to": "the master-pair copy the host hears and records; the app's own buffer, the PCM tap and the ADC word are untouched",
    },
    "input_level": {
        "formula": "gain = 4 * pot^2", "unit": "linear gain on the input capture, before the limiter",
        "grades": {"shape": "U", "range": "U (unity at the centre, +12 dB at the top assumed)"},
        "applies_to": "the injected WAV or the research tone, on the AK5386 capture",
    },
    "lp_cutoff": {
        "formula": "fc = 20 Hz * 2^(pot * log2(1000))", "unit": "Hz, two-pole low-pass",
        "grades": {"shape": "U (exponential assumed)", "range": "I (20 Hz .. 20 kHz, the audio band)"},
        "applies_to": "the Send & Return loop (return capture), together with the high-pass the firmware drives",
    },
    "lp_resonance": {
        "formula": "k = 2 - 1.9 * pot  (damping, Q = 1/k)", "unit": "Q from 0.5 to 10",
        "grades": {"shape": "U (linear in damping assumed)", "range": "U"},
        "applies_to": "the same loop",
    },
}


def pot(slot):
    """Control position 0..1 from a shared slot; out-of-range slots clamp to the top."""
    return 1.0 if slot > SLOT_MAX else slot / SLOT_MAX


def main_out_gain(slot):
    return 1.0 if slot == NEVER_MOVED else pot(slot) ** 2


def input_level_gain(slot):
    return 1.0 if slot == NEVER_MOVED else 4 * pot(slot) ** 2


def lp_cutoff_hz(slot):
    return 20000.0 if slot == NEVER_MOVED else 20.0 * 2 ** (pot(slot) * DECADES_20_TO_20K)


def lp_damping(slot):
    return 2.0 if slot == NEVER_MOVED else 2.0 - 1.9 * pot(slot)


def lp_gain(freq_hz, cutoff_slot, resonance_slot):
    """Magnitude of the two-pole low-pass at freq_hz (analog prototype; the shim's digital filter matches it well below
    the Nyquist frequency)."""
    r = freq_hz / lp_cutoff_hz(cutoff_slot)
    return 1.0 / math.hypot(1 - r * r, lp_damping(resonance_slot) * r)


def to_dbu(peak):
    """Level readout for a linear peak (1.0 = converter full scale); -99 for silence, as the shim reports."""
    return FULL_SCALE_DBU + 20 * math.log10(peak) if peak > 1e-6 else -99.0


def limit(x):
    """Input limiter curve: transparent up to the threshold, then a soft saturation towards full scale."""
    a = abs(x)
    if a <= LIMITER_THRESHOLD:
        return x
    over = (a - LIMITER_THRESHOLD) / (1 - LIMITER_THRESHOLD)
    y = LIMITER_THRESHOLD + (1 - LIMITER_THRESHOLD) * over / (1 + over)
    return math.copysign(y, x)


if __name__ == "__main__":
    print(json.dumps({"full_scale_dbu": FULL_SCALE_DBU, "limiter_threshold": LIMITER_THRESHOLD, "laws": LAWS}, indent=1))
