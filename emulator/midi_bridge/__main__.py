"""python3 -m emulator.midi_bridge COMMAND ...  (run from the repository root)"""
import argparse
from pathlib import Path
import sys
import time

from . import bridge as bridge_module
from . import learn as learn_module
from . import midi
from . import presets
from . import profile as prof
from .leds import LedEcho


def add_target(parser):
    parser.add_argument("--runtime", help="a running research session's runtime.json (default: the newest one)")
    parser.add_argument("--build-dir", default=str(midi.REPO / "build"), help="where the builds are (default build/)")
    parser.add_argument("--build", help="build name under --build-dir (default: any)")


def open_port(args, with_output):
    spec = ["run", "--in", args.input]
    out = getattr(args, "output", None) or (args.input if with_output else None)
    if out:
        spec += ["--out", out]
    return midi.Port(spec, build_dir=args.build_dir)


def runtime_of(args):
    return Path(args.runtime) if args.runtime else bridge_module.find_runtime(args.build_dir, args.build)


def cmd_ports(args):
    ports = midi.list_ports(args.build_dir)
    for kind in ("source", "destination"):
        print(f"MIDI {kind}s:")
        for name in ports[kind] or ["(none)"]:
            print(f"  {name}")


def cmd_learn(args):
    controls = args.controls.split(",") if args.controls else learn_module.DEFAULT_CONTROLS
    port = open_port(args, with_output=False)
    try:
        profile, mapping, skipped = learn_module.learn(controls, port.get, print, args.name, timeout=args.timeout,
                                                       led_echo=not args.no_leds)
    finally:
        port.close()
    for kind, data in (("profile", profile), ("mapping", mapping)):
        print(f"wrote {prof.save(kind, args.name, data, folder=args.out_dir and Path(args.out_dir) / (kind + 's'))}")
    if skipped:
        print(f"not learned: {', '.join(skipped)}")


def cmd_preset(args):
    profile, mapping = presets.sp16_midi_out(None if args.channel == "any" else int(args.channel),
                                             note_base=args.note_base, cc_page=args.cc_page, name=args.name)
    for kind, data in (("profile", profile), ("mapping", mapping)):
        print(f"wrote {prof.save(kind, args.name, data, folder=args.out_dir and Path(args.out_dir) / (kind + 's'))}")


def cmd_run(args):
    import panel_map
    import sessionctl
    if args.preset == "sp16":
        profile, mapping = presets.sp16_midi_out(None if args.channel == "any" else int(args.channel),
                                                 note_base=args.note_base, cc_page=args.cc_page)
    else:
        profile = prof.load("profile", args.profile)
        mapping = prof.load("mapping", args.mapping or profile.get("mapping") or args.profile)
        prof.validate_mapping(mapping, profile)
    runtime = runtime_of(args)
    session = sessionctl.Session(runtime)
    port = open_port(args, with_output=not args.no_leds)
    names = {**panel_map.CONTROL_NAMES, **panel_map.LAMP_NAMES}
    echo = None
    if mapping.get("lamps") and not args.no_leds:
        session.call(op="txlog", on=1)
        echo = LedEcho(session.dir, profile, mapping, panel_map.decode_lamps, names)
    translator = bridge_module.Translator(session, profile, mapping, analog_start=panel_map.ANALOG_DEFAULTS,
                                          log=lambda text: print(f"midi-bridge: {text}", file=sys.stderr))
    print(f"midi-bridge: {args.input!r} -> {runtime}" + ("; LED echo on" if echo else ""), file=sys.stderr)
    until = time.monotonic() + args.seconds if args.seconds else None
    try:
        bridge_module.Bridge(port, translator, echo).run(lambda: until is not None and time.monotonic() >= until)
    except KeyboardInterrupt:
        pass
    finally:
        if echo:
            try:
                session.call(op="txlog", on=0)
            except Exception:                                   # noqa: BLE001 - the app may be gone already
                pass
        port.close()
        print(f"midi-bridge: {translator.sent} calls, {translator.errors} failed", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m emulator.midi_bridge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ports", help="list the CoreMIDI sources and destinations")
    p.add_argument("--build-dir", default=str(midi.REPO / "build"))
    p.set_defaults(handler=cmd_ports)

    p = sub.add_parser("learn", help="record a controller's pads and knobs into a profile and a mapping")
    p.add_argument("--in", dest="input", required=True, help="part of the MIDI source's name")
    p.add_argument("--name", required=True, help="name of the profile/mapping to write (private/midi/)")
    p.add_argument("--controls", help=f"comma list of emulator controls (default {','.join(learn_module.DEFAULT_CONTROLS)})")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each control")
    p.add_argument("--no-leds", action="store_true", help="do not generate LED outputs for learned buttons")
    p.add_argument("--out-dir", help="write under DIR/profiles and DIR/mappings instead of private/midi")
    p.add_argument("--build-dir", default=str(midi.REPO / "build"))
    p.set_defaults(handler=cmd_learn)

    p = sub.add_parser("preset", help="write the 'mirror the real SP-16' profile and mapping")
    p.add_argument("--name", default=presets.NAME)
    p.add_argument("--channel", default="1", help="the track's MIDI channel 1..16, or 'any'")
    p.add_argument("--note-base", type=int, default=60, help="note of pad 1 (default 60 = C5)")
    p.add_argument("--cc-page", type=int, default=1, choices=(1, 2), help="knobs send the MIDI CC1 or CC2 page")
    p.add_argument("--out-dir")
    p.set_defaults(handler=cmd_preset)

    p = sub.add_parser("run", help="bridge a MIDI input to the running emulator")
    p.add_argument("--in", dest="input", required=True, help="part of the MIDI source's name (the real SP-16: 'TSP-16')")
    p.add_argument("--out", dest="output", help="MIDI destination for LED echo (default: same name as --in)")
    p.add_argument("--profile", help="profile name or file")
    p.add_argument("--mapping", help="mapping name or file (default: the profile's name)")
    p.add_argument("--preset", choices=("sp16",), help="use the built-in preset instead of --profile")
    p.add_argument("--channel", default="1")
    p.add_argument("--note-base", type=int, default=60)
    p.add_argument("--cc-page", type=int, default=1, choices=(1, 2))
    p.add_argument("--no-leds", action="store_true", help="no LED echo")
    p.add_argument("--seconds", type=float, help="stop after this long (default: until interrupted)")
    add_target(p)
    p.set_defaults(handler=cmd_run)

    args = parser.parse_args(argv)
    if args.command == "run" and not (args.profile or args.preset):
        parser.error("run needs --profile NAME or --preset sp16")
    try:
        args.handler(args)
    except (RuntimeError, prof.ProfileError, OSError) as error:
        print(f"midi-bridge: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
