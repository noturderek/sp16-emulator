# Python CLI and Setup

## Command layout

Run the emulator CLI from a checkout with `./sp16`. The root `sp16` file is the Python launcher. It locates the checkout and imports the command implementation from
`emulator/sp16emu.py`, so it also works through the `make install` link.

To make `sp16` available as a command in your shell, install a link into `~/.local/bin`:

```sh
make install
```

Add `~/.local/bin` to `PATH` if it is not already there. Use `make uninstall` to remove only the link created by this
checkout. Set `PREFIX=/some/path` with either target to use a different install directory.

## Prerequisites

The emulator runs on Apple silicon Macs with macOS 13 or later. Check the local setup with:

```sh
./sp16 doctor
```

The build needs Python 3.12 or later, Xcode Command Line Tools, QEMU build dependencies (`ninja`, `pkgconf`, `glib`,
and `pixman`), and Docker Desktop or OrbStack for the Linux guest image. The CLI itself starts with Python 3.9 or later;
`doctor` reports missing build prerequisites and their fixes. For the hardware suite and the panel layout overlay
tool (`emulator/layout_overlay.py`), `pillow` is required: `python3 -m pip install pillow`.

## Common commands

```sh
./sp16 help                         # list commands
./sp16 help build                   # command options and examples
./sp16 --dry-run build --audio file # show the commands without running them
./sp16 build --audio file           # silent build that records WAV output
./sp16 run                          # launch the default build
./sp16 run --midi                   # launch with CoreMIDI DIN bridge enabled
./sp16 midi ports                   # list CoreMIDI input and output endpoints
./sp16 midi learn --in "Pads"       # learn a MIDI controller profile
./sp16 ctl snapshot screen.png      # drive a running research session
```

Firmware and GPL sources are not included. Import only files supplied by the user, and keep firmware, keys, imported
images and session data in the ignored `build/` or `private/` directories. See the [README](../README.md) for the full
fresh-clone workflow, firmware import, audio, virtual USB storage, builds and troubleshooting. The agent setup workflows
are in [sp16-bootstrap](../.agents/skills/sp16-bootstrap/SKILL.md) and
[sp16-emulator-setup](../.agents/skills/sp16-emulator-setup/SKILL.md).
