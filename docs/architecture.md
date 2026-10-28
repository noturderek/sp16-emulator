# Architecture

The emulator behaves like the hardware: a fixed machine that boots whatever firmware update it is given. Everything
below the firmware's application is emulated; the application and its userland come from the user's update.

## The hardware boundary

The firmware talks to its hardware through a small set of interfaces. The emulator implements exactly those:

| Interface | Emulated by |
|---|---|
| Panel MCU (buttons, encoders, selector, pads, analog pots, lamps) over SPI device nodes | `hardware_stub.c` (device nodes, ioctls) and `panel.py` / `panel_map.py` / `protocol.py` (128-byte frames with CRC, both directions) |
| Touch strip (capacitive converter) | `hardware_stub.c`, `panel.py` |
| LCD (800x480) and touch | Xvfb in the guest, mirrored to the app window; touch is converted from screen coordinates with the loaded calibration and clamped (0..1023), debounced (5-frame hold, 32 raw units/frame drag limit, release 0,0), and delivered as RX frame words 0x2c and 0x2e |
| Audio codec (three ALSA playback ports, two capture ports) | `hardware_stub.c` paces each stream on its own clock and carries the samples to the host (`audio-sink.swift`, `audio-output.swift`). Board PCMs are fixed at 44.1 kHz (`SP16_PCM_ANY_RATE=1` for research). AK4452 ports refuse S20_3LE and settle on S24_LE; AK4458 is fixed to S16_LE across all 16 channels. The master transport sends all 16 channels by default (`SP16_PCM_ANY_FORMAT=1` for research) |
| Analog board: filter send/return loop, high-pass DAC, MAIN OUT, INPUT LEVEL, CUTOFF, RESONANCE | `hardware_stub.c` board model (see [Analog board model](#analog-board-model)); host-only pot positions from `panel.py` through a shared state file; reference curves in `analog_laws.py` |
| GPIO lines (filter select, input limiter, MIDI thru, USB-A power and over-current) | `hardware_stub.c` serves `/dev/gpiodrv` (one byte per line); input limiter on GPIO5_IO06; active-low USB over-current on GPIO4_IO14 (`ctl usb-oc on\|off`). Logged and reported in `metrics.guest.board.gpio` |
| DIN MIDI and clock | Dedicated `sp16.din` virtio port with host virtual CoreMIDI bridge (`emulator/midi/din.py`, gated by `--midi`), ALSA sequencer bridge (`seq-bridge.c`), rawmidi UART backend, and shared clock engine (`emulator/midi/clock.py`). Note: host-to-guest transport is demonstrated, but firmware MIDI discovery/routing and audio-position clock output remain parked. Factory test loopback (`/dev/ttymxc1`) serves PAGE 8: DEVICE TEST |
| Network and Pro DJ Link | Virtio-net NIC (`--net host\|bridged:IF\|mcast[:PORT]`, `--net-ip`). `hardware_stub.c` provides fixed GLINK carrier=1 response and passes real ARP packet sockets. Link-local 169.254.A.B assigned by guest. Note: the firmware binds UDP 50001/50002 but does not bind or emit port 50000 UDP keep-alives; Pro DJ Link sync is parked |
| Internal storage | a per-firmware 1 GB data image (`data/emmc.img`), formatted once; `./sp16 run --ephemeral` uses a disposable one with no saved project and no lock, so a second app can run the same firmware |
| USB stick | `virtio-blk-pci` on a PCIe root port supporting QMP hot attach at runtime (`ctl usb attach PATH`, USB > Attach USB Image...) |
| Backlight, media notifications, helper processes | `hardware_stub.c` serves `/dev/backlight` ioctl `0x40047000` (raw 0..255), scaling panel LCD brightness from Utility settings. Unknown device nodes and ioctls fail loudly and are counted |

Nothing in the emulator depends on a firmware version's internals. What differs between versions (lamp channel roles,
pad curve limits, selector window, calibration file format, runtime layout) lives in that version's compatibility
profile ([profiles.md](profiles.md)).

## Components

| Part | Files (in `emulator/`) |
|---|---|
| One command for all of the below | `sp16emu.py` (`./sp16`; `make` for the common configurations) |
| Build, launch and firmware commands | `native.py`; `qemu_reaper.py` (started with QEMU: stops it, after a clean guest stop, when the app quits, crashes or is killed) |
| Firmware library and importer, host side (stdlib only) | `firmware.py`, `profiles.py`, `recipes.py`, `fwpack.py` |
| Diagnostics for bug reports (stdlib only; the app bundles a copy) | `diagnostics.py` |
| Linux guest base image | `Dockerfile.native-base`, `pack-guest.sh`, `guest-init.sh`, `guest-firmware.py` (import and repack in the guest), `prepare.py` (unpacks an update's layers without running its programs), `guest-bridge.py`, `enter.py`, `input.py` |
| Hardware shim (ARM preload library) | `hardware_stub.c` |
| Panel model and wire protocol | `panel.py`, `panel_map.py`, `protocol.py`, `analog_laws.py` |
| macOS app | `native-window.swift`, `panel-view.swift`, `panel-layout.json`, `firmware-window.swift`, `firmware-library.swift`, `app-diagnostics.swift`, `data-image.swift`, `audio-sink.swift`, `audio-output.swift`, `make-icon.swift` |
| Developer tools | `fwdev.py`, `sessionctl.py`, `e2e-screen.py`, `pcm-tap-analyze.py`, `arm-contracts.sh`, `exectrace.py` (reads `--trace exec` output), `gdb-bridge.py` (host side of `--trace gdb`), `audio-selftest.swift` (`./sp16 audio selftest`), `service_stimulus.py`, `timing_probe.py`, `layout_overlay.py` |
| MIDI controller bridge and clock | `emulator/midi_bridge/` (`midi.py`, `profile.py`, `learn.py`, `actions.py`, `leds.py`, `presets.py`, `coremidi-io.swift`), `emulator/midi/` (`clock.py`, `din.py`, `seq-bridge.c`) |
| Network and Pro DJ Link tooling | `net/qemu-net.sh`, `emulator/net/segment_watch.py`, `emulator/net/fake_master.py` |
| Hardware conformance suite | `hwsuite.py`, `shotdiff.py` |
| Data | `profiles/*.json`, `profiles/products/sp16.json` |

## Analog board model

The hardware has analog circuitry the firmware never sees as samples. The shim models it so the firmware's own
audio paths run as on the hardware:

- **Filter insert loop.** The analog filter sits between the "Send & Return" playback (send DAC) and capture (return
  ADC). The loop queue is a FIFO: it queues send frames and delivers them whenever the return capture drains it,
  rather than enforcing a fixed one-period delay. The signal passes through a two-pole low-pass (CUTOFF and
  RESONANCE, pots no microcontroller reads) and a two-pole high-pass whose cutoff follows the 12-bit code the
  firmware writes to `/dev/dac_spi4.0` (one 2-byte write per change of the HP CUTOFF pot word). ACTIVE drives the filter
  select GPIO line. Reference curves and transfer functions are defined in `analog_laws.py` (their shapes are
  assumptions, U). **Open item:** the firmware decides what feeds the send, and in the sessions tried normal sampler
  playback has not put signal there, so the loop only carries the research `filter_tone` and LP CUTOFF, RESONANCE, HP
  CUTOFF and ACTIVE have no audible effect on playback yet. `metrics.guest.board.filter_loop` is absent while the loop
  carries nothing.
- **DRIVE** has no model here: the firmware reads the pot as an ADC word (`0x26`) and applies it itself. The emulator
  only supplies the word, and it leaves the DAC code alone (HW-30 checks that).
- **MAIN OUT** is an analog volume after the master DAC: the shim scales OUT1-2 (master channels 12 and 14) of the copy
  the host receives (the firmware still gets the pot position as an ADC word).
- **INPUT LEVEL and limiter.** INPUT LEVEL scales the input capture. When the firmware asserts AUDIO TEST LIMIT
  (GPIO5_IO06), a soft limiter activates on the input capture (transparent up to half scale, soft saturation above).
  Converter full scale is taken as +16 dBu peak-equivalent (grade I). Readouts in `guest.board.analog_levels`
  report peak-equivalent dBu (`in_dbu_peak` and `out_dbu_peak`, where dBu = dBFS + 16), along with `limiter_active`
  and `limited_samples`.
- A control the host never moved is neutral (unity gain, low-pass open), so a session that does not touch them sounds
  as before. The cutoff laws (20 Hz to 20 kHz, exponential over the pot or DAC range) are assumptions, not
  measurements of the hardware.
- The panel knobs start with the filter wide open: DRIVE, RESONANCE and HP CUTOFF at minimum, LP CUTOFF at maximum,
  MAIN OUT centred (`ANALOG_DEFAULTS` in `panel_map.py`, mirrored as `initial` in `panel-layout.json`; a test keeps the
  two equal). LP CUTOFF and RESONANCE are never sent at start because "never moved" already means exactly those positions.
- ACTIVE starts off. The stock app lights it (filter-select line high) at every start, on a fresh and on a saved data
  image, so the guest bridge presses the button once its lamp has stayed lit for 3 s, repeating a press the app missed
  up to 4 times, then leaves it alone (`panel.StartOff`, `START_OFF` in `panel_map.py`; console line
  `SP16_START_OFF active pressed`).
- Research stimuli (`./sp16 ctl event NAME value 0..1000`, 0 = off): `input_tone` puts a 1 kHz tone on the input
  jack, `filter_tone` one at the filter input. `input_level` and `power` (the power/standby key, RX bit 5.4) are host
  controls without a place on the panel drawing.
- `metrics.guest.board` reports the newest DAC word (`dac`), the filter loop's peaks every 5 s while it carries
  signal (`filter_loop`), the GPIO lines (`gpio`), `analog_levels` and the MIDI loopback (`midi_loopback`).
- **Power-on mute.** CoreAudio output holds the firmware's ~19.5 s power-on mute until the application begins
  active playback.
- **Power visual states.** The host app tracks power visual states independently: BOOT (boot progress overlay),
  RUN (live screen and lamps), OFF on standby (LCD dark, WAKE UP lit steady red), and FAULT (`ctl fault on`: panel
  dark, WAKE UP blinking red at ~1 Hz). Standby and fault states do not clobber each other.

The power/standby key (`./sp16 ctl press power` or `./sp16 ctl standby yes`) puts the firmware in standby: it
unmounts the USB stick, stops project saves and raises its standby flag in the panel frame (TX byte 0x67 bit 0).
A real unit then cuts power; the emulator has no power controller, so the firmware stays in standby until
`./sp16 ctl app-restart` (decoupled builds) or a new session.

## Factory test mode

On the hardware, holding HOLD + TRACK + STEP 1 while powering on makes the initramfs (which reads the panel for about
1.5 s before the firmware starts) start the application with the argument `testmode`. The emulator provides this via
Firmware > Power On With Keys Held > Test Mode, or the launch flag `--app-arg testmode` (research builds, allowed by
the profile's `runtime.app_args`). In the menu, Normal restarts the firmware app, and Update Mode (HOLD + MODE) holds
those keys across an app restart (experimental).

In test mode the DIN MIDI ports get a loopback cable. On PAGE 8 (DEVICE TEST), tapping the MIDI CHECK touch button at
about (115, 112) runs the loopback test and reports `MIDI CHECK : OK`.

## App shell, menus and UI

- **Lamp Glow.** View > Lamp Glow adds an additive bloom halo (plus-lighter blend, LCD glass excluded) around lit
  pads, steps, bars, buttons and LEDs. It is off by default and is not remembered between launches.
- **Rear I/O panel.** View > Rear I/O Panel (Cmd-R) draws the rear jack layout in code from `panel-layout.json`
  (DC IN, power, RJ45 LINK, MIDI OUT/THRU/IN, USB-B, INPUT L/R with LEVEL knob, OUT 7-8..1-2/MASTER, lock slot,
  PHONES). The jacks are non-functional visual representations.
- **Private Skin Overlay.** View > Private Skin Overlay displays a transparent PNG (1773 x 1011 or multiple)
  placed at `private/skin/overlay.png` (or pointed to by `SP16_SKIN_OVERLAY`). The setting is remembered across
  launches. The repository ships no OEM artwork or logos; personal skins stay in git-ignored `private/`.
- **Output monitor menu.** The menu bar includes an **Output** menu (MAIN, PHONES, OUT 3-4, OUT 5-6, OUT 7-8)
  allowing the user to select which jack pair CoreAudio plays (via `AudioSink.monitor`).
- **USB hot attach.** The virtual USB stick uses `virtio-blk-pci` on a PCIe root port. Choosing USB > Attach USB
  Image... or running `./sp16 ctl usb attach PATH` ejects the current stick, swaps the backing image via QMP, and
  announces the new stick to the guest. Active-low over-current can be tested with `./sp16 ctl usb-oc on|off`.
- **App Nap and teardown.** The app takes a latency-critical activity assertion at launch (`ProcessInfo.beginActivity`)
  so background sessions without audio sinks are not throttled. On termination, teardown follows a clean guest
  stop ladder bounded by a 9-second `_exit(0)` fallback.

## MIDI controller bridge and DIN MIDI

- **MIDI controller bridge.** `./sp16 midi` (`emulator/midi_bridge`) connects any CoreMIDI controller to the emulator:
  `ports` lists endpoints, `learn` interactively maps pads, knobs and keys into `private/midi/`, and `run` translates
  MIDI input into session control events (`actions.py`). Pad velocity, knob detents, analog pots and the touch strip
  are supported, with lamp feedback echoed as LED note/CC velocity (`leds.py`). A built-in preset mirrors the real
  SP-16 MIDI OUT layout (`--preset sp16`).
- **Shared clock engine.** `emulator/midi/clock.py` provides a second-order delay-locked loop (DLL) that recovers
  BPM and phase from an inbound 0xF8 clock stream, and a sample-accurate clock generator from audio position.
- **DIN MIDI transport.** Launched with `./sp16 run --midi`, a dedicated `sp16.din` virtio port connects a host
  virtual CoreMIDI bridge (`SP-16 DIN [<build>]`) to an ALSA sequencer bridge (`seq-bridge.c`) and rawmidi UART backend
  with 320 µs slot pacing. **Current status:** host-to-guest delivery is demonstrated, but firmware-side MIDI routing,
  sequencer subscriptions and audio-position clock output remain incomplete and parked. USB-B MIDI device emulation is
  not implemented.

## Networking and Pro DJ Link

`./sp16 run --net host|bridged:IF|mcast[:PORT]` (`--net-ip ADDR`) launches QEMU with a virtio-net NIC:
- `host` connects via the `socket_vmnet` daemon on `bridge100`.
- `bridged:IF` connects to the physical LAN interface (with `--net-ip` configuring a static IP).
- `mcast` joins a local multicast socket (230.0.0.1:34567) without root.
- The shim provides a synthetic GLINK carrier=1 response and passes real ARP packet sockets, and the guest assigns a
  169.254.A.B link-local address.
- **Current status:** the firmware binds UDP ports 50001 and 50002, but does not bind or transmit port 50000 UDP
  keep-alive packets. Consequently, following a fake tempo master (`fake_master.py`) and Pro DJ Link sync remain parked.

## Boot

1. The app reads its `default-config.json`, resolves the selected firmware in the library, checks it against the
   profiles bundled in the app (a verified profile for that exact application hash makes the run verified, anything
   else runs unverified on the reference profile), re-hashes the firmware image and starts QEMU with Hypervisor.framework.
2. QEMU boots the Debian guest kernel and the base initramfs. The firmware image is attached read-only as a
   virtio-blk disk, the data image as a second disk.
3. The guest init re-verifies the image hash and the application hash against the manifest and its own copy of the
   profiles, mounts the image read-only under a tmpfs overlay, starts Xvfb and the bridge, and runs the firmware's
   application with `qemu-arm-static` and the hardware shim preloaded.
4. The bridge connects the shim to the host over virtio-serial ports: panel frames, lamps, touch, audio, USB and
   control messages. The app draws the panel and the LCD and plays or records audio.

## Logs and diagnostics

The app is started through LaunchServices, so its own output is lost; it writes its own log instead. Everything is in
the build folder, owner-only:

| File | Holds |
|---|---|
| `sessions/native-<id>/app.log` (+ `.1`) | timestamped app events: launch configuration, firmware chosen and its status, import and library actions, window open and close, QEMU start and exit status, bridge connect and disconnect, notices shown. 2 MB, one rotation. No key or firmware content exists in the app. |
| `logs/app-<time>-<pid>.log` | the same for the time before a session exists (import screen, library window); the newest 20 are kept |
| `sessions/native-<id>/qemu-stderr.txt` | the last 200 lines of QEMU's stderr and its exit status (also still copied into `console.txt`) |
| `sessions/native-<id>/clean-exit` | written when the app quits normally; `app.pid` tells a later launch whether a session without it is still running |

At launch the app looks at the previous session: no `clean-exit` and no live process, or a new macOS crash report
(`~/Library/Logs/DiagnosticReports/SP16Emulator-*.ips`) naming this app, shows a non-blocking notice that offers to
collect diagnostics, once per finding. Help > Collect Diagnostics… (and `./sp16 diagnostics`) runs the bundled
`diagnostics.py`, which writes one redacted zip; see "Reporting a bug" in the README for what it contains.

## Import

1. The host checks the update's trailer and CRC, and reads the key into memory: from the key file, or from the GPL
   source archive (its `.tar.gz`, split parts or the two official zip downloads).
2. It boots the base guest in import mode with the update attached read-only and sends the key over a private
   virtio-serial socket. The key is never written to a file.
3. The guest decrypts the update, checks the volume, unpacks the firmware layers, drops credential and key files,
   refuses if any remaining file contains the key, scans the application for the compatibility report, and writes a
   deterministic squashfs to an output disk.
4. The host hashes the image and stores it with its manifest (every file's hash, the update and image hashes,
   revisions, compatibility facts) in `<library>/<id>/`.

## Design decisions

| ID | Decision |
|---|---|
| D1 | The user supplies the key (a key file, or the official GPL source archive it comes from). Nothing is bundled or downloaded. The key is used for the import boot only. |
| D2 | Any update encrypted with the correct key is accepted and runs. A version with a verified profile runs as verified; any other runs on the reference profile, marked unverified (title, library status, a one-time notice, console `SP16_FIRMWARE_UNVERIFIED`). `--allow-unverified` is accepted and does nothing. |
| D3 | The developer workflow also produces repacked updates, for this emulator only. They carry an `SP16MODLAB-<version>` marker and are verified when the importer can rebuild them byte for byte from a verified base and the declared recipes, and run as unverified otherwise. The repository ships the tooling, never a mod. |
| D4 | Neutral product name and bundle id, no vendor logos, source-only distribution. |

Code comments refer to the stages in which the design was built: M1 firmware registry and profiles, M2 importer,
M3 runtime attach (firmware as a disk, no firmware in the guest image), M4 import screen and firmware library,
M5 compatibility report and other versions, M6 developer loop (overlay, app restart, recipes, `fwdev`, repacked
updates), M7 distribution packaging.

## Adding a firmware version

1. Import it. It is stored as `unverified` and already runs; read the compatibility report for unknown device nodes, ALSA names,
   ioctls, panel revisions and calibration markers.
2. `./sp16 firmware profile --id <id> > emulator/profiles/sp16-<version>.json`
   writes a `candidate` profile with this import's identity and the reference profile's measurements.
3. Build a research variant that shares the library:
   `./sp16 build --name sp16-dev --research --audio file --bundle-suffix dev` (all builds share `build/library`).
4. Launch it, correct any measurement that differs (lamp roles, pad curve, selector window, runtime layout,
   calibration), and run the hardware suite at the full tier ([hardware-suite.md](hardware-suite.md)).
5. Only a full-tier pass marks the profile `verified` (`./sp16 hwsuite --tier full --mark-verified`). Until then the version
   runs as unverified.

## Research socket

Builds made with `--research` open an owner-only control socket per session; its path is in the session's
`runtime.json`. `./sp16 ctl <command>` (it runs `emulator/sessionctl.py` against the newest research session) sends
panel events, touches, raw frames, snapshots, USB attach and eject, `power`, `standby`, `pressure`, `calibrate`,
`fault`, `monitor`, `usb-oc`, `metrics`, `firmware` and `app-restart`. The hardware suite and `fwdev.py` use it.
Each call waits up to 60 s for the app's whole reply (`--timeout`, or `SP16_CONTROL_TIMEOUT`; a reply arriving in pieces
counts as one): a heavily loaded host can take well over 10 s to answer. A call with no usable reply ends in one line
naming the operation, how long it waited and whether the app process is still running (the app hung up, did not answer
in time, or answered something that is not one UTF-8 JSON object); an error the app reports is one line too. After a
timeout the client closes the connection, since a late reply would otherwise answer the next call.

### Research traces (`--trace`)

A research build (`--research`) can add one opt-in qemu-user trace of the OEM app: `./sp16 build --research --trace MODE`.
`strace` streams the syscalls to `console.txt`, `gdb` opens the qemu-user gdb stub (`-g 1234`) for lldb, `pcmtap`
records the first 4 s of each playback stream, and `exec` records which translation blocks the app executes.

For `gdb` the guest has no network beyond loopback, so the stub is tunnelled out over a dedicated virtio-serial port
to a unix socket next to the session's control socket. `emulator/gdb-bridge.py --build <name>` (or `--socket PATH`)
listens on `127.0.0.1:1234` and splices one client at a time to it; then `lldb` and `gdb-remote 127.0.0.1:1234`
attach. The app stays halted at its entry point until the debugger continues it, so attach before expecting the
firmware's screen. The bridge keeps running, so you can detach and attach again.

`exec` runs the app with `-d exec,nochain -dfilter 0x116f8..0x831ca8` (the 1.60 app's text range; `SP16_EXEC_RANGE` in
`guest-init.sh`). The raw log is one line per executed block, far too much for the serial console, so it goes to a fifo
that a small guest filter reads. Every app start (including `app-restart`) is a new qemu-user that numbers its guest
threads from cpu 0 again, so the filter keeps an app-instance counter (from 0, bumped each time it reopens the fifo,
announced by `SP16_TB_INSTANCE i<n>`) and dedupes on (instance, cpu, address): qemu-user gives each guest thread its own
cpu index (the first field of every log line). It prints each triple once, in first-execution order, in batches as
`SP16_TB i<n> c<cpu> <hex> ...` lines in the session's `console.txt`. It also prints `SP16_TB_CPU i<n> c<cpu> t=<s>` when
a thread first appears in an instance, and `SP16_TB_END i<n> distinct=N lines=M t=<s> cpus=c0:n@t,...` when that
instance's app exits (`SP16_TB_STATS` with the same fields every 10 s while new blocks appear): `t` is seconds since the
instance's first log byte, `cpus` the instance's distinct blocks and first-seen second per thread. The guest has no
shared writable folder, so the console is the way to the host. The output is bounded by the number of distinct
(instance, cpu, address) triples (a 9-byte token each, at most 1,000,000 printed per instance); another console writer
can cut into a batch line, which the reader tolerates. `python3 emulator/exectrace.py <session>/console.txt --out tb.txt`
collects them (distinct count, per-instance and per-cpu counts checked against the guest's, first five, range check)
into one `i<n> c<cpu> 0x%08x` per line. A console from an older build still reads: `SP16_TB c<cpu> ...` is instance 0,
`SP16_TB <hex> ...` is instance 0 with no cpu.
Tracing every block slows the app a lot: expect a long boot. A clean stop (`SIGTERM` to the app's process id, or Cmd-Q)
loses at most the last half second of addresses.

### Bench mode (`--no-ui-input`)

A script that drives a session through the socket and compares the LCD against reference data can be disturbed by
anyone using the Mac: macOS sends scroll-wheel events to the window under the pointer whether or not it is active, and
a scroll over an analog pot moves it (the AD TEST page shows the new value). `./sp16 run --no-ui-input` starts the app
in bench mode: the panel drops every click, drag, scroll, key and accessibility action before it reaches the guest,
the toolbar's input controls are replaced by a "Bench mode: input off" badge, and the app opens with `open -g` and does
not activate itself, so it takes no focus. Only the app's gate stops the scroll; opening in the background alone would
not. The research socket, LCD snapshots and panelshots are unchanged (they never pass through the panel's input
handlers), the window still closes and Cmd-Q still quits, and the session's `runtime.json` records `"ui_input": false`.

An audio build (`--audio file` or `both`) writes three WAVs of about 21 MB a minute each (about 64 MB a minute together)
into the session folder.
`./sp16 run --no-audio-files` keeps the audio sink (it still reads every PCM packet from the guest and keeps the
counters that `metrics` reports, so the guest's audio path and timing are those of a file build) but opens no WAV:
for unattended runs that read no audio. A `both` build keeps playing through CoreAudio. The metrics' audio `mode` says
`counters` for a `file` build and `coreaudio` for a `both` build, each stream's `wav` is empty, and `runtime.json`
records `"audio_files": false`. The flag survives the relaunch after a firmware choice or import.
