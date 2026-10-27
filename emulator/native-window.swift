import AppKit
import Darwin

struct Configuration: Decodable {
    let qemu: String
    let arguments: [String]
    let session: String
    let socket: String
    let variant: String?
    let profile: String?
    let research: Bool?
    let data: DataConfig?
    let audio: String?
    /// Decoupled builds only: the firmware library.
    let firmware: FirmwareConfig?
    /// The firmware entry this session boots (decoupled builds), recorded in runtime.json.
    var firmwareID: String? = nil
    var firmwareName: String? = nil
    var devOverlay: String? = nil

    enum CodingKeys: String, CodingKey { case qemu, arguments, session, socket, variant, profile, research, data, audio, firmware }

    init(qemu: String, arguments: [String], session: String, socket: String, variant: String?, profile: String?,
         research: Bool?, data: DataConfig?, audio: String?, firmware: FirmwareConfig?, firmwareID: String? = nil,
         firmwareName: String? = nil, devOverlay: String? = nil) {
        self.qemu = qemu; self.arguments = arguments; self.session = session; self.socket = socket
        self.variant = variant; self.profile = profile; self.research = research; self.data = data; self.audio = audio
        self.firmware = firmware; self.firmwareID = firmwareID; self.firmwareName = firmwareName
        self.devOverlay = devOverlay
    }
}

/// Persistent data image settings (fullpanel only). Absent: the session is as before, with no block device.
struct DataConfig: Decodable {
    let dir: String
    let size: UInt64?
}

/// Held for the whole session; the kernel drops the flock if the app dies.
var sessionLockFD: Int32 = -1
var ephemeralImagePath: String?
/// Bench mode (`--no-ui-input`): the panel ignores pointer, scroll, keyboard and accessibility input, and the app does
/// not activate itself; the research control socket keeps driving the panel. For unattended test runs.
var uiInputDisabled = false
/// `--no-audio-files`: an audio build's sink still reads every PCM packet from the guest and keeps its counters (the
/// guest's audio path and timing stay those of a file build) but opens no WAV. For unattended runs that read no audio.
var audioFilesDisabled = false

/// Host <-> guest message kinds; see protocol.py for the authoritative description.
enum Kind {
    static let frame: UInt8 = 1, touch: UInt8 = 2, ack: UInt8 = 3, control: UInt8 = 6
    static let inputResult: UInt8 = 7, feedback: UInt8 = 8, research: UInt8 = 9
    static let txFrame: UInt8 = 10, stats: UInt8 = 11
}

/// Fixed-size sample window for latency percentiles.
struct Samples {
    private var values: [Double] = []
    mutating func add(_ value: Double) {
        values.append(value)
        if values.count > 1024 { values.removeFirst(values.count - 1024) }
    }
    func summary() -> [String: Double] {
        guard !values.isEmpty else { return ["count": 0] }
        let sorted = values.sorted()
        func percentile(_ p: Double) -> Double { sorted[min(sorted.count - 1, Int(Double(sorted.count) * p))] }
        return ["count": Double(sorted.count), "p50": percentile(0.5), "p95": percentile(0.95), "max": sorted.last!]
    }
}

final class NativeApplication: NSObject, NSApplicationDelegate, NSWindowDelegate, PanelViewDelegate {
    let config: Configuration
    let process = Process()
    let panel: PanelView
    let writeQueue = DispatchQueue(label: "sp16.native.input")
    let lock = NSLock()
    var window: NSWindow!
    var socketFD: Int32 = -1
    var stopping = false
    var pendingFrame: Data?
    var drawing = false
    var lastPixels: Data?
    var legacyTimes: [UInt32: Double] = [:]
    var legacySequence: UInt32 = 0
    var controlTimes: [UInt32: Double] = [:]
    var controlSequence: UInt32 = 0
    var visiblePending: [Double] = []
    var ackSamples = Samples()
    var visibleSamples = Samples()
    var resultCounts = [0, 0, 0, 0]
    var guestStats: [String: Any] = [:]
    var feedbackGeneration: UInt32 = 0
    var lampsLive = false
    var frameCount = 0
    var lastMetrics = 0.0
    var log: FileHandle?
    var stderrCapture: StderrCapture?
    var audioSink: AudioSink?
    var txLog: FileHandle?
    var lastCapture = 0.0
    let started = ProcessInfo.processInfo.systemUptime

    init(_ config: Configuration) {
        self.config = config
        let layout = Bundle.main.url(forResource: "panel-layout", withExtension: "json")
            ?? URL(fileURLWithPath: "panel-layout.json")
        panel = PanelView(layoutURL: layout)
        super.init()
        panel.delegate = self
    }

    var title: String {
        if let name = config.firmwareName {
            return "SP-16 Emulator — \(name)" + (unverifiedNotice == nil ? "" : " (unverified)")
        }
        return "SP16 Emulator"
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installMenus()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.title = title
        window.contentMinSize = NSSize(width: 960, height: 640)
        window.contentView = panel
        window.delegate = self
        window.center(); window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(panel)
        panel.uiInputEnabled = !uiInputDisabled
        // Bench mode leaves focus with the app the user is working in (`open -g` started it in the background).
        if !uiInputDisabled { NSApp.activate(ignoringOtherApps: true) }
        AppLog.log("main window opened: \(window.title)" + (uiInputDisabled ? " (bench mode: panel input off)" : ""))
        DiagnosticsController.shared.checkPreviousRun(current: config.session)  // no-op unless a decoupled build set it up
        logLaunchSummary()
        if let notice = unverifiedNotice {
            AppLog.log("unverified notice shown: \(notice.text.replacingOccurrences(of: "\n", with: " | "))")
            UnverifiedNotice.showOnce(key: notice.key, text: notice.text)
        }
        let console = config.session + "/console.txt"
        FileManager.default.createFile(atPath: console, contents: nil, attributes: [.posixPermissions: 0o600])
        log = try? FileHandle(forWritingTo: URL(fileURLWithPath: console))
        process.executableURL = URL(fileURLWithPath: config.qemu)
        process.arguments = config.arguments
        process.standardOutput = log
        if config.firmware == nil {
            process.standardError = log
        } else {
            // Decoupled builds: QEMU's stderr still reaches console.txt, and its tail and exit status are kept for bug reports.
            let capture = StderrCapture(console: log, file: config.session + "/qemu-stderr.txt")
            process.standardError = capture.pipe
            stderrCapture = capture
        }
        process.terminationHandler = { [weak self] process in
            AppLog.log("QEMU exited: status \(process.terminationStatus), "
                       + (process.terminationReason == .exit ? "exit" : "uncaught signal"))
            self?.stderrCapture?.terminated(status: process.terminationStatus, reason: process.terminationReason)
            DispatchQueue.main.async {
                self?.panel.releaseAll()
                self?.panel.setStatus("QEMU stopped (\(process.terminationStatus)). See console.txt.")
            }
        }
        do { try process.run() }
        catch {
            AppLog.log("cannot start QEMU: \(error)")
            panel.setStatus("Cannot start QEMU: \(error.localizedDescription)"); return
        }
        AppLog.log("QEMU started pid=\(process.processIdentifier)")
        startQEMUReaper()
        panel.setStatus("Starting native QEMU…")
        if let audio = config.audio {
            // "file": a WAV per playback stream. "coreaudio": host output only. "both": the two.
            // Optional ":<device UID>" or ":offline:<wav>[:rate[:ppm[:block]]]" follows the mode; no UID = system default.
            let parts = audio.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
            let mode = parts[0], device = parts.count > 1 && !parts[1].isEmpty ? parts[1] : nil
            var output: AudioOutput? = nil
            if mode == "coreaudio" || mode == "both" {
                output = AudioOutput(deviceUID: device, offline: device.flatMap { OfflineSpec($0) })
                output?.start()
            }
            let sink = AudioSink(socketPath: config.socket + ".pcm", directory: config.session,
                                 writeFiles: (mode == "file" || mode == "both") && !audioFilesDisabled, output: output,
                                 isRunning: { [weak self] in self?.process.isRunning ?? false })
            audioSink = sink; sink.start()
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in self?.connectAndRead() }
        if config.research == true {
            DispatchQueue.global(qos: .utility).async { [weak self] in self?.serveResearchSocket() }
        }
    }

    /// A kill -9 or crash of this app leaves no chance to run cleanup, so a small helper (qemu_reaper.py, stdlib only)
    /// watches the app and ends QEMU when the app is gone: a guest clean stop first (persistent builds), then SIGTERM
    /// and SIGKILL, only for the QEMU PID it was given and only while that process still belongs to this session.
    func startQEMUReaper() {
        guard let script = Bundle.main.url(forResource: "qemu_reaper", withExtension: "py") else {
            AppLog.log("reaper: qemu_reaper.py missing from the app bundle; QEMU would outlive a killed app")
            return
        }
        let reaper = Process()
        reaper.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        var arguments = [script.path, "--qemu", "\(process.processIdentifier)", "--match", config.socket]
        if config.data != nil { arguments += ["--control", config.socket + ".ctl"] }
        reaper.arguments = arguments
        reaper.standardOutput = FileHandle.nullDevice
        reaper.standardError = FileHandle.nullDevice
        do {
            try reaper.run()
            AppLog.log("reaper started pid=\(reaper.processIdentifier)")
        } catch {
            AppLog.log("reaper: cannot start: \(error)")
        }
    }

    /// Decoupled builds: the launch configuration for the app log (no key material exists in the app; paths are redacted).
    func logLaunchSummary() {
        guard config.firmware != nil else { return }
        AppLog.log("launch: variant=\(config.variant ?? "?") profile=\(config.profile ?? "") research=\(config.research == true) "
                   + "audio=\(config.audio ?? "off") firmware=\(config.firmwareID ?? "?") name=\(config.firmwareName ?? "?")")
        AppLog.log("launch: library=\(config.firmware?.library ?? "?") qemu=\(config.qemu) session=\(config.session)")
        AppLog.log("launch: qemu arguments: " + config.arguments.joined(separator: " "))
        if let overlay = config.devOverlay { AppLog.log("launch: developer overlay \(overlay)") }
        if uiInputDisabled { AppLog.log("launch: bench mode, panel input off (--no-ui-input)") }
        if audioFilesDisabled { AppLog.log("launch: Audio files: off (--no-audio-files), no WAV is written") }
    }

    func installMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: config.firmware != nil ? "Quit SP-16 Emulator" : "Quit SP16 Emulator",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        if config.firmware != nil {
            menu.addItem(editMenuItem())  // decoupled builds: text fields in the import and library windows
            // Decoupled builds (M4): the firmware library (select = restart on that firmware, rename, remove, import).
            let firmwareItem = NSMenuItem(); menu.addItem(firmwareItem)
            let firmwareMenu = NSMenu(title: "Firmware")
            let library = NSMenuItem(title: "Library…", action: #selector(showFirmwareLibrary), keyEquivalent: "l")
            library.target = self
            firmwareMenu.addItem(library)
            firmwareItem.submenu = firmwareMenu
            menu.addItem(helpMenuItem())
        }
        let viewItem = NSMenuItem(); menu.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        let rear = NSMenuItem(title: "Rear I/O Panel", action: #selector(toggleRearPanel(_:)), keyEquivalent: "r")
        rear.target = self
        viewMenu.addItem(rear)
        let glow = NSMenuItem(title: "Lamp Glow", action: #selector(toggleLampGlow(_:)), keyEquivalent: "")
        glow.target = self
        viewMenu.addItem(glow)
        viewItem.submenu = viewMenu
        let inputItem = NSMenuItem(); menu.addItem(inputItem)
        let inputMenu = NSMenu(title: "Input")
        let release = NSMenuItem(title: "Release All Holds", action: #selector(releaseAllHolds), keyEquivalent: ".")
        release.target = self
        inputMenu.addItem(release)
        inputItem.submenu = inputMenu
        NSApp.mainMenu = menu
    }

    @objc func releaseAllHolds() { panel.releaseAll() }

    @objc func toggleRearPanel(_ sender: NSMenuItem) { panel.showRear.toggle() }
    @objc func toggleLampGlow(_ sender: NSMenuItem) { panel.lampBloom.toggle() }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        switch item.action {
        case #selector(toggleRearPanel(_:)): item.state = panel.showRear ? .on : .off
        case #selector(toggleLampGlow(_:)): item.state = panel.lampBloom ? .on : .off
        default: break
        }
        return true
    }

    var libraryWindow: FirmwareLibraryWindow?
    @objc func showFirmwareLibrary() {
        guard let firmware = config.firmware else { return }
        AppLog.log("library window opened")
        let window = libraryWindow ?? FirmwareLibraryWindow(config: config, library: firmware.library, current: config.firmwareID)
        window.onClose = { [weak self] in AppLog.log("library window closed"); self?.libraryWindow = nil }
        libraryWindow = window
        window.reload()
        window.show()
    }

    // MARK: Guest connection

    func readExact(_ fd: Int32, _ count: Int) throws -> Data {
        var result = Data(count: count)
        try result.withUnsafeMutableBytes { (buffer: UnsafeMutableRawBufferPointer) in
            var offset = 0
            while offset < count {
                let n = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), count - offset)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 { throw NSError(domain: "Bridge disconnected", code: Int(errno)) }
                offset += n
            }
        }
        return result
    }

    func unixAddress(_ path: String) -> sockaddr_un? {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bytes = Array(path.utf8) + [0]
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { return nil }
        _ = withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            bytes.withUnsafeBytes { memcpy(pointer, $0.baseAddress!, bytes.count) }
        }
        return address
    }

    func connectAndRead() {
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0, var address = unixAddress(config.socket) else { return }
        var noSignal: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        var connected = false
        for _ in 0..<1200 {
            lock.lock(); let quit = stopping; lock.unlock()
            if quit || !process.isRunning { break }
            let rc = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
            }
            if rc == 0 { connected = true; break }
            Thread.sleep(forTimeInterval: 0.1)
        }
        guard connected else {
            AppLog.log("bridge: never connected (\(process.isRunning ? "timed out after 120 s" : "QEMU is not running"))")
            Darwin.close(fd); return
        }
        AppLog.log("bridge: connected")
        lock.lock(); socketFD = fd; lock.unlock()
        defer {
            writeQueue.sync {
                lock.lock(); socketFD = -1; lock.unlock()
                Darwin.close(fd)
            }
        }
        do {
            while true {
                let header = try readExact(fd, 4)
                let size = header.reduce(0) { ($0 << 8) | Int($1) }
                guard size >= 1 && size <= 800 * 480 * 4 + 9 else { throw NSError(domain: "Invalid bridge frame", code: 1) }
                let body = try readExact(fd, size)
                try handle(body)
            }
        } catch {
            lock.lock(); let quitting = stopping; lock.unlock()
            AppLog.log("bridge: disconnected (\(quitting ? "app is quitting" : "\(error)"))")
            DispatchQueue.main.async { [weak self] in
                guard let self = self, !self.stopping else { return }
                self.panel.releaseAll()
                self.lampsLive = false
                self.panel.setFeedback(generation: self.feedbackGeneration, live: false, lamps: [:])
                self.panel.setStatus("Native display disconnected. See console.txt.")
            }
        }
    }

    func handle(_ body: Data) throws {
        let size = body.count
        switch body[0] {
        case Kind.frame:
            guard size == 800 * 480 * 4 + 9, body[1] == 3, body[2] == 32,
                  body[3] == 1, body[4] == 224 else { throw NSError(domain: "Invalid display dimensions", code: 2) }
            deliverFrame(Data(body.dropFirst(9)))
        case Kind.ack:
            guard size == 5 else { throw NSError(domain: "Invalid input acknowledgement", code: 3) }
            let sequence = body.dropFirst().reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
            lock.lock()
            if let sent = legacyTimes.removeValue(forKey: sequence) {
                ackSamples.add((ProcessInfo.processInfo.systemUptime - sent) * 1000)
            }
            lock.unlock()
        case Kind.inputResult:
            guard size == 6 else { throw NSError(domain: "Invalid input result", code: 5) }
            let sequence = body[1..<5].reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
            let status = Int(body[5])
            lock.lock()
            if status < resultCounts.count { resultCounts[status] += 1 }
            if let sent = controlTimes.removeValue(forKey: sequence) {
                ackSamples.add((ProcessInfo.processInfo.systemUptime - sent) * 1000)
            }
            lock.unlock()
        case Kind.feedback:
            guard size >= 8 else { throw NSError(domain: "Invalid feedback", code: 6) }
            let generation = body[1..<5].reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
            let live = body[5] & 1 == 1
            let count = Int(body[6]) << 8 | Int(body[7])
            guard size == 8 + 4 * count else { throw NSError(domain: "Invalid feedback size", code: 7) }
            var lamps: [UInt8: PanelView.Lamp] = [:]
            for index in 0..<count {
                let at = 8 + 4 * index
                lamps[body[at]] = PanelView.Lamp(r: body[at + 1], g: body[at + 2], b: body[at + 3])
            }
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                self.feedbackGeneration = generation
                self.lampsLive = live
                self.panel.setFeedback(generation: generation, live: live, lamps: lamps)
            }
        case Kind.txFrame:
            writeTxLog(body.dropFirst())
        case Kind.stats:
            if let object = try? JSONSerialization.jsonObject(with: body.dropFirst()) as? [String: Any] {
                lock.lock(); guestStats = object; lock.unlock()
            }
        default: throw NSError(domain: "Unknown native message", code: 4)
        }
    }

    func writeTxLog(_ frame: Data) {
        guard config.research == true else { return }
        lock.lock(); defer { lock.unlock() }
        if txLog == nil {
            let path = config.session + "/tx-frames.jsonl"
            FileManager.default.createFile(atPath: path, contents: nil, attributes: [.posixPermissions: 0o600])
            txLog = try? FileHandle(forWritingTo: URL(fileURLWithPath: path))
        }
        let line = "{\"t\":\(ProcessInfo.processInfo.systemUptime - started),\"hex\":\"\(frame.map { String(format: "%02x", $0) }.joined())\"}\n"
        txLog?.write(Data(line.utf8))
    }

    // MARK: Display

    func deliverFrame(_ pixels: Data) {
        lock.lock()
        pendingFrame = pixels
        if drawing { lock.unlock(); return }
        drawing = true; lock.unlock()
        DispatchQueue.main.async { [weak self] in self?.renderLatest() }
    }

    func makeImage(_ pixels: Data) -> CGImage? {
        guard let provider = CGDataProvider(data: pixels as CFData) else { return nil }
        return CGImage(width: 800, height: 480, bitsPerComponent: 8, bitsPerPixel: 32,
                       bytesPerRow: 3200, space: CGColorSpaceCreateDeviceRGB(),
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue).union(.byteOrder32Little),
                       provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }

    func renderLatest() {
        lock.lock(); let pixels = pendingFrame; pendingFrame = nil; drawing = false
        if pixels != nil { lastPixels = pixels }
        // Inputs submitted before this frame reached the window: time to the next display update.
        let now = ProcessInfo.processInfo.systemUptime
        for sent in visiblePending where now - sent < 2 { visibleSamples.add((now - sent) * 1000) }
        visiblePending.removeAll()
        lock.unlock()
        guard let pixels = pixels, let image = makeImage(pixels) else { return }
        panel.setLCD(image)
        frameCount += 1
        updateStatus()
        if now - lastCapture >= 1 {
            lastCapture = now
            DispatchQueue.global(qos: .utility).async { [config] in
                if let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) {
                    try? png.write(to: URL(fileURLWithPath: config.session + "/screen.png"), options: .atomic)
                }
            }
        }
    }

    func snapshot(named name: String) -> Bool {
        lock.lock(); let pixels = lastPixels; lock.unlock()
        guard name.range(of: "^[A-Za-z0-9._-]{1,64}$", options: .regularExpression) != nil,
              let pixels = pixels, let image = makeImage(pixels),
              let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { return false }
        return (try? png.write(to: URL(fileURLWithPath: config.session + "/" + name), options: .atomic)) != nil
    }

    func metrics() -> [String: Any] {
        // Read before taking `lock`: the main thread takes it in renderLatest, so a main.sync under it could deadlock.
        let redraw = Thread.isMainThread ? panel.redrawStats.json : DispatchQueue.main.sync { panel.redrawStats.json }
        lock.lock(); defer { lock.unlock() }
        return ["frames": frameCount, "uptime_seconds": ProcessInfo.processInfo.systemUptime - started,
                "ack_ms": ackSamples.summary(), "visible_ms": visibleSamples.summary(),
                "input_results": ["ok": resultCounts[0], "unsupported": resultCounts[1],
                                   "failed": resultCounts[2], "invalid": resultCounts[3]],
                "pending_controls": controlTimes.count, "lamps_live": lampsLive,
                "feedback_generation": feedbackGeneration, "guest": guestStats,
                "panel_redraw": redraw]
            .merging(audioSink.map { ["audio": $0.snapshot()] } ?? [:]) { first, _ in first }
    }

    func updateStatus() {
        let m = metrics()
        let ack = (m["ack_ms"] as? [String: Double])?["p50"] ?? 0
        let results = m["input_results"] as? [String: Int] ?? [:]
        panel.setStatus(String(format: "%d frames · ack %.1f ms · lamps %@ · unsupp %d",
                               frameCount, ack, lampsLive ? "live" : "no feedback",
                               results["unsupported"] ?? 0))
        let now = ProcessInfo.processInfo.systemUptime
        if now - lastMetrics >= 1 {
            lastMetrics = now
            if let data = try? JSONSerialization.data(withJSONObject: m, options: [.sortedKeys]) {
                try? data.write(to: URL(fileURLWithPath: config.session + "/metrics.json"), options: .atomic)
            }
        }
    }

    // MARK: Input

    func panelControl(_ id: UInt8, action: UInt8, value: Int16) {
        lock.lock()
        controlSequence += 1
        let sequence = controlSequence
        let now = ProcessInfo.processInfo.systemUptime
        controlTimes[sequence] = now
        visiblePending.append(now)
        if controlTimes.count > 4096 { controlTimes.removeAll() }
        lock.unlock()
        let raw = UInt16(bitPattern: value)
        sendBody([Kind.control, id, action, UInt8(raw >> 8), UInt8(raw & 255),
                  UInt8(sequence >> 24), UInt8((sequence >> 16) & 255), UInt8((sequence >> 8) & 255), UInt8(sequence & 255)])
    }

    func panelTouch(x: Int, y: Int, action: UInt8) {
        sendBody([Kind.touch, UInt8(x >> 8), UInt8(x & 255), UInt8(y >> 8), UInt8(y & 255), action], legacy: true)
    }

    func sendBody(_ body: [UInt8], legacy: Bool = false) {
        writeQueue.async { [weak self] in
            guard let self = self else { return }
            self.lock.lock()
            let fd = self.socketFD
            guard fd >= 0, !self.stopping else { self.lock.unlock(); return }
            if legacy {
                self.legacySequence += 1
                self.legacyTimes[self.legacySequence] = ProcessInfo.processInfo.systemUptime
                self.visiblePending.append(ProcessInfo.processInfo.systemUptime)
            }
            self.lock.unlock()
            let size = body.count
            let bytes: [UInt8] = [UInt8(size >> 24), UInt8((size >> 16) & 255), UInt8((size >> 8) & 255), UInt8(size & 255)] + body
            bytes.withUnsafeBytes { buffer in
                var offset = 0
                while offset < bytes.count {
                    let count = Darwin.write(fd, buffer.baseAddress!.advanced(by: offset), bytes.count - offset)
                    if count < 0 && errno == EINTR { continue }
                    if count <= 0 { break }
                    offset += count
                }
            }
        }
    }

    // MARK: Research control socket (owner-only, enabled by the experimental profile)

    func serveResearchSocket() {
        let path = URL(fileURLWithPath: config.socket).deletingLastPathComponent().path + "/control.sock"
        let server = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard server >= 0, var address = unixAddress(path) else { return }
        unlink(path)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(server, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard bound == 0, Darwin.listen(server, 4) == 0 else { Darwin.close(server); return }
        chmod(path, 0o600)
        while true {
            let client = Darwin.accept(server, nil, nil)
            lock.lock(); let quit = stopping; lock.unlock()
            if client < 0 || quit { break }
            var noSignal: Int32 = 1
            setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
            DispatchQueue.global(qos: .utility).async { [weak self] in self?.serveClient(client) }
        }
        Darwin.close(server)
    }

    func serveClient(_ client: Int32) {
        defer { Darwin.close(client) }
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = Darwin.read(client, &chunk, chunk.count)
            if n <= 0 { return }
            buffer.append(contentsOf: chunk[0..<n])
            if buffer.count > 65536 { return }
            while let newline = buffer.firstIndex(of: 10) {
                let line = buffer[buffer.startIndex..<newline]
                buffer = Data(buffer[buffer.index(after: newline)...])
                let reply = research(Data(line)) + "\n"
                _ = reply.withCString { Darwin.write(client, $0, strlen($0)) }
            }
        }
    }

    func research(_ line: Data) -> String {
        guard let command = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              let op = command["op"] as? String else { return "{\"error\":\"invalid command\"}" }
        func int(_ key: String) -> Int? { command[key] as? Int }
        func hex(_ key: String) -> [UInt8]? {
            guard let text = command[key] as? String, text.count % 2 == 0 else { return nil }
            var out: [UInt8] = []
            var index = text.startIndex
            while index < text.endIndex {
                let next = text.index(index, offsetBy: 2)
                guard let byte = UInt8(text[index..<next], radix: 16) else { return nil }
                out.append(byte); index = next
            }
            return out
        }
        switch op {
        case "event":
            guard let id = int("id"), let action = int("action"), (1...255).contains(id), (0...3).contains(action) else {
                return "{\"error\":\"event needs id 1-255 and action 0-3\"}"
            }
            panelControl(UInt8(id), action: UInt8(action), value: Int16(truncatingIfNeeded: int("value") ?? 0))
            return "{\"ok\":true}"
        case "touch":
            guard let x = int("x"), let y = int("y"), let action = int("action"),
                  (0..<800).contains(x), (0..<480).contains(y), (0...2).contains(action) else {
                return "{\"error\":\"touch needs x 0-799 y 0-479 action 0-2\"}"
            }
            panelTouch(x: x, y: y, action: UInt8(action))
            return "{\"ok\":true}"
        case "raw":
            guard let offset = int("offset"), let data = hex("hex"), !data.isEmpty, offset >= 0, offset + data.count <= 128 else {
                return "{\"error\":\"raw needs offset and hex within 128 bytes\"}"
            }
            sendBody([Kind.research, 1, UInt8(offset)] + data)
            return "{\"ok\":true}"
        case "cdc":
            guard let data = hex("hex"), data.count == 4 else { return "{\"error\":\"cdc needs 4 bytes\"}" }
            sendBody([Kind.research, 2] + data)
            return "{\"ok\":true}"
        case "clear":
            sendBody([Kind.research, 3]); return "{\"ok\":true}"
        case "txlog":
            sendBody([Kind.research, 4, UInt8(int("on") == 0 ? 0 : 1)]); return "{\"ok\":true}"
        case "resize":
            guard let width = int("width"), let height = int("height"),
                  (640...3200).contains(width), (400...2000).contains(height) else {
                return "{\"error\":\"resize needs width 640-3200 and height 400-2000\"}"
            }
            DispatchQueue.main.sync { self.window.setContentSize(NSSize(width: width, height: height)) }
            return "{\"ok\":true}"
        case "panelshot":
            guard let name = command["name"] as? String,
                  name.range(of: "^[A-Za-z0-9._-]{1,64}$", options: .regularExpression) != nil else {
                return "{\"error\":\"panelshot needs a plain file name\"}"
            }
            var ok = false
            DispatchQueue.main.sync {
                guard let rep = self.panel.bitmapImageRepForCachingDisplay(in: self.panel.bounds) else { return }
                self.panel.cacheDisplay(in: self.panel.bounds, to: rep)
                if let png = rep.representation(using: .png, properties: [:]) {
                    ok = (try? png.write(to: URL(fileURLWithPath: self.config.session + "/" + name), options: .atomic)) != nil
                }
            }
            return ok ? "{\"ok\":true}" : "{\"error\":\"panelshot failed\"}"
        case "view":
            // View toggles for screenshots: rear on|off, glow on|off.
            guard let name = command["name"] as? String, ["rear", "glow"].contains(name),
                  let on = command["on"] as? Bool else { return "{\"error\":\"view needs name rear|glow and on true|false\"}" }
            DispatchQueue.main.sync { if name == "rear" { self.panel.showRear = on } else { self.panel.lampBloom = on } }
            return "{\"ok\":true}"
        case "period":
            guard let ms = int("ms"), (1...50).contains(ms) else { return "{\"error\":\"period needs ms 1-50\"}" }
            sendBody([Kind.research, 5, UInt8(ms)]); return "{\"ok\":true}"
        case "snapshot":
            guard let name = command["name"] as? String, snapshot(named: name) else { return "{\"error\":\"snapshot failed\"}" }
            return "{\"ok\":true}"
        case "usb":
            // Virtual USB stick: "eject" (flush, unmount, announce removal) or "attach" (remount and announce again).
            guard config.data != nil, let action = command["action"] as? String, ["eject", "attach"].contains(action) else {
                return "{\"error\":\"usb needs action eject|attach and a persistent build\"}"
            }
            let tokens = action == "eject" ? ["usb-ejected", "usb-busy"] : ["usb-attached", "usb-error"]
            guard let result = DataImage.requestControl(socketPath: config.socket + ".ctl", message: "usb-" + action,
                                                        until: tokens, timeout: 30) else {
                return "{\"error\":\"usb \(action) got no reply from the guest\"}"
            }
            return "{\"ok\":\(result == "usb-ejected" || result == "usb-attached"),\"result\":\"\(result)\"}"
        case "app-restart":
            // Decoupled builds: restart only the OEM app in the running guest (developer overlay re-applied first).
            guard config.firmware != nil, config.data != nil else {
                return "{\"error\":\"app-restart needs a decoupled build\"}"
            }
            let started = ProcessInfo.processInfo.systemUptime
            guard let result = DataImage.requestControl(socketPath: config.socket + ".ctl", message: "app-restart",
                                                        until: ["app-restarted", "app-restart-error"], timeout: 30) else {
                return "{\"error\":\"app-restart got no reply from the guest\"}"
            }
            let elapsed = (ProcessInfo.processInfo.systemUptime - started) * 1000
            return "{\"ok\":\(result == "app-restarted"),\"result\":\"\(result)\",\"ms\":\(Int(elapsed))}"
        case "firmware":
            // Decoupled builds: the running firmware and the library (list, current, manifest summary).
            guard let firmware = config.firmware else { return "{\"error\":\"firmware needs a decoupled build\"}" }
            var reply: [String: Any] = ["library": firmware.library, "current": config.firmwareID ?? NSNull(),
                                        "dev_overlay": config.devOverlay ?? NSNull()]
            var list: [[String: Any]] = []
            for id in FirmwareLibrary.ids(library: firmware.library) {
                guard let entry = try? FirmwareLibrary.load(library: firmware.library, id: id) else { continue }
                var item: [String: Any] = ["id": id, "name": entry.name, "version": entry.version, "kind": entry.kind,
                                           "status": entry.status, "profile": entry.profile ?? NSNull(),
                                           "reason": entry.reason]
                if id == config.firmwareID {
                    let image = entry.manifest["image"] as? [String: Any] ?? [:]
                    let update = entry.manifest["update"] as? [String: Any] ?? [:]
                    reply["manifest"] = ["app_sha256": entry.appSHA256, "base_app_sha256": entry.baseAppSHA256,
                                         "image": image, "update_sha256": update["sha256"] ?? NSNull(),
                                         "files": (entry.manifest["files"] as? [String: Any])?.count ?? 0,
                                         "mod": entry.manifest["mod"] ?? NSNull()]
                    item["current"] = true
                }
                list.append(item)
            }
            reply["list"] = list
            guard let data = try? JSONSerialization.data(withJSONObject: reply, options: [.sortedKeys]),
                  let text = String(data: data, encoding: .utf8) else { return "{\"error\":\"firmware\"}" }
            return text
        case "redraw-bench":
            // Mean ms of a whole-view repaint (what every update would cost without dirty rects), for comparison
            // with panel_redraw in `metrics`.
            var ms = 0.0
            DispatchQueue.main.sync { ms = self.panel.benchmarkFullRedraw(count: min(500, max(1, int("count") ?? 60))) }
            return "{\"ok\":true,\"full_redraw_ms\":\(ms)}"
        case "metrics":
            guard let data = try? JSONSerialization.data(withJSONObject: metrics(), options: [.sortedKeys]),
                  let text = String(data: data, encoding: .utf8) else { return "{\"error\":\"metrics\"}" }
            return text
        default:
            return "{\"error\":\"unknown op\"}"
        }
    }

    // MARK: Lifecycle

    func windowDidResignKey(_ notification: Notification) { panel.releaseAll() }
    func windowShouldClose(_ sender: NSWindow) -> Bool { AppLog.log("main window closed: quitting"); NSApp.terminate(nil); return false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        AppLog.log("quit requested (VM \(process.isRunning ? "running" : "not running"))")
        panel.releaseAll()
        lock.lock(); stopping = true
        // With a data image the UI socket stays open until the guest confirms its unmount: closing it makes the
        // guest bridge exit, and the guest powers off before it can sync.
        let keepUIOpen = config.data != nil && process.isRunning
        if socketFD >= 0 && !keepUIOpen { shutdown(socketFD, SHUT_RDWR) }
        lock.unlock()
        guard process.isRunning else { return .terminateNow }
        let controlPath = config.data != nil ? config.socket + ".ctl" : nil
        DispatchQueue.global().async { [weak self] in
            guard let self = self else { return }
            if let controlPath = controlPath {
                // Guest stops the OEM app, syncs and unmounts the data filesystem; bounded, then proceed.
                let clean = DataImage.requestCleanStop(socketPath: controlPath, timeout: 5)
                self.log?.write(Data("SP16_HOST_CLEAN_STOP clean=\(clean ? "yes" : "no")\n".utf8))
                AppLog.log("guest clean stop: \(clean ? "yes" : "no")")
            }
            self.lock.lock(); if self.socketFD >= 0 { shutdown(self.socketFD, SHUT_RDWR) }; self.lock.unlock()
            self.process.terminate()
            for _ in 0..<30 { if !self.process.isRunning { break }; Thread.sleep(forTimeInterval: 0.1) }
            if self.process.isRunning { Darwin.kill(self.process.processIdentifier, SIGKILL) }
            self.process.waitUntilExit()
            // The termination loop does not service plain main-queue blocks; post to the common run-loop modes.
            CFRunLoopPerformBlock(CFRunLoopGetMain(), CFRunLoopMode.commonModes.rawValue) {
                NSApp.reply(toApplicationShouldTerminate: true)
            }
            CFRunLoopWakeUp(CFRunLoopGetMain())
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        AppLog.markCleanExit("quit")
        audioSink?.stop()
        audioSink?.output?.stop()
        // Only the launcher-created IPC directory; diagnostic session files persist.
        let parent = URL(fileURLWithPath: config.socket).deletingLastPathComponent()
        if parent.path.hasPrefix("/tmp/sp16-") { try? FileManager.default.removeItem(at: parent) }
        if let image = ephemeralImagePath { try? FileManager.default.removeItem(atPath: image) }
    }
}

func refuse(_ message: String) -> Never {
    fputs("SP16 Emulator: \(message)\n", stderr)
    AppLog.log("refused to start: \(message)")
    AppLog.markCleanExit("refused")
    let alert = NSAlert()
    let neutral = Bundle.main.bundleIdentifier?.hasPrefix("local.sp16-compatible.") == true
    alert.messageText = neutral ? "SP-16 Compatible Emulator cannot start" : "SP16 Emulator cannot start"
    alert.informativeText = message
    alert.runModal()
    exit(3)
}

/// Attach the persistent (or, with --ephemeral, disposable) data image: fill @DATA@ in the QEMU arguments.
func attachData(_ config: Configuration, ephemeral: Bool, usbImage: String? = nil) -> Configuration {
    guard let data = config.data else {
        if usbImage != nil { refuse("--usb-image needs a persistent (fullpanel) build: the stick is mounted through its control port") }
        return config
    }
    do {
        let directory = ephemeral ? config.session + "/ephemeral-data" : data.dir
        if !ephemeral && sessionLockFD < 0 { sessionLockFD = try DataImage.acquireLock(directory: directory) }
        let image = try DataImage.prepare(directory: directory, size: data.size ?? DataImage.defaultSize)
        if ephemeral { ephemeralImagePath = image }
        var extra: [String] = []
        if let usb = usbImage {
            try DataImage.validateUSBImage(usb)
            // Virtual USB stick: a second virtio disk told apart by its serial; the guest formats it only if blank.
            extra = ["-drive", "file=\(usb),format=raw,if=none,id=sp16usb",
                     "-device", "virtio-blk-device,drive=sp16usb,serial=sp16usb"]
        }
        return Configuration(qemu: config.qemu, arguments: config.arguments.map { $0.replacingOccurrences(of: "@DATA@", with: image) } + extra,
                             session: config.session, socket: config.socket, variant: config.variant,
                             profile: config.profile, research: config.research, data: data, audio: config.audio,
                             firmware: config.firmware, firmwareID: config.firmwareID, firmwareName: config.firmwareName,
                             devOverlay: config.devOverlay)
    } catch { refuse("\(error)") }
}

/// Decoupled builds: pick the requested (or selected) firmware, check the image integrity, mark it unverified when no
/// verified profile matches (it still boots, D2 in docs/architecture.md) and point the data image at that firmware's
/// own data directory.
/// Decoupled build with nothing to boot yet: the app shows the import screen (or the library) instead of a VM.
var firmwareSetup: (config: Configuration, library: String)?

/// Set when the booted firmware is unverified: the compatibility notice shown once after the window opens, and the
/// reason the window title carries "(unverified)".
var unverifiedNotice: (key: String, text: String)?

func resolveFirmware(_ defaults: Configuration, requested: String?) -> Configuration {
    guard let firmware = defaults.firmware else {
        if requested != nil { refuse("--firmware applies to decoupled builds only") }
        return defaults
    }
    guard let id = requested ?? FirmwareLibrary.selected(library: firmware.library) else {
        refuse("No SP-16 firmware has been imported yet. Import your SP-16 update with "
               + "`native.py firmware import --output <build> --update <UPD> --key-file <key>` and start again.")
    }
    do {
        let entry = try FirmwareLibrary.load(library: firmware.library, id: id)
        try FirmwareLibrary.verifyImage(entry)
        try FileManager.default.createDirectory(atPath: entry.dataDirectory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        AppLog.log("firmware chosen: id=\(entry.id) name=\(entry.name) version=\(entry.version) kind=\(entry.kind) "
                   + "status=\(entry.status) profile=\(entry.profile ?? "none") reason=\(entry.reason) "
                   + "app=\(entry.appSHA256) image=\(entry.imageSHA256)")
        if entry.status != "verified" {
            unverifiedNotice = (entry.id + ":" + entry.imageSHA256, FirmwareLibrary.unverifiedNotice(entry))
        }
        return Configuration(qemu: defaults.qemu, arguments: FirmwareLibrary.substitute(defaults.arguments, entry: entry),
                             session: defaults.session, socket: defaults.socket, variant: entry.kind,
                             profile: defaults.profile, research: defaults.research,
                             data: DataConfig(dir: entry.dataDirectory, size: defaults.data?.size), audio: defaults.audio,
                             firmware: firmware, firmwareID: entry.id,
                             firmwareName: "\(entry.name) (\(entry.version))")
    } catch { refuse("\(error)") }
}

/// Developer overlay (docs/architecture.md M6): a host folder shared read-only over virtio-9p into decoupled
/// research builds only. The guest applies its files, recipes and preload libraries into the overlay's upper layer.
func attachDevOverlay(_ config: Configuration, folder: String?) -> Configuration {
    guard let folder = folder else { return config }
    guard config.firmware != nil, config.research == true else {
        refuse("--dev-overlay needs a decoupled research build (native.py build --decoupled --research)")
    }
    let url = URL(fileURLWithPath: folder).standardizedFileURL
    let path = url.path
    var isDirectory: ObjCBool = false
    guard path.hasPrefix("/"), !path.contains(","), FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
          isDirectory.boolValue, url.resolvingSymlinksInPath().path == path,
          let attributes = try? FileManager.default.attributesOfItem(atPath: path),
          (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid() else {
        refuse("--dev-overlay must be an existing folder you own, given without symlinks or commas")
    }
    var arguments = config.arguments
    if let index = arguments.firstIndex(of: "-append"), index + 1 < arguments.count {
        arguments[index + 1] += " sp16.dev=1"
    }
    arguments += ["-fsdev", "local,id=sp16dev,path=\(path),security_model=none,readonly=on",
                  "-device", "virtio-9p-device,fsdev=sp16dev,mount_tag=sp16dev"]
    return Configuration(qemu: config.qemu, arguments: arguments, session: config.session, socket: config.socket,
                         variant: config.variant, profile: config.profile, research: config.research, data: config.data,
                         audio: config.audio, firmware: config.firmware, firmwareID: config.firmwareID,
                         firmwareName: (config.firmwareName ?? "") + " + developer overlay", devOverlay: path)
}

func configuration() -> Configuration? {
    var arguments = CommandLine.arguments
    let ephemeral = arguments.contains("--ephemeral")
    arguments.removeAll { $0 == "--ephemeral" }
    uiInputDisabled = arguments.contains("--no-ui-input")
    arguments.removeAll { $0 == "--no-ui-input" }
    audioFilesDisabled = arguments.contains("--no-audio-files")
    arguments.removeAll { $0 == "--no-audio-files" }
    var requestedFirmware: String? = nil
    var devOverlay: String? = nil
    if let index = arguments.firstIndex(of: "--dev-overlay") {
        guard index + 1 < arguments.count else { refuse("--dev-overlay needs a folder") }
        devOverlay = arguments[index + 1]
        arguments.removeSubrange(index...(index + 1))
    }
    if let index = arguments.firstIndex(of: "--firmware") {
        guard index + 1 < arguments.count else { refuse("--firmware needs a firmware id") }
        requestedFirmware = arguments[index + 1]
        arguments.removeSubrange(index...(index + 1))
    }
    var usbImage: String? = nil
    if let index = arguments.firstIndex(of: "--usb-image") {
        guard index + 1 < arguments.count else { refuse("--usb-image needs a path") }
        usbImage = arguments[index + 1]
        arguments.removeSubrange(index...(index + 1))
    }
    if arguments.count == 3, arguments[1] == "--config",
       let data = try? Data(contentsOf: URL(fileURLWithPath: arguments[2])),
       let config = try? JSONDecoder().decode(Configuration.self, from: data) {
        return attachData(config, ephemeral: ephemeral, usbImage: usbImage)
    }
    // Double-clicking the app creates a fresh disposable VM/session every time.
    guard arguments.count == 1,
          let url = Bundle.main.url(forResource: "default-config", withExtension: "json"),
          let data = try? Data(contentsOf: url),
          let decoded = try? JSONDecoder().decode(Configuration.self, from: data) else { return nil }
    if decoded.firmware != nil {
        // Decoupled builds: the app log and the diagnostics button work from here on.
        let build = URL(fileURLWithPath: decoded.session).deletingLastPathComponent().path
        AppLog.start(buildDirectory: build)
        DiagnosticsController.shared.buildDirectory = build
        if arguments.count > 1 { AppLog.log("arguments: " + arguments.dropFirst().joined(separator: " ")) }
    }
    if let firmware = decoded.firmware, requestedFirmware == nil, FirmwareLibrary.selected(library: firmware.library) == nil {
        AppLog.log("no firmware selected in \(firmware.library): showing the "
                   + (FirmwareLibrary.ids(library: firmware.library).isEmpty ? "import screen" : "library window"))
        firmwareSetup = (decoded, firmware.library)
        return nil
    }
    let defaults = attachDevOverlay(resolveFirmware(decoded, requested: requestedFirmware), folder: devOverlay)
    // The persistent profile lock comes first: a refused second session leaves no session directory behind.
    if !ephemeral, let data = defaults.data {
        do { sessionLockFD = try DataImage.acquireLock(directory: data.dir) } catch { refuse("\(error)") }
    }
    let identity = UUID().uuidString.lowercased()
    let session = defaults.session + "/native-" + identity
    let socketDirectory = "/tmp/sp16-" + identity
    do {
        try FileManager.default.createDirectory(atPath: session, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
        try FileManager.default.createDirectory(atPath: socketDirectory, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
        AppLog.attach(session: session)
        let socket = socketDirectory + "/ui.sock"
        let config = attachData(Configuration(qemu: defaults.qemu,
                                   arguments: defaults.arguments.map { $0.replacingOccurrences(of: defaults.socket, with: socket) },
                                   session: session, socket: socket, variant: defaults.variant,
                                   profile: defaults.profile, research: defaults.research, data: defaults.data,
                                   audio: defaults.audio, firmware: defaults.firmware, firmwareID: defaults.firmwareID,
                                   firmwareName: defaults.firmwareName, devOverlay: defaults.devOverlay),
                                ephemeral: ephemeral, usbImage: usbImage)
        var json: [String: Any] = ["qemu": config.qemu, "arguments": config.arguments,
                                   "session": session, "socket": socket, "variant": config.variant ?? "stock",
                                   "profile": config.profile ?? "", "research": config.research ?? false]
        if let audio = config.audio { json["audio"] = audio }
        if let id = config.firmwareID { json["firmware"] = id }
        if let overlay = config.devOverlay { json["dev_overlay"] = overlay }
        if uiInputDisabled { json["ui_input"] = false }
        if audioFilesDisabled { json["audio_files"] = false }
        if config.research == true { json["control_socket"] = socketDirectory + "/control.sock" }
        try JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys])
            .write(to: URL(fileURLWithPath: session + "/runtime.json"), options: .atomic)
        return config
    } catch {
        AppLog.log("cannot create the session: \(error)")
        return nil
    }
}

@main
enum Launcher {
    static func main() {
        guard let config = configuration() else {
            if let setup = firmwareSetup {
                let application = NSApplication.shared
                if FirmwareLibrary.ids(library: setup.library).isEmpty {
                    let delegate = FirmwareSetupApplication(config: setup.config, library: setup.library)
                    application.delegate = delegate
                    // NSApplication.delegate is weak: keep the delegate, and so the windows it owns, alive for run().
                    withExtendedLifetime(delegate) { application.run() }
                } else {
                    let delegate = FirmwareLibrarySetupApplication(config: setup.config, library: setup.library)
                    application.delegate = delegate
                    withExtendedLifetime(delegate) { application.run() }
                }
                exit(0)
            }
            fputs("Usage: SP16Emulator --config /absolute/private/runtime.json\n", stderr)
            exit(2)
        }
        // SIGTERM takes the same clean-stop path as Cmd-Q (guest sync + unmount) instead of killing the app.
        signal(SIGTERM, SIG_IGN)
        // A peer closing a socket mid-write (a timed-out sessionctl client, a stalled bridge) must surface as EPIPE,
        // not kill the app silently: launchd logged "exited due to SIGPIPE" with no crash report.
        signal(SIGPIPE, SIG_IGN)
        let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        termination.setEventHandler { NSApp.terminate(nil) }
        termination.resume()
        let application = NSApplication.shared
        let delegate = NativeApplication(config)
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
