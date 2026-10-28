"""LLDB startup probes for the imported OEM 1.60 application (no binary changes).

Attach to a fresh --trace gdb session, then:
    command script import emulator/net/diag/startup.py
    continue

Each breakpoint logs once and auto-continues. Only control-flow addresses,
return values and singleton/guard state are read; no interface identity is read.
The final CONSTRUCTOR_RETURN line proves construction completed independently
of the guest bridge's intentional exclusion of OEM stdout from console.txt.
"""

import lldb


PROBES = {
    0x1BCE98: "SINGLETON_LOOKUP",
    0x522AA0: "CONSTRUCTOR_ENTER",
    0x522C44: "BANNER_CALL",
    0x522C48: "BANNER_RETURN",
    0x1C0CA0: "CONSTRUCTOR_RETURN",
}


def hit(frame, location, _internal):
    pc = frame.GetPC()
    label = PROBES[pc]
    fields = [f"SP16_NET_DIAG {label} pc=0x{pc:x}"]
    for name in ("r0", "lr"):
        fields.append(f"{name}=0x{frame.FindRegister(name).GetValueAsUnsigned():x}")
    if label == "SINGLETON_LOOKUP":
        process = frame.GetThread().GetProcess()
        for name, address, size in (("singleton", 0xF6E2A0, 4),
                                    ("guards", 0xF53C94, 2)):
            error = lldb.SBError()
            value = process.ReadUnsignedFromMemory(address, size, error)
            fields.append(f"{name}=0x{value:x}" if error.Success() else f"{name}=READ_ERROR")
    print(" ".join(fields), flush=True)
    location.GetBreakpoint().SetEnabled(False)
    return False


def __lldb_init_module(debugger, _internal):
    target = debugger.GetSelectedTarget()
    for address in PROBES:
        breakpoint = target.BreakpointCreateByAddress(address)
        breakpoint.SetScriptCallbackFunction(__name__ + ".hit")
    print("SP16_NET_DIAG installed five startup probes; continue the fresh guest")
