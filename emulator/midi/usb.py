"""Bridge the emulator's USB MIDI byte stream to separate CoreMIDI endpoints.

The guest's ``g_midi`` sequencer peer uses this stream without DIN byte pacing.
These are virtual CoreMIDI endpoints; they do not create a physical USB device.
"""
import argparse
from contextlib import suppress
from pathlib import Path
from queue import Full, Queue
import socket
import sys
import threading

from midi.din import Messages, connect, session_socket
from midi_bridge.midi import Port, hex_line


def bridge(path, build_dir=None, name="SP-16 USB"):
    sock = connect(path)
    pending = Queue(maxsize=64)
    closed = threading.Event()
    dropped = 0

    def received(data):
        nonlocal dropped
        if closed.is_set():
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
                port.process.stdin.write("out " + hex_line(message) + "\n")
                port.process.stdin.flush()
    finally:
        closed.set()
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with suppress(Full):
            pending.put_nowait(None)
        worker.join(timeout=1)
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
    args = parser.parse_args(argv)
    path = args.socket or session_socket(args.wait_for_session, args.since, suffix=".usb-midi")
    bridge(path, args.build_dir, args.name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, TimeoutError) as error:
        print(f"usb-midi: {error}", file=sys.stderr)
        sys.exit(1)
