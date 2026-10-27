// CoreMIDI line bridge for the SP-16 emulator's MIDI controller bridge (emulator/midi_bridge).
//
// Talks to Python over stdin/stdout, one text line per message, so the bridge needs no pip package:
//   stdout  "in HEX..."   a MIDI message received from the chosen input ("in 90 24 64")
//           "ready"       the endpoints are open
//           "ports in:N out:N"-style lines for `list`
//   stdin   "out HEX..."  send a MIDI message to the chosen output
//           "quit"
// Modes:
//   list                         print every source and destination
//   run --in NAME [--out NAME]   open the first source whose name contains NAME (case-insensitive); the optional
//                                destination receives "out" lines
//   virtual NAME                 create a virtual source and destination called NAME: what "out" writes comes out of
//                                the source, what arrives at the destination is printed as "in" (a stand-in
//                                controller for tests, without hardware)
import CoreMIDI
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)

func name(_ object: MIDIObjectRef) -> String {
    var value: Unmanaged<CFString>?
    MIDIObjectGetStringProperty(object, kMIDIPropertyDisplayName, &value)
    return (value?.takeRetainedValue() as String?) ?? ""
}

func bytes(_ hex: Substring) -> [UInt8] { hex.split(separator: " ").compactMap { UInt8($0, radix: 16) } }

func emit(_ packets: UnsafePointer<MIDIPacketList>) {
    var packet = packets.pointee.packet
    for _ in 0..<packets.pointee.numPackets {
        let length = Int(packet.length)
        let data = withUnsafeBytes(of: packet.data) { Array($0.prefix(length)) }
        // Split a packet into messages: a status byte starts one; running status is not expanded (CoreMIDI does not
        // send it). Realtime bytes (>= 0xF8) are single messages.
        var message: [UInt8] = []
        func flush() { if !message.isEmpty { print("in " + message.map { String(format: "%02x", $0) }.joined(separator: " ")); message = [] } }
        for byte in data {
            if byte >= 0xF8 { flush(); print("in " + String(format: "%02x", byte)); continue }
            if byte & 0x80 != 0 { flush() }
            message.append(byte)
        }
        flush()
        packet = MIDIPacketNext(&packet).pointee
    }
}

func send(_ message: [UInt8], _ send: (UnsafePointer<MIDIPacketList>) -> Void) {
    var buffer = [UInt8](repeating: 0, count: 1024)
    buffer.withUnsafeMutableBytes { raw in
        let list = raw.baseAddress!.assumingMemoryBound(to: MIDIPacketList.self)
        let packet = MIDIPacketListInit(list)
        _ = MIDIPacketListAdd(list, 1024, packet, 0, message.count, message)
        send(UnsafePointer(list))
    }
}

func option(_ flag: String) -> String? {
    guard let at = CommandLine.arguments.firstIndex(of: flag), at + 1 < CommandLine.arguments.count else { return nil }
    return CommandLine.arguments[at + 1]
}

let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "list"
var client = MIDIClientRef()
MIDIClientCreate("sp16-midi-bridge" as CFString, nil, nil, &client)

switch mode {
case "list":
    for i in 0..<MIDIGetNumberOfSources() { print("source \(name(MIDIGetSource(i)))") }
    for i in 0..<MIDIGetNumberOfDestinations() { print("destination \(name(MIDIGetDestination(i)))") }
    exit(0)

case "run":
    guard let want = option("--in") else { fputs("run needs --in NAME\n", stderr); exit(2) }
    func find(_ count: Int, _ get: (Int) -> MIDIEndpointRef, _ wanted: String) -> MIDIEndpointRef? {
        for i in 0..<count where name(get(i)).range(of: wanted, options: .caseInsensitive) != nil { return get(i) }
        return nil
    }
    guard let source = find(MIDIGetNumberOfSources(), { MIDIGetSource($0) }, want) else {
        fputs("no MIDI source matches \"\(want)\"\n", stderr); exit(3)
    }
    var port = MIDIPortRef()
    MIDIInputPortCreateWithBlock(client, "in" as CFString, &port) { list, _ in emit(list) }
    MIDIPortConnectSource(port, source, nil)
    var destination: MIDIEndpointRef? = nil
    var outPort = MIDIPortRef()
    if let out = option("--out") {
        destination = find(MIDIGetNumberOfDestinations(), { MIDIGetDestination($0) }, out)
        if destination == nil { fputs("no MIDI destination matches \"\(out)\"\n", stderr); exit(3) }
        MIDIOutputPortCreate(client, "out" as CFString, &outPort)
    }
    print("ready")
    while let line = readLine() {
        if line == "quit" { break }
        if line.hasPrefix("out "), let destination = destination {
            send(bytes(line.dropFirst(4))) { MIDISend(outPort, destination, $0) }
        }
    }

case "virtual":
    let label = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "SP16 Test Controller"
    var source = MIDIEndpointRef()
    var destination = MIDIEndpointRef()
    MIDISourceCreate(client, label as CFString, &source)
    MIDIDestinationCreateWithBlock(client, label as CFString, &destination) { list, _ in emit(list) }
    print("ready")
    while let line = readLine() {
        if line == "quit" { break }
        if line.hasPrefix("out ") { send(bytes(line.dropFirst(4))) { MIDIReceived(source, $0) } }
    }

default:
    fputs("usage: coremidi-io list | run --in NAME [--out NAME] | virtual NAME\n", stderr); exit(2)
}
