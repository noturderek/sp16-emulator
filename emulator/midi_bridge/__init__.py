"""MIDI controller bridge for the SP-16 emulator: drive a running research session from any CoreMIDI input.

    python3 -m emulator.midi_bridge --help

The pieces, each in its own module:
  midi      MIDI messages and the CoreMIDI helper (coremidi-io.swift) that carries them over pipes
  profile   what a controller sends (a profile) apart from what it should do (a mapping of named emulator actions)
  learn     record a profile by asking for a control and listening
  actions   a decoded controller message -> a control-socket call (pads with velocity, knobs as deltas, strip, buttons)
  leds      the emulator's decoded lamp frames -> note/CC velocity back to the controller
  presets   the "mirror the real SP-16" profile, from the unit's MIDI OUT settings
  bridge    the loop that joins them
"""
