"""OEM 1.60 panel RX packets for the isolated native emulator only."""
from collections import deque
import socket
import struct
import threading
import time

import panel_map
from protocol import INPUT_FAILED, INPUT_INVALID, INPUT_OK, INPUT_UNSUPPORTED


def _crc_table():
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc16(data):
    """CRC-16/X-25: reflected 0x8408, init/final XOR 0xffff."""
    crc = 0xffff
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 0xff]
    return crc ^ 0xffff


def packet(counter=0, buttons=0):
    if buttons not in (0, 0x02, 0x08):
        raise ValueError("unsupported selector button")
    frame = bytearray(128)
    frame[0], frame[1] = 1, buttons
    struct.pack_into(">H", frame, 0x3c, counter & 0xffff)
    struct.pack_into("<H", frame, 124, crc16(frame[:124]))
    return bytes(frame)


class PanelInput:
    def __init__(self, path):
        self.path, self.counter, self.initialized = path, 0, False
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.settimeout(.5)

    def send(self, buttons=0):
        self.socket.sendto(packet(self.counter, buttons), self.path)

    def baseline(self):
        if not self.initialized:
            self.send()
            time.sleep(.02)
            self.initialized = True

    def turn(self, rows):
        if not isinstance(rows, int) or not 1 <= abs(rows) <= 12:
            raise ValueError("selector rows out of range")
        self.baseline()
        # E9: a counter increase moves the Utility highlight down (next row).
        for _ in range(abs(rows)):
            self.counter = (self.counter + (1 if rows > 0 else -1)) & 0xffff
            self.send()
            time.sleep(.01)

    def press(self, button):
        self.baseline()
        self.send(button)
        time.sleep(.03)
        self.send()

    def close(self):
        self.socket.close()


def finish(frame):
    """Write the CRC-16/X-25 over bytes 0..123 into a complete 128-byte RX frame."""
    struct.pack_into("<H", frame, 124, crc16(frame[:124]))
    return bytes(frame)


RX_HEADER, RX_CRC_OFFSET = 0x01, 124


def decode_rx(frame, check_crc=True):
    """A complete RX frame -> the input it carries, or None if the frame is not one.

    The inverse of PanelModel.frame() and the one place a received frame is named: buttons by role (frame bytes 1..5),
    encoder counters (u16 detents, offsets from the profile), every pad's pressure and contact word, and the pots the
    profile maps as raw words and as host levels (0..1000, 0x3ff minus the word where the profile says inverted). The
    touch strip is not in this frame: it has its own CDC channel and its own decoder, decode_strip().

    A wrong length, header or CRC-16 means the frame is not an RX frame, never a frame with unknown content.
    A frame whose pot words are outside 0..1023 is likewise not one (it would carry an unencodable analog level).
    Counters stay unsigned 16-bit, the way the model wraps them; signed_detents() reads a delta out of one.
    """
    if len(frame) != panel_map.FRAME_BYTES or frame[0] != RX_HEADER:
        return None
    if check_crc and struct.unpack_from("<H", frame, RX_CRC_OFFSET)[0] != crc16(frame[:RX_CRC_OFFSET]):
        return None
    word = lambda offset: struct.unpack_from(">H", frame, offset)[0]
    buttons = {role: bool(frame[byte] >> bit & 1) for role, (byte, bit) in panel_map.BUTTON_BITS.items()}
    counters = {name: word(offset) for name, (offset, _) in panel_map.COUNTERS.items()}
    pads = [{"pressure": word(panel_map.PAD_A_OFFSET + 2 * index),
             "contact": word(panel_map.PAD_B_OFFSET + 2 * index)} for index in range(16)]
    analog_words = {name: word(offset) for name, (offset, _) in panel_map.ANALOG_WORDS.items()}
    if any(raw > panel_map.ANALOG_FULL_SCALE for raw in analog_words.values()):
        return None
    analog = {name: analog_level(analog_words[name], inverted)
              for name, (_, inverted) in panel_map.ANALOG_WORDS.items()}
    return {"buttons": buttons, "counters": counters, "pads": pads, "analog_words": analog_words, "analog": analog}


def analog_level(raw, inverted=False):
    """A 10-bit pot word -> the host level 0..1000 the panel view uses, inverted where the profile says so."""
    if not 0 <= raw <= panel_map.ANALOG_FULL_SCALE:
        raise ValueError(f"analog word {raw} is outside 0..{panel_map.ANALOG_FULL_SCALE}")
    return round((panel_map.ANALOG_FULL_SCALE - raw if inverted else raw) * 1000 / panel_map.ANALOG_FULL_SCALE)


def signed_detents(value):
    """A counter word (0..0xffff, wrapping) as the signed step count it represents."""
    if not 0 <= value <= 0xffff:
        raise ValueError(f"counter word {value} is outside 0..0xffff")
    return value - 0x10000 if value & 0x8000 else value


def decode_strip(byte0, byte1):
    """E5 over the two CDC bytes: value = (0x7f - b0) | b1 << 8; position = value & 0x7f, contact = bit 15."""
    if not 0 <= byte0 <= 0xff or not 0 <= byte1 <= 0xff:
        raise ValueError("strip bytes are 0..255")
    word = (0x7f - byte0) | byte1 << 8
    return word & 0x7f, word >> 15 & 1


def analog_state_bytes(slots, sequence=0, tone_hz=None):
    """Encode the shared analog state: magic, u32 sequence (even = stable), five u16 slots (MAIN OUT, INPUT LEVEL,
    CUTOFF, RESONANCE, INPUT TONE), u16 tone frequency, then the FILTER TONE slot (hardware_stub.c reads the same)."""
    body = b"SP16ANA1" + struct.pack("<I", sequence) + struct.pack("<5H", *slots[:5])
    body += struct.pack("<H", tone_hz or panel_map.INPUT_TONE_HZ) + struct.pack("<H", slots[5])
    return body.ljust(64, b"\0")


class AnalogStateFile:
    """The bridge's writer of the shared analog state: one 64-byte file mapped by the shim, updated in place under a
    sequence lock (odd while writing), so the audio thread never sees a torn update and never blocks."""

    def __init__(self, path, model):
        import mmap
        self.model, self.sequence = model, 0
        with open(path, "wb") as handle:
            handle.write(model.analog_state(0))
        self.file = open(path, "r+b")
        self.map = mmap.mmap(self.file.fileno(), 64)

    def update(self):
        state = self.model.analog_state()
        self.map[8:12] = struct.pack("<I", self.sequence + 1)
        self.map[12:64] = state[12:64]
        self.sequence += 2
        self.map[8:12] = struct.pack("<I", self.sequence)

    def close(self):
        self.map.close()
        self.file.close()


class PanelModel:
    """Semantic panel state -> complete OEM RX frames, with held state preserved.

    Mapped controls only: an unmapped control reports INPUT_UNSUPPORTED rather than
    producing guessed bits. Encoder motion is queued and applied one detent per frame
    so firmware acceleration sees physical-like rates instead of a single large jump.
    """

    # E10: the firmware samples whole frames, so an edge pair shorter than one frame period is
    # invisible. Each queued edge is shown for at least this many frames before the next one.
    MIN_EDGE_FRAMES = 3
    MAX_DELTA = 64
    # The OEM rotary object notifies once per 8 received frames with a sign-only selector
    # consumer, so at most one selector count is released per window; HOT_FRAMES keeps the fast
    # cadence for two windows after any input change so the last window closes quickly.
    SELECTOR_WINDOW_FRAMES = panel_map.SELECTOR_WINDOW_FRAMES
    HOT_FRAMES = 16
    # LCD touch (RX words 0x2c/0x2e): hold an idle->point jump for this many frames before a move applies, and never
    # move the raw point more than TOUCH_STEP_MAX ADC units between consecutive frames (a teleport breaks the
    # firmware's stable history and reads as a release).
    TOUCH_HOLD_FRAMES = panel_map.LCD_TOUCH_HOLD_FRAMES
    TOUCH_STEP_MAX = panel_map.LCD_TOUCH_STEP_MAX

    def __init__(self, bits=None, counters=None, pad_curve=panel_map.PAD_CURVE, touch_calibration=None):
        self.lock = threading.Lock()
        self.bits = dict(panel_map.BUTTON_BITS if bits is None else bits)
        self.counters = dict(panel_map.COUNTERS if counters is None else counters)
        self.pad_curve = pad_curve
        self.raw = bytearray(panel_map.FRAME_BYTES)
        self.raw[0] = 1
        self.pending = {}
        self.cooldown = 0
        self.hot = 0
        self.edges = {}
        self.hold_frames = {}
        self.held = {}
        self.overrides = {}
        for name in panel_map.ANALOG_WORDS:
            self._set_analog(name, panel_map.ANALOG_DEFAULTS.get(name, panel_map.ANALOG_DEFAULT))
        self.cdc_contact, self.cdc_position = False, 0
        self.cdc_override = None
        self.cdc_changed = False
        self.changed = True
        # Host-side analog state for the shim's board model; an absent name was never moved (neutral).
        self.host_analog = {}
        self.analog_changed = False
        # LCD touch: the raw point currently in the frame, whether a finger is down, how many frames remain in the
        # initial hold, and the pending bounded-step drag target (raw coordinates; (0, 0) is the out-of-bounds idle).
        self.touch_calibration = touch_calibration if touch_calibration is not None else panel_map.LCD_TOUCH_CALIBRATION
        self.touch_raw = (0, 0)
        self.touch_contact = False
        self.touch_hold = 0
        self.touch_target = None

    def _set_bit(self, bit, on):
        byte, mask = bit[0], 1 << bit[1]
        before = self.raw[byte]
        self.raw[byte] = before | mask if on else before & ~mask & 0xff
        self.changed |= before != self.raw[byte]

    def _set_analog(self, name, value):
        offset, inverted = panel_map.ANALOG_WORDS[name]
        word = round(value * panel_map.ANALOG_FULL_SCALE / 1000)
        self._set_word(offset, panel_map.ANALOG_FULL_SCALE - word if inverted else word)

    def _set_word(self, offset, value):
        struct.pack_into(">H", self.raw, offset, value & 0xffff)

    def apply(self, control, action, value=0):
        name = panel_map.ALL_CONTROL_NAMES.get(control)
        if name is None:
            return INPUT_INVALID
        with self.lock:
            if name == "strip":
                return self._strip(action, value)
            if name in panel_map.ANALOG_WORDS or name in panel_map.HOST_ANALOG:
                if action != "value" or not 0 <= value <= 1000:
                    return INPUT_INVALID
                if name in panel_map.ANALOG_WORDS:
                    self._set_analog(name, value)
                    self.changed = True
                if name in panel_map.HOST_ANALOG:
                    self.host_analog[name] = value
                    self.analog_changed = True
                return INPUT_OK
            if name.startswith("pressure"):
                if action != "value" or not 0 <= value <= panel_map.PAD_RAW_MAX:
                    return INPUT_INVALID
                self._pressure(int(name[8:]) - 1, value)
                return INPUT_OK
            if name.startswith("pad") and action in ("press", "release", "value"):
                if self.pad_curve is None:
                    return INPUT_UNSUPPORTED
                if action == "value":
                    return self._pad(int(name[3:]) - 1, action, value)
                self.edges.setdefault(name, deque()).append((action, value))
                return INPUT_OK
            if action == "delta":
                if name not in self.counters or not value:
                    return INPUT_UNSUPPORTED if name not in self.counters else INPUT_INVALID
                offset, sign = self.counters[name]
                total = self.pending.get(offset, 0) + sign * max(-self.MAX_DELTA, min(self.MAX_DELTA, value))
                # Bound queued motion so one bad event cannot spin a counter for minutes.
                total = max(-4 * self.MAX_DELTA, min(4 * self.MAX_DELTA, total))
                if total:
                    self.pending[offset] = total
                else:
                    self.pending.pop(offset, None)
                self.changed = True
                return INPUT_OK
            if action in ("press", "release") and name in self.bits:
                self.edges.setdefault(name, deque()).append((action, value))
                return INPUT_OK
            return INPUT_UNSUPPORTED

    def _drain_edges(self):
        """Apply at most one queued edge per control per step, then hold it MIN_EDGE_FRAMES."""
        for name, queue in list(self.edges.items()):
            remaining = self.hold_frames.get(name, 0)
            if remaining > 0:
                self.hold_frames[name] = remaining - 1
                continue
            if not queue:
                continue
            action, value = queue.popleft()
            if name.startswith("pad"):
                self._pad_edge(int(name[3:]) - 1, action, value)
            else:
                count = self.held.get(name, 0) + (1 if action == "press" else -1)
                self.held[name] = max(0, count)
                self._set_bit(self.bits[name], self.held[name] > 0)
            self.hold_frames[name] = self.MIN_EDGE_FRAMES - 1
            self.changed = True
            if not queue:
                del self.edges[name]

    def _pad(self, index, action, value):
        """Pressure update for a pad that is down (action "value")."""
        name = f"pad{index + 1}"
        queued = any(action == "press" for action, _ in self.edges.get(name, ()))
        if not self.held.get(name) and not queued:
            return INPUT_INVALID  # pressure only exists while the pad is down
        if not self.held.get(name):
            # The press has not reached a frame yet: make this the pressure it will show.
            self.edges[name] = deque((act, value if act == "press" else v) for act, v in self.edges[name])
            return INPUT_OK
        a, _ = self.pad_curve(value)
        self._set_word(panel_map.PAD_A_OFFSET + 2 * index, a)
        self.changed = True
        return INPUT_OK

    def _pressure(self, index, raw):
        """Hold pad `index`'s pressure word at `raw` (the calibration walkthrough); contact is set while it is above 0."""
        self._set_word(panel_map.PAD_A_OFFSET + 2 * index, raw)
        self._set_word(panel_map.PAD_B_OFFSET + 2 * index, panel_map.PAD_B_CONTACT if raw else 0)
        self.changed = True

    def _pad_edge(self, index, action, value):
        a, b = self.pad_curve(value if action == "press" else 0)
        self._set_word(panel_map.PAD_A_OFFSET + 2 * index, a)
        self._set_word(panel_map.PAD_B_OFFSET + 2 * index, b)
        self.held[f"pad{index + 1}"] = int(action == "press")

    def _strip(self, action, value):
        if action not in ("press", "release", "value") or not 0 <= value <= 1000:
            return INPUT_INVALID
        self.cdc_contact = action != "release"
        if action != "release":
            self.cdc_position = round(value * 127 / 1000)
        self.cdc_changed = True
        return INPUT_OK

    def _set_touch_words(self, raw_x, raw_y):
        struct.pack_into(">H", self.raw, panel_map.LCD_TOUCH_X_OFFSET, raw_x & 0xffff)
        struct.pack_into(">H", self.raw, panel_map.LCD_TOUCH_Y_OFFSET, raw_y & 0xffff)

    def lcd_touch(self, screen_x, screen_y, action):
        """Host LCD touch in 800x480 screen coordinates (action "down"/"move"/"up") -> RX touch words.

        Screen coordinates are converted with the loaded calibration and the model keeps the firmware's contact
        debounce contract: an idle->point jump is held for at least TOUCH_HOLD_FRAMES frames, a drag moves at most
        TOUCH_STEP_MAX raw units per frame, and a release writes (0, 0), which is outside the contact rectangle
        under every calibration. The ribbon strip is the separate CDC path.
        """
        if action not in ("down", "move", "up"):
            return INPUT_INVALID
        width, height = self.touch_calibration["screen"]
        if not (0 <= screen_x < width and 0 <= screen_y < height):
            return INPUT_INVALID
        if action == "up":
            return self._lcd_touch_raw(0, 0, False)
        raw_x, raw_y = panel_map.screen_to_touch_raw(screen_x, screen_y, self.touch_calibration)
        limit = panel_map.ANALOG_FULL_SCALE
        raw_x, raw_y = min(limit, max(0, raw_x)), min(limit, max(0, raw_y))
        return self._lcd_touch_raw(raw_x, raw_y, True)

    def lcd_touch_raw(self, raw_x, raw_y, contact):
        """Raw-coordinate injection (0..1023): set the RX touch words directly, bypassing calibration, for research."""
        if not (0 <= raw_x <= panel_map.ANALOG_FULL_SCALE and 0 <= raw_y <= panel_map.ANALOG_FULL_SCALE):
            return INPUT_INVALID
        return self._lcd_touch_raw(int(raw_x), int(raw_y), bool(contact))

    def _lcd_touch_raw(self, raw_x, raw_y, contact):
        with self.lock:
            if not contact:
                self.touch_contact = False
                self.touch_hold = 0
                self.touch_target = None
                self.touch_raw = (0, 0)
                self._set_touch_words(0, 0)
                self.changed = True
                return INPUT_OK
            if not self.touch_contact:
                # Idle -> point: show this exact point for at least TOUCH_HOLD_FRAMES frames before any move applies.
                self.touch_contact = True
                self.touch_hold = self.TOUCH_HOLD_FRAMES
                self.touch_target = None
                self.touch_raw = (raw_x, raw_y)
                self._set_touch_words(raw_x, raw_y)
                self.changed = True
                return INPUT_OK
            # Already down: a move queues a bounded-step drag toward the new point.
            self.touch_target = (raw_x, raw_y)
            self.changed = True
            return INPUT_OK

    def _step_touch(self):
        """Advance the touch point by at most one bounded step, after the initial hold has elapsed."""
        if self.touch_hold > 0:
            self.touch_hold -= 1
            return
        if self.touch_target is None:
            return
        target_x, target_y = self.touch_target
        cur_x, cur_y = self.touch_raw
        cur_x += max(-self.TOUCH_STEP_MAX, min(self.TOUCH_STEP_MAX, target_x - cur_x))
        cur_y += max(-self.TOUCH_STEP_MAX, min(self.TOUCH_STEP_MAX, target_y - cur_y))
        self.touch_raw = (cur_x, cur_y)
        self._set_touch_words(cur_x, cur_y)
        self.changed = True
        if self.touch_raw == self.touch_target:
            self.touch_target = None

    def cdc_bytes(self):
        """E5: b0 = 0x7f - position, byte 1 bit 7 = contact. With no contact the position is
        centred (E10: PITCH mode lights no segment away from centre), not the all-zero value
        the earlier prototype used, which the firmware reads as position 127 (top)."""
        with self.lock:
            self.cdc_changed = False
            if self.cdc_override is not None:
                return bytes(self.cdc_override)
            if not self.cdc_contact:
                return bytes((0x7f - 64, 0, 0, 0))
            return bytes((0x7f - self.cdc_position, 0x80, 0, 0))

    def analog_state(self, sequence=0):
        """The 64-byte shared analog state the shim's board model reads (hardware_stub.c): "SP16ANA1", u32 sequence,
        then u16 slots in panel_map.HOST_ANALOG order (0..1000, 0xffff = never moved) and the tone frequency."""
        with self.lock:
            self.analog_changed = False
            slots = [self.host_analog.get(name, 0xFFFF) for name in panel_map.HOST_ANALOG]
        return analog_state_bytes(slots, sequence)

    def set_raw(self, offset, data):
        """Research only: pin frame bytes after normal state is composed."""
        with self.lock:
            for i, byte in enumerate(data):
                self.overrides[offset + i] = byte
            self.changed = True

    def set_cdc_raw(self, data):
        with self.lock:
            self.cdc_override, self.cdc_changed = bytes(data), True

    def clear_overrides(self):
        with self.lock:
            self.overrides.clear()
            self.cdc_override, self.cdc_changed = None, True
            self.changed = True

    def active(self):
        with self.lock:
            # Anything held (buttons, step keys, pads, the touch strip) keeps the fast
            # cadence for as long as it is held, not only for HOT_FRAMES after the last change.
            return bool(self.pending or self.edges or any(self.held.values()) or self.cdc_contact
                        or self.touch_contact or self.overrides or self.hot > 0)

    def step(self):
        """Advance queued detents by one count each; True when a new frame is due."""
        with self.lock:
            self._drain_edges()
            self._step_touch()
            if self.cooldown:
                self.cooldown -= 1
            for offset, remaining in list(self.pending.items()):
                if offset == panel_map.SELECTOR_COUNTER:
                    if self.cooldown:
                        continue
                    self.cooldown = self.SELECTOR_WINDOW_FRAMES
                direction = 1 if remaining > 0 else -1
                current = struct.unpack_from(">H", self.raw, offset)[0]
                self._set_word(offset, current + direction)
                if remaining == direction:
                    del self.pending[offset]
                else:
                    self.pending[offset] = remaining - direction
                self.changed = True
            due, self.changed = self.changed, False
            if due:
                self.hot = self.HOT_FRAMES
            elif self.hot:
                self.hot -= 1
            return due

    def frame(self):
        with self.lock:
            frame = bytearray(self.raw)
            for offset, byte in self.overrides.items():
                frame[offset] = byte
            return finish(frame)

    def adc_words(self):
        """The analog words the guest is currently sending in RX frames (overrides included),
        by control name plus the raw offset-keyed words, for research readback."""
        with self.lock:
            frame = bytearray(self.raw)
            for offset, byte in self.overrides.items():
                frame[offset] = byte
        words = {name: struct.unpack_from(">H", frame, off)[0]
                 for name, (off, _) in panel_map.ANALOG_WORDS.items()}
        words.update({f"0x{off:02x}": struct.unpack_from(">H", frame, off)[0]
                      for off in (0x26, 0x28, 0x2a)})
        return words

    def rx_state(self):
        """Research readback of the held input the guest is sending (hardware suite HW-13): button bytes 1..5 as
        hex and the pads whose pressure word is non-zero."""
        frame = self.frame()
        state = decode_rx(frame)
        if state is None:  # a research raw pin can make the frame not an RX frame (e.g. an out-of-range pot word)
            return {"buttons": frame[1:6].hex(), "pads": []}
        return {"buttons": frame[1:6].hex(),
                "pads": [index + 1 for index, pad in enumerate(state["pads"]) if pad["pressure"]]}

    def release_all(self):
        with self.lock:
            for byte in range(1, 6):
                self.raw[byte] = 0
            for index in range(16):
                self._set_word(panel_map.PAD_A_OFFSET + 2 * index, 0)
                self._set_word(panel_map.PAD_B_OFFSET + 2 * index, 0)
            self.held.clear()
            self.pending.clear()
            self.cooldown = self.hot = 0
            self.edges.clear()
            self.hold_frames.clear()
            self.overrides.clear()
            self.cdc_override = None
            self.cdc_contact, self.cdc_changed, self.changed = False, True, True
            self.touch_contact = False
            self.touch_hold = 0
            self.touch_target = None
            self.touch_raw = (0, 0)
            self._set_touch_words(0, 0)


class StartOff:
    """Presses toggle buttons after boot until their lamps are dark, so the firmware starts with them off.

    The bridge asks once per streamer period. The app's lamp frames are dark for the first seconds of a boot and only
    then show its start state, so darkness alone never ends the wait. A lamp that has stayed lit for SETTLE seconds is
    pressed; a press the app was not yet listening for leaves the lamp lit, so it is repeated every RETRY seconds, at
    most ATTEMPTS times. A control is dropped as soon as its lamp is dark after a press, or WINDOW seconds after the
    first lamp frame; from then on the user's own presses are never second-guessed.
    """

    SETTLE = 3.0
    RETRY = 2.5
    ATTEMPTS = 4
    WINDOW = 60.0

    def __init__(self, names):
        self.pending = {name: {"lit_since": None, "next": 0.0, "tries": 0} for name in names}
        self.started = None

    def step(self, now, lit):
        """`lit` maps a control name to True or False, or to None while its lamp state is unknown.
        Returns the names to press now."""
        press = []
        for name in list(self.pending):
            state = lit.get(name)
            if state is None:
                continue
            if self.started is None:
                self.started = now
            info = self.pending[name]
            if now - self.started > self.WINDOW:
                del self.pending[name]
            elif not state:
                info["lit_since"] = None
                if info["tries"]:
                    del self.pending[name]
            else:
                info["lit_since"] = now if info["lit_since"] is None else info["lit_since"]
                if now - info["lit_since"] >= self.SETTLE and now >= info["next"]:
                    if info["tries"] >= self.ATTEMPTS:
                        del self.pending[name]
                        continue
                    info["tries"] += 1
                    info["next"] = now + self.RETRY
                    press.append(name)
        return press
