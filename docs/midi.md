# MIDI

How MIDI gets in and out of the emulated sampler: the ALSA sequencer the firmware's application talks to, the two
virtual CoreMIDI endpoints the emulator publishes on the host, the routing preferences window, and the firmware menu
settings that gate generated output.

Grades below: **O** observed in this emulator, **S** static (code, manual), **I** inferred, **U** unknown. Nothing
here was measured on a unit.

## What the firmware sees

The firmware's 32-bit application opens `/dev/snd/seq` and speaks the ALSA sequencer ABI. The shim
(`hardware_stub.c`) answers that ABI itself instead of forwarding it to the Linux guest's own sequencer: the version
and running-mode ioctls, client and port enumeration, port creation, subscriptions, and the note/CC/realtime events.
QEMU user mode returns `ENOSYS` for the sequencer version ioctl, so without the proxy the app cannot create a client
at all.

Two kernel-style clients are always or conditionally present:

| Client | Name | Port | Present when |
|---|---|---|---|
| 128 | `snd-din-midi` (`DIN MIDI`) | 0 | always; the DIN UART (`/dev/din_midi`) is behind it |
| 129 | `g_midi` (`USB MIDI Gadget`) | 0 | only while the USB-B MIDI mode marker exists |

The marker is `/tmp/sp16-root/dev/sp16-usb-midi-enabled` (override with `SP16_USB_MIDI_MODE_PATH`). On hardware the
firmware's own start-up script loads the gadget module and the kernel's `seq_midi` adapter registers `g_midi` first.
The emulator does not run that script, so `emulator/midi/seq-bridge.c` creates the marker as soon as the USB MIDI
transport opens, before the app registers. The shim then advertises client 129 and the app subscribes its
per-peer port to it (**O**, console line `SP16_USB_MIDI peer=g_midi`). Removing the marker takes the peer away
again, which is what the firmware sees when it switches the gadget out of MIDI mode.

What the app does with those peers is logged on the guest console (`--trace strace`, or a normal boot's
`console.txt`): it registers `JuceMidiInput`/`JuceMidiOutput` clients, subscribes DIN and USB input ports, and
routes its output to both (**O**, firmware 1.60).

## The two host endpoints

`./sp16 run --midi` starts both bridges for that session; `./sp16 midi ports` lists the endpoints afterwards.

| Endpoint | Bridge | Transport |
|---|---|---|
| `SP-16 DIN [<build>]` | `emulator/midi/din.py` | dedicated `sp16.din` virtio port, rawmidi UART at 31,250 baud (320 µs per byte, never burst past it) |
| `SP-16 USB [<build>]` | `emulator/midi/usb.py` | separate byte stream for the `g_midi` peer, no byte pacing |

Both are **virtual CoreMIDI endpoints, not a physical USB device**: no USB MIDI gadget is enumerated, and nothing
appears in Audio MIDI Setup as a hardware interface. A DAW or a monitor that selects `SP-16 USB` sees the firmware's
generated messages exactly as it sees the DIN endpoint's.

The two bridges are independent. A missing USB transport leaves the DIN route and the sequencer proxy serving; poll
faults or a failed USB flush close and disable only the USB route and clear its queued bytes (**S**, pinned by
`emulator/tests/sp16_usb_route_contract.c`).

## Routing preferences

The app has a **MIDI > Routing…** window. Nothing else in the emulator depends on it.

- One row per CoreMIDI endpoint, sources first, with the columns **Port**, **In**, **Out**, **Send clock**,
  **Receive clock**. A toggle that does not apply to an endpoint's kind is disabled. Ports appear and disappear live
  as devices are plugged in.
- Under the table: a **Clock source** picker (Internal or any source) and **BPM**, **Lock** (`locked`/`unlocked`) and
  **Jitter** readouts. Jitter is the sample standard deviation of the last 96 clock intervals, so a steady clock reads
  close to 0 µs instead of the interval itself. The lock loop is the second-order delay-locked loop of
  `emulator/midi/clock.py` (`wn = 2*pi*loop_hz` rad/s, gains normalised to the nominal period).
- Toggling **Send clock** on a destination starts a 24 PPQN 0xF8 stream to it; turning it off stops the stream.
- Choices persist in the app's `UserDefaults` and are also written to
  `~/Library/Application Support/SP-16 Emulator/midi-routing.json` for the Python bridges to read. Only ports the user
  actually toggled are written.

The bridges read that file and act on it without a restart (re-read when its mtime, size or inode changes, on a
0.25 s throttle):

- A source row is what the host reads: **In** gates the whole guest-to-host stream, **Receive clock** gates only the
  0xF8 bytes inside it. A destination row is what the host writes, gated by **Out** and **Send clock**.
- A port absent from the file carries everything, which is what a bridge did before the window existed; only a
  present entry can close a direction, and a toggle that is not literally `true` closes it.
- An unreadable file keeps the routes already in force.
- Entries are matched by display **name** (the label the bridge gives its own endpoint), not by the CoreMIDI unique
  ID; the app remembers a re-plugged device by unique ID instead.
- `--config PATH` (both bridges) points them at a different preferences file.

The window writes only the ports you toggle, so a file that lists every port with every toggle off closes all
routes: delete it and the bridges carry everything again.

## Firmware menu settings

Generated output is gated by the firmware's own settings, so the emulator drives the menus with OCR recipes. They need
a running research session (`--research`):

```sh
python3 emulator/menu/sync_source.py --build NAME sync-source-pdl   # SYNC SOURCE SELECT = PRO DJ LINK
python3 emulator/menu/sync_source.py --build NAME usb-out           # MIDI OUT MODE (USB) = OUT
python3 emulator/menu/sync_source.py --build NAME usb-note-out      # the above plus MIDI OUT NOTE/CC = ENABLE
# --runtime PATH/runtime.json targets one session instead of the newest one of a build
```

Each recipe taps the UTILITY icon on the HOME screen, focuses a row in the setting list, pushes the selector, focuses
the value in the right column, pushes again and returns HOME. The setting list is remembered across visits, so the
recipe searches from wherever the focus is and confirms each label by OCR rather than counting detents.

- `sync-source-pdl` sets setting `0x2d` to PRO DJ LINK (INTERNAL, PRO DJ LINK, DIN MIDI or USB MIDI are the values).
  See [Pro DJ Link](pro-dj-link.md) for what the firmware then does.
- `usb-out` sets `0x2b` (MIDI OUT MODE (USB)) to OUT.
- `usb-note-out` also sets the project's MIDI OUT NOTE/CC (`0x11000102`) to ENABLE. The list has two NOTE/CC rows, so
  the recipe reaches the MIDI OUT one by focusing the unique MIDI OUT MODE (USB) row first and then searching upward.

## State of each route

| Route | State |
|---|---|
| DIN out: clock, transport, channel messages | **O.** With MIDI OUT enabled, PLAY emits `FA`, 24 PPQN `F8` and `FC`: 48.05 clocks/s at 120 BPM, 48.03 Hz after a USB mode round trip |
| DIN in: a host note reaching the sampler | **O.** A note sent to the app's subscribed input port triggers the loaded sample (nonzero PCM frames rose 353111 → 438575) |
| USB-B out: transport and CC | **O.** With `g_midi` published, `SP-16 USB` carries the same clock, stop and CC 123 all-notes-off as `SP-16 DIN` |
| USB-B out: notes from a pad hit | **Not finished.** With `usb-note-out` applied (both settings OCR-confirmed) a pad hit still produces no NOTEON/NOTEOFF. The project was empty (`MPL OFF`, no sample names), so there was no playable content to turn into a note; assigning a sample to a pad and the per-track MIDI channel and NOTE/CC send is still to be done |
| Route and mode switching | **O.** After a mass-storage/USB-MIDI mode round trip all four ports re-registered and both DIN and USB inputs delivered one callback each |
| Realtime queue and capacity | **S.** Realtime and F2 requests go through one bounded UART queue; a malformed, full or unavailable request fails explicitly instead of being dropped silently. The factory-test loopback stays a separate path |

Not implemented: a physical USB MIDI gadget, more than one MIDI port per peer, and a per-output clock offset. The
sequencer proxy serves the ABI the app needs, not the guest kernel's own sequencer.

## Controller bridge

`./sp16 midi` is a separate tool: it maps a CoreMIDI controller's pads, knobs and keys onto emulator actions and
echoes lamp state back as LED note/CC. `ports` lists endpoints, `learn` builds a profile under `private/midi/`, `run`
runs the bridge, and `--preset sp16` mirrors the SP-16's own MIDI layout. See
[architecture.md](architecture.md#midi-controller-bridge-alsa-sequencer-and-din-midi) for its files.

## MIDI clock from a Pro DJ Link master

`emulator/sync/bridge.py` turns a Pro DJ Link master's tempo into a 24 PPQN clock on any CoreMIDI destination, with an
optional Ableton Link half. It is described in [Pro DJ Link](pro-dj-link.md#the-sync-bridge).
