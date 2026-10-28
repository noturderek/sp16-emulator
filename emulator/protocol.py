"""Bounded binary messages for the private native-window virtio channel.

Four-byte network-order body length, one-byte kind, then payload.
FRAME: >HHI width/height/sequence followed by packed BGRX pixels.
TOUCH: >HHB x/y/action (0 move, 1 down, 2 up).
ACK: >I event sequence. Input latency is measured independently of rendering.
WHEEL: >HHh x/y/signed steps (positive scrolls down), bounded to 12.
KEY: one-byte navigation key index.
CONTROL: >BBhI control id/action/value/sequence (host -> guest). Actions: 0 release,
  1 press, 2 signed detent delta, 3 value/move. Pads: press value is velocity, strip
  value is position 0..1000. The guest answers with INPUT_RESULT, never ACK.
INPUT_RESULT: >IB event sequence/status (guest -> host). Status is delivery to the
  guest panel model, not proof that the OEM application acted on the input.
FEEDBACK: >IBH generation/flags/count then count x (>BBBB lamp id, r, g, b). Flag 1 =
  live firmware output has been decoded; entries only cover lamps with a proven mapping.
RESEARCH: one-byte operation then payload (host research socket only). 1 = raw frame
  bytes (offset, data), 2 = raw CDC bytes (4), 3 = clear raw overrides, 4 = TX log on/off,
  5 = RX frame period in milliseconds (1..50), 6 = USB over-current input (0 idle, 1 asserted).
TX_FRAME: >BBB then up to 128 bytes: multi/part id/part count of an OEM panel TX frame
  (guest -> host, research logging only).
STATS: UTF-8 JSON object of guest panel counters (guest -> host, about 1 Hz).
"""
import struct

FRAME, TOUCH, ACK, WHEEL, KEY, CONTROL, INPUT_RESULT, FEEDBACK, RESEARCH, TX_FRAME, STATS = range(1, 12)
INPUT_OK, INPUT_UNSUPPORTED, INPUT_FAILED, INPUT_INVALID = range(4)
CONTROL_ACTIONS = ("release", "press", "delta", "value")
MAX_FEEDBACK_LAMPS = 128
MAX_BODY = 800 * 480 * 4 + 9
ACTIONS = ("move", "down", "up")
KEYS = ("Up", "Down", "Left", "Right", "Return", "Escape")


def message(kind, payload):
    body = bytes([kind]) + payload
    if len(body) > MAX_BODY:
        raise ValueError("native message exceeds the display bound")
    return struct.pack(">I", len(body)) + body


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError("native bridge disconnected")
        result.extend(part)
    return bytes(result)


def receive(stream):
    size = struct.unpack(">I", read_exact(stream, 4))[0]
    if not 1 <= size <= MAX_BODY:
        raise ValueError("invalid native message size")
    body = read_exact(stream, size)
    return body[0], body[1:]


def decode_touch(payload):
    if len(payload) != 5:
        raise ValueError("invalid native touch size")
    x, y, action = struct.unpack(">HHB", payload)
    if x >= 800 or y >= 480 or action >= len(ACTIONS):
        raise ValueError("invalid native touch coordinates/action")
    return x, y, ACTIONS[action]


def decode_wheel(payload):
    if len(payload) != 6:
        raise ValueError("invalid native wheel size")
    x, y, steps = struct.unpack(">HHh", payload)
    if x >= 800 or y >= 480 or not 0 < abs(steps) <= 12:
        raise ValueError("invalid native wheel coordinates/steps")
    return x, y, steps


def decode_key(payload):
    if len(payload) != 1 or payload[0] >= len(KEYS):
        raise ValueError("invalid native navigation key")
    return KEYS[payload[0]]


def decode_control(payload):
    if len(payload) != 8:
        raise ValueError("invalid native control size")
    control, action, value, sequence = struct.unpack(">BBhI", payload)
    if action >= len(CONTROL_ACTIONS) or control == 0:
        raise ValueError("invalid native control id/action")
    return control, CONTROL_ACTIONS[action], value, sequence


def encode_control(control, action, value, sequence):
    return message(CONTROL, struct.pack(">BBhI", control, CONTROL_ACTIONS.index(action), value, sequence))


def encode_result(sequence, status):
    return message(INPUT_RESULT, struct.pack(">IB", sequence, status))


def encode_feedback(generation, live, lamps):
    """lamps: mapping lamp id -> (r, g, b); ordered for stable wire bytes."""
    if len(lamps) > MAX_FEEDBACK_LAMPS:
        raise ValueError("too many feedback lamps")
    body = struct.pack(">IBH", generation & 0xffffffff, 1 if live else 0, len(lamps))
    for lamp in sorted(lamps):
        r, g, b = lamps[lamp]
        body += bytes((lamp, r, g, b))
    return message(FEEDBACK, body)


def decode_feedback(payload):
    generation, flags, count = struct.unpack_from(">IBH", payload)
    if count > MAX_FEEDBACK_LAMPS or len(payload) != 7 + 4 * count:
        raise ValueError("invalid feedback size")
    lamps = {payload[7 + 4 * i]: tuple(payload[8 + 4 * i:11 + 4 * i]) for i in range(count)}
    return generation, bool(flags & 1), lamps


def decode_research(payload):
    if not payload:
        raise ValueError("empty research message")
    operation, data = payload[0], payload[1:]
    if operation == 1 and 2 <= len(data) <= 129 and data[0] + len(data) - 1 <= 128:
        return "raw", data[0], data[1:]
    if operation == 2 and len(data) == 4:
        return "cdc", 0, data
    if operation == 3 and not data:
        return "clear", 0, b""
    if operation == 4 and len(data) == 1 and data[0] in (0, 1):
        return "txlog", data[0], b""
    if operation == 5 and len(data) == 1 and 1 <= data[0] <= 50:
        return "period", data[0], b""
    if operation == 6 and len(data) == 1 and data[0] in (0, 1):
        return "usb-oc", data[0], b""
    raise ValueError("invalid research operation")


def encode_stats(counters):
    import json
    body = json.dumps(counters, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > 2048:  # the board-model report added about 200 bytes to the roughly 700 of the panel counters
        raise ValueError("guest stats too large")
    return message(STATS, body)


def frame_pixels(source):
    """Extract Xvfb's bounded BGRX framebuffer, with explicit layout checks."""
    if len(source) < 100 or len(source) > 2 * 1024 * 1024:
        raise ValueError("invalid native XWD size")
    fields = struct.unpack_from(">25I", source)
    header, version, fmt, depth, width, height, xoffset, order = fields[:8]
    bpp, stride, visual, red, green, blue = fields[11:17]
    colors = fields[19]
    if (version, fmt, depth, width, height, xoffset, order, bpp, stride,
            visual, red, green, blue) != (7, 2, 24, 800, 480, 0, 0, 32,
                                        3200, 4, 0xff0000, 0xff00, 0xff):
        raise ValueError("unsupported native framebuffer layout")
    start = header + colors * 12
    end = start + stride * height
    if header < 100 or end > len(source):
        raise ValueError("truncated native framebuffer")
    return bytes(source[start:end])
