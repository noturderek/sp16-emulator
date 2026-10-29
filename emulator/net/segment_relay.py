#!/usr/bin/env python3
"""Host-side frame relay for `./sp16 run --net mcast` (no root needed).

QEMU's `socket,mcast=` backend binds its UDP socket to the multicast group address; on macOS a socket bound that way
sends successfully but its datagrams are never delivered to another local listener that joined the group. So the guest
cannot reach `emulator/net/segment_watch.py` or another emulator on the same `--net mcast` segment.

This relay replaces that backend. QEMU attaches to it with the `stream` netdev (a unix socket, the same framing
socket_vmnet speaks: each packet is a 4-byte big-endian length then the raw Ethernet frame). The relay joins the
multicast group with a socket bound to the wildcard address (the setup that *does* deliver on macOS) and forwards:

* QEMU -> group: every frame the guest transmits is sent to `group:port`, where `segment_watch.py` and the other
  emulators' relays receive it;
* group -> QEMU: frames from other relays (or any host process) are handed back to the guest.

A frame sent by this relay loops back to its own receive socket (macOS multicast loopback); the relay drops the echo
by matching the frame's source MAC against its own guest's MAC, so a guest never receives its own traffic back.

  segment_relay.py --socket PATH --target GROUP:PORT --mac 52:54:00:aa:bb:cc [--iface ADDR] [--idle-timeout SECONDS]

The relay exits (and unlinks its socket) when QEMU disconnects, or after `--idle-timeout` if no QEMU ever connects.
Stdlib only.
"""
import argparse
import collections
import errno
import os
import select
import socket
import struct
import sys
import time

OUTBOX_LIMIT = 1 << 20  # bytes of frames the relay holds for a QEMU that has stopped reading


def parse_target(target):
    """`GROUP:PORT` -> (group, port). Refuses anything that is not a single well-formed `host:port` pair."""
    group, sep, port = target.rpartition(":")
    if not sep or not group or not port:
        raise ValueError(f"target must be GROUP:PORT, got {target!r}")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"target port must be 1..65535, got {port!r}")
    return group, int(port)


def mac_key(mac):
    """`aa:bb:cc:dd:ee:ff` -> `aabbccddeeff` (lowercase), '' for an empty/absent MAC (no loop filtering)."""
    return "".join(mac.lower().split(":"))


def join_group(port, group, iface=None):
    """A UDP socket bound to the wildcard address and joined to `group` on `port` (SO_REUSEADDR/REUSEPORT so several
    relays and `segment_watch.py` can share the port on one host)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind(("", port))
    membership = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    if iface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    return sock


def flush_stream(sock, buffer):
    """Send `buffer` to the nonblocking stream `sock`; return the part not yet sent.

    Backpressure (BlockingIOError / InterruptedError) is not an error: the caller keeps the remainder and retries when
    the socket is writable again. A partial send keeps only the unsent tail."""
    try:
        sent = sock.send(buffer)
    except (BlockingIOError, InterruptedError):
        return buffer
    return buffer[sent:] if sent > 0 else buffer


class Outbox:
    """A bounded FIFO of framed records waiting to be written to the QEMU stream.

    QEMU's stream backend can stop reading (a paused guest, a slow reader); the relay must not buffer without bound.
    Records are held in a deque under a byte budget `limit`; when a push would exceed it, the oldest whole records are
    dropped first (never a partial record) and counted so the relay can report the overflow once. Byte accounting
    stays exact across partial sends because the unsent tail of a record is kept in place at the head.
    """

    def __init__(self, limit=OUTBOX_LIMIT):
        if limit <= 0:
            raise ValueError("outbox limit must be positive")
        self.limit = limit
        self.queue = collections.deque()
        self.bytes = 0
        self.dropped_frames = 0
        self.dropped_bytes = 0

    def push(self, record: bytes) -> None:
        self.queue.append(record)
        self.bytes += len(record)
        while self.bytes > self.limit and self.queue:
            head = self.queue.popleft()
            self.bytes -= len(head)
            self.dropped_frames += 1
            self.dropped_bytes += len(head)

    def flush(self, sock) -> None:
        """Write records oldest-first to the nonblocking `sock`, keeping the unsent tail of the record in progress."""
        while self.queue:
            head = self.queue[0]
            try:
                sent = sock.send(head)
            except (BlockingIOError, InterruptedError):
                return
            if sent <= 0:
                return
            self.bytes -= sent
            if sent < len(head):
                self.queue[0] = head[sent:]
                return
            self.queue.popleft()

    def __bool__(self):
        return bool(self.queue)


def relay(sock_path, target, own_mac="", iface=None, idle_timeout=30.0, log=sys.stderr):
    """Run until QEMU disconnects (returns 0) or no QEMU connects in `idle_timeout` (returns 1)."""
    group, port = parse_target(target)
    seg = join_group(port, group, iface)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    listener.bind(sock_path)
    listener.listen(1)
    listener.settimeout(idle_timeout)
    own = mac_key(own_mac)
    print(f"segment-relay: {sock_path} -> {group}:{port} mac {own or '-'}", file=log, flush=True)
    qemu = None
    transient = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ECONNREFUSED, errno.EHOSTDOWN, errno.ENETDOWN}
    warned = set()
    stats = {"to_segment": 0, "to_guest": 0, "recv_err": 0, "send_err": 0, "last": time.monotonic()}

    def note(kind, err):
        # A UDP socket on macOS surfaces an asynchronous ICMP error (e.g. "No route to host" after a multicast
        # send on a link with no forwarder) on the next recvfrom; log it once and keep relaying rather than die.
        stats["recv_err" if kind == "recv" else "send_err"] += 1
        key = (kind, err.errno)
        if key not in warned:
            warned.add(key)
            print(f"segment-relay: {kind} {err} (continuing)", file=log, flush=True)

    def heartbeat():
        now = time.monotonic()
        if now - stats["last"] >= 30.0:
            stats["last"] = now
            print(f"segment-relay: to_segment={stats['to_segment']} to_guest={stats['to_guest']} "
                  f"recv_err={stats['recv_err']} send_err={stats['send_err']}", file=log, flush=True)

    try:
        try:
            qemu, _ = listener.accept()
        except socket.timeout:
            return 1
        qemu.setblocking(False)
        seg.setblocking(False)
        pending = b""
        outbox = Outbox()
        dropped_reported = False
        while True:
            writable = [qemu] if outbox else []
            readable, ready_write, _ = select.select([seg, qemu], writable, [], 1.0)
            for ready in readable:
                if ready is seg:
                    try:
                        frame, _ = seg.recvfrom(65535)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError as err:
                        if err.errno in transient:
                            note("recv", err)
                            continue
                        raise
                    if not frame or len(frame) < 14:
                        continue
                    if own and frame[6:12].hex() == own:
                        continue  # our own send echoed back: never hand it to our guest
                    stats["to_guest"] += 1
                    outbox.push(struct.pack(">I", len(frame)) + frame)
                    if outbox.dropped_frames and not dropped_reported:
                        dropped_reported = True
                        print(f"segment-relay: QEMU stopped reading; dropping the oldest queued frames "
                              f"({outbox.dropped_frames} frames, {outbox.dropped_bytes} bytes so far)",
                              file=log, flush=True)
                else:
                    try:
                        chunk = qemu.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not chunk:
                        return 0  # QEMU closed: the session ended
                    pending += chunk
                    while len(pending) >= 4:
                        length = struct.unpack(">I", pending[:4])[0]
                        if len(pending) < 4 + length:
                            break
                        frame = pending[4:4 + length]
                        pending = pending[4 + length:]
                        try:
                            seg.sendto(frame, (group, port))
                        except OSError as err:
                            if err.errno in transient:
                                note("send", err)
                                continue
                            raise
                        stats["to_segment"] += 1
            if ready_write and outbox:
                # The guest has not kept up: only write when the socket is writable, keep the rest for the next
                # writable turn instead of letting BlockingIOError end the relay.
                try:
                    outbox.flush(qemu)
                except OSError as err:
                    if err.errno in transient:
                        note("send", err)
                    else:
                        raise
            heartbeat()
    finally:
        if qemu is not None:
            qemu.close()
        listener.close()
        seg.close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", required=True, help="unix socket path QEMU connects to (the relay listens here)")
    parser.add_argument("--target", required=True, help="multicast GROUP:PORT the segment uses, e.g. 230.0.0.1:34567")
    parser.add_argument("--mac", default="", help="this guest's MAC; frames with this source are not echoed back")
    parser.add_argument("--iface", help="IPv4 address of the interface to send the multicast on (default: the route)")
    parser.add_argument("--idle-timeout", type=float, default=30.0, help="exit after this many seconds without QEMU")
    args = parser.parse_args(argv)
    return relay(args.socket, args.target, args.mac, args.iface, args.idle_timeout)


if __name__ == "__main__":
    sys.exit(main())
