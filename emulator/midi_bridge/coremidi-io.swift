// CoreMIDI line bridge for the SP-16 emulator's MIDI controller bridge (emulator/midi_bridge).
//
// Talks to Python over stdin/stdout, one text line per message, so the bridge needs no pip package:
//   stdout  "in HEX..."   a MIDI message received from the chosen input ("in 90 24 64")
//           "ready"       the endpoints are open
//           "ports in:N out:N"-style lines for `list`
//   stdin   "out HEX..."  send a MIDI message to the chosen output
//           "burst HEX | HEX | ..."  send several messages in one packet list
//           "quit"
// Modes:
//   list                         print every source and destination
//   run --in NAME [--out NAME]   open the first source whose name contains NAME (case-insensitive); the optional
//                                destination receives "out" lines
//   decode HEX | HEX | ...       build one packet list from the messages and run the receive path on it, printing
//                                what it decodes (a self-test of the list walk, no CoreMIDI endpoint involved)
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

func emit(_ list: UnsafePointer<MIDIPacketList>) {
    // Walk the callback's own list: MIDIPacketNext must be given a pointer into it, never into a copy of a packet.
    for packet in list.unsafeSequence() {
        let length = Int(packet.pointee.length)
        let start = UnsafeRawPointer(packet) + MemoryLayout<MIDIPacket>.offset(of: \.data)!
        let data = Array(UnsafeBufferPointer(start: start.assumingMemoryBound(to: UInt8.self), count: length))
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
    }
}

// One MIDIPacketList holding a packet per message, in order.
func send(_ messages: [[UInt8]], _ send: (UnsafePointer<MIDIPacketList>) -> Void) {
    var buffer = [UInt8](repeating: 0, count: 4096)
    buffer.withUnsafeMutableBytes { raw in
        let list = raw.baseAddress!.assumingMemoryBound(to: MIDIPacketList.self)
        var packet = MIDIPacketListInit(list)
        for message in messages { packet = MIDIPacketListAdd(list, raw.count, packet, 0, message.count, message) }
        send(UnsafePointer(list))
    }
}

// "out HEX" is one message, "burst HEX | HEX | HEX" several in one packet list.
func messages(_ line: String) -> [[UInt8]]? {
    if line.hasPrefix("out ") { return [bytes(line.dropFirst(4))] }
    if line.hasPrefix("burst ") { return line.dropFirst(6).split(separator: "|").map { bytes($0) } }
    return nil
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
        if let destination = destination, let list = messages(line) { send(list) { MIDISend(outPort, destination, $0) } }
    }

case "decode":
    let given = CommandLine.arguments.dropFirst(2).joined(separator: " ")
    send(given.split(separator: "|").map { bytes($0) }) { emit($0) }
    exit(0)

case "virtual":
    let label = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "SP16 Test Controller"
    var source = MIDIEndpointRef()
    var destination = MIDIEndpointRef()
    MIDISourceCreate(client, label as CFString, &source)
    MIDIDestinationCreateWithBlock(client, label as CFString, &destination) { list, _ in emit(list) }
    print("ready")
    while let line = readLine() {
        if line == "quit" { break }
        if let list = messages(line) { send(list) { MIDIReceived(source, $0) } }
    }

default:
    fputs("usage: coremidi-io list | run --in NAME [--out NAME] | virtual NAME | decode HEX | HEX ...\n", stderr); exit(2)
}
