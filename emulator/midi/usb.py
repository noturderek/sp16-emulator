"""Bridge the emulator's USB MIDI byte stream to separate CoreMIDI endpoints.

The guest's ``g_midi`` sequencer peer uses this stream without DIN byte pacing.
These are virtual CoreMIDI endpoints; they do not create a physical USB device.
As on DIN, the app's routing preferences (see midi.routing) gate each direction through this port's own rows.
"""
import argparse
from contextlib import suppress
from pathlib import Path
from queue import Empty, Full, Queue
import socket
import sys
import threading
import time

from midi.din import Messages, connect, session_socket
from midi.routing import Routing, config_argument
from midi_bridge.midi import Port, hex_line


def signal_stop(pending, timeout=1.0):
    """Put the shutdown sentinel on the bounded input queue, making room for it if the queue is full.

    The queue drops a message when it is full, which is right for MIDI bytes that the link no longer needs and wrong
    for the sentinel: a lost sentinel leaves ``send_input`` blocked in ``pending.get()`` for good, so ``join`` on the
    input thread expires with that thread still alive. Once the link is closing no producer can add anything, so
    discarding the oldest queued message cannot race; returns False only if the queue stayed full for ``timeout``.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            pending.put_nowait(None)
            return True
        except Full:
            try:
                pending.get_nowait()
            except Empty:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)   # back off instead of spinning: the queue is being consumed (or refilled) elsewhere


def bridge(path, build_dir=None, name="SP-16 USB", routing=None):
    sock = connect(path)
    routing = Routing() if routing is None else routing
    pending = Queue(maxsize=64)
    closed = threading.Event()
    dropped = 0

    def received(data):
        nonlocal dropped
        # Host -> guest: our destination row's Out and Send clock decide what reaches the guest.
        to_guest = routing.route(name, is_source=False)
        data = bytes(byte for byte in data if to_guest.carries_to_guest(byte))
        if not data or closed.is_set():
            return
        try:
            pending.put_nowait(data)
        except Full:
            dropped += 1
            if dropped & (dropped - 1) == 0:
                print(f"USB MIDI input messages dropped={dropped}", file=sys.stderr, flush=True)

    def send_input():
        while not closed.is_set():
            data = pending.get()
            if data is None:
                return
            try:
                sock.sendall(data)
            except OSError:
                closed.set()
                return

    port = None
    worker = threading.Thread(target=send_input, name="usb-midi-input", daemon=True)
    worker.start()
    try:
        port = Port(["virtual", name], build_dir=build_dir, on_raw=received, queue_messages=False)
        print(f"USB MIDI bridge ready: {name}", flush=True)
        messages = Messages()
        while True:
            data = sock.recv(4096)
            if not data:
                break
            for message in messages.feed(data):
                # Guest -> host: our source row's In and Receive clock decide what leaves the guest.
                if not routing.route(name, is_source=True).carries_from_guest(message):
                    continue
                port.process.stdin.write("out " + hex_line(message) + "\n")
                port.process.stdin.flush()
    finally:
        closed.set()
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        if not signal_stop(pending):
            print("USB MIDI: the input thread did not see the shutdown sentinel within 1 s",
                  file=sys.stderr, flush=True)
        worker.join(timeout=1)
        if worker.is_alive():
            print("USB MIDI: the input thread is still alive after shutdown", file=sys.stderr, flush=True)
        if port is not None:
            port.close()
        sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--socket", type=Path)
    source.add_argument("--wait-for-session", type=Path)
    parser.add_argument("--name", default="SP-16 USB")
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--config", type=config_argument, help="the app's midi-routing.json")
    args = parser.parse_args(argv)
    path = args.socket or session_socket(args.wait_for_session, args.since, suffix=".usb-midi")
    bridge(path, args.build_dir, args.name, Routing(args.config))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, TimeoutError) as error:
        print(f"usb-midi: {error}", file=sys.stderr)
        sys.exit(1)
