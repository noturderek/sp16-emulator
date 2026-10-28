"""The MIDI routing preferences gate what each bridge port carries (pt-i44.37).

The app's routing window writes midi-routing.json; the DIN and USB bridges read it (midi/routing.py). Every test
below drives one toggle, closed and open, first at the level of the route and then through a running bridge: closed
carries nothing, open carries bytes. The bridges run against a socket pair and a fake CoreMIDI helper, so none of
this needs CoreMIDI or a device.
"""
import argparse
import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from midi import din, usb  # noqa: E402
from midi.routing import CLOCK, Route, Routing, config_argument  # noqa: E402

NAME = "SP-16 DIN [test]"
NOTE = b"\x90\x3c\x64"
CLOCK_BYTE = bytes([CLOCK])


def preferences(folder, ports, **extra):
    """Write a midi-routing.json holding `ports` into `folder` and return its path."""
    path = Path(folder) / "midi-routing.json"
    path.write_text(json.dumps({"clockSource": "internal", "bpm": 120.0, "ports": ports, **extra}))
    return path


def port_entry(name=NAME, is_source=True, **toggles):
    """One ports[] entry the way the window writes it: every toggle on unless the test turns it off."""
    entry = {"name": name, "uniqueID": 1193046, "isSource": is_source, "inEnabled": True, "outEnabled": True,
             "sendClock": True, "receiveClock": True}
    entry.update(toggles)
    return entry


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def route_for(ports, is_source=True, name=NAME):
    """The route a bridge would use for its own row, loaded from a preferences file written from `ports`."""
    with tempfile.TemporaryDirectory() as folder:
        return Routing(preferences(folder, ports)).route(name, is_source)


class RouteTests(unittest.TestCase):
    """The route itself: an absent port carries everything, a closed direction carries nothing."""

    def test_a_port_absent_from_the_preferences_carries_everything(self):
        routing = Routing(Path(tempfile.gettempdir()) / "sp16-no-such-midi-routing.json")
        for is_source in (True, False):
            route = routing.route(NAME, is_source)
            self.assertEqual(route, Route())  # a bridge started before the window was ever opened
            self.assertTrue(route.carries_from_guest(CLOCK_BYTE))
            self.assertTrue(route.carries_from_guest(NOTE))
            self.assertTrue(route.carries_to_guest(CLOCK))
            self.assertTrue(route.carries_to_guest(0x90))

    def test_in_off_carries_nothing_from_the_guest(self):
        off = route_for([port_entry(inEnabled=False)])
        self.assertFalse(off.carries_from_guest(NOTE))
        self.assertFalse(off.carries_from_guest(CLOCK_BYTE))
        on = route_for([port_entry(inEnabled=True)])
        self.assertTrue(on.carries_from_guest(NOTE))
        self.assertTrue(on.carries_from_guest(CLOCK_BYTE))

    def test_receive_clock_off_carries_everything_but_the_clock_from_the_guest(self):
        off = route_for([port_entry(receiveClock=False)])
        self.assertFalse(off.carries_from_guest(CLOCK_BYTE))
        self.assertTrue(off.carries_from_guest(NOTE))
        on = route_for([port_entry(receiveClock=True)])
        self.assertTrue(on.carries_from_guest(CLOCK_BYTE))

    def test_out_off_carries_nothing_to_the_guest(self):
        off = route_for([port_entry(is_source=False, outEnabled=False)], is_source=False)
        self.assertFalse(off.carries_to_guest(0x90))
        self.assertFalse(off.carries_to_guest(CLOCK))
        on = route_for([port_entry(is_source=False, outEnabled=True)], is_source=False)
        self.assertTrue(on.carries_to_guest(0x90))
        self.assertTrue(on.carries_to_guest(CLOCK))

    def test_send_clock_off_carries_everything_but_the_clock_to_the_guest(self):
        off = route_for([port_entry(is_source=False, sendClock=False)], is_source=False)
        self.assertFalse(off.carries_to_guest(CLOCK))
        self.assertTrue(off.carries_to_guest(0x90))
        on = route_for([port_entry(is_source=False, sendClock=True)], is_source=False)
        self.assertTrue(on.carries_to_guest(CLOCK))

    def test_the_source_and_the_destination_row_are_separate(self):
        # Closing In on our source row says nothing about Out on our destination row: each direction is its own row.
        with tempfile.TemporaryDirectory() as folder:
            routing = Routing(preferences(folder, [port_entry(inEnabled=False), port_entry(is_source=False)]))
            self.assertFalse(routing.route(NAME, True).carries_from_guest(NOTE))
            self.assertTrue(routing.route(NAME, False).carries_to_guest(0x90))


class MalformedPreferencesTests(unittest.TestCase):
    def test_a_file_that_is_not_the_expected_document_carries_everything(self):
        documents = ["", "not json", "[]", '{"ports": "one"}', '{"ports": [1, null, "x", {}]}',
                     '{"ports": [{"isSource": false, "outEnabled": false}]}',  # no name: cannot be matched
                     '{"ports": [{"name": "", "outEnabled": false}]}']
        for document in documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "midi-routing.json"
                path.write_text(document)
                self.assertEqual(Routing(path).route(NAME, True), Route())

    def test_an_unreadable_file_keeps_the_routes_it_already_had(self):
        with tempfile.TemporaryDirectory() as folder:
            path = preferences(folder, [port_entry(inEnabled=False)])
            routing = Routing(path, refresh_seconds=0)
            self.assertFalse(routing.route(NAME, True).carries_from_guest(NOTE))
            path.write_text("half a docu")
            self.assertFalse(routing.route(NAME, True).carries_from_guest(NOTE))
            self.assertIsNotNone(routing.problem)
            path.write_text(json.dumps({"ports": [port_entry(inEnabled=True)]}))
            self.assertTrue(routing.route(NAME, True).carries_from_guest(NOTE))
            self.assertIsNone(routing.problem)

    def test_a_toggle_that_is_not_a_json_true_fails_closed(self):
        # A hand-edited "true"/1 must not read as on: only a real JSON true opens a direction.
        route = route_for([port_entry(inEnabled="true", outEnabled=1)])
        self.assertFalse(route.carries_from_guest(NOTE))
        self.assertFalse(route.carries_to_guest(0x90))

    def test_the_arguments_are_range_checked(self):
        with self.assertRaises(ValueError):
            Routing(refresh_seconds=-1)
        routing = Routing(Path(tempfile.gettempdir()) / "sp16-no-such-midi-routing.json")
        with self.assertRaises(ValueError):
            routing.route("", True)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(argparse.ArgumentTypeError):
                config_argument(folder)  # a directory is never a preferences file
            self.assertEqual(config_argument(Path(folder) / "absent.json"), Path(folder) / "absent.json")


class FakePort:
    """The coremidi-io helper's text protocol without CoreMIDI: `out` lines are recorded, host input is injected."""

    last = None

    def __init__(self, args, build_dir=None, on_raw=None, queue_messages=True):
        self.args = args
        self.on_raw = on_raw
        self.out = []
        self.process = types.SimpleNamespace(stdin=self)
        FakePort.last = self

    def write(self, text):
        self.out.append(text)

    def flush(self):
        pass

    def close(self):
        pass

    def send_to_guest(self, data):
        """Deliver host input the way the helper's `in` lines do."""
        self.on_raw(data)


class BridgeHarness:
    """Run a bridge against a socket pair and the fake helper; `peer` is what the guest's socket would be."""

    def __init__(self, module, routing, name=NAME, timeout=5.0):
        self.module, self.routing, self.name, self.timeout = module, routing, name, timeout

    def __enter__(self):
        self.peer, guest = socket.socketpair()
        self.peer.settimeout(self.timeout)
        FakePort.last = None
        self.patches = [mock.patch.object(self.module, "Port", FakePort),
                        mock.patch.object(self.module, "connect", lambda path, timeout=60: guest)]
        for patch in self.patches:
            patch.start()
        self.errors = []
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()
        if not wait_for(lambda: FakePort.last is not None):
            raise RuntimeError("the bridge never created its CoreMIDI port")
        self.port = FakePort.last
        return self

    def run(self):
        try:
            self.module.bridge(None, name=self.name, routing=self.routing)
        except Exception as error:  # noqa: BLE001 - re-raised by __exit__ so a bridge crash fails the test
            self.errors.append(error)

    def guest_bytes(self, length=1, timeout=0.3):
        """Up to `length` bytes reaching the guest within `timeout`; b"" when the route carries nothing. The DIN path
        paces one byte per 320 us, so a message arrives in pieces and is collected here rather than in one recv."""
        deadline = time.monotonic() + timeout
        data = bytearray()
        try:
            while len(data) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.peer.settimeout(remaining)
                chunk = self.peer.recv(length - len(data))
                if not chunk:
                    break
                data.extend(chunk)
        except socket.timeout:
            pass
        finally:
            self.peer.settimeout(self.timeout)
        return bytes(data)

    def __exit__(self, *exc):
        self.peer.close()
        self.worker.join(self.timeout)
        for patch in reversed(self.patches):
            patch.stop()
        FakePort.last = None
        if self.errors:
            raise self.errors[0]
        return False


class BridgeRoutingTests(unittest.TestCase):
    """Each toggle through a running bridge, closed and open, on both the DIN and the USB route."""

    def open_bridge(self, module, folder, ports):
        return BridgeHarness(module, Routing(preferences(folder, ports), refresh_seconds=0))

    def test_the_in_toggle_gates_what_reaches_the_host(self):
        for module in (din, usb):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as folder:
                with self.open_bridge(module, folder, [port_entry(inEnabled=False)]) as open_bridge:
                    open_bridge.peer.sendall(NOTE + CLOCK_BYTE)
                    time.sleep(0.3)  # room for a frame that should never be written
                    self.assertEqual(open_bridge.port.out, [])
                with self.open_bridge(module, folder, [port_entry(inEnabled=True)]) as open_bridge:
                    open_bridge.peer.sendall(NOTE + CLOCK_BYTE)
                    self.assertTrue(wait_for(lambda: len(open_bridge.port.out) >= 2),
                                    f"nothing reached the host: {open_bridge.port.out}")
                    self.assertEqual(open_bridge.port.out, ["out 90 3c 64\n", "out f8\n"])

    def test_the_receive_clock_toggle_gates_only_the_clock_to_the_host(self):
        for module in (din, usb):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as folder:
                with self.open_bridge(module, folder, [port_entry(receiveClock=False)]) as open_bridge:
                    open_bridge.peer.sendall(CLOCK_BYTE)
                    time.sleep(0.3)
                    self.assertEqual(open_bridge.port.out, [])
                    open_bridge.peer.sendall(NOTE)
                    self.assertTrue(wait_for(lambda: open_bridge.port.out == ["out 90 3c 64\n"]),
                                    f"a channel message was swallowed: {open_bridge.port.out}")

    def test_the_out_toggle_gates_what_reaches_the_guest(self):
        for module in (din, usb):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as folder:
                with self.open_bridge(module, folder, [port_entry(is_source=False, outEnabled=False)]) as open_bridge:
                    open_bridge.port.send_to_guest(NOTE + CLOCK_BYTE)
                    self.assertEqual(open_bridge.guest_bytes(4), b"")
                with self.open_bridge(module, folder, [port_entry(is_source=False, outEnabled=True)]) as open_bridge:
                    open_bridge.port.send_to_guest(NOTE)
                    self.assertEqual(open_bridge.guest_bytes(3, timeout=self.guest_timeout), NOTE)

    def test_the_send_clock_toggle_gates_only_the_clock_to_the_guest(self):
        for module in (din, usb):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as folder:
                with self.open_bridge(module, folder,
                                      [port_entry(is_source=False, sendClock=False)]) as open_bridge:
                    open_bridge.port.send_to_guest(CLOCK_BYTE)
                    self.assertEqual(open_bridge.guest_bytes(), b"")
                    open_bridge.port.send_to_guest(NOTE)
                    self.assertEqual(open_bridge.guest_bytes(3, timeout=self.guest_timeout), NOTE)

    def test_a_toggle_takes_effect_without_restarting_the_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            path = preferences(folder, [port_entry(is_source=False, outEnabled=False)])
            with BridgeHarness(usb, Routing(path, refresh_seconds=0)) as open_bridge:
                open_bridge.port.send_to_guest(CLOCK_BYTE)
                self.assertEqual(open_bridge.guest_bytes(), b"")
                time.sleep(0.01)  # a distinct mtime, so the bridge re-reads the new content, not a cached file
                preferences(folder, [port_entry(is_source=False, outEnabled=True)])
                open_bridge.port.send_to_guest(CLOCK_BYTE)
                self.assertEqual(open_bridge.guest_bytes(timeout=self.guest_timeout), CLOCK_BYTE)

    guest_timeout = 3.0  # a DIN byte takes 320 us, so a few bytes need well under a second


if __name__ == "__main__":
    unittest.main()
