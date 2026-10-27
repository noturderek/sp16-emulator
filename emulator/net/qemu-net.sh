#!/bin/sh
# Starts the build's QEMU. Stock runs have no network; `./sp16 run --net MODE` hands the choice over in the environment
# (SP16_NET_KIND, SP16_NET_TARGET) and this adds the guest's virtio NIC with a MAC of its own for this instance.
# It replaces itself with QEMU (exec), so the app still supervises QEMU's own process id.
set -eu
real="$(cd "$(dirname "$0")" && pwd)/qemu-system-aarch64"
if [ -n "${SP16_NET_KIND:-}" ]; then
  # Locally administered (52:54:00), random per start: two instances never share a MAC, and none is a real unit's.
  hex=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
  mac="52:54:00:$(echo "$hex" | sed 's/\(..\)\(..\)\(..\)/\1:\2:\3/')"
  case "$SP16_NET_KIND" in
   unix) backend="stream,id=sp16net,server=off,addr.type=unix,addr.path=$SP16_NET_TARGET" ;;
   mcast) backend="socket,id=sp16net,mcast=$SP16_NET_TARGET" ;;
   *) echo "qemu-net: unknown SP16_NET_KIND" >&2; exit 2 ;;
  esac
  echo "qemu-net: NIC $mac on $SP16_NET_KIND $SP16_NET_TARGET" >&2
  set -- "$@" -netdev "$backend" -device "virtio-net-device,netdev=sp16net,mac=$mac"
fi
exec "$real" "$@"
