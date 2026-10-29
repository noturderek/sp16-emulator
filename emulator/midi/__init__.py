"""MIDI clock and transport timing shared by the DIN and USB MIDI paths.

Everything here lives in one host time domain (time.monotonic_ns on the host): inbound events are timestamped at the
edge with host time and released to the app a fixed latency later; outbound clock is derived from the audio sample
position, never from a timer. See clock.py for the DLL and the generator.
"""
