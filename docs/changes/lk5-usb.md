---
title: "Lane LK5 USB-B note output: g_midi was never published, so the USB route was never subscribed"
description: "The firmware does not run apl_start in the emulator, so g_midi is never published and the app subscribes its output to DIN only; the bridge now publishes the peer, and the remaining gate is a note on a pad with no playable content."
type: report
status: complete
created: 2026-10-28
tags: [sp16, emulator, dispatch, lane-report, midi, usb]
author:
  harness: pi
  agent: lk5-usb
  model: deepseek-v4-pro
  thinking: high
  tokens: unknown
---

# Lane LK5 hand-back

Branch `lane/lk5-usb`, cut from `emu/2026-10-28` (`2270310`). One commit plus this file. One research build
(`build/lk5-usb`, `--research --audio file`, QEMU reused from `build/qemu-source`) was made and booted several
times, always stopped by the exact app PID.

The USB-B note path has **two gates**, found by instrumenting the existing shim trace (no new trace code was
needed: `hardware_stub.c` already logs sequencer opens, subscriptions and every NOTEON/NOTEOFF write). The first
gate was a code bug and is fixed; the second is the firmware's pad-content gate and stays parked.

## What the bisect found

**Stage 1, host path (not the gate).** The USB route carries bytes end to end: `midi_bridge` `test_virtual_usb_route_is_bidirectional_and_unpaced`
round-trips a guest-to-host note through `midi/usb.py` and a real CoreMIDI monitor, and no `midi-routing.json`
existed on this host (an absent port entry carries everything, per `midi/routing.py`).

**Stage 2, guest side (the first gate).** A boot trace of firmware 1.60 showed the app registering its MIDI manager
and subscribing only to DIN:

```
[sp16-ui-stub] MIDI seq client=130 port=0 name=JuceMidiInput caps=0x42
[sp16-ui-stub] MIDI seq subscribe 128:0 -> 130:0
[sp16-ui-stub] MIDI seq client=131 port=0 name=JuceMidiOutput caps=0x21
[sp16-ui-stub] MIDI seq subscribe 131:0 -> 128:0
```

There was **no `g_midi` (client 129) client and no `subscribe ... -> 129:0`**. The reason: the emulator does not run
the firmware's `apl_start`, whose `usb_gadget.sh midi probe` loads the gadget module on real hardware; nothing
created `/dev/sp16-usb-midi-enabled`, so the shim never advertised client 129 and the app never subscribed its
output to the USB route. `docs/changes/lq-seq-proxy.md` had already proven the app *does* subscribe to `g_midi`
when it is published (its USB FA/F8/FC round trip), so the missing publication was the whole gate.

## Per-task hand-back

### `pt-i44.35` USB-B note output — route fixed, note acceptance parked — `d908644`

Files: `emulator/midi/seq-bridge.c`, `emulator/tests/sp16_usb_route_contract.c`.

`seq-bridge.c` now creates the `g_midi` mode marker (`/tmp/sp16-root/dev/sp16-usb-midi-enabled`, overridable via
`SP16_USB_MIDI_MODE_PATH`) as soon as `/dev/sp16-usb-midi` opens, before the app registers. The shim advertises
client 129 while that marker exists, so the app now creates a per-peer port and subscribes it:

```
SP16_USB_MIDI peer=g_midi
[sp16-ui-stub] MIDI seq client=130 port=0 name=JuceMidiInput caps=0x42
[sp16-ui-stub] MIDI seq subscribe 128:0 -> 130:0
[sp16-ui-stub] MIDI seq client=130 port=1 name=JuceMidiInput caps=0x42
[sp16-ui-stub] MIDI seq subscribe 129:0 -> 130:1
[sp16-ui-stub] MIDI seq client=131 port=0 name=JuceMidiOutput caps=0x21
[sp16-ui-stub] MIDI seq subscribe 131:0 -> 128:0
[sp16-ui-stub] MIDI seq client=131 port=1 name=JuceMidiOutput caps=0x21
[sp16-ui-stub] MIDI seq subscribe 131:1 -> 129:0
```

Live proof that the route now works: a CoreMIDI monitor on `SP-16 USB [lk5-usb]` receives the app's generated
transport — `clock`, `stop` and CC 123 all-notes-off on all 16 channels — identically to `SP-16 DIN`, where before
the fix the USB source carried nothing.

- `bash emulator/arm-contracts.sh` — **all contracts PASS**, including the new `publish_usb_peer` case in
  `sp16_usb_route_contract` (marker appears for an open transport, never for an absent one).
- Quick set (14 modules) plus `test_sp16_usb_midi test_sp16_din_midi test_sp16_midi_routing test_sp16_midi_clock` —
  **489/489 OK** (`Ran 489 tests`, `OK`).
- Swift type-check not run: no Swift file changed. Emulator smoke and hwsuite not run: not the brief's acceptance and
  the fix is C only, pinned by the contract and the live trace.

### The remaining gate (parked, not fixed)

With the recipe `usb-note-out` applied (MIDI OUT MODE (USB) = OUT, MIDI OUT NOTE/CC = ENABLE, OCR-confirmed) and the
USB subscription now present, pressing all 16 pads still produced **0** `[sp16-ui-stub] MIDI seq output` NOTEON/NOTEOFF
lines, on both DIN and USB. The project is empty (HOME shows `MPL OFF` and no sample names), so a pad hit has no
playable content to turn into a note; transport and CC do flow because they do not need pad content. The next step is
to assign a sample to a pad and set the per-track MIDI channel and NOTE/CC send (1.60 addendum pp. 47-51). A genuine
attempt was made: a 16-bit 44.1 kHz WAV was added to `PIONEER DJ SAMPLER/Samples` on the stick and the firmware's
BROWSE reached and highlighted it, but the `sample_load` profile recipe's `focus_row` could not OCR the row label
(`> sp16-tone.wav`) through its `> ` prefix and `.wav` suffix, and the pad never showed a loaded sample, so the note
acceptance stays open.

## Side effects

- Built `build/lk5-usb` once and rebuilt it in place after the fix (`./sp16 build --rebuild ...`).
- Booted and stopped five sessions (three `--ephemeral`, two persistent) with `--midi`; every emulator was stopped by
  its exact app PID, and no emulator or MIDI helper process is left running.
- Added `sp16-tone.wav` to the git-ignored `build/stick.img` under `PIONEER DJ SAMPLER/Samples` (a test fixture; not
  committed, the image is git-ignored).
- No firmware, key, device identity, data image, PCAP or session capture was committed.

## Parked tasks

- `pt-i44.35` note acceptance: the route is fixed and proven, but a pad hit still generates no note because no sample
  is assigned to a pad; needs the sample-assign recipe and the per-track channel/note-cc settings.

## Cross-lane requests

None.

## Open questions for the owner

- Is a transport message (clock / stop / CC 123) on the `SP-16 USB` CoreMIDI source acceptable evidence that the
  USB-B note route is fixed, or must a pad-generated NOTEON also arrive (which additionally needs playable content)?
- What is the exact firmware menu sequence to assign a stick sample to a pad and set the per-track MIDI channel, so a
  recipe can be authored that closes the note acceptance?
