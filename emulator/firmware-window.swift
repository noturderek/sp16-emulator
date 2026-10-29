import AppKit
import Foundation

/// Decoupled builds (docs/architecture.md, M4): the import screen shown on first run and the firmware library
/// window. Both drive the bundled firmware library tool (Resources/fwlib/firmware.py, the same code path as
/// `native.py firmware`), so the key is read and sent to the import VM by that tool and never touches the app.
enum FirmwareTool {
    static func script(_ name: String) -> String? { Bundle.main.url(forResource: "fwlib", withExtension: nil)?.appendingPathComponent(name).path }
    static let python = "/usr/bin/python3"

    /// Run a bundled tool (default: the firmware library tool; "diagnostics.py" collects the bug-report zip);
    /// `progress` gets each JSON progress line from stderr. Returns the parsed stdout document.
    static func run(_ arguments: [String], tool: String = "firmware.py", progress: ((String) -> Void)? = nil) -> [String: Any] {
        guard let script = script(tool), FileManager.default.isExecutableFile(atPath: python) else {
            AppLog.log("\(tool): Python 3 (/usr/bin/python3) is missing")
            return ["status": "error", "error": "Python 3 (/usr/bin/python3, Xcode Command Line Tools) is required to import firmware"]
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-E", "-s", script] + arguments  // -I would drop the script folder (fwlib) from sys.path
        process.environment = ["PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
        let output = Pipe(), errors = Pipe()
        process.standardOutput = output; process.standardError = errors
        errors.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n") { progress?(String(line)) }
        }
        do { try process.run() } catch {
            AppLog.log("\(tool): cannot run: \(error)")
            return ["status": "error", "error": "cannot run the firmware tool: \(error)"]
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        errors.fileHandleForReading.readabilityHandler = nil
        guard let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            AppLog.log("\(tool) \(arguments.first ?? ""): no result (exit \(process.terminationStatus))")
            return ["status": "error", "error": "no result from the firmware tool"]
        }
        return result
    }

    /// Boot assets of this build (QEMU, base kernel and initramfs) from the bundled default configuration.
    static func boot(_ config: Configuration) -> [String] {
        var result = ["--qemu", config.qemu]
        for flag in ["-kernel", "-initrd"] {
            if let index = config.arguments.firstIndex(of: flag), index + 1 < config.arguments.count {
                result += ["--" + flag.dropFirst(), config.arguments[index + 1]]
            }
        }
        return result
    }

    /// A non-empty string from a JSON value; a missing key or JSON null (NSNull) is absent.
    static func text(_ value: Any?) -> String? {
        guard let string = value as? String, !string.isEmpty else { return nil }
        return string
    }

    static func describe(_ result: [String: Any]) -> String {
        var lines: [String] = []
        if let error = result["error"] as? String { lines.append(error) }
        if let report = result["report"] as? [String: Any] {
            let status = text(report["status"]) ?? "?"
            if status == "verified" {
                lines.append("Status: verified  Profile: \(text(report["profile"]) ?? "none")")
            } else if let boot = report["boot"] as? [String: Any], let runs = text(boot["profile"]) {
                lines.append("Status: \(status)  Runs on: \(runs) (\(text(boot["source"]) ?? "?"))")
            } else {
                lines.append("Status: \(status)  Profile: \(text(report["profile"]) ?? "none")")
            }
            if let reason = report["reason"] as? String, !reason.isEmpty { lines.append("Reason: \(reason)") }
            for item in report["checks"] as? [[String: Any]] ?? [] {
                lines.append("[\(item["result"] ?? "?")] \(item["check"] ?? "")")
            }
        }
        return lines.joined(separator: "\n")
    }

    /// The new instance's arguments: the chosen firmware, and --no-audio-files when this one was started with it, so a
    /// relaunch after a firmware choice or import does not start recording WAVs again.
    static func relaunchArguments(firmware id: String) -> [String] {
        ["--firmware", id] + (audioFilesDisabled ? ["--no-audio-files"] : [])
    }

    /// Start a new instance on the chosen firmware, then quit this one (its clean stop runs as usual).
    static func relaunch(firmware id: String) {
        let open = Process()
        open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        open.arguments = ["-n", Bundle.main.bundlePath, "--args"] + relaunchArguments(firmware: id)
        try? open.run()
        open.waitUntilExit()
        NSApp.terminate(nil)
    }
}

/// First run (or File > Import): choose the SP-16 update and the key (key file or the official GPL source archive parts).
final class FirmwareImportWindow: NSObject, NSWindowDelegate {
    let config: Configuration
    let library: String
    let window: NSWindow
    let updateField = NSTextField(), keyField = NSTextField(), nameField = NSTextField()
    let keyKind = NSSegmentedControl(labels: ["Key file", "GPL archive"], trackingMode: .selectOne, target: nil, action: nil)
    let status = NSTextField(labelWithString: "")
    let report = NSTextView()
    let steps = NSTextField(labelWithString: "")
    let progress = NSProgressIndicator()
    let importButton = NSButton(title: "Import", target: nil, action: nil)
    let startButton = NSButton(title: "Start", target: nil, action: nil)
    var keyPaths: [String] = []
    var importedID: String?
    var onClose: (() -> Void)?

    init(config: Configuration, library: String, firstRun: Bool) {
        self.config = config; self.library = library
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 460), styleMask: [.titled, .closable],
                          backing: .buffered, defer: false)
        super.init()
        // Created in code: the controller owns the window, so AppKit must not also release it on close.
        window.isReleasedWhenClosed = false
        window.title = firstRun ? "Import SP-16 firmware" : "Import firmware"
        window.delegate = self
        let intro = NSTextField(wrappingLabelWithString: firstRun
            ? "Welcome. This emulator ships without any firmware, so it starts here. Three steps: choose your SP-16 update (.UPD), choose its key (the key file, or the official GPL source archive: the .tar.gz, or both official zip parts in order), then import. The import decrypts and checks the update inside the emulator's own VM; the key is used only for this import and is never stored or logged. When it is done, Start boots the firmware to its home screen. Nothing is downloaded for you; `./sp16 firmware download --info` shows the official update's source and size if you want it."
            : "Choose an SP-16 update (.UPD) and its key file or GPL archive parts.")
        steps.font = .systemFont(ofSize: 12, weight: .medium)
        progress.style = .bar; progress.isIndeterminate = true; progress.isHidden = true
        progress.widthAnchor.constraint(equalToConstant: 600).isActive = true
        updateField.placeholderString = "SP-16 update (.UPD)"; updateField.isEditable = false
        keyField.placeholderString = "Key file or GPL archive parts"; keyField.isEditable = false
        nameField.placeholderString = "Name (optional)"
        keyKind.selectedSegment = 0
        keyKind.target = self; keyKind.action = #selector(keyKindChanged)
        let chooseUpdate = NSButton(title: "Choose…", target: self, action: #selector(chooseUpdate))
        let chooseKey = NSButton(title: "Choose…", target: self, action: #selector(chooseKey))
        importButton.target = self; importButton.action = #selector(startImport); importButton.keyEquivalent = "\r"
        startButton.target = self; startButton.action = #selector(startEmulator); startButton.isHidden = true
        report.isEditable = false; report.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        let scroll = NSScrollView(); scroll.documentView = report; scroll.hasVerticalScroller = true
        scroll.heightAnchor.constraint(equalToConstant: 200).isActive = true
        func row(_ views: [NSView]) -> NSStackView { let s = NSStackView(views: views); s.orientation = .horizontal; return s }
        let stack = NSStackView(views: [intro, steps, row([updateField, chooseUpdate]), keyKind, row([keyField, chooseKey]),
                                        nameField, row([importButton, startButton, status]), progress, scroll])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        for field in [updateField, keyField] { field.widthAnchor.constraint(equalToConstant: 480).isActive = true }
        nameField.widthAnchor.constraint(equalToConstant: 300).isActive = true
        intro.widthAnchor.constraint(equalToConstant: 600).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 600).isActive = true
        window.contentView = stack
        window.center()
        updateField.setAccessibilityLabel("Update file"); keyField.setAccessibilityLabel("Key source")
        progress.setAccessibilityLabel("Import progress")
        refreshSteps()
    }

    /// The wizard's checklist: ✓ when a step is done, ▸ for the one to do next.
    enum Phase { case choosing, importing, imported, refused }
    var phase = Phase.choosing
    func refreshSteps() {
        let haveUpdate = !updateField.stringValue.isEmpty, haveKey = !keyPaths.isEmpty
        func mark(_ done: Bool, _ next: Bool) -> String { done ? "✓" : (next ? "▸" : "○") }
        let importing = phase == .importing, imported = phase == .imported
        steps.stringValue = "\(mark(haveUpdate, !haveUpdate)) 1 Update file     \(mark(haveKey, haveUpdate && !haveKey)) 2 Key     "
            + "\(mark(imported, haveUpdate && haveKey && !imported)) 3 \(importing ? "Importing…" : "Import")     "
            + "\(mark(false, imported)) 4 Start"
    }

    func show() {
        AppLog.log("import window opened")
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func windowWillClose(_ notification: Notification) {
        AppLog.log("import window closed")
        // AppKit is still inside close(): detach the delegate, then let the owner drop this controller on the next
        // main-queue turn so the window is not deallocated under its own close().
        window.delegate = nil
        let close = onClose
        DispatchQueue.main.async { close?() }
    }

    @objc func keyKindChanged() { keyPaths = []; keyField.stringValue = ""; refreshSteps() }

    @objc func chooseUpdate() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.message = "Choose the SP-16 update (.UPD)"
        if panel.runModal() == .OK, let url = panel.url { updateField.stringValue = url.path; refreshSteps() }
    }

    @objc func chooseKey() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = false
        panel.allowsMultipleSelection = keyKind.selectedSegment == 1
        panel.message = keyKind.selectedSegment == 0 ? "Choose the key file"
            : "Choose the GPL archive (.tar.gz) or both zip parts; they are used in name order"
        if panel.runModal() == .OK {
            keyPaths = panel.urls.map { $0.path }.sorted()
            keyField.stringValue = keyPaths.map { ($0 as NSString).lastPathComponent }.joined(separator: ", ")
            refreshSteps()
        }
    }

    @objc func startImport() {
        guard !updateField.stringValue.isEmpty, !keyPaths.isEmpty else { status.stringValue = "Choose the update and the key first."; return }
        var arguments = ["import", "--library", library] + FirmwareTool.boot(config) + ["--update", updateField.stringValue]
        arguments += keyKind.selectedSegment == 0 ? ["--key-file", keyPaths[0]] : ["--gpl-archive"] + keyPaths
        if !nameField.stringValue.isEmpty { arguments += ["--name", nameField.stringValue] }
        importButton.isEnabled = false
        phase = .importing; refreshSteps()
        progress.isHidden = false; progress.startAnimation(nil)
        status.stringValue = "Importing… (the update is decrypted and checked inside the emulator VM)"
        report.string = ""
        // Which files, never the key: its kind and how many parts, not its name or path.
        AppLog.log("import started: update=\((updateField.stringValue as NSString).lastPathComponent) "
                   + "key=\(keyKind.selectedSegment == 0 ? "key file" : "GPL archive, \(keyPaths.count) part(s)") "
                   + "name=\(nameField.stringValue.isEmpty ? "(default)" : nameField.stringValue)")
        DispatchQueue.global(qos: .userInitiated).async {
            let result = FirmwareTool.run(arguments) { line in
                AppLog.log("import progress: \(line.prefix(200))")
                DispatchQueue.main.async { self.status.stringValue = "Importing… \(line)" }
            }
            DispatchQueue.main.async { self.finish(result) }
        }
    }

    func finish(_ result: [String: Any]) {
        importButton.isEnabled = true
        progress.stopAnimation(nil); progress.isHidden = true
        report.string = FirmwareTool.describe(result)
        let imported = result["report"] as? [String: Any]
        AppLog.log("import result: \(result["status"] ?? "?") id=\(result["id"] ?? "-") "
                   + "firmware status=\(imported?["status"] ?? "-") reason=\(imported?["reason"] ?? result["error"] ?? "-")")
        for item in imported?["checks"] as? [[String: Any]] ?? [] where "\(item["result"] ?? "")" != "pass" {
            AppLog.log("import check [\(item["result"] ?? "?")] \(item["check"] ?? "")")
        }
        if result["status"] as? String == "firmware_imported", let id = result["id"] as? String {
            importedID = id
            let report = result["report"] as? [String: Any]
            let verified = report?["status"] as? String == "verified"
            status.stringValue = verified ? "Imported \(id)."
                : "Imported \(id). It is unverified (\(report?["reason"] ?? "no verified profile")) and runs on the reference measurements."
            startButton.isHidden = false
            startButton.keyEquivalent = "\r"; importButton.keyEquivalent = ""   // Return now starts the firmware
            phase = .imported; refreshSteps()
            _ = FirmwareTool.run(["select", "--library", library, "--id", id])
        } else {
            status.stringValue = "Import refused."
            phase = .refused; refreshSteps()
        }
    }

    @objc func startEmulator() {
        guard let id = importedID else { return }
        AppLog.log("start requested on imported firmware \(id)")
        FirmwareTool.relaunch(firmware: id)
    }
}

/// Firmware > Library: list, select (restarts on that firmware), rename and remove imports; each has its own data.
final class FirmwareLibraryWindow: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate {
    let config: Configuration
    let library: String
    let current: String?
    let window: NSWindow
    let table = NSTableView()
    let status = NSTextField(labelWithString: "")
    var entries: [[String: Any]] = []
    var importWindow: FirmwareImportWindow?
    var onClose: (() -> Void)?

    init(config: Configuration, library: String, current: String?) {
        self.config = config; self.library = library; self.current = current
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 360), styleMask: [.titled, .closable, .resizable],
                          backing: .buffered, defer: false)
        super.init()
        // Created in code: the controller owns the window, so AppKit must not also release it on close.
        window.isReleasedWhenClosed = false
        window.title = "Firmware library"
        window.delegate = self
        for (id, title, width) in [("name", "Name", 220.0), ("version", "Version", 70.0), ("kind", "Kind", 60.0),
                                   ("status", "Status", 90.0), ("id", "Id", 240.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title; column.width = CGFloat(width)
            table.addTableColumn(column)
        }
        table.dataSource = self; table.delegate = self
        table.setAccessibilityLabel("Imported firmware")
        let scroll = NSScrollView(); scroll.documentView = table; scroll.hasVerticalScroller = true
        scroll.heightAnchor.constraint(equalToConstant: 240).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 690).isActive = true
        let buttons = NSStackView(views: [
            NSButton(title: "Start selected", target: self, action: #selector(startSelected)),
            NSButton(title: "Rename…", target: self, action: #selector(renameSelected)),
            NSButton(title: "Remove…", target: self, action: #selector(removeSelected)),
            NSButton(title: "Import…", target: self, action: #selector(importFirmware))])
        let stack = NSStackView(views: [scroll, buttons, status])
        stack.orientation = .vertical; stack.alignment = .leading
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        window.contentView = stack
        window.center()
        reload()
    }

    func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    func windowWillClose(_ notification: Notification) {
        AppLog.log("library window closing")
        // An open import is owned here, so take it off screen first: this controller (and with it the import
        // controller) can only be dropped by the owner after both windows have closed.
        importWindow?.window.close()
        // Same teardown as FirmwareImportWindow: the owner drops this controller only after close() has returned.
        window.delegate = nil
        let close = onClose
        DispatchQueue.main.async { close?() }
    }

    func reload() {
        let result = FirmwareTool.run(["list", "--library", library])
        entries = result["firmware"] as? [[String: Any]] ?? []
        if let error = result["error"] as? String { status.stringValue = error; AppLog.log("library list failed: \(error)") }
        table.reloadData()
    }

    func numberOfRows(in tableView: NSTableView) -> Int { entries.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let key = tableColumn?.identifier.rawValue else { return nil }
        var text = "\(entries[row][key] ?? "")"
        if key == "name", entries[row]["id"] as? String == current { text += " (running)" }
        return NSTextField(labelWithString: text)
    }

    var selected: [String: Any]? { table.selectedRow >= 0 && table.selectedRow < entries.count ? entries[table.selectedRow] : nil }

    @objc func startSelected() {
        guard let entry = selected, let id = entry["id"] as? String else { return }
        AppLog.log("library: start selected \(id) (status \(entry["status"] ?? "?"))")
        _ = FirmwareTool.run(["select", "--library", library, "--id", id])
        FirmwareTool.relaunch(firmware: id)
    }

    @objc func renameSelected() {
        guard let id = selected?["id"] as? String else { return }
        let alert = NSAlert(); alert.messageText = "Rename \(id)"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = selected?["name"] as? String ?? ""
        alert.accessoryView = field; alert.addButton(withTitle: "Rename"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let result = FirmwareTool.run(["rename", "--library", library, "--id", id, "--name", field.stringValue])
        AppLog.log("library: rename \(id): \(result["error"] ?? "ok")")
        status.stringValue = result["error"] as? String ?? "Renamed."
        reload()
    }

    @objc func removeSelected() {
        guard let id = selected?["id"] as? String else { return }
        guard id != current else { status.stringValue = "The running firmware cannot be removed."; return }
        let alert = NSAlert(); alert.messageText = "Remove \(id)?"
        alert.informativeText = "Its imported image, manifest and its own projects and settings (data image) are deleted."
        alert.addButton(withTitle: "Remove"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let result = FirmwareTool.run(["remove", "--library", library, "--id", id])
        AppLog.log("library: remove \(id): \(result["error"] ?? "ok")")
        status.stringValue = result["error"] as? String ?? "Removed."
        reload()
    }

    @objc func importFirmware() {
        AppLog.log("library: import requested")
        let importer = FirmwareImportWindow(config: config, library: library, firstRun: false)
        importer.onClose = { [weak self] in self?.importWindow = nil; self?.reload() }
        importWindow = importer
        importer.show()
    }
}

/// The standard Edit menu, so text fields (import name, rename) get Undo/Redo, Cut, Copy, Paste and Select All with
/// their usual shortcuts. Actions go to the first responder (target nil). Decoupled builds only.
func editMenuItem() -> NSMenuItem {
    let item = NSMenuItem()
    let menu = NSMenu(title: "Edit")
    menu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    let redo = menu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
    redo.keyEquivalentModifierMask = [.command, .shift]
    menu.addItem(.separator())
    menu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    menu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    menu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    menu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    item.submenu = menu
    return item
}

/// App delegate for a decoupled build that has no usable firmware yet: only the import screen, no VM.
final class FirmwareSetupApplication: NSObject, NSApplicationDelegate {
    let importer: FirmwareImportWindow
    init(config: Configuration, library: String) {
        importer = FirmwareImportWindow(config: config, library: library, firstRun: true)
        super.init()
        importer.onClose = { NSApp.terminate(nil) }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let menu = NSMenu(), appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(editMenuItem())
        menu.addItem(helpMenuItem())
        NSApp.mainMenu = menu
        importer.show()
        DiagnosticsController.shared.checkPreviousRun(current: nil)
    }
}

/// App delegate for a decoupled build with imports but none selected: the library window to pick one.
final class FirmwareLibrarySetupApplication: NSObject, NSApplicationDelegate {
    let libraryWindow: FirmwareLibraryWindow
    init(config: Configuration, library: String) {
        libraryWindow = FirmwareLibraryWindow(config: config, library: library, current: nil)
        super.init()
        libraryWindow.onClose = { NSApp.terminate(nil) }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let menu = NSMenu(), appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(editMenuItem())
        menu.addItem(helpMenuItem())
        NSApp.mainMenu = menu
        libraryWindow.show()
        DiagnosticsController.shared.checkPreviousRun(current: nil)
    }
}

/// The one-time, non-blocking notice for firmware that runs without a verified profile (D2 in docs/architecture.md).
/// It shows the compatibility findings once per firmware image; the window title and the library's Status column
/// keep saying "unverified".
enum UnverifiedNotice {
    static var panel: NSPanel?
    static let defaultsKey = "sp16.unverified-notice-seen"

    static func showOnce(key: String, text: String) {
        var seen = UserDefaults.standard.stringArray(forKey: defaultsKey) ?? []
        guard !seen.contains(key) else { return }
        seen.append(key)
        UserDefaults.standard.set(Array(seen.suffix(200)), forKey: defaultsKey)
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 560, height: 300),
                            styleMask: [.titled, .closable, .utilityWindow, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.title = "Unverified firmware"
        panel.isReleasedWhenClosed = false
        panel.becomesKeyOnlyIfNeeded = true
        let view = NSTextView(frame: NSRect(x: 0, y: 0, width: 520, height: 220))
        view.isEditable = false
        view.string = text
        view.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        let scroll = NSScrollView(); scroll.documentView = view; scroll.hasVerticalScroller = true
        scroll.heightAnchor.constraint(equalToConstant: 220).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 520).isActive = true
        let close = NSButton(title: "OK", target: panel, action: #selector(NSWindow.performClose(_:)))
        let stack = NSStackView(views: [scroll, close])
        stack.orientation = .vertical; stack.alignment = .trailing
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        panel.contentView = stack
        panel.center()
        panel.orderFront(nil)
        Self.panel = panel
    }
}
