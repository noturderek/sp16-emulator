#!/bin/sh
# Starts the build's QEMU. Stock runs have no network; `./sp16 run --net MODE` hands the choice over in the environment
# (SP16_NET_KIND, SP16_NET_TARGET) and this adds the guest's virtio NIC with a MAC of its own for this instance.
# SP16_NET_MODE (and an optional SP16_NET_IP) are also carried into the guest's /proc/cmdline, the only channel the
# guest init can read them from, so a bridged run can add a real-subnet address.
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
if [ -n "${SP16_NET_MODE:-}" ]; then
  # Append `sp16.net=<mode> [sp16.netip=<addr>]` to the -append argument's value. The arguments come from the build's
  # default-config.json (native.py arguments()); none of them contains a quote or backslash, so shell-quoting each and
  # re-splitting with eval keeps the spaces of the -append value intact.
  net_token="sp16.net=${SP16_NET_MODE}"
  [ -n "${SP16_NET_IP:-}" ] && net_token="$net_token sp16.netip=${SP16_NET_IP}"
  rebuilt=
  want_next=0
  for arg do
    if [ "$arg" = "-append" ]; then
      rebuilt="$rebuilt -append"
      want_next=1
    elif [ "$want_next" = 1 ]; then
      rebuilt="$rebuilt \"$arg $net_token\""
      want_next=0
    else
      rebuilt="$rebuilt \"$arg\""
    fi
  done
  eval "set -- $rebuilt"
fi
exec "$real" "$@"
