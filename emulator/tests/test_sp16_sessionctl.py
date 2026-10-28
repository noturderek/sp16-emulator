"""The research control socket client: a call that gets no usable reply ends in one clear error.

A heavily loaded host can take more than ten seconds to answer a call; the client used to give up after a hard-coded
10 s with a traceback ending inside `readline`, which read like the app had hung up. The tests serve the socket from a
thread in this process (a fake app that answers, hangs up, stays silent or answers garbage); no emulator runs.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import sessionctl


REPLIES = {"answer": b'{"ok": true}\n', "garbage": b"not json\n", "bad-utf8": b'{"ok": "\xff\xfe"}\n',
           "null": b"null\n", "array": b"[1, 2]\n", "refuse": b'{"error": "unknown op"}\n'}


class FakeApp:
    """A one-client control socket: `behaviour` is "hang-up", "silent", "trickle" (a reply in three parts 0.2 s apart) or
    a key of REPLIES."""

    def __init__(self, folder, behaviour):
        self.path = str(folder / "c.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.behaviour = behaviour
        self.done = threading.Event()
        self.received = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            client, _ = self.server.accept()
        except OSError:                         # closed before a client came: the test is over
            return
        with client:
            reader = client.makefile("r")
            for line in iter(reader.readline, ""):
                self.received.append(json.loads(line))
                if self.behaviour in REPLIES:
                    client.sendall(REPLIES[self.behaviour])
                elif self.behaviour == "trickle":
                    for part in (b'{"ok"', b": tr", b"ue}\n"):
                        client.sendall(part)
                        if self.done.wait(0.2):
                            return
                elif self.behaviour == "hang-up":
                    break
                else:
                    self.done.wait(10)          # silent: never answers while the test runs
                    break

    def close(self):
        self.done.set()
        try:
            self.server.shutdown(socket.SHUT_RDWR)  # wakes a blocked accept() on this platform's sockets
        except OSError:
            pass
        self.server.close()
        self.thread.join(5)


class SessionctlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sctl-", dir="/tmp"))   # a short path: AF_UNIX allows 104 bytes
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def runtime(self, app, pid=None):
        (self.tmp / "app.pid").write_text(f"{pid if pid is not None else os.getpid()}\n")
        path = self.tmp / "runtime.json"
        path.write_text(json.dumps({"control_socket": app.path, "session": str(self.tmp)}))
        return path

    def app(self, behaviour):
        app = FakeApp(self.tmp, behaviour)
        self.addCleanup(app.close)
        return app

    def session(self, runtime, **kw):
        session = sessionctl.Session(runtime, **kw)
        self.addCleanup(session.close)
        return session

    def test_a_reply_is_returned(self):
        session = self.session(self.runtime(self.app("answer")))
        self.assertEqual(session.metrics(), {"ok": True})

    def test_the_default_timeout_is_a_minute_and_can_be_set(self):
        runtime = self.runtime(self.app("answer"))
        with mock.patch.dict(os.environ, {"SP16_CONTROL_TIMEOUT": ""}):
            self.assertEqual(self.session(runtime).socket.gettimeout(), 60.0)
        with mock.patch.dict(os.environ, {"SP16_CONTROL_TIMEOUT": "120"}):
            self.assertEqual(sessionctl.reply_timeout(), 120.0)
        self.assertEqual(sessionctl.reply_timeout(7), 7.0)

    def test_an_app_that_hangs_up_is_a_clear_error(self):
        session = self.session(self.runtime(self.app("hang-up")))
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.snapshot("obs003")
        message = str(caught.exception)
        self.assertIn("the app closed the control socket during snapshot", message)
        self.assertIn(f"app pid {os.getpid()} running", message)

    def test_no_reply_in_time_is_a_clear_error_and_closes_the_connection(self):
        session = self.session(self.runtime(self.app("silent")), timeout=0.3)
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.event("home", "press", 100)
        self.assertIn("no reply to event within 0.3 s", str(caught.exception))
        self.assertEqual(session.socket.fileno(), -1)            # a late reply cannot answer the next call

    def test_the_error_says_when_the_app_is_gone(self):
        gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        session = self.session(self.runtime(self.app("hang-up"), pid=int(gone.stdout)))
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.metrics()
        self.assertIn(f"app pid {int(gone.stdout)} is not running", str(caught.exception))

    def test_an_unreadable_reply_is_a_clear_error(self):
        session = self.session(self.runtime(self.app("garbage")))
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.metrics()
        self.assertIn("unreadable reply to metrics after ", str(caught.exception))
        self.assertIn("'not json", str(caught.exception))
        self.assertIn(f"app pid {os.getpid()} running", str(caught.exception))        # review finding 1

    def test_a_reply_that_is_not_a_json_object_is_a_clear_error(self):
        """Review finding 2: invalid UTF-8 and JSON null raised uncaught exceptions; a JSON array came back as success."""
        for behaviour, shown in (("bad-utf8", "not UTF-8"), ("null", "not a JSON object"), ("array", "not a JSON object")):
            with self.subTest(behaviour=behaviour):
                folder = self.tmp / behaviour
                folder.mkdir()
                app = FakeApp(folder, behaviour)
                self.addCleanup(app.close)
                (folder / "app.pid").write_text(f"{os.getpid()}\n")
                runtime = folder / "runtime.json"
                runtime.write_text(json.dumps({"control_socket": app.path, "session": str(folder)}))
                with self.assertRaises(sessionctl.ControlError) as caught:
                    self.session(runtime).metrics()
                self.assertIn(f"unreadable reply to metrics after ", str(caught.exception))
                self.assertIn(shown, str(caught.exception))

    def test_the_timeout_bounds_the_whole_reply(self):
        """Review finding 3: the timeout bounded each socket read, so a reply in fragments could take far longer."""
        session = self.session(self.runtime(self.app("trickle")), timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(sessionctl.ControlError) as caught:
            session.metrics()
        self.assertIn("no reply to metrics within 0.3 s", str(caught.exception))
        self.assertLess(time.monotonic() - started, 0.39)

    def test_a_reply_in_fragments_within_the_timeout_is_read(self):
        session = self.session(self.runtime(self.app("trickle")), timeout=2)
        self.assertEqual(session.metrics(), {"ok": True})

    def test_the_fake_app_closes_without_a_thread_exception(self):
        """Review finding 4: closing the fake app interrupted accept() and printed a thread exception in ./sp16 test."""
        seen = []
        with mock.patch.object(threading, "excepthook", lambda args: seen.append(args.exc_type)):
            app = FakeApp(self.tmp, "answer")
            app.close()
        self.assertEqual(seen, [])
        self.assertFalse(app.thread.is_alive())

    def test_an_app_error_reply_is_one_line_on_the_command_line(self):
        runtime = self.runtime(self.app("refuse"))
        result = subprocess.run([sys.executable, str(TOOLS / "sessionctl.py"), "--runtime", str(runtime), "metrics"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "sessionctl: the app refused metrics: unknown op\n")

    def test_a_socket_that_is_gone_is_a_clear_error(self):
        app = self.app("answer")
        runtime = self.runtime(app)
        app.close()
        Path(app.path).unlink()
        with self.assertRaises(sessionctl.ControlError) as caught:
            sessionctl.Session(runtime)
        self.assertIn("cannot connect to the control socket", str(caught.exception))

    def test_the_command_line_prints_one_line_and_exits_1(self):
        runtime = self.runtime(self.app("silent"))
        result = subprocess.run([sys.executable, str(TOOLS / "sessionctl.py"), "--runtime", str(runtime), "--timeout", "0.3",
                                 "snapshot", "obs003"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.count("\n"), 1, result.stderr)
        self.assertTrue(result.stderr.startswith("sessionctl: no reply to snapshot within 0.3 s"), result.stderr)

    def tx_line(self, standby):
        frame = bytearray(128)
        frame[0], frame[0x77], frame[0x67] = 2, 0xaa, 1 if standby else 0
        return json.dumps({"t": 1.0, "hex": "000001" + frame.hex()}) + "\n"

    def test_power_presses_and_releases_the_host_control(self):
        app = self.app("answer")
        self.session(self.runtime(app)).power(0.01)
        power = sessionctl.CONTROL_IDS["power"]
        self.assertEqual([(c["id"], c["action"]) for c in app.received], [(power, 1), (power, 0)])

    def test_the_tx_standby_flag_is_read_from_the_newest_frame_logged_afterwards(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        log = self.tmp / sessionctl.TX_LOG
        log.write_text(self.tx_line(True))              # before the window: ignored
        self.assertIsNone(session.tx_standby(0.05))
        def append():
            with open(log, "a") as handle:
                handle.write(self.tx_line(False) + self.tx_line(True))
        threading.Timer(0.05, append).start()
        self.assertTrue(session.tx_standby(0.3))
        self.assertEqual(app.received[0], {"op": "txlog", "on": 1})

    def test_pressure_holds_a_raw_pad_pressure_through_the_pressure_control(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        session.pressure("pad3", 300)
        session.pressure(16, 0)
        self.assertEqual([(c["id"], c["action"], c["value"]) for c in app.received],
                         [(sessionctl.CONTROL_IDS["pressure3"], 3, 300), (sessionctl.CONTROL_IDS["pressure16"], 3, 0)])
        with self.assertRaises(ValueError):
            session.pressure(17, 100)

    def test_calibrate_holds_the_pad_presses_wake_up_once_per_pass_and_lets_go(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        with mock.patch.object(sessionctl.time, "sleep"):
            session.calibrate(2, 300)
        pressure, wake_up = sessionctl.CONTROL_IDS["pressure2"], sessionctl.CONTROL_IDS["usb_stop"]
        self.assertEqual([(c["id"], c["action"], c["value"]) for c in app.received],
                         [(pressure, 3, 300)] + [(wake_up, 1, 100), (wake_up, 0, 0)] * 3 + [(pressure, 3, 0)])

    def test_calibrate_lets_go_of_the_pad_when_a_pass_fails(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        with mock.patch.object(sessionctl.time, "sleep"), \
                mock.patch.object(session, "press", side_effect=RuntimeError("the app hung up")):
            with self.assertRaises(RuntimeError):
                session.calibrate("pad7", 120)
        self.assertEqual([(c["id"], c["value"]) for c in app.received],
                         [(sessionctl.CONTROL_IDS["pressure7"], 120), (sessionctl.CONTROL_IDS["pressure7"], 0)])

    def test_calibrate_on_the_command_line_names_the_page_it_needs(self):
        session = mock.Mock()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = sessionctl.run_command(session, "calibrate", ["3"])
        session.calibrate.assert_called_once_with("3", sessionctl.CALIBRATION_RAW)
        self.assertEqual(result, {"ok": True, "pad": "3", "raw": sessionctl.CALIBRATION_RAW, "passes": 3})
        self.assertIn("test page 3", stderr.getvalue())
        self.assertIn("48..504", stderr.getvalue())

    def test_standby_on_the_command_line_needs_yes(self):
        runtime = self.runtime(self.app("answer"))
        result = subprocess.run([sys.executable, str(TOOLS / "sessionctl.py"), "--runtime", str(runtime), "standby"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("standby needs the argument yes", result.stderr)

    def test_fault_sends_on_and_off(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        self.assertEqual(session.fault(True), {"ok": True})
        self.assertEqual(session.fault(False), {"ok": True})
        self.assertEqual(app.received, [{"op": "fault", "on": True}, {"op": "fault", "on": False}])

    def test_fault_on_the_command_line_defaults_to_on_and_accepts_off(self):
        session = mock.Mock()
        sessionctl.run_command(session, "fault", [])
        sessionctl.run_command(session, "fault", ["off"])
        self.assertEqual([c.args for c in session.fault.call_args_list], [(True,), (False,)])

    def test_monitor_sends_the_jack_name(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        self.assertEqual(session.monitor("PHONES"), {"ok": True})
        self.assertEqual(app.received, [{"op": "monitor", "name": "PHONES"}])

    def test_monitor_on_the_command_line_refuses_an_unknown_jack(self):
        runtime = self.runtime(self.app("answer"))
        result = subprocess.run([sys.executable, str(TOOLS / "sessionctl.py"), "--runtime", str(runtime), "monitor", "X"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("monitor needs MAIN", result.stderr)

    def test_usb_attach_with_a_path_passes_the_path(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        self.assertEqual(session.usb("attach", "/tmp/stick.img"), {"ok": True})
        self.assertEqual(app.received, [{"op": "usb", "action": "attach", "path": "/tmp/stick.img"}])

    def test_usb_without_a_path_omits_the_path_field(self):
        app = self.app("answer")
        session = self.session(self.runtime(app))
        self.assertEqual(session.usb("eject"), {"ok": True})
        self.assertEqual(app.received, [{"op": "usb", "action": "eject"}])


if __name__ == "__main__":
    unittest.main()
