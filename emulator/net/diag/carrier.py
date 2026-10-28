"""Identity-free LLDB network probes for OEM 1.60; import after attaching.

Default: observe only, stopping at each probe (enter `continue` to resume).
To test the carrier hypothesis in this disposable guest:
    script carrier.force_up = True
    continue

That explicit intervention changes only the GLINK helper's return register to 1,
after the real ioctl has executed. It is NOT validation of an ioctl shim or ARP.
Set force_up=False to remove it. No firmware bytes are saved or patched.
Print `script print(carrier.counts)` for totals. Output samples stop after 12 per
event; the probes keep counting/stopping. Start segment_watch.py before continuing.
The initial auto-continue experiment lost the target with Trace/breakpoint trap;
these probes deliberately leave continuation to the operator. UDP return probes
are provided for wave 3b and have not yet been exercised on a successful bind.
"""

import lldb

force_up = False
counts = {}
pending = {}
return_probes = set()
PROBES = {
    0x52A290: "GLINK_IOCTL_RETURN",
    0x52A408: "GLINK_HELPER_RETURN",
    0x5258A8: "AUTOIP_MESSAGE",
    0x50CD18: "OPERATION",
    0xFD78: "BIND",
    0x10744: "SENDTO",
}


def reg(frame, name):
    return frame.FindRegister(name).GetValueAsUnsigned()


def read(frame, address, size):
    error = lldb.SBError()
    data = frame.GetThread().GetProcess().ReadMemory(address, size, error)
    if error.Fail() or len(data) != size:
        raise RuntimeError(f"probe memory read failed at 0x{address:x}")
    return data


def emit(event, details):
    counts[event] = counts.get(event, 0) + 1
    if counts[event] <= 12:
        print(f"SP16_NET_DIAG {event} {details}", flush=True)


def returned(frame, _location, _internal):
    key = (frame.GetPC(), frame.GetThread().GetThreadID(), reg(frame, "sp"))
    event = pending.pop(key, None)
    if event is not None:
        emit(event, f"r0=0x{reg(frame, 'r0'):08x}")
    return event is not None


def track_return(frame, event):
    address = reg(frame, "lr")
    key = (address, frame.GetThread().GetThreadID(), reg(frame, "sp"))
    pending[key] = event
    if address not in return_probes:
        bp = frame.GetThread().GetProcess().GetTarget().BreakpointCreateByAddress(address)
        bp.SetScriptCallbackFunction(__name__ + ".returned")
        return_probes.add(address)


def hit(frame, _location, _internal):
    event = PROBES[frame.GetPC()]
    try:
        if event == "GLINK_IOCTL_RETURN":
            result = reg(frame, "r0")
            value = read(frame, reg(frame, "sp") + 0x20, 8)
            command = int.from_bytes(value[:4], "little")
            # On failure the output word is uninitialized, not a link value.
            data = str(int.from_bytes(value[4:], "little")) if result == 0 else "undefined"
            emit(event, f"r0=0x{result:08x} cmd={command} data={data}")
        elif event == "GLINK_HELPER_RETURN":
            emit(event, f"actual={reg(frame, 'r0')} force_up={force_up}")
            if force_up and not frame.FindRegister("r0").SetValueFromCString("1"):
                raise RuntimeError("could not set diagnostic carrier result")
        elif event == "AUTOIP_MESSAGE":
            emit(event, f"message=0x{reg(frame, 'r1'):x}")
        elif event == "OPERATION":
            operation = int.from_bytes(read(frame, reg(frame, "r1") + 0x20, 4), "little")
            emit(event, f"operation={operation}")
        elif event == "BIND":
            address = read(frame, reg(frame, "r1"), 4)
            if int.from_bytes(address[:2], "little") == 2:
                port = int.from_bytes(address[2:], "big")
                emit(event, f"port={port}")
                track_return(frame, f"BIND_{port}_RETURN")
        elif event == "SENDTO":
            size = reg(frame, "r2")
            if size >= 11 and read(frame, reg(frame, "r1"), 10) == b"Qspt1WmJOL":
                kind = read(frame, reg(frame, "r1") + 10, 1)[0]
                emit(event, f"prodjlink_type={kind} bytes={size}")
                track_return(frame, f"SENDTO_{size}_RETURN")
    except RuntimeError as error:
        print(f"SP16_NET_DIAG ERROR {error}", flush=True)
        return True
    return True


def __lldb_init_module(debugger, _internal):
    for address in PROBES:
        bp = debugger.GetSelectedTarget().BreakpointCreateByAddress(address)
        bp.SetScriptCallbackFunction(__name__ + ".hit")
    print("SP16_NET_DIAG carrier probes installed; force_up=False (observe only)")
