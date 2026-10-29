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
   mcast)
     # QEMU's socket,mcast backend binds its socket to the group address, which macOS never delivers to other
     # local listeners. A host-side relay (net/segment_relay.py, copied next to this wrapper by the build) joins
     # the group on a wildcard-bound socket instead, and QEMU attaches to it over a unix stream with the same
     # framing socket_vmnet uses. SP16_NET_NO_RELAY=1 leaves the relay out (an already-running relay on
     # SP16_NET_SOCKET); SP16_NET_SOCKET overrides the socket path.
     sock="${SP16_NET_SOCKET:-${TMPDIR:-/tmp}/sp16r-$$.sock}"
     backend="stream,id=sp16net,server=off,addr.type=unix,addr.path=$sock"
     if [ -z "${SP16_NET_NO_RELAY:-}" ]; then
       "${SP16_PYTHON:-python3}" "$(dirname "$0")/segment_relay.py" \
         --socket "$sock" --target "$SP16_NET_TARGET" --mac "$mac" &
       i=0
       while [ ! -S "$sock" ] && [ "$i" -lt 100 ]; do
         kill -0 "$!" 2>/dev/null || break
         sleep 0.02
         i=$((i + 1))
       done
       [ -S "$sock" ] || { echo "qemu-net: segment relay did not start (socket $sock)" >&2; exit 2; }
     fi
     ;;
   *) echo "qemu-net: unknown SP16_NET_KIND" >&2; exit 2 ;;
  esac
  echo "qemu-net: NIC $mac on $SP16_NET_KIND $SP16_NET_TARGET" >&2
  set -- "$@" -netdev "$backend" -device "virtio-net-device,netdev=sp16net,mac=$mac"
fi
if [ -n "${SP16_NET_MODE:-}" ]; then
  # Append `sp16.net=<mode> [sp16.netip=<addr>]` to the -append argument's value. The arguments come from the build's
  # default-config.json (native.py arguments()). Rotate the original argv to the end, replacing the value after
  # -append without shell re-evaluation; QEMU arguments may contain literal dollar signs or backticks.
  net_token="sp16.net=${SP16_NET_MODE}"
  [ -n "${SP16_NET_IP:-}" ] && net_token="$net_token sp16.netip=${SP16_NET_IP}"
  remaining=$#
  found_append=0
  while [ "$remaining" -gt 0 ]; do
    arg=$1
    shift
    remaining=$((remaining - 1))
    if [ "$arg" = "-append" ]; then
      [ "$remaining" -gt 0 ] || { echo 'qemu-net: -append needs a value' >&2; exit 2; }
      value=$1
      shift
      remaining=$((remaining - 1))
      set -- "$@" -append "$value $net_token"
      found_append=1
    else
      set -- "$@" "$arg"
    fi
  done
  [ "$found_append" = 1 ] || { echo 'qemu-net: SP16_NET_MODE requires -append' >&2; exit 2; }
fi
exec "$real" "$@"
