---
title: Live hardware with the emulator
description: "Recipes for using the emulator with real gear: a physical SP-16 as a MIDI clock and transport source, a CDJ-3000 as a Pro DJ Link master over Ethernet, Ableton Link through Carabiner, and keeping the settings these need."
type: runbook
status: active
created: 2026-10-28
tags: [midi, prodjlink, link, hardware]
author:
  harness: claude-code
  agent: main
  model: claude-opus-5-5
  thinking: unknown
  tokens: unknown
---

# Live hardware with the emulator

Each recipe below was run end to end on a Mac with a physical SP-16 (1.60) on USB and a CDJ-3000 on a USB Ethernet
adapter. The helper scripts are stdlib Python or POSIX shell and answer `--help`. [midi.md](midi.md) and
[pro-dj-link.md](pro-dj-link.md) explain the machinery underneath.

| Goal | Helper |
|---|---|
| A hardware MIDI device drives the emulated sequencer | `emulator/midi/thru.py` |
| See which Pro DJ Link devices a network carries | `emulator/net/pdl_scan.py` |
| Put the emulator on a real Ethernet network | `emulator/net/vmnet-bridged.sh` |
| Keep a setting you changed in UTILITY | `emulator/save_settings.py` |

## Keep your settings: save through standby

The firmware writes its UTILITY settings to the data image **only when it goes into standby after a change**. A
setting you change and then quit without standby is lost: after a restart SYNC SOURCE SELECT, for example, is back to
what was saved before. After changing settings in a research build:

```sh
python3 emulator/save_settings.py --build NAME
```

It presses the power key into standby (the firmware saves), waits a few seconds and wakes the unit. By hand:
`./sp16 ctl --build NAME power`, then `./sp16 ctl --build NAME standby yes`. A new data image starts with LCD
BRIGHTNESS at 5 ([profiles.md](profiles.md), `settings`); everything else starts at the firmware's defaults.

## A physical SP-16 starts and clocks the emulated one (USB MIDI)

The physical unit appears on the Mac as `PIONEER DJ TSP-16 MIDI`. The emulator's MIDI input is its own virtual
endpoint, `SP-16 USB [NAME]`. macOS does not connect a hardware source to a virtual destination, so a thru runs
between them.

1. **Physical SP-16, UTILITY:** MIDI OUT MODE (USB) = OUT; PROJECT SETTING (MIDI OUT) CLOCK and START/STOP/CONTINUE
   = ENABLE; SYNC SOURCE SELECT = INTERNAL (it is the master).
2. **Emulator:** `./sp16 run --name NAME --midi`. In UTILITY: **SYNC SOURCE SELECT = USB MIDI**, and PROJECT
   SETTING **(MIDI IN)** CLOCK and START/STOP/CONTINUE = ENABLE. The list has a MIDI IN and a MIDI OUT block with the
   same rows; the MIDI IN block is the upper one. Then save (above).
3. **The thru**, in its own terminal:

   ```sh
   python3 emulator/midi/thru.py --build NAME
   ```

   It waits for both ends, then prints `thru: TSP-16 -> SP-16 USB [NAME] (Ctrl-C to stop)` and each message it
   forwards except clock and active sensing.
4. Press **PLAY** on the physical unit: the thru prints `fa`, the emulated sequencer starts and takes the tempo from
   the clock (it reads, for example, 120.1 against 120.0: tempo from clock timing always wobbles a little). STOP
   prints `fc`.

With SYNC SOURCE SELECT = DIN MIDI instead, run the thru with `--din`. Another device works the same way with
`--source "part of its name"` (`./sp16 midi ports` lists the names).

To check each stage: ShowMIDI (or any monitor) on `PIONEER DJ TSP-16 MIDI` shows whether the unit sends; the session's
`console.txt` logs the first 16 MIDI events the emulated app receives (`MIDI seq input ... type=30` is Start,
`type=36` Clock).

### Its knobs and pads as controls (MIDI mapping)

`./sp16 midi run --build NAME --in TSP-16 --preset sp16 --cc-page 1 --channel any` turns the physical unit's MIDI
into emulator panel actions: pads to pads, knob CCs to encoder detents. The physical encoders send MIDI only on a MIDI
track with the knobs on a MIDI CC page (1.60 addendum pp. 47-51), and the selector and most buttons send none, so
this is a mapping, not a full takeover of the panel.

## A CDJ-3000 as the Pro DJ Link master (Ethernet)

1. **Cable:** CDJ-3000 LINK port to a USB Ethernet adapter on the Mac (a hub with a powered adapter works). When the
   adapter is up, `networksetup -listallhardwareports` names its interface (for example `en8`), and `ifconfig en8`
   shows `status: active` with a `169.254.x.x` address.
2. **Check the network:**

   ```sh
   python3 emulator/net/pdl_scan.py
   ```

   It should list `CDJ-3000` with its player number, keep-alive and beat packets and its BPM.
3. **The bridged daemon**, in its own terminal (asks for your password):

   ```sh
   emulator/net/vmnet-bridged.sh en8
   ```

4. **The emulator on that network:** stop any `segment_master.py` fake master, then

   ```sh
   ./sp16 run --name NAME --net bridged:en8
   ```

   `pdl_scan.py` now also lists `TORAIZ`, the emulated SP-16.
5. **Emulator, UTILITY:** SYNC SOURCE SELECT = PRO DJ LINK (then save). The header shows **LINK**.
6. **CDJ-3000:** load and play a track, and press **MASTER** so it is the tempo master. The emulator's BPM changes
   to the CDJ's effective BPM (its BPM times the pitch).
7. Press **PLAY on the emulator.** Pro DJ Link shares tempo and beat position, not transport: the SP-16 starts when
   its own PLAY is pressed and then runs in step with the master.

If the header shows LINK but the BPM stays the emulator's own, the follower is rejecting the master's status packets.
The 1.60 follower needs a player numbered 1 to 4, the tempo-master flag and a play state of loaded and playing. A host
scan cannot see the status packets, because the CDJ sends them straight to each announced player; `pdl_scan.py`
prints these checks as hints. Pressing MASTER on the CDJ was the fix in our run.

Without a CDJ, the fake master on the private segment does the same job: `./sp16 run --name NAME --net mcast` with
`python3 emulator/net/segment_master.py --bpm 128 --pitch 2` ([pro-dj-link.md](pro-dj-link.md#following-a-master)).
Its `--duration` stops it after that many seconds, and LINK drops with it.

## Ableton Link (Carabiner)

`emulator/sync/bridge.py --link` carries a Pro DJ Link master's tempo out to Ableton Link apps through a Carabiner you
build and run. It reads the `mcast` segment, so it works with the fake master; it does not feed the emulator.

```sh
git clone --recursive https://github.com/Deep-Symmetry/carabiner && cd carabiner
mkdir build && cd build && cmake .. -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && cmake --build .
./bin/Carabiner
```

(CMake 4 needs the policy flag for Carabiner's old minimum version.) Then `python3 emulator/sync/bridge.py --link`;
an app with Link on (Ableton Live) follows the master's tempo.

## Not covered here

- Taking over the emulator's whole panel from a physical SP-16 (every button, encoder detent, the selector, touch
  strip and pad pressure) needs the unit's internal panel frames, which never leave the unit; it is not built.
- Whether the SP-16's bar lands on a real CDJ's downbeat or one beat off: the fake master reports the beat that just
  completed, which the firmware's beat handler expects; a real CDJ's convention is not yet measured.
