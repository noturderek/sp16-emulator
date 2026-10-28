#!/usr/bin/env python3
"""Stream real pixels, touch and semantic panel controls over a dedicated virtio port."""
import importlib.util
import os
import queue
import re
import select
from pathlib import Path
import socket
import struct
import threading
import time

import panel_map
from protocol import (ACK, CONTROL, FRAME, INPUT_FAILED, INPUT_INVALID, INPUT_UNSUPPORTED, KEY,
                      RESEARCH, TOUCH, TX_FRAME, WHEEL, decode_control, decode_key,
                      decode_research, decode_touch, decode_wheel, encode_feedback,
                      encode_result, encode_stats, frame_pixels, message, receive)
from panel import AnalogStateFile, PanelModel, StartOff, crc16

spec = importlib.util.spec_from_file_location("sp16_input", "/opt/sp16/input.py")
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)

ROOT = "/tmp/sp16-root"
PERIOD = float(os.environ.get("SP16_PANEL_PERIOD_MS", "10")) / 1000
timing = {"period": PERIOD}
HEARTBEAT = 0.1
# The host connects to the UI port once and never reconnects. When it goes away (quit, crash, kill -9) the bridge
# waits this long for a hang-up to clear, then exits; guest-init then runs the clean stop (OEM killed, data synced
# and unmounted) and powers off, so QEMU does not outlive the app and keep the data image locked.
HOST_GRACE = 8.0


class Counters:
    """Small thread-safe counters reported to the host for event-loss/queue checks."""

    def __init__(self):
        self.lock = threading.Lock()
        self.values = {"rx_frames": 0, "rx_drops": 0, "cdc_frames": 0, "cdc_drops": 0,
                       "tx_frames": 0, "tx_bad_crc": 0, "events": 0, "unsupported": 0,
                       "failed": 0}
        # Requests the shim refused with ENOTTY (it still fails them loudly); reported so that a firmware version
        # using driver requests the shim does not know is diagnosable from metrics (docs/architecture.md, M5).
        self.unknown_ioctls = {}
        # Device nodes the shim does not serve (hardware suite HW-45): path -> opens reported.
        self.unknown_devices = {}
        # Requests the shim accepted, by device kind (hardware suite HW-45: every declared request was served).
        self.ioctls_seen = {}
        # Analog board model (hardware_stub.c): the last DAC7811 word the app wrote for the high-pass, the newest
        # filter-loop report and the bytes sent over the DIN MIDI loopback cable.
        self.board = {}

    def add(self, name, amount=1):
        with self.lock:
            self.values[name] += amount

    def unknown_ioctl(self, request):
        with self.lock:
            if request in self.unknown_ioctls or len(self.unknown_ioctls) < 64:
                self.unknown_ioctls[request] = self.unknown_ioctls.get(request, 0) + 1

    def unknown_device(self, path):
        with self.lock:
            if path in self.unknown_devices or len(self.unknown_devices) < 64:
                self.unknown_devices[path] = self.unknown_devices.get(path, 0) + 1

    def ioctl_seen(self, request, kind):
        with self.lock:
            if request in self.ioctls_seen or len(self.ioctls_seen) < 64:
                self.ioctls_seen[request] = kind

    def board_update(self, key, value):
        with self.lock:
            if key == "gpio":  # one entry per line: the newest level the app wrote
                self.board.setdefault("gpio", {})[value[0]] = value[1]
            else:
                self.board[key] = value

    def snapshot(self):
        with self.lock:
            return {**self.values, "unknown_ioctls": dict(self.unknown_ioctls),
                    "unknown_devices": dict(self.unknown_devices), "ioctls_seen": dict(self.ioctls_seen),
                    "board": json_copy(self.board)}


class LinkTiming:
    """Timing of the guest side of the panel link, reported in the guest stats (board.bridge_timing): how long a message
    waited for the host port and how long its write took (the port is one byte stream, so a large LCD frame in flight
    delays every lamp message queued behind it), and the gaps between the firmware's panel frames as the bridge received
    them. Cumulative since the bridge started; the host compares readings taken before and after a window."""

    LIMIT = 0.040

    def __init__(self, counters):
        self.counters, self.lock = counters, threading.Lock()
        self.values = {"tx_recv": 0, "tx_gap_max_ms": 0.0, "tx_gaps_over_40ms": 0, "lock_wait_max_ms": 0.0,
                       "frame_writes": 0, "frame_write_max_ms": 0.0, "frame_write_mean_ms": 0.0, "frame_bytes_per_s": 0,
                       "outbox_max": 0, "outbox_dropped": 0}
        self.last_arrival, self.last_report, self.frame_total, self.frame_started = None, 0.0, 0.0, time.monotonic()
        self.frame_bytes = 0

    def arrival(self, now):
        with self.lock:
            v = self.values
            v["tx_recv"] += 1
            if self.last_arrival is not None:
                gap = now - self.last_arrival
                v["tx_gap_max_ms"] = max(v["tx_gap_max_ms"], gap * 1000)
                if gap > self.LIMIT:
                    v["tx_gaps_over_40ms"] += 1
            self.last_arrival = now

    def write(self, size, waited, took):
        with self.lock:
            v = self.values
            v["lock_wait_max_ms"] = max(v["lock_wait_max_ms"], waited * 1000)
            if size > 65536:  # an LCD frame
                v["frame_writes"] += 1
                v["frame_write_max_ms"] = max(v["frame_write_max_ms"], took * 1000)
                self.frame_total += took
                self.frame_bytes += size

    def outbox(self, depth, dropped=False):
        with self.lock:
            self.values["outbox_max"] = max(self.values["outbox_max"], depth)
            if dropped:
                self.values["outbox_dropped"] += 1

    def report(self, now):
        """Publish into the guest stats at most once a second."""
        if now - self.last_report < 1.0:
            return
        self.last_report = now
        with self.lock:
            v = self.values
            if v["frame_writes"]:
                v["frame_write_mean_ms"] = self.frame_total / v["frame_writes"] * 1000
            v["frame_bytes_per_s"] = int(self.frame_bytes / max(1e-3, now - self.frame_started))
            published = {k: round(x, 1) if isinstance(x, float) else x for k, x in v.items()}
        self.counters.board_update("bridge_timing", published)


def json_copy(value):
    return {k: dict(v) if isinstance(v, dict) else v for k, v in value.items()}


UNKNOWN_IOCTL = re.compile(r"\[sp16-ui-stub\] unsupported ioctl fd=\d+ request=(0x[0-9a-f]{1,8})$")
UNKNOWN_DEVICE = re.compile(r"\[sp16-ui-stub\] unknown device open path=(/[!-~]{1,63})$")
IOCTL_SEEN = re.compile(r"\[sp16-ui-stub\] ioctl accepted request=(0x[0-9a-f]{1,8}) device=([a-z]{1,16})$")
DAC_WORD = re.compile(r"\[sp16-ui-stub\] dac hpf word=0x([0-9a-f]{4}) ctrl=(\d+) code=(\d+) writes=(\d+)$")
FILTER_LOOP = re.compile(r"\[sp16-ui-stub\] filter loop send_peak=([0-9.]+) return_peak=([0-9.]+) frames=(\d+) "
                         r"underruns=(\d+) drops=(\d+) lp_hz=(\d+) hp=(dac|off)$")
ANALOG_LEVELS = re.compile(r"\[sp16-ui-stub\] analog levels limiter=([01]) in_peak=([0-9.]+) in_dbu_peak=(-?[0-9.]+) out_peak=([0-9.]+) "
                           r"out_dbu_peak=(-?[0-9.]+) limited=(\d+) main_out_gain=([0-9.]+) input_gain=([0-9.]+) source=(off|tone|wav)$")
TX_DROPPED = re.compile(r"\[sp16-ui-stub\] panel TX frames dropped=(\d+) errno=(\d+)$")
GPIO_LINE = re.compile(r"\[sp16-ui-stub\] gpio (GPIO\d_IO\d\d)=([01]) ([A-Za-z_/?]+)$")
BACKLIGHT_LEVEL = re.compile(r"\[sp16-ui-stub\] backlight level=(\d+)$")
MIDI_LOOP = re.compile(r"\[sp16-ui-stub\] midi loopback (/dev/[a-z0-9_]+) first bytes=(\d+)$")


def board_line(line):
    """A board-model line of the shim -> (metrics key, value), or None."""
    match = DAC_WORD.match(line)
    if match:
        return "dac", {"word": int(match.group(1), 16), "ctrl": int(match.group(2)), "code": int(match.group(3)),
                       "writes": int(match.group(4))}
    match = FILTER_LOOP.match(line)
    if match:
        return "filter_loop", {"send_peak": float(match.group(1)), "return_peak": float(match.group(2)),
                               "frames": int(match.group(3)), "underruns": int(match.group(4)),
                               "drops": int(match.group(5)), "lp_hz": int(match.group(6)), "hp": match.group(7)}
    match = ANALOG_LEVELS.match(line)
    if match:
        # in/out: peak over the report window (1.0 = converter full scale); *_dbu_peak = dBFS + 16 from that peak
        # (peak-equivalent dBu, grade I, not measured on a unit; dBu proper is RMS).
        return "analog_levels", {"limiter_active": match.group(1) == "1", "in_peak": float(match.group(2)),
                                 "in_dbu_peak": float(match.group(3)), "out_peak": float(match.group(4)),
                                 "out_dbu_peak": float(match.group(5)), "limited_samples": int(match.group(6)),
                                 "main_out_gain": float(match.group(7)), "input_gain": float(match.group(8)),
                                 "source": match.group(9)}
    match = MIDI_LOOP.match(line)
    if match:
        return "midi_loopback", {"node": match.group(1), "first_bytes": int(match.group(2))}
    match = TX_DROPPED.match(line)
    if match:  # the shim's panel TX datagram queue to the bridge overflowed (logged at powers of two)
        return "tx_dropped_in_guest", int(match.group(1))
    match = GPIO_LINE.match(line)
    if match:
        return "gpio", (match.group(1), int(match.group(2)))
    match = BACKLIGHT_LEVEL.match(line)
    if match:
        level = int(match.group(1))
        return ("backlight", level) if 0 <= level <= 255 else None
    return None


def main():
    # Device supplied only by this VM's QEMU arguments; no TCP listener.
    path = Path("/dev/vport0p1")
    for _ in range(100):
        if path.exists():
            break
        time.sleep(.1)
    try:  # the shim's panel TX datagrams queue here: the default depth of 10 overflows within 30 ms of a stalled reader
        Path("/proc/sys/net/unix/max_dgram_qlen").write_text("1024")
    except OSError:
        pass
    descriptor = os.open(path, os.O_RDWR)
    stream = os.fdopen(os.dup(descriptor), "rb", buffering=0)
    lock = threading.Lock()
    stopped = threading.Event()
    model = PanelModel(pad_curve=panel_map.PAD_CURVE)
    start_off = StartOff(panel_map.START_OFF)
    counters = Counters()
    link = LinkTiming(counters)
    state = {"txlog": False, "generation": 0, "lamps": {}, "live": False}
    usb_oc_path = Path(f"{ROOT}/dev/sp16-usb-oc")
    usb_oc_path.write_bytes(b"1")
    logged_lines = 0

    def diagnostics():
        nonlocal logged_lines
        # Export only our own messages (shim device lines, fwdev preload libraries), never arbitrary OEM logs.
        lines = Path("/output/application.txt").read_text(errors="replace").splitlines()
        for line in lines[logged_lines:]:
            if line.startswith(("[sp16-ui-stub]", "[sp16-mod]")):  # shim and developer preload libraries
                print(line, flush=True)
                unknown = UNKNOWN_IOCTL.match(line)
                if unknown:
                    counters.unknown_ioctl(unknown.group(1))
                seen = IOCTL_SEEN.match(line)
                if seen:
                    counters.ioctl_seen(seen.group(1), seen.group(2))
                device = UNKNOWN_DEVICE.match(line)
                if device:
                    counters.unknown_device(device.group(1))
                board = board_line(line)
                if board:
                    counters.board_update(*board)
        logged_lines = len(lines)

    def send(kind, payload):
        send_raw(message(kind, payload))

    def send_raw(prepared):
        asked = time.monotonic()
        with lock:
            started = time.monotonic()
            data = memoryview(prepared)
            while data:
                count = os.write(descriptor, data)
                if not count:
                    raise EOFError("native window disconnected")
                data = data[count:]
        link.write(len(prepared), started - asked, time.monotonic() - started)

    def datagram(sock, target, payload, sent, dropped):
        try:
            sock.sendto(payload, target)
            counters.add(sent)
            return True
        except OSError:
            # No reader yet or full queue: state frames are absolute, the next one repairs it.
            counters.add(dropped)
            return False

    def frames():
        previous, sequence, last_diagnostics, pause = None, 0, 0, 0.02
        try:
            while not stopped.is_set():
                source = Path("/output/Xvfb_screen0").read_bytes()
                pixels = frame_pixels(source)
                pause = 0.02
                if pixels != previous:
                    sequence += 1
                    started = time.monotonic()
                    send(FRAME, struct.pack(">HHI", 800, 480, sequence) + pixels)
                    previous = pixels
                    # The port is one byte stream shared with the lamp messages: a 1.5 MB frame holds it for as long as
                    # the host takes to drain it. Leave it idle for as long again, so lamps wait for at most one frame
                    # and frames never take more than half of the port.
                    pause = max(0.02, time.monotonic() - started)
                if time.monotonic() - last_diagnostics >= 1:
                    link.report(time.monotonic())
                    diagnostics()
                    send_raw(encode_stats({**counters.snapshot(), "adc": model.adc_words(), "rx": model.rx_state()}))
                    last_diagnostics = time.monotonic()
                time.sleep(pause)
        except (OSError, EOFError, ValueError) as exc:
            print(f"SP16_FRAME_ERROR {exc}", flush=True)
            stopped.set()

    def streamer():
        """Deliver a complete RX frame every period while input is active, else a heartbeat.

        The OEM held-button counters advance once per received frame, so held controls
        need a steady frame stream; the nominal SPI interval is 3 ms but the real jiffy
        quantization is unknown, hence a configurable period (default 10 ms).
        """
        out = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK)
        cdc = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK)
        panel_target, cdc_target = f"{ROOT}/dev/sp16-panel-input", f"{ROOT}/dev/sp16-cdc-input"
        last, baseline = 0.0, False
        try:
            analog = AnalogStateFile(f"{ROOT}/dev/sp16-analog", model)
        except OSError as exc:
            analog = None
            print(f"SP16_ANALOG_ERROR {exc}", flush=True)
        try:
            while not stopped.wait(timing["period"]):
                if analog is not None and model.analog_changed:
                    analog.update()
                due = model.step()
                now = time.monotonic()
                if start_off.pending:
                    # Lamp colour per control id from the OEM TX frames; unknown until the first lamp frame.
                    lit = {name: any(state["lamps"].get(panel_map.CONTROL_IDS[name], (0, 0, 0))) if state["live"] else None
                           for name in start_off.pending}
                    for name in start_off.step(now, lit):
                        model.apply(panel_map.CONTROL_IDS[name], "press")
                        model.apply(panel_map.CONTROL_IDS[name], "release")
                        print(f"SP16_START_OFF {name} pressed", flush=True)
                if not baseline or due or model.active() or now - last >= HEARTBEAT:
                    datagram(out, panel_target, model.frame(), "rx_frames", "rx_drops")
                    baseline, last = True, now
                if model.cdc_changed:
                    # CDC is signalled only on change, so a dropped datagram must be retried.
                    if not datagram(cdc, cdc_target, model.cdc_bytes(), "cdc_frames", "cdc_drops"):
                        model.cdc_changed = True
        finally:
            out.close()
            cdc.close()
            if analog is not None:
                analog.close()

    def listen_tx():
        """Capture OEM panel TX frames (lamp state source); decode only proven fields.

        This thread only receives: it never takes the host-port lock, so a large LCD frame being written cannot stop it
        from draining the shim's datagram queue (a full queue makes the shim drop panel frames). Everything for the host
        (the TX log and the lamp feedback) goes through `outbox`/`feedback` to a sender thread.
        """
        tx_path = f"{ROOT}/dev/sp16-panel-tx"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.settimeout(.02)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        try:
            os.unlink(tx_path)
        except FileNotFoundError:
            pass
        listener.bind(tx_path)
        outbox = queue.SimpleQueue()
        wake = threading.Event()
        feedback = {"dirty": False, "lamps": {}, "generation": 0}
        guard = threading.Lock()

        def sender():
            last_sent = 0.0
            while not stopped.is_set():
                wake.wait(.05)
                wake.clear()
                try:
                    while True:
                        send(*outbox.get_nowait())
                except queue.Empty:
                    pass
                except (OSError, EOFError, ValueError):
                    stopped.set()
                    return
                with guard:
                    dirty = feedback["dirty"]
                if dirty:
                    wait = last_sent + 1 / 60 - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    with guard:
                        generation, lamps, feedback["dirty"] = feedback["generation"], dict(feedback["lamps"]), False
                    try:
                        send_raw(encode_feedback(generation, True, lamps))
                    except (OSError, EOFError, ValueError):
                        stopped.set()
                        return
                    last_sent = time.monotonic()

        threading.Thread(target=sender, daemon=True).start()
        last_frame = None
        try:
            while not stopped.is_set():
                try:
                    datagram_bytes = listener.recv(256)
                except socket.timeout:
                    continue
                link.arrival(time.monotonic())
                if len(datagram_bytes) < 8:
                    continue
                try:
                    multi, part, parts, _, length = struct.unpack_from("<BBBBI", datagram_bytes)
                    frame = datagram_bytes[8:8 + min(length, 128)]
                    counters.add("tx_frames")
                    if len(frame) == 128 and crc16(frame[:124]) != struct.unpack_from("<H", frame, 124)[0]:
                        counters.add("tx_bad_crc")
                        continue
                    if state["txlog"]:
                        depth = outbox.qsize()
                        if depth >= 4096:
                            link.outbox(depth, dropped=True)
                        else:
                            outbox.put((TX_FRAME, bytes((multi, part, parts)) + frame))
                            link.outbox(depth + 1)
                            wake.set()
                    if frame == last_frame:
                        continue  # the OEM resends unchanged lamp state continuously
                    last_frame = frame
                    lamps = panel_map.decode_lamps(frame) if len(frame) == 128 else None
                    if lamps is not None:
                        merged = {**state["lamps"], **lamps}
                        if merged != state["lamps"] or not state["live"]:
                            state["lamps"], state["live"] = merged, True
                            state["generation"] += 1
                            with guard:
                                feedback.update(dirty=True, lamps=merged, generation=state["generation"])
                            wake.set()
                except (struct.error, ValueError):
                    counters.add("failed")
        finally:
            listener.close()

    def host_gone(reason):
        """Exit after the grace period unless the host returns; guest-init then runs the clean stop and poweroff."""
        print(f"SP16_HOST_LOST reason={reason} grace={HOST_GRACE}", flush=True)
        time.sleep(HOST_GRACE)
        if poller.poll(0):
            print("SP16_HOST_LOST exit=yes", flush=True)
            os._exit(0)
        print("SP16_HOST_LOST host_returned=yes", flush=True)
        return False

    poller = select.poll()
    poller.register(descriptor, select.POLLERR | select.POLLHUP)

    def host_watch():
        """poll(2) reports POLLERR/POLLHUP on the port while the host end of the chardev is closed."""
        while True:
            if poller.poll(200):
                host_gone("hangup")

    threading.Thread(target=frames, daemon=True).start()
    threading.Thread(target=host_watch, daemon=True).start()
    threading.Thread(target=streamer, daemon=True).start()
    threading.Thread(target=listen_tx, daemon=True).start()
    sequence = 0
    try:
        while not stopped.is_set():
            try:
                kind, payload = receive(stream)
            except (EOFError, OSError):
                host_gone("eof")
                continue
            try:
                if kind == CONTROL:
                    try:
                        control, action, value, event = decode_control(payload)
                    except ValueError:
                        counters.add("failed")
                        send_raw(encode_result(0, INPUT_INVALID))
                        continue
                    status = model.apply(control, action, value)
                    counters.add("events")
                    if status == INPUT_UNSUPPORTED:
                        counters.add("unsupported")
                    send_raw(encode_result(event, status))
                    continue
                if kind == RESEARCH:
                    op, offset, data = decode_research(payload)
                    if op == "raw":
                        model.set_raw(offset, data)
                    elif op == "cdc":
                        model.set_cdc_raw(data)
                    elif op == "clear":
                        model.clear_overrides()
                    elif op == "period":
                        timing["period"] = offset / 1000
                    elif op == "usb-oc":
                        temporary = usb_oc_path.with_suffix(".next")
                        temporary.write_bytes(b"0" if offset else b"1")
                        temporary.replace(usb_oc_path)
                    else:
                        state["txlog"] = bool(offset)
                    continue
                if kind == TOUCH:
                    x, y, action = decode_touch(payload)
                    model.lcd_touch(x, y, action)
                elif kind == WHEEL:
                    x, y, steps = decode_wheel(payload)
                    model.apply(panel_map.CONTROL_IDS["selector"], "delta", steps)
                elif kind == KEY:
                    key = decode_key(payload)
                    selector = panel_map.CONTROL_IDS["selector"]
                    if key in ("Up", "Down"):
                        model.apply(selector, "delta", 1 if key == "Down" else -1)
                    elif key == "Return":
                        model.apply(selector, "press")
                        time.sleep(.05)
                        model.apply(selector, "release")
                    else:
                        controls.navigation_key(key)
                else:
                    raise ValueError("unsupported host message")
            except OSError as exc:
                counters.add("failed")
                print(f"SP16_INPUT_ERROR errno={exc.errno}", flush=True)
                if kind == CONTROL:
                    send_raw(encode_result(event, INPUT_FAILED))
                    continue
            except ValueError:
                # Malformed legacy/research messages are rejected without stopping the bridge.
                counters.add("failed")
            sequence += 1
            send(ACK, struct.pack(">I", sequence))
    finally:
        # Disconnect releases every hold before the guest stops reading input.
        model.release_all()
        time.sleep(timing["period"] * 3)
        stopped.set()
        stream.close()
        os.close(descriptor)


if __name__ == "__main__":
    main()
