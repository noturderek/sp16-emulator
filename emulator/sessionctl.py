#!/usr/bin/env python3
"""Drive a research-enabled native SP-16 session through its owner-only control socket.

The socket exists only for sessions built with `native.py build --research`; its path
is in the session's runtime.json (`control_socket`). Events go through the same host
transport as the panel view, so results are correlated and counted identically.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time

from panel_map import CONTROL_IDS, decode_tx

ACTIONS = {"release": 0, "press": 1, "delta": 2, "value": 3}
# How long a call waits for the app's reply. A host under heavy load can take well over 10 s to answer (the app serves
# the socket on a background queue); SP16_CONTROL_TIMEOUT or --timeout overrides it.
DEFAULT_TIMEOUT = 60.0
TX_LOG = "tx-frames.jsonl"
# The power key puts the app in standby, which a real unit's M1 follows by cutting the power; the emulator has no M1,
# so the app stays in standby, the USB stick is unmounted and saves stop until the app is restarted.
STANDBY_WARNING = ("the app enters standby: the USB stick is unmounted and saves stop until the app is restarted "
                   "(the emulator has no M1 to wake it)")
# The factory test's pad calibration page (test page 3, a build made with --app-arg testmode): one press of the WAKE UP key
# (the USB STOP key) measures one pass, a 3 s spare period and a 1 s average, so about 4.5 s with the page's redraw;
# three passes make one calibration and write the pad's value if it is inside the valid range.
CALIBRATION_PASSES = 3
CALIBRATION_PASS_SECONDS = 7.0
CALIBRATION_RAW = 300           # inside the valid range of 48..504; the synthetic default calibration is 242
CALIBRATION_NOTE = ("test page 3 must be on the LCD (a build made with --app-arg testmode; HOME and BACK step the pages); "
                    "a pad's value is recorded only inside 48..504")


class ControlError(RuntimeError):
    """A call that got no usable reply: the app hung up, did not answer in time, or answered something unreadable. The
    message names the operation, how long it waited and whether the app process is still running."""


def reply_timeout(timeout=None):
    if timeout is not None:
        return float(timeout)
    return float(os.environ.get("SP16_CONTROL_TIMEOUT") or DEFAULT_TIMEOUT)


def adc_words(metrics):
    """Extract the guest's reported analog RX words from a metrics reply."""
    words = (metrics.get("guest") or {}).get("adc")
    if not isinstance(words, dict):
        raise RuntimeError("guest has not reported ADC words yet (guest build predates the adc stat)")
    return words


class Session:
    def __init__(self, runtime_json, timeout=None):
        runtime = json.loads(Path(runtime_json).read_text())
        if not runtime.get("control_socket"):
            raise ValueError("session was not built with --research")
        self.dir = Path(runtime["session"])
        self.timeout = reply_timeout(timeout)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(self.timeout)
        try:
            self.socket.connect(runtime["control_socket"])
        except OSError as error:
            self.socket.close()
            raise ControlError(f"cannot connect to the control socket: {error} ({self.app_state()})") from None
        self.pending = b""                  # bytes after a reply's newline (the app answers one line per call, in order)

    def app_state(self):
        """Whether the app process of this session (app.pid) is running, and for how long: tells a slow app from a gone one."""
        pid_file = self.dir / "app.pid"
        try:
            pid = int(pid_file.read_text().strip())
            started = pid_file.stat().st_mtime
        except (OSError, ValueError):
            return "app pid unknown"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return f"app pid {pid} is not running"
        except PermissionError:
            pass
        return f"app pid {pid} running, up {time.time() - started:.0f} s"

    def read_line(self, deadline):
        """One reply line (bytes, newline excluded), b"" at EOF; the whole line within `deadline` (time.monotonic), not
        each read: a reply that arrives in pieces cannot take longer than the timeout."""
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(65536)
            if not chunk:
                return b""
            self.pending += chunk
        line, self.pending = self.pending.split(b"\n", 1)
        return line

    def call(self, **command):
        op = command.get("op", "?")
        started = time.monotonic()
        try:
            self.socket.settimeout(self.timeout)
            self.socket.sendall((json.dumps(command) + "\n").encode())
            line = self.read_line(started + self.timeout)
        except socket.timeout:
            self.close()                # a late reply would answer the next call: this connection is out of step
            raise ControlError(f"no reply to {op} within {self.timeout:g} s ({self.app_state()}); the app is slow or "
                               "stuck (a heavily loaded host?), the connection is closed") from None
        except OSError as error:
            raise ControlError(f"the control socket failed during {op} after {time.monotonic() - started:.1f} s: "
                               f"{error} ({self.app_state()})") from None
        if not line:
            raise ControlError(f"the app closed the control socket during {op} after {time.monotonic() - started:.1f} s "
                               f"(EOF, no reply; {self.app_state()})")

        def unreadable(why):
            return ControlError(f"unreadable reply to {op} after {time.monotonic() - started:.1f} s ({self.app_state()}): "
                                f"{why}: {line[:120]!r}")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            raise unreadable("not UTF-8") from None
        try:
            reply = json.loads(text)
        except ValueError:
            raise unreadable("not JSON") from None
        if not isinstance(reply, dict):
            raise unreadable("not a JSON object")
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply

    def close(self):
        self.socket.close()

    @staticmethod
    def control(name):
        return CONTROL_IDS[name] if name in CONTROL_IDS else int(name)

    def event(self, name, action, value=0):
        return self.call(op="event", id=self.control(name), action=ACTIONS[action], value=value)

    def press(self, name, seconds=0.15, value=100):
        self.event(name, "press", value)
        time.sleep(seconds)
        self.event(name, "release", 0)

    def turn(self, name, detents, gap=0.03):
        step = 1 if detents > 0 else -1
        for _ in range(abs(detents)):
            self.event(name, "delta", step)
            time.sleep(gap)

    def touch(self, x, y, action):
        return self.call(op="touch", x=x, y=y, action=action)

    def raw(self, offset, data):
        return self.call(op="raw", offset=offset, hex=bytes(data).hex())

    def period(self, ms):
        return self.call(op="period", ms=ms)

    def clear(self):
        return self.call(op="clear")

    def snapshot(self, name):
        self.call(op="snapshot", name=name)
        return self.dir / name

    def resize(self, width, height):
        return self.call(op="resize", width=width, height=height)

    def panelshot(self, name):
        self.call(op="panelshot", name=name)
        return self.dir / name

    def usb(self, action, path=None):
        """Virtual USB stick: "eject" (guest flushes and unmounts, then the app sees it removed; replies usb-busy
        and leaves the stick mounted if something still holds it) or "attach" (remount and announce again; with a
        `path`, hot-swap to a different image while QEMU runs)."""
        command = {"op": "usb", "action": action}
        if path is not None:
            command["path"] = path
        return self.call(**command)

    def usb_oc(self, on):
        return self.call(op="usb-oc", on=on)

    def pressure(self, pad, raw):
        """Hold a pad's raw pressure word (0..1023) until it is set to 0: the AD value the factory test's pad
        calibration page shows, not a velocity. `pad` is 1..16 or a name such as pad3."""
        number = int(str(pad).removeprefix("pad"))
        if not 1 <= number <= 16:
            raise ValueError(f"pad {pad!r} is not 1..16")
        return self.event(f"pressure{number}", "value", int(raw))

    def calibrate(self, pad, raw=CALIBRATION_RAW, passes=CALIBRATION_PASSES, pass_seconds=CALIBRATION_PASS_SECONDS):
        """The pad calibration walkthrough of the factory test: hold `pad` at `raw`, press WAKE UP (the USB STOP key)
        once per pass and wait out each pass, then let go. The pad is let go even if a call fails."""
        self.pressure(pad, raw)
        try:
            time.sleep(2.5)                             # the page redraws the held value before the first pass
            for _ in range(passes):
                self.press("usb_stop", 0.4)
                time.sleep(pass_seconds)
        finally:
            self.pressure(pad, 0)

    def power(self, seconds=0.3):
        """The POWER/STANDBY key (a host control, no place on the panel): press, hold, release. The release is what
        makes the app go to standby."""
        self.press("power", seconds)

    def tx_standby(self, window=1.0):
        """The TX standby flag (byte 0x67 bit 0) of the newest OEM frame logged in the next `window` seconds, or None
        when no frame arrived. Switches the TX log on."""
        self.call(op="txlog", on=1)
        path = self.dir / TX_LOG
        start = path.stat().st_size if path.exists() else 0
        time.sleep(window)
        if not path.exists():
            return None
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
        flag = None
        for line in data.splitlines():
            try:
                decoded = decode_tx(bytes.fromhex(json.loads(line)["hex"])[3:])
            except (ValueError, KeyError, TypeError):
                continue
            if decoded:
                flag = decoded["standby"]
        return flag

    def standby(self, wait=8.0):
        """Press POWER and wait for the TX standby flag to latch: {"before", "standby", "seconds"}."""
        before = self.tx_standby(0.5)
        started = time.monotonic()
        self.power()
        flag = self.tx_standby(0.5)
        while not flag and time.monotonic() - started < wait:
            flag = self.tx_standby(0.5)
        return {"before": before, "standby": bool(flag), "seconds": round(time.monotonic() - started, 1)}

    def fault(self, on):
        """Force the power-fault display (QRT1001 p24): dark panel, WAKE UP blinking red; off restores."""
        return self.call(op="fault", on=on)

    def monitor(self, name):
        """Choose which output jack pair the CoreAudio output plays (MAIN, PHONES, OUT3-4, OUT5-6, OUT7-8)."""
        return self.call(op="monitor", name=name)

    def metrics(self):
        return self.call(op="metrics")

    def app_restart(self):
        """Decoupled builds: restart only the OEM app (the developer overlay is re-applied first)."""
        return self.call(op="app-restart")

    def firmware(self):
        """Decoupled builds: running firmware, library list and the current manifest summary."""
        return self.call(op="firmware")

    def adc(self):
        """Analog RX words (0x26 drive, 0x28 main_out, 0x2a hp_cutoff) the guest last reported.
        The guest refreshes this about once per second, so wait ~1.2 s after an event."""
        return adc_words(self.metrics())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, help="session runtime.json")
    parser.add_argument("command", choices=["event", "press", "turn", "touch", "raw", "clear",
                                            "snapshot", "metrics", "txlog", "period", "panelshot", "resize", "adc", "usb",
                                            "app-restart", "firmware", "power", "standby", "pressure", "calibrate",
                                            "fault", "monitor", "usb-oc"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--timeout", type=float, default=None,
                        help=f"seconds to wait for each reply (default SP16_CONTROL_TIMEOUT or {DEFAULT_TIMEOUT:g})")
    args = parser.parse_args()
    try:
        result = run_command(Session(args.runtime, args.timeout), args.command, args.args)
    except ControlError as error:
        print(f"sessionctl: {error}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as error:                       # the app answered with an error: one line too
        print(f"sessionctl: the app refused {args.command}: {error}", file=sys.stderr)
        sys.exit(1)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    print()


def run_command(session, command, a):
    """One CLI command on an open session; returns what main prints."""
    if command == "event":
        result = session.event(a[0], a[1], int(a[2]) if len(a) > 2 else 0)
    elif command == "press":
        session.press(a[0], float(a[1]) if len(a) > 1 else .15, int(a[2]) if len(a) > 2 else 100)
        result = {"ok": True}
    elif command == "turn":
        session.turn(a[0], int(a[1]))
        result = {"ok": True}
    elif command == "touch":
        result = session.touch(int(a[0]), int(a[1]), ["move", "down", "up"].index(a[2]))
    elif command == "raw":
        result = session.raw(int(a[0], 0), bytes.fromhex(a[1]))
    elif command == "clear":
        result = session.clear()
    elif command == "txlog":
        result = session.call(op="txlog", on=0 if a and a[0] == "off" else 1)
    elif command == "period":
        result = session.period(int(a[0]))
    elif command == "resize":
        result = session.resize(int(a[0]), int(a[1]))
    elif command == "panelshot":
        result = {"path": str(session.panelshot(a[0]))}
    elif command == "usb":
        action = a[0] if a else "eject"
        result = session.usb(action, a[1] if len(a) > 1 else None)
    elif command == "usb-oc":
        if a != ["on"] and a != ["off"]:
            raise RuntimeError("usb-oc needs on|off")
        result = session.usb_oc(a[0] == "on")
    elif command == "adc":
        result = session.adc()
    elif command == "pressure":
        session.pressure(a[0], a[1])
        result = {"ok": True}
    elif command == "calibrate":
        print(f"sessionctl: {CALIBRATION_NOTE}", file=sys.stderr)
        raw = int(a[1]) if len(a) > 1 else CALIBRATION_RAW
        session.calibrate(a[0], raw)
        result = {"ok": True, "pad": a[0], "raw": raw, "passes": CALIBRATION_PASSES}
    elif command == "power":
        print(f"sessionctl: {STANDBY_WARNING}", file=sys.stderr)
        session.power(float(a[0]) if a else 0.3)
        result = {"ok": True, "standby": session.tx_standby()}
    elif command == "standby":
        if a[:1] != ["yes"]:
            raise RuntimeError(f"standby needs the argument yes: {STANDBY_WARNING}")
        result = session.standby()
    elif command == "fault":
        if a != ["on"] and a != ["off"]:
            raise RuntimeError("fault needs on|off")
        result = session.fault(a[0] == "on")
    elif command == "monitor":
        if not a or a[0] not in ("MAIN", "PHONES", "OUT3-4", "OUT5-6", "OUT7-8"):
            raise RuntimeError("monitor needs MAIN|PHONES|OUT3-4|OUT5-6|OUT7-8")
        result = session.monitor(a[0])
    elif command == "app-restart":
        result = session.app_restart()
    elif command == "firmware":
        result = session.firmware()
    elif command == "snapshot":
        result = {"path": str(session.snapshot(a[0]))}
    else:
        result = session.metrics()
    return result


if __name__ == "__main__":
    main()
