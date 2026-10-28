import AppKit
import CoreMIDI

/// MIDI routing preferences (pt-i44.37): an Ableton-style table of the CoreMIDI ports, with In/Out/Send-clock/
/// Receive-clock per port, a clock source picker, and a BPM/lock/jitter readout from the recovered clock. Every choice
/// is remembered by endpoint name + unique ID (so a re-plugged device keeps its routing) and persisted across
/// restarts; the same values are written as the bridge config the Python DIN/USB bridges read.
///
/// CoreMIDI splits a device into sources (the app reads them) and destinations (the app writes them), so a "port"
/// row is one source or one destination. A row's In and Receive-clock toggles apply to a source, its Out and
/// Send-clock toggles to a destination; the column is disabled for the kind it does not apply to.

struct MidiEndpoint: Equatable {
    let ref: MIDIEndpointRef
    let name: String
    let uniqueID: Int32
    let isSource: Bool
}

/// The bridge config the preferences window writes and the DIN/USB bridges read. Clock source is "internal" or the
/// decimal string of a source's unique ID. One entry per port the user has touched; untouched ports keep the bridge
/// default.
struct MidiRoutingConfig: Codable {
    var clockSource: String = "internal"
    var bpm: Double = 120.0
    var ports: [PortConfig] = []
    struct PortConfig: Codable {
        var name: String
        var uniqueID: Int32
        var isSource: Bool
        var inEnabled: Bool
        var outEnabled: Bool
        var sendClock: Bool
        var receiveClock: Bool
    }
}

final class MidiRoutingPreferences: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    /// The notify proc cannot capture self, so the live window is reachable through this (a single preferences window).
    private static var active: MidiRoutingPreferences?

    private let client: MIDIClientRef
    private var inputPort: MIDIPortRef = 0
    private var outputPort: MIDIPortRef = 0

    private var sources: [MidiEndpoint] = []
    private var destinations: [MidiEndpoint] = []
    /// Every endpoint (sources first, then destinations), the table's row order.
    private var rows: [MidiEndpoint] = []
    /// The persisted per-endpoint config, keyed by "src:ID" / "dst:ID".
    private var settings: [String: MidiRoutingConfig.PortConfig] = [:]

    private var sendThread: Thread?
    private var clockSource: String = "internal"          // "internal" or the decimal ID of a source
    private var nominalBPM: Double = 120.0

    /// Serializes the clock-send path: the clock thread reads `outputPort` and `clockDestinations` while the main
    /// thread swaps and disposes those ports in `rebuildClockRouting()` and mutates `settings` in `setConfig`/`reload()`.
    private let routingLock = NSLock()

    // Clock-recovery readout (a second-order DLL, the same loop as emulator/midi/clock.py).
    private var centre: Double?
    private var period: Double = 60.0 / (120.0 * 24.0)    // 120 BPM at 24 PPQN
    private var lockEvents = 0
    private let zeta = 1.0 / sqrt(2.0), loopHz = 2.0
    private var intervals: [Double] = []                  // recent inter-clock seconds, for the jitter readout
    private var lastClockTime: Double?
    private var readoutTimer: Timer?

    private let tableView = NSTableView()
    private let clockPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let bpmLabel = NSTextField(labelWithString: "—")
    private let lockLabel = NSTextField(labelWithString: "—")
    private let jitterLabel = NSTextField(labelWithString: "—")

    private var clockDestinations: [MidiEndpoint] {
        destinations.filter { self.config(for: $0).sendClock }
    }
    private var clockSourceEndpoint: MidiEndpoint? {
        guard clockSource != "internal" else { return nil }
        return sources.first { String($0.uniqueID) == clockSource }
    }

    // MARK: Setup

    init() {
        var c: MIDIClientRef = 0
        MIDIClientCreate("SP-16 MIDI Routing" as CFString, MidiRoutingPreferences.notify, nil, &c)
        client = c
        super.init(window: nil)
        MidiRoutingPreferences.active = self
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 420),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.title = "MIDI Routing"
        window.contentView = buildContent()
        self.window = window
        loadPersisted()
        reload()
        startClock()
        readoutTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.refreshReadout()
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not implemented") }

    deinit {
        readoutTimer?.invalidate()
        stopClock()
        routingLock.lock()
        let inPort = inputPort
        let outPort = outputPort
        inputPort = 0
        outputPort = 0
        routingLock.unlock()
        if inPort != 0 { MIDIPortDispose(inPort) }
        if outPort != 0 { MIDIPortDispose(outPort) }
        MIDIClientDispose(client)
        if MidiRoutingPreferences.active === self { MidiRoutingPreferences.active = nil }
    }

    private static let notify: MIDINotifyProc = { _, _ in
        DispatchQueue.main.async {
            // Hot-plug: any endpoint add/remove re-reads the list; the table and clock picker follow.
            MidiRoutingPreferences.active?.reload()
        }
    }

    private func buildContent() -> NSView {
        let columns: [(String, CGFloat)] = [("Port", 260), ("In", 50), ("Out", 50), ("Send clock", 90), ("Receive clock", 100)]
        for (index, spec) in columns.enumerated() {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("col\(index)"))
            column.title = spec.0
            column.width = spec.1
            if index > 0 {
                let cell = NSButtonCell()
                cell.setButtonType(.switch)
                cell.controlSize = .small
                cell.title = ""
                cell.target = self
                cell.action = #selector(toggle(_:))
                cell.tag = index
                column.dataCell = cell
            }
            tableView.addTableColumn(column)
        }
        tableView.delegate = self
        tableView.dataSource = self
        tableView.rowHeight = 22
        tableView.usesAlternatingRowBackgroundColors = true

        let scroll = NSScrollView()
        scroll.documentView = tableView
        scroll.hasVerticalScroller = true

        let clockTitle = NSTextField(labelWithString: "Clock source:")
        clockPopup.target = self
        clockPopup.action = #selector(clockSourceChosen(_:))

        let bpmTitle = NSTextField(labelWithString: "BPM:")
        let lockTitle = NSTextField(labelWithString: "Lock:")
        let jitterTitle = NSTextField(labelWithString: "Jitter:")
        bpmLabel.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        lockLabel.font = bpmLabel.font
        jitterLabel.font = bpmLabel.font

        let readout = NSStackView(views: [clockTitle, clockPopup, bpmTitle, bpmLabel, lockTitle, lockLabel,
                                          jitterTitle, jitterLabel])
        readout.orientation = .horizontal
        readout.spacing = 8
        readout.alignment = .centerY

        let stack = NSStackView(views: [scroll, readout])
        stack.orientation = .vertical
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        return stack
    }

    // MARK: Endpoints

    private func endpointName(_ ref: MIDIEndpointRef, uniqueID: inout Int32) -> String {
        var value: Unmanaged<CFString>?
        MIDIObjectGetStringProperty(ref, kMIDIPropertyDisplayName, &value)
        let name = (value?.takeRetainedValue() as String?) ?? ""
        var id: Int32 = 0
        MIDIObjectGetIntegerProperty(ref, kMIDIPropertyUniqueID, &id)
        uniqueID = id
        return name.isEmpty ? "Endpoint \(id)" : name
    }

    func reload() {
        var src: [MidiEndpoint] = []
        var dst: [MidiEndpoint] = []
        var id: Int32 = 0
        for i in 0..<MIDIGetNumberOfSources() {
            let ref = MIDIGetSource(i)
            src.append(MidiEndpoint(ref: ref, name: endpointName(ref, uniqueID: &id), uniqueID: id, isSource: true))
        }
        for i in 0..<MIDIGetNumberOfDestinations() {
            let ref = MIDIGetDestination(i)
            dst.append(MidiEndpoint(ref: ref, name: endpointName(ref, uniqueID: &id), uniqueID: id, isSource: false))
        }
        // Remembered ports keep their settings across a replug by unique ID; a brand-new port starts off.
        routingLock.lock()
        for endpoint in src + dst where settings[key(endpoint)] == nil {
            settings[key(endpoint)] = blank(endpoint)
        }
        sources = src
        destinations = dst
        rows = src + dst
        routingLock.unlock()
        tableView.reloadData()
        rebuildClockPicker()
        rebuildClockRouting()
    }

    private func key(_ endpoint: MidiEndpoint) -> String { "\(endpoint.isSource ? "src" : "dst"):\(endpoint.uniqueID)" }

    private func blank(_ endpoint: MidiEndpoint) -> MidiRoutingConfig.PortConfig {
        MidiRoutingConfig.PortConfig(name: endpoint.name, uniqueID: endpoint.uniqueID, isSource: endpoint.isSource,
                                     inEnabled: false, outEnabled: false, sendClock: false, receiveClock: false)
    }

    func config(for endpoint: MidiEndpoint) -> MidiRoutingConfig.PortConfig {
        settings[key(endpoint)] ?? blank(endpoint)
    }

    private func setConfig(_ value: MidiRoutingConfig.PortConfig, for endpoint: MidiEndpoint) {
        routingLock.lock()
        settings[key(endpoint)] = value
        routingLock.unlock()
        persist()
    }

    // MARK: Clock

    private func startClock() {
        stopClock()
        let thread = Thread { [weak self] in self?.clockLoop() }
        thread.name = "midi-routing-clock"
        thread.qualityOfService = .userInteractive
        sendThread = thread
        thread.start()
    }

    private func stopClock() {
        sendThread?.cancel()
        sendThread = nil
    }

    private func clockLoop() {
        while !Thread.current.isCancelled {
            if clockSource == "internal" {
                sendClockByte()
                Thread.sleep(forTimeInterval: clockPeriodSeconds(nominalBPM))
            } else {
                Thread.sleep(forTimeInterval: 0.05)
            }
        }
    }

    private func clockPeriodSeconds(_ bpm: Double) -> Double { 60.0 / (bpm * 24.0) }

    private func sendClockByte() {
        // Take a snapshot of the port and the destination list under the same lock that rebuildClockRouting() uses to
        // swap and dispose ports, and hold it across MIDISend so a send can never race a MIDIPortDispose on the old port.
        routingLock.lock()
        defer { routingLock.unlock() }
        let port = outputPort
        let destinations = clockDestinations
        guard port != 0, !destinations.isEmpty else { return }
        var clock = [UInt8(0xF8)]
        var buffer = [UInt8](repeating: 0, count: 256)
        let list = buffer.withUnsafeMutableBytes { raw -> MIDIPacketList in
            let listPtr = raw.baseAddress!.assumingMemoryBound(to: MIDIPacketList.self)
            let packet = MIDIPacketListInit(listPtr)
            _ = MIDIPacketListAdd(listPtr, raw.count, packet, 0, clock.count, &clock)
            return listPtr.pointee
        }
        var copy = list
        for endpoint in destinations {
            MIDISend(port, endpoint.ref, &copy)
        }
    }

    private func rebuildClockRouting() {
        routingLock.lock()
        let oldInputPort = inputPort
        let oldOutputPort = outputPort
        inputPort = 0
        outputPort = 0
        MIDIOutputPortCreate(client, "SP-16 Clock Out" as CFString, &outputPort)
        if let source = clockSourceEndpoint {
            MIDIInputPortCreateWithBlock(client, "SP-16 Clock In" as CFString, &inputPort) { [weak self] list, _ in
                guard let self = self else { return }
                for packet in list.unsafeSequence() {
                    let length = Int(packet.pointee.length)
                    let start = UnsafeRawPointer(packet) + MemoryLayout<MIDIPacket>.offset(of: \.data)!
                    let data = Array(UnsafeBufferPointer(start: start.assumingMemoryBound(to: UInt8.self), count: length))
                    for byte in data where byte == 0xF8 {
                        self.onClockByte(at: ProcessInfo.processInfo.systemUptime)
                    }
                }
            }
            MIDIPortConnectSource(inputPort, source.ref, nil)
        }
        routingLock.unlock()
        // Dispose the old ports only after the swap, so no send (which runs under the lock) can still use them.
        if oldInputPort != 0 { MIDIPortDispose(oldInputPort) }
        if oldOutputPort != 0 { MIDIPortDispose(oldOutputPort) }
    }

    private func onClockByte(at t: Double) {
        if lastClockTime == nil { centre = t } else {
            lockEvents += 1
            if centre == nil { centre = t } else {
                let e = t - (centre! + period)
                centre! += period + 2 * zeta * loopHz * period * e
                period += (loopHz * period) * (loopHz * period) * e
            }
            intervals.append(abs(t - lastClockTime!))
            if intervals.count > 96 { intervals.removeFirst() }
        }
        lastClockTime = t
        DispatchQueue.main.async { [weak self] in self?.refreshReadout() }
    }

    private func refreshReadout() {
        if clockSource == "internal" {
            bpmLabel.stringValue = String(format: "%.1f", nominalBPM)
            lockLabel.stringValue = "internal"
            jitterLabel.stringValue = "—"
        } else {
            bpmLabel.stringValue = String(format: "%.1f", 60.0 / (period * 24.0))
            lockLabel.stringValue = lockEvents >= 8 ? "locked" : "unlocked"
            let jitter = intervals.isEmpty ? 0 : intervals.reduce(0, +) / Double(intervals.count)
            jitterLabel.stringValue = String(format: "%.0f µs", jitter * 1_000_000)
        }
    }

    private func rebuildClockPicker() {
        clockPopup.removeAllItems()
        clockPopup.addItem(withTitle: "Internal")
        for source in sources {
            let item = NSMenuItem(title: source.name, action: nil, keyEquivalent: "")
            item.representedObject = String(source.uniqueID)
            clockPopup.menu?.addItem(item)
        }
        clockPopup.selectItem(at: 0)
        if let source = clockSourceEndpoint,
           let index = clockPopup.itemArray.firstIndex(where: { ($0.representedObject as? String) == String(source.uniqueID) }) {
            clockPopup.selectItem(at: index)
        }
    }

    // MARK: Persistence

    private static let defaultsKey = "midi-routing"
    private static let configURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("SP-16 Emulator/midi-routing.json")

    private func loadPersisted() {
        if let data = UserDefaults.standard.data(forKey: Self.defaultsKey),
           let stored = try? JSONDecoder().decode([String: MidiRoutingConfig.PortConfig].self, from: data) {
            settings = stored
        }
        if let src = UserDefaults.standard.string(forKey: "midi-routing.clockSource") { clockSource = src }
        let bpm = UserDefaults.standard.double(forKey: "midi-routing.bpm")
        if bpm > 0 { nominalBPM = bpm }
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(settings) {
            UserDefaults.standard.set(data, forKey: Self.defaultsKey)
        }
        UserDefaults.standard.set(clockSource, forKey: "midi-routing.clockSource")
        UserDefaults.standard.set(nominalBPM, forKey: "midi-routing.bpm")
        writeBridgeConfig()
    }

    /// The bridge config the Python DIN/USB bridges read: clock source, tempo, and every touched port.
    func writeBridgeConfig() {
        let config = MidiRoutingConfig(clockSource: clockSource, bpm: nominalBPM, ports: Array(settings.values))
        guard let data = try? JSONEncoder().encode(config) else { return }
        try? FileManager.default.createDirectory(at: Self.configURL.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try? data.write(to: Self.configURL, options: .atomic)
    }

    // MARK: Table view

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(_ tableView: NSTableView, objectValueFor tableColumn: NSTableColumn?, row: Int) -> Any? {
        guard let column = tableColumn, row < rows.count else { return nil }
        let endpoint = rows[row]
        let c = config(for: endpoint)
        switch column.identifier.rawValue {
        case "col0": return endpoint.name
        case "col1": return endpoint.isSource ? NSNumber(value: c.inEnabled) : nil
        case "col2": return endpoint.isSource ? nil : NSNumber(value: c.outEnabled)
        case "col3": return endpoint.isSource ? nil : NSNumber(value: c.sendClock)
        case "col4": return endpoint.isSource ? NSNumber(value: c.receiveClock) : nil
        default: return nil
        }
    }

    func tableView(_ tableView: NSTableView, shouldEdit tableColumn: NSTableColumn?, row: Int) -> Bool {
        guard let column = tableColumn, row < rows.count else { return false }
        let endpoint = rows[row]
        switch column.identifier.rawValue {
        case "col1", "col4": return endpoint.isSource      // In / Receive clock apply to sources
        case "col2", "col3": return !endpoint.isSource     // Out / Send clock apply to destinations
        default: return false
        }
    }

    @objc private func toggle(_ sender: NSButtonCell) {
        let row = tableView.clickedRow
        guard row >= 0, row < rows.count else { return }
        let endpoint = rows[row]
        var c = config(for: endpoint)
        let on = sender.state == .on
        switch sender.tag {
        case 1: c.inEnabled = on
        case 2: c.outEnabled = on
        case 3: c.sendClock = on
        case 4: c.receiveClock = on
        default: break
        }
        setConfig(c, for: endpoint)
        if sender.tag == 3 { rebuildClockRouting() }
    }

    @objc private func clockSourceChosen(_ sender: NSPopUpButton) {
        clockSource = (sender.selectedItem?.representedObject as? String) ?? "internal"
        persist()
        rebuildClockRouting()
    }
}
