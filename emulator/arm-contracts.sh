#!/bin/bash
# Run every ARM preload contract (tests/sp16_*_contract.c) offline in the packaged
# guest base image. The script builds its own untagged image from the current
# Dockerfile.native-base (identified by --iidfile, so parallel runs never share a tag)
# and removes it on exit. Exits non-zero if any contract fails.
set -u
cd "$(dirname "$0")" || exit 1
output=$(mktemp)
iidfile=$(mktemp)
trap 'rm -f "$output" "$iidfile"; [ -n "${image:-}" ] && docker image rm "$image" >/dev/null 2>&1; true' EXIT
status=0
if ! docker build -f Dockerfile.native-base --iidfile "$iidfile" . > "$output" 2>&1; then
  echo "FAIL docker build of native guest image"; tail -20 "$output"; exit 1
fi
image=$(cat "$iidfile")
if ! printf '%s\n' "$image" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
  echo "FAIL unexpected image identity"; exit 1
fi
run() { # contract-name shell-commands
  echo "== $1"
  docker run --rm --network none --mount "type=bind,src=$PWD/tests/$1.c,dst=/tmp/test.c,readonly" \
    --entrypoint /bin/sh "$image" -ec "mkdir -p /tmp/run; $2" > "$output" 2>&1
  code=$?
  grep -v '^\[sp16-ui-stub\]' "$output" | tail -5
  if [ $code -eq 0 ]; then echo "PASS $1"; else echo "FAIL $1 ($code)"; status=1; fi
}
expect() { # log-line-regex: the stub's own log from the run just finished
  if grep -q "$1" "$output"; then echo "PASS log $1"; else echo "FAIL log $1"; status=1; fi
}
CC="arm-linux-gnueabi-gcc -Wall -Wextra"
PRE="qemu-arm-static -L /usr/arm-linux-gnueabi -E LD_PRELOAD=/opt/sp16/libsp16-ui.so"
run sp16_stub_contract "$CC /tmp/test.c -o /tmp/run/t; $PRE /tmp/run/t"
run sp16_din_queue_contract "gcc -Wall -Wextra /tmp/test.c -lasound -o /tmp/run/t; /tmp/run/t"
run sp16_din_contract "$CC /tmp/test.c /opt/sp16/libsp16-ui.so -o /tmp/run/t; $PRE /tmp/run/t"
expect 'rawmidi hw:snddinmidi,0 ->'
run sp16_pcm_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t; $PRE -E LD_LIBRARY_PATH=/tmp/run /tmp/run/t"
run sp16_hwparams_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t; $PRE -E LD_LIBRARY_PATH=/tmp/run /tmp/run/t"
expect 'PCM hw:ak4452audio,1 set_access requested=3(RW_INTERLEAVED) result=0'
expect 'PCM hw:ak4452audio,1 set_rate_near requested=44100 dir=0 returned=44100'
expect 'PCM hw:ak4452audio,1 hw_params result=0 access=RW_INTERLEAVED(3) format=S16_LE(2) channels=2 rate=44100'
expect 'PCM hw:ak4452audio,1 io=writei first_call'
expect 'PCM hw:ak4452audio,0 set_format requested=14(OTHER) result=-22'
expect 'PCM hw:ak4452audio,0 set_format requested=6(OTHER) result=0'
expect 'media poll /proc/udev_usb1 (attempt 1)'
expect 'blocked guest helper exec: /bin/sync argc=2 argv=\[sync -n\]'
run sp16_pcm_tap_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_PCM_ANY_FORMAT=1 -E SP16_PCM_TAP=/tmp/run -E SP16_PCM_TAP_SECONDS=1 /tmp/run/t; rm -f /tmp/run/sp16-pcm-*; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_PCM_ANY_FORMAT=1 /tmp/run/t off"
expect 'PCM tap hw:ak4458audio,0 -> /tmp/run/sp16-pcm-ak4458audio-0.raw cap_frames=44100'
run sp16_audio_transport_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t; : > /tmp/run/port; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_PCM_PORT=/tmp/run/port /tmp/run/t file; rm -f /tmp/run/port; : > /tmp/run/port; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_PCM_PORT=/tmp/run/port /tmp/run/t s24; rm -f /tmp/run/port; mkfifo /tmp/run/port; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_PCM_PORT=/tmp/run/port /tmp/run/t stall; rm -f /tmp/run/port; $PRE -E LD_LIBRARY_PATH=/tmp/run /tmp/run/t off"
expect 'audio transport hw:ak4458audio,0 -> /tmp/run/port stream=0 fmt=2 channels=0..15 of 16 rate=44100'
expect 'audio transport hw:ak4452audio,1 -> /tmp/run/port stream=2 fmt=6 channels=0..1 of 2 rate=44100'
expect 'converted bit-exact'
run sp16_board_contract "$CC -DSP16_FAKE_ALSA -shared -fPIC /tmp/test.c -o /tmp/run/libsp16-fake-alsa.so; $CC /tmp/test.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t; $PRE -E LD_LIBRARY_PATH=/tmp/run -E SP16_PCM_ANY_FORMAT=1 -E SP16_ANALOG=/tmp/run/analog -E SP16_MIDI_LOOPBACK=1 -E SP16_FAKE_ETH0=1 /tmp/run/t"
expect 'dac hpf word=0x1fff ctrl=1 code=4095 writes=1'
expect 'analog board state mapped from /tmp/run/analog'
expect 'midi loopback /dev/ttymxc1 first bytes=4'
expect 'gpio GPIO5_IO05=0 SEL_xTHRU/DSF'
expect 'fake eth0 SIOCGIFHWADDR'
expect 'fake eth0 SIOCGIFADDR'
expect 'fake eth0 SIOCGIFNETMASK'
expect 'fake eth0 SIOCGIFFLAGS'
expect 'fake eth0 SIOCETHTOOL'
run sp16_panel_contract "$CC /tmp/test.c -o /tmp/run/t; $PRE -E SP16_PANEL_INPUT=1 -E SP16_PANEL_SOCKET=/tmp/run/panel.sock /tmp/run/t"
run sp16_feedback_contract "$CC /tmp/test.c -o /tmp/run/t; $PRE -E SP16_PANEL_INPUT=1 -E SP16_PANEL_SOCKET=/tmp/run/panel.sock -E SP16_CDC_SOCKET=/tmp/run/cdc.sock -E SP16_PANEL_TX=/tmp/run/tx.sock /tmp/run/t"
run sp16_media_contract "$CC /tmp/test.c -o /tmp/run/t; $PRE -E SP16_USB1_SOCKET=/tmp/run/usb1.sock -E SP16_USBCTN1_SOCKET=/tmp/run/usbctn1.sock /tmp/run/t"
run sp16_exec_contract "$CC /tmp/test.c -o /tmp/run/t; $PRE /tmp/run/t"
expect 'allowed sync no-op exec: /bin/sync'
expect 'allowed sync no-op exec: /bin/sh'
expect 'blocked guest helper exec: /sbin/mkfs.ext4'
expect 'blocked guest helper exec: /bin/mount'
expect 'blocked guest helper exec: /sbin/reboot'
exit $status
