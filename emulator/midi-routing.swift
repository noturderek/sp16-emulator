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

/// The bridge config the preferences window writes and the Python DIN/USB bridges read (`emulator/midi/routing.py`):
/// the clock source ("internal" or the decimal string of a source's unique ID), the tempo the window's own clock
/// generator sends, and one entry per port the user has toggled. A port the user has not touched is *absent*, which
/// the bridges read as "carry everything" — the same as no preferences file at all. The bridges match an entry by
/// `name`, the label they gave their own virtual endpoint, and never by `uniqueID` (the system assigns that and a
/// bridge cannot know it before its port exists); `uniqueID` is what this window remembers a re-plugged device by.
/// A row is one CoreMIDI endpoint, so its toggles apply where its kind is: In/Receive clock on a source (the host
/// reads it), Out/Send clock on a destination (the host writes it).
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
    private var sendThread: Thread?
    private var readoutTimer: Timer?

    /// One owner for every piece of mutable routing and clock state in this class: `stateLock`. Every access —
    /// reads included — takes this lock, from the main thread (UI actions, `reload`, the readout), the clock thread
    /// (`clockLoop` → `sendClockByte`) and the CoreMIDI input callback (`onClockByte`). `client` is set once in
    /// `init` and never mutated, so it stays outside the lock; `sendThread` and `readoutTimer` are main-thread only.
    private let stateLock = NSLock()

    // Routing state (owned by stateLock).
    private var inputPort: MIDIPortRef = 0
    private var outputPort: MIDIPortRef = 0
    private var sources: [MidiEndpoint] = []
    private var destinations: [MidiEndpoint] = []
    /// Every endpoint (sources first, then destinations), the table's row order.
    private var rows: [MidiEndpoint] = []
    /// The persisted per-endpoint config, keyed by "src:ID" / "dst:ID".
    private var settings: [String: MidiRoutingConfig.PortConfig] = [:]
    /// The keys whose toggles the user has actually changed. Only these are written to the bridge config: `reload`
    /// gives every visible endpoint a blank entry, so writing all of `settings` would tell the bridges that every
    /// port is switched off the first time this window opens.
    private var touched: Set<String> = []

    // Clock state (owned by stateLock).
    private var clockSource: String = "internal"          // "internal" or the decimal ID of a source
    private var nominalBPM: Double = 120.0

    // Clock-recovery readout (a second-order DLL, the same loop as emulator/midi/clock.py), owned by stateLock.
    private var centre: Double?
    private var period: Double = 60.0 / (120.0 * 24.0)    // 120 BPM at 24 PPQN
    private var lockEvents = 0
    /// Damping and loop frequency. `loopHz` is the loop's frequency in Hz (clock.py's `loop_hz`); the gains below
    /// convert it to rad/s.
    private let zeta = 1.0 / sqrt(2.0), loopHz = 2.0
    private let jitterWindow = 96
    private var intervals: [Double] = []                  // recent inter-clock seconds, for the jitter deviation
    private var lastClockTime: Double?

    private let tableView = NSTableView()
    private let clockPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let bpmLabel = NSTextField(labelWithString: "—")
    private let lockLabel = NSTextField(labelWithString: "—")
    private let jitterLabel = NSTextField(labelWithString: "—")

    private var clockDestinations: [MidiEndpoint] {
        destinations.filter { settings[key($0)]?.sendClock == true }
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
        stopClock()   // cancels the clock thread and waits for it, so no send can race the disposals below
        stateLock.lock()
        let inPort = inputPort
        let outPort = outputPort
        inputPort = 0
        outputPort = 0
        stateLock.unlock()
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
        stateLock.lock()
        for endpoint in src + dst where settings[key(endpoint)] == nil {
            settings[key(endpoint)] = blank(endpoint)
        }
        sources = src
        destinations = dst
        rows = src + dst
        stateLock.unlock()
        tableView.reloadData()
        rebuildClockPicker()
        rebuildClockRouting()
    }

    private func key(_ endpoint: MidiEndpoint) -> String { "\(endpoint.isSource ? "src" : "dst"):\(endpoint.uniqueID)" }

    private func blank(_ endpoint: MidiEndpoint) -> MidiRoutingConfig.PortConfig {
        MidiRoutingConfig.PortConfig(name: endpoint.name, uniqueID: endpoint.uniqueID, isSource: endpoint.isSource,
                                     inEnabled: false, outEnabled: false, sendClock: false, receiveClock: false)
    }

    private func setConfig(_ value: MidiRoutingConfig.PortConfig, for endpoint: MidiEndpoint) {
        stateLock.lock()
        settings[key(endpoint)] = value
        touched.insert(key(endpoint))
        stateLock.unlock()
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

    /// Cancels the clock thread and waits for it to finish, so `deinit` can dispose the ports knowing no send is
    /// still running. The loop checks `isCancelled` after every sleep (≤ ~50 ms), so this returns promptly.
    private func stopClock() {
        let thread = sendThread
        sendThread = nil
        guard let thread else { return }
        thread.cancel()
        while !thread.isFinished {
            Thread.sleep(forTimeInterval: 0.005)
        }
    }

    private func clockLoop() {
        while !Thread.current.isCancelled {
            stateLock.lock()
            let isInternal = clockSource == "internal"
            let bpm = nominalBPM
            stateLock.unlock()
            if isInternal {
                sendClockByte()
                Thread.sleep(forTimeInterval: clockPeriodSeconds(bpm))
            } else {
                Thread.sleep(forTimeInterval: 0.05)
            }
        }
    }

    private func clockPeriodSeconds(_ bpm: Double) -> Double { 60.0 / (bpm * 24.0) }

    private func sendClockByte() {
        // Take a snapshot of the port and the destination list under the same lock that rebuildClockRouting() uses to
        // swap and dispose ports, and hold it across MIDISend so a send can never race a MIDIPortDispose on the old port.
        stateLock.lock()
        defer { stateLock.unlock() }
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
        stateLock.lock()
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
        stateLock.unlock()
        // Dispose the old ports only after the swap, so no send (which runs under the lock) can still use them.
        if oldInputPort != 0 { MIDIPortDispose(oldInputPort) }
        if oldOutputPort != 0 { MIDIPortDispose(oldOutputPort) }
    }

    /// One DLL update per clock byte, the same second-order loop as `emulator/midi/clock.py`:
    ///
    ///     wn    = 2 * pi * loopHz              natural frequency, rad/s (clock.py: `wn = 2 * pi * loop_hz`)
    ///     b     = 2 * zeta * wn * t0           phase (proportional) gain
    ///     c     = (wn * t0) ** 2               period (integral) gain
    ///     e     = t - (centre + period)        phase error: this arrival against the prediction
    ///     centre += period + b * e             the loop's output time
    ///     period += c * e                      the recovered clock period
    ///
    /// The gains are normalised to the *nominal* period `t0 = clockPeriodSeconds(nominalBPM)`, not to the recovered
    /// one, so they stay put while the loop tracks a tempo away from nominal — exactly as clock.py builds them once
    /// from `nominal_bpm`. `loopHz` is a frequency in Hz and only becomes rad/s through `wn`.
    private func onClockByte(at t: Double) {
        // Runs on the CoreMIDI input thread; takes the state lock so the readout mutation is atomic against
        // `refreshReadout` on the main thread.
        stateLock.lock()
        defer { stateLock.unlock() }
        if lastClockTime == nil { centre = t } else {
            lockEvents += 1
            if centre == nil { centre = t } else {
                let wn = 2.0 * Double.pi * loopHz
                let t0 = clockPeriodSeconds(nominalBPM)
                let b = 2.0 * zeta * wn * t0
                let c = (wn * t0) * (wn * t0)
                let e = t - (centre! + period)
                centre! += period + b * e
                period += c * e
            }
            intervals.append(abs(t - lastClockTime!))
            if intervals.count > jitterWindow { intervals.removeFirst() }
        }
        lastClockTime = t
        DispatchQueue.main.async { [weak self] in self?.refreshReadout() }
    }

    private func refreshReadout() {
        stateLock.lock()
        let isInternal = clockSource == "internal"
        let bpm = nominalBPM
        let periodValue = period
        let lockValue = lockEvents
        let intervalsSnapshot = intervals
        stateLock.unlock()
        if isInternal {
            bpmLabel.stringValue = String(format: "%.1f", bpm)
            lockLabel.stringValue = "internal"
            jitterLabel.stringValue = "—"
        } else {
            bpmLabel.stringValue = String(format: "%.1f", 60.0 / (periodValue * 24.0))
            lockLabel.stringValue = lockValue >= 8 ? "locked" : "unlocked"
            // Jitter is the *deviation* of the clock interval, not its mean: the mean of a 120 BPM stream is 20.8 ms
            // (one 24th of a beat) however steady or wobbly the clock is, so it reports nothing. This is the sample
            // standard deviation sqrt(sum((x - mean)^2) / (n - 1)) of the recent intervals: 0 us for a perfectly
            // steady clock, growing with the wobble around the beat.
            jitterLabel.stringValue = String(format: "%.0f µs", sampleDeviation(intervalsSnapshot) * 1_000_000)
        }
    }

    /// The sample standard deviation of `values` (n - 1 denominator, in the same unit as the values); 0 below two
    /// samples, where a deviation is not defined.
    private func sampleDeviation(_ values: [Double]) -> Double {
        guard values.count > 1 else { return 0 }
        let mean = values.reduce(0, +) / Double(values.count)
        let squares = values.reduce(0.0) { $0 + ($1 - mean) * ($1 - mean) }
        return (squares / Double(values.count - 1)).squareRoot()
    }

    private func rebuildClockPicker() {
        stateLock.lock()
        let snapshotSources = sources
        let selectedID = clockSource
        stateLock.unlock()
        clockPopup.removeAllItems()
        clockPopup.addItem(withTitle: "Internal")
        for source in snapshotSources {
            let item = NSMenuItem(title: source.name, action: nil, keyEquivalent: "")
            item.representedObject = String(source.uniqueID)
            clockPopup.menu?.addItem(item)
        }
        clockPopup.selectItem(at: 0)
        if selectedID != "internal",
           let index = clockPopup.itemArray.firstIndex(where: { ($0.representedObject as? String) == selectedID }) {
            clockPopup.selectItem(at: index)
        }
    }

    // MARK: Persistence

    private static let defaultsKey = "midi-routing"
    private static let configURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("SP-16 Emulator/midi-routing.json")

    private func loadPersisted() {
        stateLock.lock()
        defer { stateLock.unlock() }
        if let data = UserDefaults.standard.data(forKey: Self.defaultsKey),
           let stored = try? JSONDecoder().decode([String: MidiRoutingConfig.PortConfig].self, from: data) {
            settings = stored
        }
        if let src = UserDefaults.standard.string(forKey: "midi-routing.clockSource") { clockSource = src }
        if let keys = UserDefaults.standard.stringArray(forKey: "midi-routing.touched") { touched = Set(keys) }
        let bpm = UserDefaults.standard.double(forKey: "midi-routing.bpm")
        if bpm > 0 { nominalBPM = bpm }
    }

    private func persist() {
        stateLock.lock()
        let settingsSnapshot = settings
        let touchedSnapshot = touched
        let source = clockSource
        let bpm = nominalBPM
        stateLock.unlock()
        if let data = try? JSONEncoder().encode(settingsSnapshot) {
            UserDefaults.standard.set(data, forKey: Self.defaultsKey)
        }
        UserDefaults.standard.set(touchedSnapshot.sorted(), forKey: "midi-routing.touched")
        UserDefaults.standard.set(source, forKey: "midi-routing.clockSource")
        UserDefaults.standard.set(bpm, forKey: "midi-routing.bpm")
        writeBridgeConfig()
    }

    /// The bridge config the Python DIN/USB bridges read (`emulator/midi/routing.py`): clock source, tempo, and the
    /// ports the user has toggled, in a stable order. Untouched ports are left out on purpose (see `touched`).
    func writeBridgeConfig() {
        stateLock.lock()
        let ports = touched.sorted().compactMap { settings[$0] }
        let config = MidiRoutingConfig(clockSource: clockSource, bpm: nominalBPM, ports: ports)
        stateLock.unlock()
        guard let data = try? JSONEncoder().encode(config) else { return }
        try? FileManager.default.createDirectory(at: Self.configURL.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try? data.write(to: Self.configURL, options: .atomic)
    }

    // MARK: Table view

    func numberOfRows(in tableView: NSTableView) -> Int {
        stateLock.lock()
        defer { stateLock.unlock() }
        return rows.count
    }

    func tableView(_ tableView: NSTableView, objectValueFor tableColumn: NSTableColumn?, row: Int) -> Any? {
        guard let column = tableColumn else { return nil }
        stateLock.lock()
        guard row < rows.count else {
            stateLock.unlock()
            return nil
        }
        let endpoint = rows[row]
        let c = settings[key(endpoint)] ?? blank(endpoint)
        stateLock.unlock()
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
        guard let column = tableColumn else { return false }
        stateLock.lock()
        defer { stateLock.unlock() }
        guard row < rows.count else { return false }
        let endpoint = rows[row]
        switch column.identifier.rawValue {
        case "col1", "col4": return endpoint.isSource      // In / Receive clock apply to sources
        case "col2", "col3": return !endpoint.isSource     // Out / Send clock apply to destinations
        default: return false
        }
    }

    @objc private func toggle(_ sender: NSButtonCell) {
        let row = tableView.clickedRow
        let tag = sender.tag
        let on = sender.state == .on
        stateLock.lock()
        guard row >= 0, row < rows.count else {
            stateLock.unlock()
            return
        }
        let endpoint = rows[row]
        var c = settings[key(endpoint)] ?? blank(endpoint)
        stateLock.unlock()
        switch tag {
        case 1: c.inEnabled = on
        case 2: c.outEnabled = on
        case 3: c.sendClock = on
        case 4: c.receiveClock = on
        default: break
        }
        setConfig(c, for: endpoint)
        if tag == 3 { rebuildClockRouting() }
    }

    @objc private func clockSourceChosen(_ sender: NSPopUpButton) {
        let source = (sender.selectedItem?.representedObject as? String) ?? "internal"
        stateLock.lock()
        clockSource = source
        stateLock.unlock()
        persist()
        rebuildClockRouting()
    }
}
