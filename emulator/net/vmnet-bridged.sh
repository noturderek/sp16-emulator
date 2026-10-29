#!/bin/sh
# Start the root socket_vmnet daemon that `./sp16 run --net bridged:IF` needs, for one interface.
#
#   emulator/net/vmnet-bridged.sh en8        # asks for your password (sudo), runs until Ctrl-C
#
# `networksetup -listallhardwareports` names the interface (a USB Ethernet adapter is often en5..en9). Install the
# daemon once with `brew install socket_vmnet`. The emulator connects to the socket this prints; stop the daemon
# with Ctrl-C when you are done.
set -eu
if [ $# -ne 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  echo "usage: $0 INTERFACE   (for example en8; see networksetup -listallhardwareports)"
  [ $# -eq 1 ] && exit 0 || exit 2
fi
iface=$1
case "$iface" in *[!a-z0-9]*|'') echo "$0: not an interface name: $iface" >&2; exit 2 ;; esac
for prefix in /opt/homebrew /usr/local; do
  if [ -x "$prefix/opt/socket_vmnet/bin/socket_vmnet" ]; then
    daemon=$prefix/opt/socket_vmnet/bin/socket_vmnet
    sockdir=$prefix/var/run
    break
  fi
done
if [ -z "${daemon:-}" ]; then
  echo "$0: socket_vmnet is not installed: brew install socket_vmnet" >&2
  exit 1
fi
if ! ifconfig "$iface" >/dev/null 2>&1; then
  echo "$0: no interface $iface on this Mac (networksetup -listallhardwareports lists them)" >&2
  exit 1
fi
if ! ifconfig "$iface" | grep -q 'status: active'; then
  echo "$0: warning: $iface has no link yet (cable, adapter or the other device)" >&2
fi
socket=$sockdir/socket_vmnet.bridged.$iface
echo "socket_vmnet bridged to $iface on $socket; now run: ./sp16 run --name NAME --net bridged:$iface"
exec sudo "$daemon" --vmnet-mode=bridged --vmnet-interface="$iface" "$socket"
