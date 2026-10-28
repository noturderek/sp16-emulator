import AppKit

// Host-side SP-16 panel: vector renderer, pointer/keyboard input, accessibility.
// Geometry comes from panel-layout.json (canvas units). Everything that draws, hit-tests,
// exposes accessibility frames or maps LCD pointers goes through one PanelTransform.

// MARK: - Delegate contract

protocol PanelViewDelegate: AnyObject {
    /// Semantic control event. action: 0 release, 1 press, 2 delta (value = signed detents), 3 value/move.
    /// Positive delta means clockwise / next / increase. Pads: press value = velocity 1...127, release 0.
    /// Strip: press/move value = position 0...1000 (0 = bottom), release 0. Other buttons: press 1, release 0.
    func panelControl(_ id: UInt8, action: UInt8, value: Int16)
    /// OEM LCD touch in 800x480 framebuffer coordinates. action: 0 move, 1 down, 2 up.
    func panelTouch(x: Int, y: Int, action: UInt8)
}

// MARK: - Layout model (panel-layout.json)

struct PanelLayout: Decodable {
    struct Canvas: Decodable {
        let width: CGFloat, height: CGFloat
        let cornerRadius: CGFloat, railWidth: CGFloat, plinthTop: CGFloat
        var size: CGSize { CGSize(width: width, height: height) }
    }

    struct Box: Decodable {
        let x: CGFloat, y: CGFloat, w: CGFloat, h: CGFloat
        let radius: CGFloat?
        var rect: CGRect { CGRect(x: x, y: y, width: w, height: h) }
    }

    struct LCD: Decodable {
        struct Framebuffer: Decodable { let width: Int, height: Int }
        let bezel: Box, active: Box, framebuffer: Framebuffer
    }

    enum Kind: String, Decodable { case pad, button, step, bar, knob, selector, strip, analog, lamp }

    struct Control: Decodable {
        let id: UInt8
        let name: String
        let kind: Kind
        let group: String
        let shape: String
        let accessibility: String
        let cx: CGFloat, cy: CGFloat
        let w: CGFloat?, h: CGFloat?, r: CGFloat?, capR: CGFloat?, radius: CGFloat?
        let label: String?, style: String?
        let number: String?, numberDy: CGFloat?, numberAlign: String?, numberBox: Bool?
        let initial: Int?   // analog only: where the knob starts, host value 0...1000 (panel_map.ANALOG_DEFAULTS)

        var isInput: Bool { kind != .lamp }
        var isCircle: Bool { shape == "circle" }
        var center: CGPoint { CGPoint(x: cx, y: cy) }
        var frame: CGRect {
            if isCircle { let r = self.r ?? 0; return CGRect(x: cx - r, y: cy - r, width: 2 * r, height: 2 * r) }
            let w = self.w ?? 0, h = self.h ?? 0
            return CGRect(x: cx - w / 2, y: cy - h / 2, width: w, height: h)
        }
        var cornerRadius: CGFloat { shape == "pill" ? frame.height / 2 : (radius ?? 0) }

        /// Exact hit shape in canvas units.
        func contains(_ p: CGPoint) -> Bool {
            if isCircle { return hypot(p.x - cx, p.y - cy) <= (r ?? 0) }
            let f = frame
            guard f.contains(p) else { return false }
            let cr = min(cornerRadius, f.width / 2, f.height / 2)
            let dx = max(f.minX + cr - p.x, 0, p.x - (f.maxX - cr))
            let dy = max(f.minY + cr - p.y, 0, p.y - (f.maxY - cr))
            return hypot(dx, dy) <= cr
        }
    }

    struct Decor: Decodable {
        let type: String
        let text: String?, name: String?, align: String?, weight: String?, color: String?, face: String?, fill: String?
        let x: CGFloat?, y: CGFloat?, w: CGFloat?, h: CGFloat?, x2: CGFloat?, y2: CGFloat?
        let cx: CGFloat?, cy: CGFloat?, r: CGFloat?, size: CGFloat?, tracking: CGFloat?
        let radius: CGFloat?, lineWidth: CGFloat?, from: CGFloat?, to: CGFloat?, tick: CGFloat?
        let count: Int?
        let gaps: [[CGFloat]]?, points: [[CGFloat]]?
        let width0: CGFloat?, width1: CGFloat?    // "arc": stroke width at `from` and at `to` (tapered scale)
        var rect: CGRect { CGRect(x: x ?? 0, y: y ?? 0, width: w ?? 0, height: h ?? 0) }
    }

    /// The rear I/O panel: drawn in code, traced from the product's rear view; no controls, no input.
    struct Rear: Decodable {
        struct Canvas: Decodable {
            let width: CGFloat, height: CGFloat, plateTop: CGFloat, plateBottom: CGFloat
            var size: CGSize { CGSize(width: width, height: height) }
        }
        struct Group: Decodable {
            let label: String
            let x: CGFloat, y: CGFloat, w: CGFloat, h: CGFloat
            var rect: CGRect { CGRect(x: x, y: y, width: w, height: h) }
        }
        struct Item: Decodable {
            let kind: String
            let cx: CGFloat, cy: CGFloat
            let text: String?, size: CGFloat?, weight: String?, y2: CGFloat?
            var center: CGPoint { CGPoint(x: cx, y: cy) }
        }
        let canvas: Canvas
        let groups: [Group]
        let items: [Item]
    }

    let version: Int
    let canvas: Canvas
    let lcd: LCD
    let rear: Rear?
    let controls: [Control]
    let decor: [Decor]

    struct LoadError: Error, CustomStringConvertible {
        let description: String
    }

    static func load(from url: URL) throws -> PanelLayout {
        let data: Data
        do { data = try Data(contentsOf: url) } catch { throw LoadError(description: "cannot read \(url.path)") }
        let layout: PanelLayout
        do { layout = try JSONDecoder().decode(PanelLayout.self, from: data) } catch {
            throw LoadError(description: "invalid \(url.lastPathComponent): \(error)")
        }
        guard layout.version == 1 else { throw LoadError(description: "unsupported panel-layout version \(layout.version)") }
        guard Set(layout.controls.map(\.id)).count == layout.controls.count else {
            throw LoadError(description: "duplicate control ids in \(url.lastPathComponent)")
        }
        let active = layout.lcd.active
        guard abs(active.w / active.h - CGFloat(layout.lcd.framebuffer.width) / CGFloat(layout.lcd.framebuffer.height)) < 0.005 else {
            throw LoadError(description: "lcd.active is not \(layout.lcd.framebuffer.width):\(layout.lcd.framebuffer.height)")
        }
        return layout
    }

    func control(_ id: UInt8) -> Control? { controls.first { $0.id == id } }
}

// MARK: - Geometry

/// The single canvas <-> view mapping (uniform scale, centered letterbox). View space is flipped (y down).
struct PanelTransform {
    var scale: CGFloat
    var origin: CGPoint

    static func fit(canvas: CGSize, in rect: CGRect) -> PanelTransform {
        let s = max(0.01, min(rect.width / canvas.width, rect.height / canvas.height))
        return PanelTransform(scale: s, origin: CGPoint(x: rect.midX - canvas.width * s / 2,
                                                         y: rect.midY - canvas.height * s / 2))
    }
    func toView(_ p: CGPoint) -> CGPoint { CGPoint(x: origin.x + p.x * scale, y: origin.y + p.y * scale) }
    func toCanvas(_ p: CGPoint) -> CGPoint { CGPoint(x: (p.x - origin.x) / scale, y: (p.y - origin.y) / scale) }
    func toView(_ r: CGRect) -> CGRect {
        CGRect(x: origin.x + r.minX * scale, y: origin.y + r.minY * scale, width: r.width * scale, height: r.height * scale)
    }
    func toCanvas(_ r: CGRect) -> CGRect {
        CGRect(x: (r.minX - origin.x) / scale, y: (r.minY - origin.y) / scale, width: r.width / scale, height: r.height / scale)
    }
}

// MARK: - Colour and drawing primitives

private let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

func hexColor(_ v: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: sRGB, components: [CGFloat((v >> 16) & 255) / 255, CGFloat((v >> 8) & 255) / 255,
                                           CGFloat(v & 255) / 255, a])!
}

struct LampColor {
    var r: CGFloat, g: CGFloat, b: CGFloat
    init(r: CGFloat, g: CGFloat, b: CGFloat) { self.r = r; self.g = g; self.b = b }
    init(_ l: PanelView.Lamp) { self.init(r: CGFloat(l.r) / 255, g: CGFloat(l.g) / 255, b: CGFloat(l.b) / 255) }
    func cg(_ a: CGFloat = 1) -> CGColor { CGColor(colorSpace: sRGB, components: [r, g, b, a])! }
    func mixed(with o: LampColor, _ t: CGFloat) -> LampColor {
        LampColor(r: r + (o.r - r) * t, g: g + (o.g - g) * t, b: b + (o.b - b) * t)
    }
    func lighter(_ t: CGFloat) -> LampColor { mixed(with: LampColor(r: 1, g: 1, b: 1), t) }
    func darker(_ t: CGFloat) -> LampColor { mixed(with: LampColor(r: 0, g: 0, b: 0), t) }
}

enum LampLook {
    case unknown          // no firmware feedback (never received, or stale): flat dark neutral
    case off              // live feedback, lamp dark
    case lit(LampColor)
}

func roundedPath(_ r: CGRect, _ radius: CGFloat) -> CGPath {
    let cr = max(0, min(radius, r.width / 2, r.height / 2))
    return CGPath(roundedRect: r, cornerWidth: cr, cornerHeight: cr, transform: nil)
}

extension CGContext {
    func gradient(_ stops: [(CGFloat, CGColor)]) -> CGGradient {
        CGGradient(colorsSpace: sRGB, colors: stops.map { $0.1 } as CFArray, locations: stops.map { $0.0 })!
    }
    func fillLinear(_ stops: [(CGFloat, CGColor)], from a: CGPoint, to b: CGPoint) {
        drawLinearGradient(gradient(stops), start: a, end: b, options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    }
    func fillRadial(_ stops: [(CGFloat, CGColor)], center: CGPoint, radius: CGFloat, focus: CGPoint? = nil) {
        drawRadialGradient(gradient(stops), startCenter: focus ?? center, startRadius: 0, endCenter: center,
                           endRadius: radius, options: [])
    }
    func fill(path: CGPath, color: CGColor) { addPath(path); setFillColor(color); fillPath() }
    func stroke(path: CGPath, color: CGColor, width: CGFloat) {
        addPath(path); setStrokeColor(color); setLineWidth(width); strokePath()
    }
    func fillCircle(_ c: CGPoint, _ r: CGFloat, _ color: CGColor) {
        setFillColor(color); fillEllipse(in: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
    }
    func strokeCircle(_ c: CGPoint, _ r: CGFloat, _ color: CGColor, _ width: CGFloat) {
        setStrokeColor(color); setLineWidth(width)
        strokeEllipse(in: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
    }
    func line(_ a: CGPoint, _ b: CGPoint, _ color: CGColor, _ width: CGFloat, cap: CGLineCap = .butt) {
        setStrokeColor(color); setLineWidth(width); setLineCap(cap)
        move(to: a); addLine(to: b); strokePath()
    }
}

private struct SeededRandom {
    var state: UInt64
    mutating func next() -> CGFloat {
        state = state &* 6364136223846793005 &+ 1442695040888963407
        return CGFloat((state >> 33) & 0xFFFFFF) / CGFloat(0xFFFFFF)
    }
}

private let sans = "system"

// MARK: - Renderer

/// Everything the dynamic layer needs; snapshot owned by PanelView.
struct PanelRenderState {
    var lamps: [UInt8: PanelView.Lamp] = [:]
    var feedbackLive = false            // true once feedback arrived and the link is live
    var held: Set<UInt8> = []
    var knobAngle: [UInt8: CGFloat] = [:]   // degrees, clockwise from 12 o'clock
    var selectorAngle: CGFloat = 0
    var stripTouch: Int?
    var lcdImage: CGImage?
    var debugOverlay = false
    var bootStage = "Starting the virtual machine"   // shown on the dark LCD until the first firmware frame
    var bootSeconds = 0
    var lcdBrightness: CGFloat = 1      // the firmware's LCD backlight level, 0...1 (1 = full, as before)
    var bloom = false                   // additive glow around lit keys, buttons and LEDs (View > Lamp Glow; off by default)
    var panelDark = false               // standby/fault: black LCD, every lamp off except WAKE UP
    var fault = false                   // power fault: dark panel, WAKE UP blinking red

    func look(_ id: UInt8) -> LampLook {
        guard feedbackLive else { return .unknown }
        guard let l = lamps[id], max(l.r, l.g, l.b) >= 8 else { return .off }
        return .lit(LampColor(l))
    }
}

final class PanelRenderer {
    let layout: PanelLayout
    private var fontCache: [String: NSFont] = [:]

    init(layout: PanelLayout) { self.layout = layout }

    // MARK: Static layer

    /// Renders backdrop + chassis + printed art + empty control shapes into a top-down bitmap.
    func makeStaticLayer(viewSize: CGSize, scale: CGFloat, transform: PanelTransform, lcdOnly: Bool, rear: Bool = false, skin: CGImage? = nil) -> CGImage? {
        guard let ctx = CGContext(data: nil, width: Int(ceil(viewSize.width * scale)), height: Int(ceil(viewSize.height * scale)),
                                  bitsPerComponent: 8, bytesPerRow: 0, space: sRGB,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
        else { return nil }
        ctx.scaleBy(x: scale, y: scale)
        ctx.translateBy(x: 0, y: viewSize.height)
        ctx.scaleBy(x: 1, y: -1)
        ctx.setShouldSmoothFonts(true)
        ctx.setShouldAntialias(true)

        NSGraphicsContext.saveGraphicsState()
        defer { NSGraphicsContext.restoreGraphicsState() }
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: true)

        ctx.setFillColor(hexColor(0x1c1d20))
        ctx.fill(CGRect(origin: .zero, size: viewSize))
        guard !lcdOnly else { return ctx.makeImage() }

        ctx.translateBy(x: transform.origin.x, y: transform.origin.y)
        ctx.scaleBy(x: transform.scale, y: transform.scale)
        let s = transform.scale
        if rear {
            if let r = layout.rear { drawRear(ctx, r) }
            return ctx.makeImage()
        }
        drawChassis(ctx, s)
        for d in layout.decor { drawDecor(ctx, d) }
        for c in layout.controls { drawControlStatic(ctx, c, s) }
        drawLCDBezel(ctx)
        if let skin {
            // The user's own overlay (logos, printing), stretched over the whole canvas; lamps still draw on top.
            ctx.saveGState()
            ctx.translateBy(x: 0, y: layout.canvas.height); ctx.scaleBy(x: 1, y: -1)
            ctx.interpolationQuality = .high
            ctx.draw(skin, in: CGRect(origin: .zero, size: layout.canvas.size))
            ctx.restoreGState()
        }
        return ctx.makeImage()
    }

    // MARK: Rear I/O panel

    private func drawRear(_ ctx: CGContext, _ r: PanelLayout.Rear) {
        let cv = r.canvas
        let plate = CGRect(x: 0, y: cv.plateTop, width: cv.width, height: cv.plateBottom - cv.plateTop)
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: 6), blur: 14, color: hexColor(0, 0.6))
        ctx.fill(path: roundedPath(plate, 6), color: hexColor(0x141517))
        ctx.restoreGState()
        ctx.saveGState(); ctx.addPath(roundedPath(plate, 6)); ctx.clip()
        if let metal = brushedMetal(plate.size, base: 30, rowSigma: 2.5, streakSigma: 5, seed: 71) { ctx.draw(metal, in: plate) }
        ctx.fillLinear([(0, hexColor(0xffffff, 0.07)), (0.2, hexColor(0, 0)), (1, hexColor(0, 0.25))],
                       from: CGPoint(x: 0, y: plate.minY), to: CGPoint(x: 0, y: plate.maxY))
        ctx.restoreGState()
        ctx.stroke(path: roundedPath(plate.insetBy(dx: 0.5, dy: 0.5), 6), color: hexColor(0xffffff, 0.12), width: 1)

        for g in r.groups {
            // A white-outlined box under a white header tab, as printed on the unit.
            let box = g.rect, tab = CGRect(x: box.minX, y: box.minY, width: box.width, height: 28)
            ctx.stroke(path: roundedPath(box, 4), color: hexColor(0xf2f3f4), width: 1.8)
            ctx.fill(path: roundedPath(tab, 4), color: hexColor(0xe6e7e9))
            drawText(g.label, at: CGPoint(x: tab.midX, y: tab.midY), size: g.label.count > 9 ? 14 : 16, weight: .bold,
                     color: hexColor(0x1a1b1d))
        }
        for i in r.items { drawRearItem(ctx, i) }
    }

    private func drawRearItem(_ ctx: CGContext, _ i: PanelLayout.Rear.Item) {
        let c = i.center
        switch i.kind {
        case "text":
            drawText(i.text ?? "", at: c, size: i.size ?? 14, weight: PanelRenderer.weight(i.weight ?? "semibold"), color: hexColor(0xf2f3f4))
        case "rule":
            ctx.line(c, CGPoint(x: c.x, y: i.y2 ?? c.y), hexColor(0xf2f3f4), 1.6)
        case "jack":
            // 6.3 mm TRS socket: gold ring, dark bore.
            ctx.fillCircle(c, 17, hexColor(0x0b0b0c))
            ctx.fillRadial([(0, hexColor(0xf3d46a)), (0.6, hexColor(0xb8892a)), (1, hexColor(0x6b4d14))], center: c, radius: 16)
            ctx.fillCircle(c, 11, hexColor(0x070708))
            ctx.fillCircle(c, 6, hexColor(0x1c1d20))
            ctx.strokeCircle(c, 16, hexColor(0xffffff, 0.2), 1)
        case "knob":
            ctx.saveGState()
            ctx.setShadow(offset: CGSize(width: 0, height: 3), blur: 6, color: hexColor(0, 0.8))
            ctx.fillCircle(c, 21, hexColor(0x0d0d0f))
            ctx.restoreGState()
            spunMetal(ctx, c, radius: 18, low: 0.05, high: 0.22, rings: false)
            ctx.line(CGPoint(x: c.x, y: c.y - 5), CGPoint(x: c.x, y: c.y - 16), hexColor(0xe4e5e8), 2.5, cap: .round)
        case "din":
            drawDIN(ctx, c)
        case "rj45":
            let body = CGRect(x: c.x - 24, y: c.y - 22, width: 48, height: 44)
            ctx.fill(path: roundedPath(body, 3), color: hexColor(0xb9bbc0))
            ctx.saveGState(); ctx.addPath(roundedPath(body, 3)); ctx.clip()
            ctx.fillLinear([(0, hexColor(0xffffff, 0.35)), (1, hexColor(0, 0.25))], from: CGPoint(x: 0, y: body.minY), to: CGPoint(x: 0, y: body.maxY))
            ctx.restoreGState()
            let socket = body.insetBy(dx: 6, dy: 6)
            ctx.fill(path: roundedPath(socket, 2), color: hexColor(0x0a0a0b))
            for k in 0..<8 { ctx.line(CGPoint(x: socket.minX + 4 + CGFloat(k) * 4.2, y: socket.minY + 2), CGPoint(x: socket.minX + 4 + CGFloat(k) * 4.2, y: socket.minY + 9), hexColor(0xc9a243), 1.4) }
        case "usbb":
            let body = CGRect(x: c.x - 19, y: c.y - 20, width: 38, height: 40)
            ctx.fill(path: roundedPath(body, 3), color: hexColor(0xb0b2b7))
            let port = body.insetBy(dx: 5, dy: 5)
            ctx.fill(path: roundedPath(port, 2), color: hexColor(0x0a0a0b))
            ctx.fill(path: roundedPath(port.insetBy(dx: 4, dy: 5).offsetBy(dx: 0, dy: 2), 1.5), color: hexColor(0x3a3b3f))
            ctx.fillCircle(CGPoint(x: c.x, y: body.minY - 9), 4, hexColor(0xe6e7e9, 0.8))
        case "dc":
            ctx.fillCircle(c, 17, hexColor(0x0b0b0c))
            ctx.strokeCircle(c, 15, hexColor(0x4a4c50), 2)
            ctx.fillCircle(c, 8, hexColor(0x060607))
            ctx.fillCircle(c, 3, hexColor(0x8d8f93))
        case "power":
            ctx.saveGState()
            ctx.setShadow(offset: CGSize(width: 0, height: 2), blur: 4, color: hexColor(0, 0.8))
            ctx.fillCircle(c, 19, hexColor(0x1b1c1f))
            ctx.restoreGState()
            ctx.strokeCircle(c, 19, hexColor(0x3c3e42), 1.5)
            // Standby glyph printed above the button: an open ring with a stroke through the gap.
            let g = CGPoint(x: c.x, y: c.y - 41)
            ctx.setLineCap(.round); ctx.setStrokeColor(hexColor(0xe6e7e9)); ctx.setLineWidth(1.8)
            ctx.addArc(center: g, radius: 7, startAngle: -.pi * 0.3, endAngle: .pi * 1.3, clockwise: false)
            ctx.strokePath()
            ctx.line(CGPoint(x: g.x, y: g.y - 9), CGPoint(x: g.x, y: g.y - 1), hexColor(0xe6e7e9), 1.8, cap: .round)
        case "screw":
            ctx.fillCircle(c, 8, hexColor(0x0c0c0e))
            ctx.strokeCircle(c, 7, hexColor(0x3a3b3f), 1.4)
            ctx.line(CGPoint(x: c.x - 4, y: c.y), CGPoint(x: c.x + 4, y: c.y), hexColor(0x55575b), 1.6)
        case "lock":
            ctx.fill(path: roundedPath(CGRect(x: c.x - 14, y: c.y - 8, width: 28, height: 16), 3), color: hexColor(0x0b0b0c))
            ctx.stroke(path: roundedPath(CGRect(x: c.x - 14, y: c.y - 8, width: 28, height: 16), 3), color: hexColor(0x55575b), width: 1)
            drawText("K", at: CGPoint(x: c.x, y: c.y - 24), size: 15, weight: .black, color: hexColor(0xf2f3f4))
        case "clip":
            ctx.fill(path: roundedPath(CGRect(x: c.x - 26, y: c.y - 36, width: 52, height: 72), 14), color: hexColor(0x0b0b0c))
            ctx.stroke(path: roundedPath(CGRect(x: c.x - 25, y: c.y - 35, width: 50, height: 70), 14), color: hexColor(0x3a3b3f), width: 1.5)
            ctx.fill(path: roundedPath(CGRect(x: c.x - 12, y: c.y - 14, width: 24, height: 28), 8), color: hexColor(0x1d1e21))
        default:
            break
        }
    }

    /// 5-pin DIN socket with its two mounting screws drawn separately as `screw` items.
    private func drawDIN(_ ctx: CGContext, _ c: CGPoint) {
        ctx.fillCircle(c, 25, hexColor(0xb3b5ba))
        ctx.fillRadial([(0, hexColor(0xffffff, 0.3)), (1, hexColor(0, 0.25))], center: c, radius: 25)
        ctx.fillCircle(c, 20, hexColor(0x0a0a0b))
        ctx.strokeCircle(c, 20, hexColor(0x4a4c50), 1.2)
        ctx.fill(path: roundedPath(CGRect(x: c.x - 5, y: c.y - 21, width: 10, height: 5), 1), color: hexColor(0xb3b5ba))
        for k in 0..<5 {
            let a = CGFloat.pi + CGFloat(k) * .pi / 4
            ctx.fillCircle(CGPoint(x: c.x + cos(a) * 11, y: c.y + 5 + sin(a) * 11), 2.2, hexColor(0xc9a243))
        }
    }

    private func drawChassis(_ ctx: CGContext, _ s: CGFloat) {
        let cv = layout.canvas
        let rect = CGRect(origin: .zero, size: cv.size)
        let body = roundedPath(rect, cv.cornerRadius)
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: -9 * s), blur: 22 * s, color: hexColor(0, 0.65))
        ctx.fill(path: body, color: hexColor(0x151618))
        ctx.restoreGState()

        ctx.saveGState()
        ctx.addPath(body); ctx.clip()
        let face = CGRect(x: 0, y: 0, width: cv.width, height: cv.plinthTop)
        if let metal = brushedMetal(face.size, base: 29, rowSigma: 5.5, streakSigma: 5, seed: 7) { ctx.draw(metal, in: face) }
        ctx.fillLinear([(0, hexColor(0xffffff, 0.10)), (0.012, hexColor(0xffffff, 0)), (0.8, hexColor(0, 0)), (1, hexColor(0, 0.18))],
                       from: .zero, to: CGPoint(x: 0, y: cv.plinthTop))

        // Side rails
        let rw = cv.railWidth
        for (x, leftSide) in [(CGFloat(0), true), (cv.width - rw, false)] {
            let r = CGRect(x: x, y: 0, width: rw, height: cv.height)
            ctx.saveGState(); ctx.clip(to: r)
            let a = leftSide ? 0x4d4f53 : 0x2a2b2e, b = leftSide ? 0x2b2c2f : 0x4a4c50
            ctx.fillLinear([(0, hexColor(UInt32(a))), (1, hexColor(UInt32(b)))], from: CGPoint(x: r.minX, y: 0), to: CGPoint(x: r.maxX, y: 0))
            brush(ctx, r, strength: 0.05, seed: leftSide ? 11 : 13)
            ctx.restoreGState()
            let edge = leftSide ? rw : cv.width - rw
            ctx.line(CGPoint(x: edge, y: 0), CGPoint(x: edge, y: cv.height), hexColor(0, 0.75), 1.4)
            ctx.line(CGPoint(x: edge + (leftSide ? 1.4 : -1.4), y: 0), CGPoint(x: edge + (leftSide ? 1.4 : -1.4), y: cv.height),
                     hexColor(0xffffff, 0.09), 1)
        }

        // Plinth
        let plinth = CGRect(x: 0, y: cv.plinthTop, width: cv.width, height: cv.height - cv.plinthTop)
        ctx.saveGState(); ctx.clip(to: plinth)
        if let metal = brushedMetal(plinth.size, base: 47, rowSigma: 3.5, streakSigma: 8, seed: 29) { ctx.draw(metal, in: plinth) }
        ctx.fillLinear([(0, hexColor(0xffffff, 0.08)), (0.15, hexColor(0, 0)), (1, hexColor(0, 0.22))],
                       from: CGPoint(x: 0, y: plinth.minY), to: CGPoint(x: 0, y: plinth.maxY))
        ctx.restoreGState()
        ctx.line(CGPoint(x: rw, y: cv.plinthTop), CGPoint(x: cv.width - rw, y: cv.plinthTop), hexColor(0, 0.8), 2)
        ctx.line(CGPoint(x: rw, y: cv.plinthTop + 2), CGPoint(x: cv.width - rw, y: cv.plinthTop + 2), hexColor(0xffffff, 0.1), 1)
        ctx.restoreGState()

        ctx.stroke(path: roundedPath(rect.insetBy(dx: 0.5, dy: 0.5), cv.cornerRadius), color: hexColor(0xffffff, 0.14), width: 1)
    }

    /// Hairline-brushed aluminium, measured on the product's top-view render: a flat grey base, crisp per-row offsets
    /// (row-to-row sigma about 5.5) and long horizontal streaks (along-row sigma about 5). Generated once per size at
    /// 2x canvas resolution and cached; the panel only rebuilds it when the canvas changes.
    private var metalCache: [String: CGImage] = [:]

    private func brushedMetal(_ size: CGSize, base: Double, rowSigma: Double, streakSigma: Double, seed: UInt64) -> CGImage? {
        let key = "\(Int(size.width))x\(Int(size.height))/\(base)/\(seed)"
        if let cached = metalCache[key] { return cached }
        let w = max(1, Int(size.width * 2)), h = max(1, Int(size.height * 2))
        var rng = SeededRandom(state: seed)
        func gauss() -> Double { Double(rng.next() + rng.next() + rng.next() + rng.next() - 2) * 1.732 }
        var pixels = [UInt8](repeating: 255, count: w * h * 4)
        var row = [Double](repeating: 0, count: w)
        for y in 0..<h {
            let offset = gauss() * rowSigma
            for x in 0..<w { row[x] = offset + gauss() * 1.2 }
            for _ in 0..<max(2, w / 110) {
                let x0 = Int(rng.next() * CGFloat(w)), len = Int(40 + rng.next() * 1600), amp = gauss() * streakSigma
                let end = min(w, x0 + len)
                guard end > x0 else { continue }
                for x in x0..<end { row[x] += amp * sin(Double(x - x0) / Double(len) * .pi) }
            }
            for x in 0..<w {
                let v = max(0, min(253, base + row[x])), o = (y * w + x) * 4
                pixels[o] = UInt8(v); pixels[o + 1] = UInt8(v); pixels[o + 2] = UInt8(v + 2)
            }
        }
        guard let provider = CGDataProvider(data: Data(pixels) as CFData),
              let image = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4, space: sRGB,
                                  bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue), provider: provider,
                                  decode: nil, shouldInterpolate: true, intent: .defaultIntent)
        else { return nil }
        metalCache[key] = image
        return image
    }

    private func brush(_ ctx: CGContext, _ r: CGRect, strength: CGFloat, seed: UInt64) {
        var rng = SeededRandom(state: seed)
        ctx.saveGState(); ctx.clip(to: r)
        ctx.setLineWidth(0.7)
        var y = r.minY
        while y < r.maxY {
            for _ in 0..<3 {
                let x0 = r.minX + rng.next() * r.width, len = 80 + rng.next() * 700
                let a = rng.next() * strength
                ctx.setStrokeColor(rng.next() > 0.45 ? hexColor(0xffffff, a) : hexColor(0, a * 1.7))
                ctx.move(to: CGPoint(x: x0, y: y)); ctx.addLine(to: CGPoint(x: x0 + len, y: y)); ctx.strokePath()
            }
            y += 0.5
        }
        ctx.restoreGState()
    }

    // MARK: Text

    func font(_ face: String, _ size: CGFloat, _ weight: NSFont.Weight) -> NSFont {
        let key = "\(face)/\(size)/\(weight.rawValue)"
        if let f = fontCache[key] { return f }
        var f: NSFont
        switch face {
        case "script":
            f = NSFont(name: "SnellRoundhand-Bold", size: size)
                ?? NSFont(descriptor: NSFont.systemFont(ofSize: size, weight: .semibold).fontDescriptor.withDesign(.serif)!, size: size)
                ?? NSFont.systemFont(ofSize: size)
        default:
            f = NSFont.systemFont(ofSize: size, weight: weight)
        }
        fontCache[key] = f
        return f
    }

    static func weight(_ name: String?) -> NSFont.Weight {
        switch name {
        case "regular": return .regular
        case "medium": return .medium
        case "bold": return .bold
        case "heavy": return .heavy
        default: return .semibold
        }
    }

    /// Button styles lit through a rim light pipe rather than a translucent cap.
    static let rimLitStyles: Set<String> = ["white", "mode", "rec", "stop", "play"]

    static func color(_ name: String?) -> CGColor {
        switch name {
        case "gray": return hexColor(0x8d8f93)
        case "dark": return hexColor(0x1b1b1d)
        case "green": return hexColor(0xb7f25b)
        default: return hexColor(0xeceeef)
        }
    }

    /// Draws text with its cap-height middle on `p.y`. Requires a flipped NSGraphicsContext.
    @discardableResult
    func drawText(_ text: String, at p: CGPoint, size: CGFloat, weight: NSFont.Weight = .semibold, color: CGColor,
                  align: String = "center", face: String = sans, tracking: CGFloat = 0) -> CGFloat {
        let f = font(face, size, weight)
        var attrs: [NSAttributedString.Key: Any] = [.font: f, .foregroundColor: NSColor(cgColor: color) ?? .white]
        if tracking != 0 { attrs[.kern] = tracking }
        let str = NSAttributedString(string: text, attributes: attrs)
        let w = str.size().width - tracking
        let x: CGFloat
        switch align {
        case "left": x = p.x
        case "right": x = p.x - w
        default: x = p.x - w / 2
        }
        let top = p.y + f.capHeight / 2 - f.ascender
        str.draw(at: CGPoint(x: x, y: top))
        return w
    }

    // MARK: Printed art

    private func drawDecor(_ ctx: CGContext, _ d: PanelLayout.Decor) {
        let ink = PanelRenderer.color(d.color)
        let lw = d.lineWidth ?? 1.6
        switch d.type {
        case "text":
            drawText(d.text ?? "", at: CGPoint(x: d.x ?? 0, y: d.y ?? 0), size: d.size ?? 14, weight: PanelRenderer.weight(d.weight),
                     color: ink, align: d.align ?? "center", face: d.face ?? sans, tracking: d.tracking ?? 0)
        case "line":
            ctx.line(CGPoint(x: d.x ?? 0, y: d.y ?? 0), CGPoint(x: d.x2 ?? 0, y: d.y2 ?? 0), ink, lw)
        case "polyline":
            guard let pts = d.points, pts.count > 1 else { return }
            ctx.setStrokeColor(ink); ctx.setLineWidth(lw); ctx.setLineJoin(.miter)
            ctx.move(to: CGPoint(x: pts[0][0], y: pts[0][1]))
            for p in pts.dropFirst() { ctx.addLine(to: CGPoint(x: p[0], y: p[1])) }
            ctx.strokePath()
        case "box":
            ctx.stroke(path: roundedPath(d.rect.insetBy(dx: lw / 2, dy: lw / 2), d.radius ?? 0), color: ink, width: lw)
        case "labelbox":
            ctx.fill(path: roundedPath(d.rect, 1.5), color: d.fill == "lightgray" ? hexColor(0xd9dadc) : ink)
        case "frame":
            let r = d.rect
            ctx.saveGState()
            ctx.addRect(CGRect(x: r.minX - 10, y: r.minY - 10, width: r.width + 20, height: r.height + 20))
            for g in d.gaps ?? [] where g.count == 2 {
                ctx.addRect(CGRect(x: g[0], y: r.minY - 6, width: g[1] - g[0], height: 12))
            }
            ctx.clip(using: .evenOdd)
            ctx.stroke(path: roundedPath(r.insetBy(dx: lw / 2, dy: lw / 2), d.radius ?? 0), color: ink, width: lw)
            ctx.restoreGState()
        case "dots":
            let c = CGPoint(x: d.cx ?? 0, y: d.cy ?? 0), r = d.r ?? 0
            let n = max(1, d.count ?? 1)
            for i in 0..<n {
                let t = n == 1 ? 0.5 : CGFloat(i) / CGFloat(n - 1)
                let a = ((d.from ?? 0) + ((d.to ?? 0) - (d.from ?? 0)) * t) * .pi / 180
                let dir = CGPoint(x: sin(a), y: -cos(a))
                if let tick = d.tick, tick > 0 {
                    ctx.line(CGPoint(x: c.x + dir.x * r, y: c.y + dir.y * r),
                             CGPoint(x: c.x + dir.x * (r + tick), y: c.y + dir.y * (r + tick)), ink, 2.2, cap: .round)
                } else {
                    ctx.fillCircle(CGPoint(x: c.x + dir.x * r, y: c.y + dir.y * r), (d.size ?? 3) / 2, ink)
                }
            }
        case "arc":
            // Printed tapered scale around a CUTOFF knob: width runs from width0 at `from` to width1 at `to` (degrees,
            // clockwise from 12 o'clock); `gaps` are [start, end] angle pairs left unprinted.
            let c = CGPoint(x: d.cx ?? 0, y: d.cy ?? 0), r = d.r ?? 0
            let a0 = d.from ?? -140, a1 = d.to ?? 140, w0 = d.width0 ?? 2, w1 = d.width1 ?? 2
            let steps = 120
            let gaps = (d.gaps ?? []).filter { $0.count == 2 }
            var run: [(CGFloat, CGFloat)] = []    // (angle, width) of the current unbroken stretch
            func flush() {
                guard run.count > 1 else { run.removeAll(); return }
                let path = CGMutablePath()
                for (i, (a, w)) in run.enumerated() {
                    let rad = a * .pi / 180, p = CGPoint(x: c.x + sin(rad) * (r + w / 2), y: c.y - cos(rad) * (r + w / 2))
                    i == 0 ? path.move(to: p) : path.addLine(to: p)
                }
                for (a, w) in run.reversed() {
                    let rad = a * .pi / 180
                    path.addLine(to: CGPoint(x: c.x + sin(rad) * (r - w / 2), y: c.y - cos(rad) * (r - w / 2)))
                }
                path.closeSubpath()
                ctx.fill(path: path, color: ink)
                run.removeAll()
            }
            for i in 0...steps {
                let t = CGFloat(i) / CGFloat(steps), a = a0 + (a1 - a0) * t
                if gaps.contains(where: { a >= $0[0] && a <= $0[1] }) { flush(); continue }
                run.append((a, w0 + (w1 - w0) * t))
            }
            flush()
        case "insert":
            let r = d.rect
            ctx.fill(path: roundedPath(r.insetBy(dx: -2, dy: -2), (d.radius ?? 0) + 2), color: hexColor(0xffffff, 0.06))
            ctx.fill(path: roundedPath(r, d.radius ?? 0), color: hexColor(0x050506))
        case "usbport":
            drawUSBPort(ctx, d.rect, d.radius ?? 6)
        case "icon":
            drawIcon(ctx, d.name ?? "", c: CGPoint(x: d.cx ?? 0, y: d.cy ?? 0), size: d.size ?? 12, ink: ink)
        default:
            break
        }
    }

    private func drawUSBPort(_ ctx: CGContext, _ r: CGRect, _ radius: CGFloat) {
        ctx.fill(path: roundedPath(r.insetBy(dx: -2, dy: -2), radius + 2), color: hexColor(0xffffff, 0.07))
        ctx.saveGState()
        ctx.addPath(roundedPath(r, radius)); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x0a0a0b)), (1, hexColor(0x131416))], from: CGPoint(x: 0, y: r.minY), to: CGPoint(x: 0, y: r.maxY))
        ctx.restoreGState()
        ctx.stroke(path: roundedPath(r.insetBy(dx: 0.8, dy: 0.8), radius), color: hexColor(0x3a3b3f), width: 1.6)
        let top = CGRect(x: r.minX + 7, y: r.minY + 8, width: r.width - 14, height: r.height * 0.52)
        ctx.saveGState()
        ctx.addPath(roundedPath(top, 4)); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x34353a)), (1, hexColor(0x16171a))], from: CGPoint(x: 0, y: top.minY), to: CGPoint(x: 0, y: top.maxY))
        ctx.restoreGState()
        ctx.fillCircle(CGPoint(x: r.midX, y: top.maxY - 3), 7, hexColor(0x0b0b0c))
    }

    private func drawIcon(_ ctx: CGContext, _ name: String, c: CGPoint, size: CGFloat, ink: CGColor) {
        ctx.setStrokeColor(ink); ctx.setFillColor(ink); ctx.setLineWidth(1.8); ctx.setLineCap(.round)
        switch name {
        case "power":
            let r = size * 0.36
            let steps = 28
            for i in 0...steps {
                let a = (-50 + 280 * CGFloat(i) / CGFloat(steps)) * .pi / 180
                let p = CGPoint(x: c.x + r * sin(a), y: c.y - r * cos(a))
                i == 0 ? ctx.move(to: p) : ctx.addLine(to: p)
            }
            ctx.strokePath()
            ctx.line(CGPoint(x: c.x, y: c.y - size * 0.46), CGPoint(x: c.x, y: c.y - size * 0.02), ink, 1.8, cap: .round)
        case "usb":
            let h = size / 2, u = size / 24
            ctx.line(CGPoint(x: c.x - h, y: c.y), CGPoint(x: c.x + h - 2 * u, y: c.y), ink, 1.5)
            ctx.fillCircle(CGPoint(x: c.x - h, y: c.y), 2 * u, ink)
            ctx.move(to: CGPoint(x: c.x + h, y: c.y)); ctx.addLine(to: CGPoint(x: c.x + h - 5 * u, y: c.y - 2.8 * u))
            ctx.addLine(to: CGPoint(x: c.x + h - 5 * u, y: c.y + 2.8 * u)); ctx.fillPath()
            ctx.setLineWidth(1.4); ctx.setLineJoin(.round)
            ctx.move(to: CGPoint(x: c.x - 4 * u, y: c.y)); ctx.addLine(to: CGPoint(x: c.x, y: c.y - 4 * u))
            ctx.addLine(to: CGPoint(x: c.x + 3 * u, y: c.y - 4 * u)); ctx.strokePath()
            ctx.fill(CGRect(x: c.x + 3 * u, y: c.y - 6 * u, width: 3 * u, height: 3 * u))
            ctx.move(to: CGPoint(x: c.x - 2 * u, y: c.y)); ctx.addLine(to: CGPoint(x: c.x + 2 * u, y: c.y + 4 * u))
            ctx.addLine(to: CGPoint(x: c.x + 4 * u, y: c.y + 4 * u)); ctx.strokePath()
            ctx.fillCircle(CGPoint(x: c.x + 5.5 * u, y: c.y + 4 * u), 1.6 * u, ink)
        case "dc":
            ctx.setLineWidth(1.4); ctx.setLineCap(.butt)
            ctx.move(to: CGPoint(x: c.x - size / 2, y: c.y - 2.2)); ctx.addLine(to: CGPoint(x: c.x + size / 2, y: c.y - 2.2)); ctx.strokePath()
            ctx.setLineDash(phase: 0, lengths: [2, 1.6])
            ctx.move(to: CGPoint(x: c.x - size / 2, y: c.y + 2.2)); ctx.addLine(to: CGPoint(x: c.x + size / 2, y: c.y + 2.2)); ctx.strokePath()
            ctx.setLineDash(phase: 0, lengths: [])
        default:
            break
        }
    }

    // MARK: Static control shapes

    private func drawControlStatic(_ ctx: CGContext, _ c: PanelLayout.Control, _ s: CGFloat) {
        let f = c.frame
        switch c.kind {
        case .pad, .step, .bar:
            ctx.fill(path: roundedPath(f.insetBy(dx: -4, dy: -4), c.cornerRadius + 4), color: hexColor(0x050506))
            ctx.stroke(path: roundedPath(f.insetBy(dx: -3.5, dy: -3.5), c.cornerRadius + 3.5), color: hexColor(0xffffff, 0.08), width: 1)
            if let n = c.number {
                let y = c.cy + (c.numberDy ?? -f.height / 2 - 12)
                if c.numberAlign == "left" {
                    drawText(n, at: CGPoint(x: f.minX, y: y), size: 14, color: PanelRenderer.color(nil), align: "left")
                } else {
                    drawText(n, at: CGPoint(x: c.cx, y: y), size: 14, color: PanelRenderer.color(nil))
                    if c.numberBox == true {
                        ctx.stroke(path: roundedPath(CGRect(x: f.minX, y: y - 9, width: f.width, height: 18).insetBy(dx: 0.8, dy: 0.8), 1),
                                   color: PanelRenderer.color(nil), width: 1.6)
                    }
                }
            }
        case .button:
            drawButtonStatic(ctx, c)
        case .knob:
            drawEncoderBody(ctx, c)
        case .analog:
            drawAnalogBody(ctx, c)
        case .selector:
            drawSelectorWell(ctx, c)
        case .strip:
            drawStripFrame(ctx, f)
        case .lamp:
            if c.shape != "text" {
                ctx.fill(path: roundedPath(f.insetBy(dx: -2, dy: -2), c.cornerRadius + 2), color: hexColor(0x050506))
            }
        }
    }

    private func drawButtonStatic(_ ctx: CGContext, _ c: PanelLayout.Control) {
        let f = c.frame, ctr = c.center, style = c.style ?? "white"
        switch style {
        case "round": drawRoundButton(ctx, c)
        case "home":
            let r = f.width / 2
            ctx.fillCircle(ctr, r + 3, hexColor(0x040405))
            ctx.fillRadial([(0, hexColor(0x5a5c60)), (0.35, hexColor(0x1d1e20)), (1, hexColor(0x070708))], center: ctr, radius: r,
                           focus: CGPoint(x: ctr.x - r * 0.3, y: ctr.y - r * 0.4))
            ctx.strokeCircle(ctr, r - 0.5, hexColor(0xffffff, 0.12), 1)
        case "back":
            ctx.fill(path: roundedPath(f.insetBy(dx: -3, dy: -3), f.height / 2 + 3), color: hexColor(0x040405))
            ctx.saveGState()
            ctx.addPath(roundedPath(f, f.height / 2)); ctx.clip()
            ctx.fillLinear([(0, hexColor(0x4a4b4f)), (0.45, hexColor(0x1b1c1e)), (1, hexColor(0x08080a))],
                           from: CGPoint(x: 0, y: f.minY), to: CGPoint(x: 0, y: f.maxY))
            ctx.restoreGState()
            let groove = CGRect(x: f.minX + 10, y: f.midY - 3.5, width: f.width - 20, height: 7)
            ctx.fill(path: roundedPath(groove, 3.5), color: hexColor(0x0a0a0b))
            ctx.stroke(path: roundedPath(groove, 3.5), color: hexColor(0xffffff, 0.14), width: 1)
        case "gray":
            // SHIFT has no LED: a dark rubber key carrying a light printed label plate.
            ctx.fill(path: roundedPath(f.insetBy(dx: -3, dy: -3), c.cornerRadius + 3), color: hexColor(0x050506))
            ctx.saveGState()
            ctx.addPath(roundedPath(f, c.cornerRadius)); ctx.clip()
            ctx.fillLinear([(0, hexColor(0x2a2b2e)), (1, hexColor(0x141517))], from: CGPoint(x: 0, y: f.minY), to: CGPoint(x: 0, y: f.maxY))
            ctx.restoreGState()
            let plate = f.insetBy(dx: 7, dy: 8)
            ctx.saveGState()
            ctx.addPath(roundedPath(plate, 2)); ctx.clip()
            ctx.fillLinear([(0, hexColor(0xe4e5e8)), (1, hexColor(0xc4c5c9))], from: CGPoint(x: 0, y: plate.minY), to: CGPoint(x: 0, y: plate.maxY))
            ctx.restoreGState()
        default:
            // Unlit light-pipe rim; drawButtonLamp lights it in the lamp colour.
            let outline: CGColor
            switch style {
            case "rec": outline = hexColor(0x4a1a24)
            case "play": outline = hexColor(0x1d4322)
            default: outline = hexColor(0x55575b)
            }
            ctx.fill(path: roundedPath(f.insetBy(dx: -3.5, dy: -3.5), c.cornerRadius + 3.5), color: hexColor(0x050506))
            if style == "mode" {
                ctx.saveGState()
                ctx.addPath(roundedPath(f.insetBy(dx: -3.5, dy: -3.5), c.cornerRadius + 3.5)); ctx.clip()
                ctx.fillLinear([(0, hexColor(0x8d8f93)), (1, hexColor(0x3a3b3e))], from: CGPoint(x: 0, y: f.minY), to: CGPoint(x: 0, y: f.maxY))
                ctx.restoreGState()
            } else {
                ctx.stroke(path: roundedPath(f.insetBy(dx: -2, dy: -2), c.cornerRadius + 2), color: hexColor(0x8e9094), width: 1.3)
            }
            ctx.fill(path: roundedPath(f, c.cornerRadius), color: hexColor(0x070708))
            ctx.stroke(path: roundedPath(f.insetBy(dx: 2, dy: 2), max(0, c.cornerRadius - 1)), color: outline, width: 4)
        }
        drawButtonGlyph(ctx, c)
    }

    /// Printed symbol/label on top of a button; redrawn over lamp tint so it stays legible.
    private func drawButtonGlyph(_ ctx: CGContext, _ c: PanelLayout.Control, labelColor: CGColor? = nil) {
        let ctr = c.center, style = c.style ?? "white"
        switch style {
        case "rec":
            ctx.fillRadial([(0, labelColor ?? hexColor(0xff8a8a)), (1, labelColor ?? hexColor(0xe51818))], center: ctr, radius: 8)
        case "stop":
            ctx.setFillColor(labelColor ?? hexColor(0xf4f5f6))
            ctx.fill(CGRect(x: ctr.x - 7, y: ctr.y - 7, width: 14, height: 14))
        case "play":
            ctx.setFillColor(labelColor ?? hexColor(0x35ee48))
            ctx.move(to: CGPoint(x: ctr.x - 8, y: ctr.y - 10)); ctx.addLine(to: CGPoint(x: ctr.x + 10, y: ctr.y))
            ctx.addLine(to: CGPoint(x: ctr.x - 8, y: ctr.y + 10)); ctx.fillPath()
        case "round":
            let r = c.r ?? 16
            ctx.fillRadial([(0, hexColor(0xff6a68)), (0.55, hexColor(0xe01818)), (1, hexColor(0x8c0c0c))], center: ctr,
                           radius: r * 0.36, focus: CGPoint(x: ctr.x - r * 0.08, y: ctr.y - r * 0.1))
        default: break
        }
        if let label = c.label, !label.isEmpty {
            let gray = style == "gray"
            drawText(label, at: ctr, size: gray ? 14.5 : 15.5, weight: gray ? .medium : .bold,
                     color: labelColor ?? (gray ? hexColor(0x1b1c1f) : PanelRenderer.color(nil)))
        }
    }

    private func drawRoundButton(_ ctx: CGContext, _ c: PanelLayout.Control) {
        let ctr = c.center, r = c.r ?? 16
        ctx.fillCircle(ctr, r + 4, hexColor(0x040405))
        ctx.saveGState()
        ctx.addEllipse(in: CGRect(x: ctr.x - r - 2, y: ctr.y - r - 2, width: 2 * r + 4, height: 2 * r + 4)); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x707276)), (0.5, hexColor(0x1a1b1d)), (1, hexColor(0x4b4d50))],
                       from: CGPoint(x: ctr.x, y: ctr.y - r), to: CGPoint(x: ctr.x, y: ctr.y + r))
        ctx.restoreGState()
        ctx.fillCircle(ctr, r - 1.5, hexColor(0x070708))
        ctx.fillRadial([(0, hexColor(0x4a1316)), (1, hexColor(0x0a0708))], center: ctr, radius: r - 1.5)
    }

    private func drawEncoderBody(_ ctx: CGContext, _ c: PanelLayout.Control) {
        let ctr = c.center, r = c.r ?? 30
        ctx.fillCircle(ctr, r + 4, hexColor(0x050506))
        ctx.strokeCircle(ctr, r + 1, hexColor(0xc9cbce, 0.7), 1.6)
        ctx.fillRadial([(0, hexColor(0x2b2c2f)), (0.7, hexColor(0x141517)), (1, hexColor(0x060607))], center: ctr, radius: r,
                       focus: CGPoint(x: ctr.x - r * 0.25, y: ctr.y - r * 0.3))
        for i in 0..<28 {
            let a = CGFloat(i) / 28 * 2 * .pi
            ctx.line(CGPoint(x: ctr.x + sin(a) * r * 0.78, y: ctr.y - cos(a) * r * 0.78),
                     CGPoint(x: ctr.x + sin(a) * r * 0.93, y: ctr.y - cos(a) * r * 0.93), hexColor(0, 0.55), 2.2)
        }
        ctx.fillRadial([(0, hexColor(0x34363a)), (1, hexColor(0x111214))], center: ctr, radius: r * 0.66,
                       focus: CGPoint(x: ctr.x - r * 0.2, y: ctr.y - r * 0.25))
        ctx.strokeCircle(ctr, r * 0.66, hexColor(0x8a8c90, 0.55), 1.2)
    }

    /// Knob silhouette with `lobes` rounded flutes; `depth` is how far the cusps between flutes sit inside `r`.
    private func flutedPath(_ ctr: CGPoint, r: CGFloat, lobes: Int, depth: CGFloat) -> CGPath {
        let path = CGMutablePath(), n = lobes * 16
        for i in 0...n {
            let a = CGFloat(i) / CGFloat(n) * 2 * .pi
            let rr = r - depth * (1 - abs(sin(CGFloat(lobes) * a / 2)))
            let p = CGPoint(x: ctr.x + sin(a) * rr, y: ctr.y - cos(a) * rr)
            i == 0 ? path.move(to: p) : path.addLine(to: p)
        }
        path.closeSubpath()
        return path
    }

    /// Analog pots as on the product render: a fluted skirt (gunmetal for the filter, black for MAIN OUT) under a
    /// smooth spun-metal cap whose sheen is light top-right and bottom-left. The white pointer is dynamic.
    private func drawAnalogBody(_ ctx: CGContext, _ c: PanelLayout.Control) {
        let ctr = c.center, r = c.r ?? 30, matte = c.style == "matte"
        let skirt = flutedPath(ctr, r: r, lobes: 14, depth: r * 0.08)
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: 3), blur: 7, color: hexColor(0, 0.75))
        ctx.fill(path: skirt, color: hexColor(matte ? 0x0f1012 : 0x1c1b21))
        ctx.restoreGState()
        ctx.saveGState(); ctx.addPath(skirt); ctx.clip()
        let rim: [(CGFloat, CGColor)] = matte
            ? [(0, hexColor(0x55565b)), (0.42, hexColor(0x1a1b1e)), (0.7, hexColor(0x0c0c0e)), (1, hexColor(0x3a3b40))]
            : [(0, hexColor(0xb9b6c4)), (0.4, hexColor(0x3b3a43)), (0.68, hexColor(0x1b1a20)), (1, hexColor(0x8a7f99))]
        ctx.fillLinear(rim, from: CGPoint(x: ctr.x - r * 0.7, y: ctr.y - r), to: CGPoint(x: ctr.x + r * 0.7, y: ctr.y + r))
        ctx.restoreGState()
        ctx.stroke(path: skirt, color: hexColor(0, 0.65), width: 1)

        spunMetal(ctx, ctr, radius: r * 0.72, low: matte ? 0.09 : 0.20, high: matte ? 0.19 : 0.56)
    }

    /// Spun-metal disc: a two-lobed sheen (light top-right and bottom-left, as lit on the product render) between grey
    /// levels `low` and `high`, with optional fine concentric spin rings and a dark rim.
    private func spunMetal(_ ctx: CGContext, _ ctr: CGPoint, radius cap: CGFloat, low: CGFloat, high: CGFloat, rings: Bool = false) {
        ctx.saveGState()
        ctx.addEllipse(in: CGRect(x: ctr.x - cap, y: ctr.y - cap, width: 2 * cap, height: 2 * cap)); ctx.clip()
        let wedges = 96
        for i in 0..<wedges {
            let a0 = CGFloat(i) / CGFloat(wedges) * 2 * .pi, a1 = CGFloat(i + 1) / CGFloat(wedges) * 2 * .pi + 0.01
            let sheen = 0.5 + 0.5 * cos(2 * ((a0 + a1) / 2 - .pi / 4))      // peaks top-right and bottom-left
            let v = low + (high - low) * sheen
            let wedge = CGMutablePath()
            wedge.move(to: ctr)
            wedge.addLine(to: CGPoint(x: ctr.x + sin(a0) * cap * 1.1, y: ctr.y - cos(a0) * cap * 1.1))
            wedge.addLine(to: CGPoint(x: ctr.x + sin(a1) * cap * 1.1, y: ctr.y - cos(a1) * cap * 1.1))
            wedge.closeSubpath()
            ctx.fill(path: wedge, color: CGColor(colorSpace: sRGB, components: [v, v * 0.98, v * 1.04, 1])!)
        }
        if rings {
            var k = cap * 0.08
            while k < cap { ctx.strokeCircle(ctr, k, hexColor(0xffffff, 0.035), 0.6); k += 2.2 }
        }
        ctx.fillRadial([(0, hexColor(0xffffff, 0.05)), (0.8, hexColor(0, 0)), (1, hexColor(0, 0.45))], center: ctr, radius: cap)
        ctx.restoreGState()
        ctx.strokeCircle(ctr, cap, hexColor(0, 0.6), 1.2)
    }

    /// The selector's fixed parts, as on the product render: the printed white ring, a black well and a soft rubber
    /// collar around the knob (no tick marks).
    private func drawSelectorWell(_ ctx: CGContext, _ c: PanelLayout.Control) {
        let ctr = c.center, r = c.r ?? 60
        ctx.fillCircle(ctr, r + 2, hexColor(0x040405))
        ctx.strokeCircle(ctr, r, hexColor(0xf0f1f2), 2.4)
        ctx.fillCircle(ctr, r - 2, hexColor(0x060607))
        let collar = r * 0.9
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: 2), blur: 6, color: hexColor(0, 0.8))
        ctx.fillCircle(ctr, collar, hexColor(0x141518))
        ctx.restoreGState()
        ctx.fillRadial([(0.6, hexColor(0x0c0d0f)), (0.82, hexColor(0x202125)), (0.94, hexColor(0x16171a)), (1, hexColor(0x0a0a0c))],
                       center: ctr, radius: collar, focus: CGPoint(x: ctr.x - collar * 0.12, y: ctr.y - collar * 0.16))
        ctx.strokeCircle(ctr, collar - 0.5, hexColor(0xffffff, 0.07), 1)
        ctx.strokeCircle(ctr, collar * 0.74, hexColor(0, 0.7), 1.5)
    }

    private func drawStripFrame(_ ctx: CGContext, _ f: CGRect) {
        ctx.fill(path: roundedPath(f.insetBy(dx: -2, dy: -2), 7), color: hexColor(0x050506))
        ctx.saveGState()
        ctx.addPath(roundedPath(f, 5)); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x9a9ca0)), (0.1, hexColor(0x3c3d40)), (0.86, hexColor(0x2c2d30)), (1, hexColor(0xc4c6ca))],
                       from: CGPoint(x: f.minX, y: 0), to: CGPoint(x: f.maxX, y: 0))
        ctx.restoreGState()
        let inner = f.insetBy(dx: 8, dy: 6)
        ctx.saveGState()
        ctx.addPath(roundedPath(inner, 3)); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x040405)), (1, hexColor(0x0d0e10))], from: CGPoint(x: 0, y: inner.minY), to: CGPoint(x: 0, y: inner.maxY))
        ctx.restoreGState()
        ctx.stroke(path: roundedPath(inner, 3), color: hexColor(0xffffff, 0.08), width: 1)
    }

    private func drawLCDBezel(_ ctx: CGContext) {
        let b = layout.lcd.bezel
        let outer = b.rect, glass = layout.lcd.active.rect
        let path = roundedPath(outer, b.radius ?? 6)
        ctx.fill(path: roundedPath(outer.insetBy(dx: -3, dy: -3), (b.radius ?? 6) + 3), color: hexColor(0x050506))
        ctx.saveGState()
        ctx.addPath(path); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x2a2b2e)), (1, hexColor(0x0e0f10))], from: CGPoint(x: 0, y: outer.minY), to: CGPoint(x: 0, y: outer.maxY))
        // Chamfered highlight along the left and bottom edges, as on the product.
        let left = CGMutablePath()
        left.move(to: CGPoint(x: outer.minX, y: outer.minY)); left.addLine(to: CGPoint(x: outer.minX + 12, y: outer.minY + 12))
        left.addLine(to: CGPoint(x: outer.minX + 12, y: outer.maxY - 12)); left.addLine(to: CGPoint(x: outer.minX, y: outer.maxY))
        left.closeSubpath()
        ctx.saveGState(); ctx.addPath(left); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x9a9ca0)), (1, hexColor(0x3d3e42))], from: CGPoint(x: outer.minX, y: 0), to: CGPoint(x: outer.minX + 12, y: 0))
        ctx.restoreGState()
        let bottom = CGMutablePath()
        bottom.move(to: CGPoint(x: outer.minX, y: outer.maxY)); bottom.addLine(to: CGPoint(x: outer.minX + 12, y: outer.maxY - 12))
        bottom.addLine(to: CGPoint(x: outer.maxX - 12, y: outer.maxY - 12)); bottom.addLine(to: CGPoint(x: outer.maxX, y: outer.maxY))
        bottom.closeSubpath()
        ctx.saveGState(); ctx.addPath(bottom); ctx.clip()
        ctx.fillLinear([(0, hexColor(0x3d3e42)), (1, hexColor(0xa0a2a6))], from: CGPoint(x: 0, y: outer.maxY - 12), to: CGPoint(x: 0, y: outer.maxY))
        ctx.restoreGState()
        ctx.restoreGState()
        ctx.stroke(path: path, color: hexColor(0xffffff, 0.12), width: 1)
        ctx.fill(path: roundedPath(glass.insetBy(dx: -2.5, dy: -2.5), 2), color: hexColor(0))
    }

    // MARK: Dynamic layer

    /// The WAKE UP (USB STOP / STBY) key: the only lamp lit while the panel is dark (standby or fault), red as the
    /// service manual's LED matrix has it (MONO_COLORS usb_stop).
    static let wakeUpName = "usb_stop"
    static let wakeUpColor = LampColor(r: 1, g: 60.0 / 255, b: 60.0 / 255)

    /// Effective lamp look: in the dark state every lamp is off except WAKE UP, regardless of firmware feedback.
    private func look(_ c: PanelLayout.Control, _ st: PanelRenderState) -> LampLook {
        if st.panelDark || st.fault {
            guard c.name == PanelRenderer.wakeUpName else { return .off }
            if st.fault {
                // QRT1001 p24: a fault blinks the WAKE UP LED red (about 1 Hz) and everything else is dark.
                return Int(ProcessInfo.processInfo.systemUptime * 2) % 2 == 0 ? .lit(PanelRenderer.wakeUpColor) : .off
            }
            return .lit(PanelRenderer.wakeUpColor)
        }
        return st.look(c.id)
    }

    func drawDynamic(_ ctx: CGContext, state: PanelRenderState, scale s: CGFloat, dirty: CGRect) {
        drawLCD(ctx, state: state, dirty: dirty)
        for c in layout.controls where c.frame.insetBy(dx: -PanelRenderer.dirtyReach, dy: -PanelRenderer.dirtyReach).intersects(dirty) {
            drawControlDynamic(ctx, c, state, s)
        }
        if state.bloom && state.feedbackLive && !state.panelDark && !state.fault { drawBloom(ctx, state: state, dirty: dirty) }
        if state.debugOverlay { drawDebug(ctx) }
    }

    /// Canvas units around a control that its drawing (shadow glow, bloom) may reach; the dirty-rect margin.
    static let dirtyReach: CGFloat = 40
    static let bloomReach: CGFloat = 34

    /// Soft additive halo over the frame around every lit key, button and LED, drawn after all controls so it also
    /// lights its neighbours and the chassis. Static lamps cost one gradient each and only inside the dirty rect;
    /// nothing animates, so an idle panel repaints nothing. The LCD glass is excluded from the halo.
    private func drawBloom(_ ctx: CGContext, state: PanelRenderState, dirty: CGRect) {
        let reach = PanelRenderer.bloomReach
        var lit: [(PanelLayout.Control, LampColor)] = []
        for c in layout.controls where c.kind != .knob && c.kind != .analog && c.kind != .selector && c.kind != .strip {
            guard !(c.kind == .lamp && c.shape == "text"), case .lit(let col) = look(c, state),
                  c.frame.insetBy(dx: -reach, dy: -reach).intersects(dirty) else { continue }
            lit.append((c, col))
        }
        guard !lit.isEmpty else { return }
        ctx.saveGState()
        ctx.clip(to: dirty)
        // Keep the halo off the LCD glass (even-odd: everything but the bezel).
        ctx.addRect(dirty.insetBy(dx: -reach, dy: -reach)); ctx.addRect(layout.lcd.bezel.rect); ctx.clip(using: .evenOdd)
        ctx.setBlendMode(.plusLighter)
        for (c, col) in lit {
            let f = c.frame
            let big = max(f.width, f.height)
            let strength: CGFloat = c.kind == .lamp ? 0.5 : (big > 60 ? 0.34 : 0.42)
            let halfH = f.height / 2, outer = halfH + reach
            let edge = halfH / outer
            ctx.saveGState()
            ctx.translateBy(x: f.midX, y: f.midY)
            ctx.scaleBy(x: (f.width / 2 + reach) / outer, y: 1)
            ctx.fillRadial([(0, col.lighter(0.25).cg(strength * 0.5)), (edge * 0.9, col.cg(strength * 0.55)),
                            (edge + (1 - edge) * 0.35, col.cg(strength * 0.22)), (1, col.cg(0))],
                           center: .zero, radius: outer)
            ctx.restoreGState()
        }
        ctx.restoreGState()
    }

    func drawLCD(_ ctx: CGContext, state: PanelRenderState, dirty: CGRect, rect: CGRect? = nil) {
        let r = rect ?? layout.lcd.active.rect
        guard r.intersects(dirty) else { return }
        ctx.saveGState()
        ctx.clip(to: r)
        ctx.setFillColor(hexColor(0))
        ctx.fill(r)
        if state.panelDark || state.fault {
            // Standby/fault: the glass is dark; no firmware image and no boot overlay.
            ctx.restoreGState()
            return
        }
        if let image = state.lcdImage {
            ctx.interpolationQuality = .none
            ctx.translateBy(x: 0, y: r.minY + r.maxY)
            ctx.scaleBy(x: 1, y: -1)
            ctx.draw(image, in: r)
        } else {
            ctx.restoreGState(); ctx.saveGState()
            drawBootOverlay(ctx, in: r, stage: state.bootStage, seconds: state.bootSeconds)
        }
        ctx.restoreGState()
        if state.lcdImage != nil, state.lcdBrightness < 1 {
            // A dimmed backlight: a black veil over the glass, never fully dark so the screen stays readable.
            ctx.setFillColor(hexColor(0, 1 - max(0.2, state.lcdBrightness)))
            ctx.fill(r)
        }
    }

    /// Shown on the dark LCD until the firmware's first frame arrives: what the emulator is doing and for how long, with an
    /// indeterminate bar, instead of a bare "waiting" line.
    private func drawBootOverlay(_ ctx: CGContext, in r: CGRect, stage: String, seconds: Int) {
        let size = max(11, r.width / 34)
        drawText("SP-16 EMULATOR", at: CGPoint(x: r.midX, y: r.midY - size * 2.6), size: size * 0.8, weight: .semibold, color: hexColor(0x6b6d72),
                 tracking: 2)
        drawText(stage + "…", at: CGPoint(x: r.midX, y: r.midY - size * 0.6), size: size, weight: .regular, color: hexColor(0xd7d9dd))
        let bar = CGRect(x: r.midX - r.width * 0.22, y: r.midY + size * 0.9, width: r.width * 0.44, height: 4)
        ctx.fill(path: roundedPath(bar, 2), color: hexColor(0x2a2c30))
        let t = CGFloat(seconds % 3) / 3
        let seg = CGRect(x: bar.minX + (bar.width * 0.7) * t, y: bar.minY, width: bar.width * 0.3, height: bar.height)
        ctx.fill(path: roundedPath(seg, 2), color: hexColor(0x4aa3ff))
        drawText("\(seconds) s", at: CGPoint(x: r.midX, y: r.midY + size * 2.2), size: size * 0.75, weight: .regular, color: hexColor(0x6b6d72))
    }

    private func drawControlDynamic(_ ctx: CGContext, _ c: PanelLayout.Control, _ st: PanelRenderState, _ s: CGFloat) {
        let f = c.frame
        let held = st.held.contains(c.id)
        let look = self.look(c, st)
        switch c.kind {
        case .pad, .step, .bar:
            drawGlassKey(ctx, f, c.cornerRadius, look, held, s)
        case .button:
            drawButtonLamp(ctx, c, look, held, s)
        case .knob:
            drawIndicator(ctx, c, angle: st.knobAngle[c.id] ?? 0, long: false)
        case .analog:
            drawIndicator(ctx, c, angle: st.knobAngle[c.id] ?? 0, long: true)
        case .selector:
            drawSelector(ctx, c, angle: st.selectorAngle, held: held)
        case .strip:
            if let pos = st.stripTouch { drawStripThumb(ctx, f, pos) }
        case .lamp:
            if c.shape == "text" { drawLampText(ctx, c, look) } else { drawLED(ctx, c, look, s) }
        }
    }

    /// Pad / step / bar surface. Firmware lamp glow is colored bloom; local press is a dark inset with a white ring.
    private func drawGlassKey(_ ctx: CGContext, _ f: CGRect, _ radius: CGFloat, _ look: LampLook, _ held: Bool, _ s: CGFloat) {
        let path = roundedPath(f, radius)
        switch look {
        case .unknown:
            ctx.fill(path: path, color: hexColor(0x2a2c30))
            ctx.stroke(path: roundedPath(f.insetBy(dx: 0.5, dy: 0.5), radius), color: hexColor(0x3d3f44), width: 1)
        case .off:
            ctx.saveGState(); ctx.addPath(path); ctx.clip()
            ctx.fillLinear([(0, hexColor(0x0d0e10)), (1, hexColor(0x1b1c20))], from: CGPoint(x: 0, y: f.minY), to: CGPoint(x: 0, y: f.maxY))
            ctx.fillLinear([(0, hexColor(0xffffff, 0.09)), (1, hexColor(0xffffff, 0))], from: CGPoint(x: 0, y: f.minY),
                           to: CGPoint(x: 0, y: f.midY))
            ctx.restoreGState()
            ctx.stroke(path: roundedPath(f.insetBy(dx: 0.5, dy: 0.5), radius), color: hexColor(0x2c2d31), width: 1)
        case .lit(let c):
            ctx.saveGState()
            ctx.setShadow(offset: .zero, blur: 11 * s, color: c.cg(0.6))
            ctx.fill(path: path, color: c.cg())
            ctx.restoreGState()
            ctx.saveGState(); ctx.addPath(path); ctx.clip()
            ctx.fillRadial([(0, c.lighter(0.38).cg()), (0.7, c.cg()), (1, c.darker(0.22).cg())], center: CGPoint(x: f.midX, y: f.midY),
                           radius: max(f.width, f.height) * 0.62)
            ctx.fillLinear([(0, hexColor(0xffffff, 0.26)), (1, hexColor(0xffffff, 0))], from: CGPoint(x: 0, y: f.minY),
                           to: CGPoint(x: 0, y: f.midY + f.height * 0.08))
            ctx.restoreGState()
            ctx.stroke(path: roundedPath(f.insetBy(dx: 0.7, dy: 0.7), radius), color: hexColor(0xffffff, 0.38), width: 1.4)
        }
        if held { drawPressedShade(ctx, path: path, frame: f, radius: radius, s) }
    }

    private func drawPressedShade(_ ctx: CGContext, path: CGPath, frame f: CGRect, radius: CGFloat, _ s: CGFloat) {
        ctx.saveGState(); ctx.addPath(path); ctx.clip()
        ctx.setFillColor(hexColor(0, 0.34)); ctx.fill(f)
        let inner = roundedPath(f.insetBy(dx: 3, dy: 3), max(0, radius - 3))
        ctx.stroke(path: inner, color: hexColor(0, 0.55), width: 6)
        ctx.restoreGState()
        ctx.stroke(path: roundedPath(f.insetBy(dx: 1.8, dy: 1.8), max(0, radius - 1.8)), color: hexColor(0xffffff, 0.95), width: 1.8)
    }

    private func drawButtonLamp(_ ctx: CGContext, _ c: PanelLayout.Control, _ look: LampLook, _ held: Bool, _ s: CGFloat) {
        let f = c.frame
        let path: CGPath = c.isCircle ? CGPath(ellipseIn: f, transform: nil) : roundedPath(f, c.cornerRadius)
        if case .lit(let col) = look, !c.isCircle, PanelRenderer.rimLitStyles.contains(c.style ?? "white") {
            // As on the unit: the light pipe around the key glows; the face stays black with its printed label.
            let rim = roundedPath(f.insetBy(dx: 2, dy: 2), max(0, c.cornerRadius - 1))
            ctx.saveGState()
            ctx.setShadow(offset: .zero, blur: 12 * s, color: col.cg(0.85))
            ctx.stroke(path: rim, color: col.cg(), width: 4)
            ctx.restoreGState()
            ctx.stroke(path: rim, color: col.lighter(0.55).cg(), width: 1.6)
            drawButtonGlyph(ctx, c)
        } else if case .lit(let col) = look {
            ctx.saveGState()
            ctx.setShadow(offset: .zero, blur: 14 * s, color: col.cg(0.9))
            ctx.fill(path: path, color: col.cg(0.85))
            ctx.restoreGState()
            ctx.saveGState(); ctx.addPath(path); ctx.clip()
            ctx.fillRadial([(0, col.lighter(0.4).cg(0.75)), (1, col.cg(0.55))], center: c.center, radius: max(f.width, f.height) * 0.6)
            ctx.restoreGState()
            ctx.stroke(path: path, color: col.lighter(0.5).cg(), width: 2)
            let luminance = 0.3 * col.r + 0.59 * col.g + 0.11 * col.b
            drawButtonGlyph(ctx, c, labelColor: luminance > 0.35 ? hexColor(0x0c0c0e) : nil)
        }
        if held { drawPressedShade(ctx, path: path, frame: f, radius: c.isCircle ? f.width / 2 : c.cornerRadius, s) }
    }

    private func drawLED(_ ctx: CGContext, _ c: PanelLayout.Control, _ look: LampLook, _ s: CGFloat) {
        let f = c.frame
        let path = roundedPath(f, c.cornerRadius)
        switch look {
        case .unknown: ctx.fill(path: path, color: hexColor(0x2a2c30))
        case .off: ctx.fill(path: path, color: hexColor(0x15171b))
        case .lit(let col):
            ctx.saveGState()
            ctx.setShadow(offset: .zero, blur: 9 * s, color: col.cg(0.9))
            ctx.fill(path: path, color: col.cg())
            ctx.restoreGState()
            ctx.fill(path: roundedPath(f.insetBy(dx: 0.8, dy: 0.8), c.cornerRadius), color: col.lighter(0.35).cg())
        }
    }

    private func drawLampText(_ ctx: CGContext, _ c: PanelLayout.Control, _ look: LampLook) {
        guard let label = c.label else { return }
        let size: CGFloat = 14
        switch look {
        case .unknown:
            drawText(label, at: c.center, size: size, color: hexColor(0x56585c))
        case .off:
            drawText(label, at: c.center, size: size, color: hexColor(0x2f5f2c))
        case .lit(let col):
            ctx.saveGState()
            ctx.setShadow(offset: .zero, blur: 6, color: col.cg(0.8))
            drawText(label, at: c.center, size: size, color: col.lighter(0.25).cg())
            ctx.restoreGState()
        }
    }

    private func drawIndicator(_ ctx: CGContext, _ c: PanelLayout.Control, angle: CGFloat, long: Bool) {
        let r = c.r ?? 30
        ctx.saveGState()
        ctx.translateBy(x: c.cx, y: c.cy)
        ctx.rotate(by: angle * .pi / 180)
        if long {
            // A white bar from inside the cap out over the skirt's edge, as on the product.
            ctx.line(CGPoint(x: 0, y: -r * 0.12), CGPoint(x: 0, y: -r * 0.96), hexColor(0x050506, 0.6), r * 0.17 + 2, cap: .round)
            ctx.line(CGPoint(x: 0, y: -r * 0.12), CGPoint(x: 0, y: -r * 0.96), hexColor(0xf4f5f6), r * 0.17, cap: .round)
        } else {
            ctx.line(CGPoint(x: 0, y: -r * 0.32), CGPoint(x: 0, y: -r * 0.6), hexColor(0xe4e5e8), 3, cap: .round)
        }
        ctx.restoreGState()
    }

    private func drawSelector(_ ctx: CGContext, _ c: PanelLayout.Control, angle: CGFloat, held: Bool) {
        let ctr = c.center, ring = c.capR ?? 40, cap = ring * 0.78
        // Chrome band with dimples notched into its outer edge: the dimples turn with the selector; the light stays fixed.
        let band = CGRect(x: ctr.x - ring, y: ctr.y - ring, width: 2 * ring, height: 2 * ring)
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: 2), blur: 5, color: hexColor(0, 0.85))
        ctx.fillCircle(ctr, ring, hexColor(0x6d6f74))
        ctx.restoreGState()
        ctx.saveGState(); ctx.addEllipse(in: band); ctx.clip()
        ctx.fillLinear([(0, hexColor(0xf4f5f8)), (0.3, hexColor(0xa4a6ac)), (0.52, hexColor(0x45464b)), (0.74, hexColor(0xcfd1d6)),
                        (1, hexColor(0x707277))],
                       from: CGPoint(x: ctr.x - ring, y: ctr.y - ring), to: CGPoint(x: ctr.x + ring, y: ctr.y + ring))
        let dimples = 12, spin = angle * .pi / 180
        for i in 0..<dimples {
            let a = spin + CGFloat(i) / CGFloat(dimples) * 2 * .pi
            let p = CGPoint(x: ctr.x + sin(a) * ring * 1.0, y: ctr.y - cos(a) * ring * 1.0)
            ctx.fillRadial([(0, hexColor(0x050506)), (0.7, hexColor(0x111214)), (1, hexColor(0x2a2b2f, 0))], center: p, radius: ring * 0.17)
        }
        ctx.restoreGState()
        ctx.strokeCircle(ctr, ring - 0.5, hexColor(0, 0.5), 1)
        // Spun-metal top cap.
        ctx.fillCircle(ctr, cap + 1.5, hexColor(0x0a0a0c))
        spunMetal(ctx, ctr, radius: cap, low: 0.05, high: 0.30, rings: true)
        if held {
            ctx.saveGState()
            ctx.addEllipse(in: CGRect(x: ctr.x - cap, y: ctr.y - cap, width: 2 * cap, height: 2 * cap)); ctx.clip()
            ctx.fillCircle(ctr, cap, hexColor(0, 0.38))
            ctx.strokeCircle(ctr, cap - 3, hexColor(0, 0.6), 6)
            ctx.restoreGState()
            ctx.strokeCircle(ctr, cap - 1.8, hexColor(0xffffff, 0.95), 1.8)
        }
    }

    private func drawStripThumb(_ ctx: CGContext, _ f: CGRect, _ position: Int) {
        let y = f.maxY - CGFloat(position) / 1000 * f.height
        let inner = f.insetBy(dx: 7, dy: 6)
        ctx.saveGState(); ctx.clip(to: inner)
        ctx.setFillColor(hexColor(0xffffff, 0.12)); ctx.fill(CGRect(x: inner.minX, y: y - 9, width: inner.width, height: 18))
        ctx.restoreGState()
        ctx.line(CGPoint(x: inner.minX, y: y), CGPoint(x: inner.maxX, y: y), hexColor(0xffffff, 0.95), 1.8)
    }

    private func drawDebug(_ ctx: CGContext) {
        ctx.setLineWidth(1)
        for c in layout.controls {
            let col = c.isInput ? hexColor(0xff00ff) : hexColor(0x00e5ff)
            ctx.setStrokeColor(col)
            ctx.stroke(c.frame)
            drawText("\(c.id)", at: c.center, size: 11, weight: .bold, color: col)
        }
        ctx.setStrokeColor(hexColor(0xffff00))
        ctx.stroke(layout.lcd.active.rect)
    }
}

// MARK: - Footer toolbar

/// Native AppKit footer: latch / pad velocity / release all / LCD only / status line.
final class PanelToolbar: NSView {
    static let height: CGFloat = 32
    static let velocities: [(title: String, value: Int)] =
        [("Soft 40", 40), ("Medium 80", 80), ("Firm 100", 100), ("Hard 120", 120), ("Max 127", 127)]

    let latch = NSButton(checkboxWithTitle: "Latch holds", target: nil, action: nil)
    let velocityCaption = NSTextField(labelWithString: "Pad velocity")
    let velocity = NSPopUpButton(frame: .zero, pullsDown: false)
    let releaseAll = NSButton(title: "Release all", target: nil, action: nil)
    let lcdOnly = NSButton(checkboxWithTitle: "LCD only", target: nil, action: nil)
    /// Shown instead of the controls above in bench mode (`PanelView.uiInputEnabled == false`).
    let benchBadge = NSTextField(labelWithString: " Bench mode: input off ")
    let status = NSTextField(labelWithString: "")

    override init(frame: NSRect) {
        super.init(frame: frame)
        appearance = NSAppearance(named: .darkAqua)
        for (control, label) in [(latch, "Latch holds"), (releaseAll, "Release all"), (lcdOnly, "LCD only")] as [(NSButton, String)] {
            control.controlSize = .small
            control.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            control.refusesFirstResponder = true      // keep keyboard focus on the panel
            control.setAccessibilityLabel(label)
        }
        releaseAll.bezelStyle = .rounded
        velocityCaption.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        velocityCaption.textColor = .secondaryLabelColor
        velocity.controlSize = .small
        velocity.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        velocity.addItems(withTitles: Self.velocities.map(\.title))
        velocity.refusesFirstResponder = true
        velocity.setAccessibilityLabel("Pad velocity")
        status.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        status.textColor = .secondaryLabelColor
        status.alignment = .right
        status.lineBreakMode = .byTruncatingTail
        status.setAccessibilityLabel("Emulator status")
        benchBadge.font = .systemFont(ofSize: NSFont.smallSystemFontSize, weight: .semibold)
        benchBadge.textColor = .black
        benchBadge.drawsBackground = true
        benchBadge.backgroundColor = .systemOrange
        benchBadge.toolTip = "Started with --no-ui-input: clicks, drags, scrolling, keys and accessibility actions are "
            + "ignored; the research control socket still drives the panel"
        benchBadge.isHidden = true
        for v in [latch, velocityCaption, velocity, releaseAll, lcdOnly, benchBadge, status] as [NSView] { addSubview(v) }
        selectVelocity(100)
    }

    /// Bench mode hides the input controls and shows the badge in their place.
    func setInputEnabled(_ enabled: Bool) {
        for v in [latch, velocityCaption, velocity, releaseAll, lcdOnly] as [NSView] { v.isHidden = !enabled }
        benchBadge.isHidden = enabled
        needsLayout = true
    }

    required init?(coder: NSCoder) { nil }

    func selectVelocity(_ value: Int) {
        if let i = Self.velocities.firstIndex(where: { $0.value == value }) { velocity.selectItem(at: i) }
        else { velocity.selectItem(at: -1) }
    }

    override func layout() {
        super.layout()
        var x: CGFloat = 12
        for v in [latch, velocityCaption, velocity, releaseAll, lcdOnly, benchBadge] as [NSControl] where !v.isHidden {
            v.sizeToFit()
            v.frame.origin = CGPoint(x: x, y: ((bounds.height - v.frame.height) / 2).rounded())
            x = v.frame.maxX + (v === velocityCaption ? 4 : 16)
        }
        let h: CGFloat = 16
        status.frame = CGRect(x: x + 8, y: ((bounds.height - h) / 2).rounded(), width: max(0, bounds.width - x - 20), height: h)
    }

    override func draw(_ dirtyRect: NSRect) {
        hexNSColor(0x131416).setFill()
        bounds.fill()
        hexNSColor(0xffffff, 0.09).setFill()
        CGRect(x: 0, y: bounds.maxY - 1, width: bounds.width, height: 1).fill()
    }
}

private func hexNSColor(_ v: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((v >> 16) & 255) / 255, green: CGFloat((v >> 8) & 255) / 255, blue: CGFloat(v & 255) / 255, alpha: a)
}

// MARK: - Accessibility element

final class PanelAccessibilityElement: NSAccessibilityElement {
    private unowned let panel: PanelView
    /// nil identifies the LCD.
    let controlID: UInt8?

    init(panel: PanelView, id: UInt8?, role: NSAccessibility.Role, label: String) {
        self.panel = panel
        controlID = id
        super.init()
        setAccessibilityParent(panel)
        setAccessibilityRole(role)
        setAccessibilityLabel(label)
    }

    /// NSAccessibilityElement reports "disabled" unless told otherwise. Everything is enabled except the
    /// analog pots that have no emulated effect (stubs), which stay disabled to match their tooltip.
    override func isAccessibilityEnabled() -> Bool { panel.accessibilityEnabled(forControl: controlID) }
    override func accessibilityFrame() -> NSRect { panel.screenFrame(forControl: controlID) }
    override func accessibilityValue() -> Any? { panel.accessibilityValueText(forControl: controlID) }
    override func accessibilityPerformPress() -> Bool { panel.accessibilityActivate(controlID) }
    override func accessibilityPerformIncrement() -> Bool { panel.accessibilityStep(controlID, by: 1) }
    override func accessibilityPerformDecrement() -> Bool { panel.accessibilityStep(controlID, by: -1) }
}

// MARK: - PanelView

final class PanelView: NSView, NSViewToolTipOwner {
    struct Lamp: Equatable {
        var r: UInt8, g: UInt8, b: UInt8
        init(r: UInt8, g: UInt8, b: UInt8) { self.r = r; self.g = g; self.b = b }
    }

    static let stubTooltip = "Analog control — not emulated (stub)"
    static let adcTooltip = "Analog pot — only the firmware's ADC readback is emulated (no audio effect)"
    /// MAIN OUT, DRIVE and HP CUTOFF have a proven firmware ADC field (AD TEST page); the others do not.
    static let adcControls: Set<UInt8> = [61, 62, 65]

    private func analogNote(_ c: PanelLayout.Control) -> String {
        c.kind != .analog ? c.accessibility
            : "\(c.accessibility)\n\(Self.adcControls.contains(c.id) ? Self.adcTooltip : Self.stubTooltip)"
    }

    /// Position -135...135 degrees -> firmware value 0...1000 (the guest scales it to the 10-bit ADC word).
    private func emitAnalog(_ id: UInt8) {
        guard Self.adcControls.contains(id) else { return }
        let value = Int16(((state.knobAngle[id] ?? 0) + 135) / 270 * 1000)
        emit(id, 3, value)
    }
    static let lcdOnlyMargin: CGFloat = 8
    static let margin: CGFloat = 10

    /// Receives semantic control events. See PanelViewDelegate for value conventions.
    weak var delegate: PanelViewDelegate?
    /// Called after a toolbar control changed `latchMode`, `padVelocity` or `lcdOnly` through the UI.
    var onToolbarChange: (() -> Void)?

    /// Mouse strike velocity for pads, 1...127.
    var padVelocity = 100 {
        didSet {
            let c = min(127, max(1, padVelocity))
            if c != padVelocity { padVelocity = c }
            toolbar.selectVelocity(padVelocity)
        }
    }
    /// When true a click toggles a hold on pads/buttons/steps/bars instead of a momentary press.
    var latchMode = false { didSet { toolbar.latch.state = latchMode ? .on : .off } }
    /// Enlarged LCD inspection: LCD fills the view, controls are hidden, touch and wheel still work.
    var lcdOnly = false {
        didSet {
            guard lcdOnly != oldValue else { return }
            releaseAll()
            toolbar.lcdOnly.state = lcdOnly ? .on : .off
            axElements = nil
            relayout()
            needsDisplay = true
            NSAccessibility.post(element: self, notification: .layoutChanged)
        }
    }
    /// View > Rear I/O Panel: the back of the unit (jacks, no controls) replaces the front; input is ignored meanwhile.
    var showRear = false {
        didSet {
            guard showRear != oldValue, panelLayout?.rear != nil else { showRear = oldValue; return }
            releaseAll()
            axElements = nil
            needsDisplay = true
            NSAccessibility.post(element: self, notification: .layoutChanged)
        }
    }
    /// Optional private artwork: a transparent PNG with the panel's canvas proportions (1773 x 1011 or any multiple),
    /// loaded by the app from the git-ignored private/ folder. The repository ships none; the overlay is off until
    /// the user turns it on (View > Private Skin Overlay).
    private var skinImage: CGImage?
    var hasSkin: Bool { skinImage != nil }
    var showSkin = false { didSet { if showSkin != oldValue { staticCache.removeAll(); needsDisplay = true } } }
    private var skinActive: Bool { showSkin && skinImage != nil && !showRear }

    /// Loads the overlay image; nil clears it. Returns false when the file is not a readable image.
    @discardableResult
    func loadSkin(from url: URL?) -> Bool {
        defer { staticCache.removeAll(); needsDisplay = true }
        guard let url else { skinImage = nil; return true }
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil), let image = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            skinImage = nil
            return false
        }
        skinImage = image
        return true
    }
    /// Lit keys, buttons and LEDs throw an additive halo (View > Lamp Glow).
    var lampBloom: Bool {
        get { state.bloom }
        set { state.bloom = newValue; needsDisplay = true }
    }
    /// Draws hit regions and control ids for calibration.
    var debugOverlay = false { didSet { state.debugOverlay = debugOverlay; needsDisplay = true } }
    /// False in bench mode (`--no-ui-input`): the window drops every click, drag, scroll, key and accessibility action,
    /// so an unattended run that drives the panel through the research control socket cannot be disturbed by someone
    /// using the Mac (macOS sends scroll to the window under the pointer even when it is not active). The control
    /// socket, the LCD and screenshots are unaffected: they never pass through these handlers.
    var uiInputEnabled = true {
        didSet {
            guard uiInputEnabled != oldValue else { return }
            releaseAll()
            toolbar.setInputEnabled(uiInputEnabled)
            NSAccessibility.post(element: self, notification: .layoutChanged)
        }
    }

    private(set) var panelLayout: PanelLayout?
    private(set) var feedbackGeneration: UInt32 = 0
    private var renderer: PanelRenderer?
    private var controlIndex: [UInt8: PanelLayout.Control] = [:]
    private var loadError: String?
    private let toolbar = PanelToolbar(frame: .zero)
    private var state = PanelRenderState()
    /// The last two static layers: the window's scale and the offscreen scale of panelshot/cacheDisplay alternate, and
    /// one slot would rebuild the whole chassis on every switch.
    private var staticCache: [(image: CGImage, key: StaticKey)] = []
    private var axElements: [PanelAccessibilityElement]?
    private var input = InputState()
    private var observers: [NSObjectProtocol] = []

    private struct StaticKey: Equatable { var size: CGSize, scale: CGFloat, lcdOnly: Bool, rear: Bool, skin: Bool }

    // MARK: Setup

    init(layoutURL: URL) {
        super.init(frame: NSRect(x: 0, y: 0, width: 1280, height: 800))
        autoresizingMask = [.width, .height]
        do {
            let l = try PanelLayout.load(from: layoutURL)
            panelLayout = l
            renderer = PanelRenderer(layout: l)
            controlIndex = Dictionary(uniqueKeysWithValues: l.controls.map { ($0.id, $0) })
            // Analog knobs start where the panel model starts them (-135...135 degrees = host value 0...1000).
            for c in l.controls where c.kind == .analog {
                if let v = c.initial { state.knobAngle[c.id] = CGFloat(v) / 1000 * 270 - 135 }
            }
        } catch {
            loadError = "\(error)"
        }
        addSubview(toolbar)
        startBootClock()
        toolbar.latch.target = self; toolbar.latch.action = #selector(toolbarLatchChanged(_:))
        toolbar.velocity.target = self; toolbar.velocity.action = #selector(toolbarVelocityChanged(_:))
        toolbar.releaseAll.target = self; toolbar.releaseAll.action = #selector(toolbarReleaseAll(_:))
        toolbar.lcdOnly.target = self; toolbar.lcdOnly.action = #selector(toolbarLCDOnlyChanged(_:))
        relayout()
    }

    required init?(coder: NSCoder) { nil }

    deinit { observers.forEach(NotificationCenter.default.removeObserver) }

    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        guard let window else { return }
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSWindow.didResignKeyNotification, object: window, queue: .main) { [weak self] _ in
            self?.releaseAll()
        })
        observers.append(center.addObserver(forName: NSWindow.didMiniaturizeNotification, object: window, queue: .main) { [weak self] _ in
            self?.releaseAll()
        })
        observers.append(center.addObserver(forName: NSApplication.didResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
            self?.releaseAll()
        })
        DispatchQueue.main.async { [weak self] in
            guard let self, let window = self.window else { return }
            window.makeFirstResponder(self)
        }
    }

    override func resignFirstResponder() -> Bool {
        releaseAll()
        return super.resignFirstResponder()
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        relayout()
    }

    override func layout() {
        super.layout()
        relayout()
    }

    private func relayout() {
        toolbar.frame = CGRect(x: 0, y: bounds.height - PanelToolbar.height, width: bounds.width, height: PanelToolbar.height)
        toolbar.needsLayout = true
        rebuildToolTips()
        NSAccessibility.post(element: self, notification: .layoutChanged)
    }

    // MARK: Geometry (single transform)

    /// Canvas -> view mapping for the current mode. In LCD-only mode the LCD glass is fitted instead of the chassis.
    var panelTransform: PanelTransform? {
        guard let layout = panelLayout else { return nil }
        let footer = PanelToolbar.height
        if showRear, let rear = layout.rear {
            let m = Self.margin
            let avail = CGRect(x: m, y: m, width: bounds.width - 2 * m, height: bounds.height - footer - 2 * m)
            guard avail.width > 1, avail.height > 1 else { return nil }
            return .fit(canvas: rear.canvas.size, in: avail)
        }
        if lcdOnly {
            let m = Self.lcdOnlyMargin
            let avail = CGRect(x: m, y: m, width: bounds.width - 2 * m, height: bounds.height - footer - 2 * m)
            guard avail.width > 1, avail.height > 1 else { return nil }
            let a = layout.lcd.active.rect
            var t = PanelTransform.fit(canvas: a.size, in: avail)
            t.origin = CGPoint(x: t.origin.x - a.minX * t.scale, y: t.origin.y - a.minY * t.scale)
            return t
        }
        let m = Self.margin
        let avail = CGRect(x: m, y: m, width: bounds.width - 2 * m, height: bounds.height - footer - 2 * m)
        guard avail.width > 1, avail.height > 1 else { return nil }
        return .fit(canvas: layout.canvas.size, in: avail)
    }

    /// Frame of a control in view coordinates (.zero when unknown or hidden by LCD-only mode).
    func controlFrame(_ id: UInt8) -> NSRect {
        guard !lcdOnly, !showRear, let t = panelTransform, let c = controlIndex[id] else { return .zero }
        return t.toView(c.frame)
    }

    /// The OEM LCD active area in view coordinates.
    var lcdFrame: NSRect {
        guard let layout = panelLayout, !showRear, let t = panelTransform else { return .zero }
        return t.toView(layout.lcd.active.rect)
    }

    /// Control id under a view-space point; nil for background, LCD and lamp-only controls.
    func hitTest(point: NSPoint) -> UInt8? {
        guard !lcdOnly, !showRear, let t = panelTransform, let layout = panelLayout else { return nil }
        let p = t.toCanvas(point)
        return layout.controls.first { $0.isInput && $0.contains(p) }?.id
    }

    /// Maps a view point to OEM framebuffer coordinates (0..799, 0..479). Nil when outside the LCD unless `clamped`.
    func lcdFramebufferPoint(at p: NSPoint, clamped: Bool) -> (x: Int, y: Int)? {
        guard let layout = panelLayout else { return nil }
        let r = lcdFrame
        guard r.width > 0 else { return nil }
        if !clamped && !(p.x >= r.minX && p.x <= r.maxX && p.y >= r.minY && p.y <= r.maxY) { return nil }
        let fb = layout.lcd.framebuffer
        let x = Int(floor((p.x - r.minX) / r.width * CGFloat(fb.width)))
        let y = Int(floor((p.y - r.minY) / r.height * CGFloat(fb.height)))
        return (min(fb.width - 1, max(0, x)), min(fb.height - 1, max(0, y)))
    }

    // MARK: Public state

    func setLCD(_ image: CGImage?) {
        state.lcdImage = image
        setNeedsDisplay(lcdFrame)
    }

    /// Firmware lamp state. `live == false` (or never called) draws every lamp as "unknown". Generations are
    /// recorded for diagnostics only; the latest call always wins.
    func setFeedback(generation: UInt32, live: Bool, lamps: [UInt8: Lamp]) {
        feedbackGeneration = generation
        let liveChanged = live != state.feedbackLive
        let changed = Set(state.lamps.keys).union(lamps.keys).filter { state.lamps[$0] != lamps[$0] }
        state.lamps = lamps
        state.feedbackLive = live
        if liveChanged { needsDisplay = true } else { invalidate(changed) }
    }

    /// The firmware's LCD backlight level, 0...1. Host side only for now: nothing feeds it from the guest yet
    /// (docs/changes/l1-panel-ui.md, cross-lane requests); the research op `lcd-backlight` sets it.
    var lcdBacklight: CGFloat {
        get { state.lcdBrightness }
        set { state.lcdBrightness = min(1, max(0, newValue)); setNeedsDisplay(lcdFrame) }
    }

    /// What the guest is doing before the first LCD frame (host-side knowledge only: VM started, bridge connected, ...).
    func setBootStage(_ stage: String) {
        guard state.lcdImage == nil, stage != state.bootStage else { return }
        state.bootStage = stage
        setNeedsDisplay(lcdFrame)
    }

    private var bootClock: Timer?
    private let bootBegan = ProcessInfo.processInfo.systemUptime

    /// One tick a second while there is no LCD image; stops by itself at the first frame, so a running panel has no timer.
    private func startBootClock() {
        bootClock = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] timer in
            guard let self, self.state.lcdImage == nil else { timer.invalidate(); self?.bootClock = nil; return }
            self.state.bootSeconds = Int(ProcessInfo.processInfo.systemUptime - self.bootBegan)
            self.setNeedsDisplay(self.lcdFrame)
        }
    }

    func setStatus(_ text: String) {
        guard toolbar.status.stringValue != text else { return }
        toolbar.status.stringValue = text
        toolbar.status.toolTip = text
    }

    /// Enter or leave the dark panel state (standby or fault): every lamp off except WAKE UP, and a black LCD with no
    /// boot overlay. The host drives this from the TX standby flag; on wake the LCD is cleared so the boot overlay
    /// shows until the firmware's next frame.
    func setPanelDark(_ dark: Bool) {
        guard dark != state.panelDark else { return }
        state.panelDark = dark
        if dark { state.lcdImage = nil }
        needsDisplay = true
    }

    /// Force the power-fault display (QRT1001 p24): dark panel with the WAKE UP LED blinking red; off restores the live
    /// screen. The firmware keeps running behind the veil, so the LCD image is not cleared.
    func setFault(_ on: Bool) {
        guard on != state.fault else { return }
        state.fault = on
        if on { startFaultClock() } else { stopFaultClock() }
        needsDisplay = true
    }

    private var faultClock: Timer?

    /// Repaints the WAKE UP lamp a few times a second while the fault state is on, so the blink is smooth.
    private func startFaultClock() {
        guard faultClock == nil,
              let id = panelLayout?.controls.first(where: { $0.name == PanelRenderer.wakeUpName })?.id else { return }
        faultClock = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] timer in
            guard let self else { return }
            guard self.state.fault else { timer.invalidate(); self.faultClock = nil; return }
            self.invalidate([id])
        }
    }

    private func stopFaultClock() {
        faultClock?.invalidate(); faultClock = nil
    }

    var heldControls: Set<UInt8> { state.held }
    func localAngle(_ id: UInt8) -> CGFloat { id == 53 ? state.selectorAngle : (state.knobAngle[id] ?? 0) }
    /// Fractional drag detents not yet emitted for a knob or the selector ring.
    func dragResidual(_ id: UInt8) -> CGFloat { input.residual[id] ?? 0 }
    func scrollResidual(_ id: UInt8) -> CGFloat { input.scrollResidual[id] ?? 0 }

    // MARK: Drawing

    /// Host redraw cost, for metrics.json: passes through draw(_:), their wall time and how much of the view each
    /// repainted (dirty-rect area as a fraction of the view).
    struct RedrawStats {
        var passes = 0, totalMs = 0.0, maxMs = 0.0, areaFraction = 0.0, full = 0
        mutating func add(ms: Double, fraction: Double) {
            passes += 1; totalMs += ms; maxMs = max(maxMs, ms); areaFraction += fraction
            if fraction > 0.95 { full += 1 }
        }
        var json: [String: Any] {
            ["passes": passes, "total_ms": totalMs, "ms_per_pass": passes > 0 ? totalMs / Double(passes) : 0, "max_ms": maxMs,
             "mean_area_fraction": passes > 0 ? areaFraction / Double(passes) : 0, "full_passes": full]
        }
    }
    private(set) var redrawStats = RedrawStats()

    /// Forces `count` whole-view repaints and returns their mean wall time in ms: the cost without dirty rects.
    func benchmarkFullRedraw(count: Int) -> Double {
        let saved = redrawStats
        // cacheDisplay always paints the whole view, even when the window is hidden or occluded (displayIfNeeded may not).
        // One warm-up pass builds the static layer for the bitmap's scale, so the timed passes are the dynamic layer only.
        var start = ProcessInfo.processInfo.systemUptime
        if let rep = bitmapImageRepForCachingDisplay(in: bounds) {
            cacheDisplay(in: bounds, to: rep)
            start = ProcessInfo.processInfo.systemUptime
            for _ in 0..<max(1, count) { cacheDisplay(in: bounds, to: rep) }
        }
        let ms = (ProcessInfo.processInfo.systemUptime - start) * 1000 / Double(max(1, count))
        redrawStats = saved
        return ms
    }

    override func draw(_ dirtyRect: NSRect) {
        let began = ProcessInfo.processInfo.systemUptime
        defer {
            redrawStats.add(ms: (ProcessInfo.processInfo.systemUptime - began) * 1000,
                            fraction: Double(dirtyRect.width * dirtyRect.height / max(1, bounds.width * bounds.height)))
        }
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        guard let renderer, let t = panelTransform else {
            drawFailure(ctx)
            return
        }
        let scale = abs(ctx.convertToDeviceSpace(CGSize(width: 1, height: 0)).width)
        let key = StaticKey(size: bounds.size, scale: scale, lcdOnly: lcdOnly, rear: showRear, skin: skinActive)
        if let hit = staticCache.firstIndex(where: { $0.key == key }) {
            if hit != 0 { staticCache.insert(staticCache.remove(at: hit), at: 0) }
        } else if let image = renderer.makeStaticLayer(viewSize: bounds.size, scale: scale, transform: t, lcdOnly: lcdOnly, rear: showRear, skin: skinActive ? skinImage : nil) {
            staticCache.insert((image, key), at: 0)
            if staticCache.count > 2 { staticCache.removeLast() }
        }
        ctx.saveGState()
        ctx.clip(to: dirtyRect)
        if let image = staticCache.first?.image {
            ctx.translateBy(x: 0, y: bounds.height)
            ctx.scaleBy(x: 1, y: -1)
            ctx.draw(image, in: CGRect(origin: .zero, size: bounds.size))
            ctx.scaleBy(x: 1, y: -1)
            ctx.translateBy(x: 0, y: -bounds.height)
        }
        ctx.translateBy(x: t.origin.x, y: t.origin.y)
        ctx.scaleBy(x: t.scale, y: t.scale)
        let dirty = t.toCanvas(dirtyRect)
        if showRear {
            // The rear panel is entirely static.
        } else if lcdOnly {
            renderer.drawLCD(ctx, state: state, dirty: dirty)
        } else {
            renderer.drawDynamic(ctx, state: state, scale: t.scale, dirty: dirty)
        }
        ctx.restoreGState()
    }

    private func drawFailure(_ ctx: CGContext) {
        hexNSColor(0x1c1d20).setFill()
        bounds.fill()
        let message = "Panel layout unavailable\n\(loadError ?? "no layout loaded")"
        let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.systemRed]
        let str = NSAttributedString(string: message, attributes: attrs)
        let size = str.boundingRect(with: NSSize(width: bounds.width - 40, height: 400), options: .usesLineFragmentOrigin)
        str.draw(with: NSRect(x: 20, y: (bounds.height - size.height) / 2, width: bounds.width - 40, height: size.height),
                 options: .usesLineFragmentOrigin)
    }

    private func invalidate<S: Sequence>(_ ids: S) where S.Element == UInt8 {
        guard !lcdOnly, !showRear, let t = panelTransform else { return }
        for id in ids {
            guard let c = controlIndex[id] else { continue }
            setNeedsDisplay(t.toView(c.frame.insetBy(dx: -PanelRenderer.dirtyReach, dy: -PanelRenderer.dirtyReach)))
        }
    }

    // MARK: Hold bookkeeping

    private enum HoldSource: Hashable { case mouse, key(UInt16), shift, latch, accessibility, boot }

    private enum Drag { case hold(UInt8), knob(UInt8), analog(UInt8), ring, strip, lcd }

    private struct InputState {
        var holds: [UInt8: Set<HoldSource>] = [:]
        var drag: Drag?
        var lastPoint = CGPoint.zero
        var residual: [UInt8: CGFloat] = [:]
        var scrollResidual: [UInt8: CGFloat] = [:]
        var keysDown: [UInt16: UInt8] = [:]
        var shiftDown = false
        var lastStrip: Int?
        var lastTouch: (x: Int, y: Int)?
    }

    private func emit(_ id: UInt8, _ action: UInt8, _ value: Int16) {
        delegate?.panelControl(id, action: action, value: value)
    }

    private func pressValue(_ id: UInt8) -> Int16 { id <= 16 ? Int16(padVelocity) : 1 }

    private func addSource(_ id: UInt8, _ source: HoldSource, value: Int16? = nil) {
        var sources = input.holds[id] ?? []
        let first = sources.isEmpty
        sources.insert(source)
        input.holds[id] = sources
        guard first else { return }
        state.held.insert(id)
        emit(id, 1, value ?? pressValue(id))
        invalidate([id])
    }

    private func removeSource(_ id: UInt8, _ source: HoldSource) {
        guard var sources = input.holds[id], sources.remove(source) != nil else { return }
        guard sources.isEmpty else { input.holds[id] = sources; return }
        input.holds[id] = nil
        state.held.remove(id)
        emit(id, 0, 0)
        invalidate([id])
    }

    /// Holds the named controls as the host's own "keys held at power-on" (Power On With Keys Held); the release
    /// comes from `releaseBootHolds`, or from releaseAll when the window loses focus.
    func holdForBoot(_ names: [String]) {
        for name in names {
            if let id = panelLayout?.controls.first(where: { $0.name == name })?.id { addSource(id, .boot) }
        }
    }

    func releaseBootHolds() {
        for id in input.holds.keys.sorted() { removeSource(id, .boot) }
    }

    /// Emits a release for every held or latched control and ends any LCD touch. Safe to call repeatedly.
    func releaseAll() {
        if case .lcd? = input.drag, let t = input.lastTouch { delegate?.panelTouch(x: t.x, y: t.y, action: 2) }
        let ids = input.holds.keys.sorted()
        input.holds = [:]
        input.keysDown = [:]
        input.drag = nil
        input.shiftDown = false
        input.lastTouch = nil
        state.held = []
        state.stripTouch = nil
        for id in ids { emit(id, 0, 0) }
        invalidate(ids + [58])
    }

    // MARK: Pointer input
    // Every NSEvent and accessibility entry point checks `uiInputEnabled` first; the handle* methods below stay
    // ungated so they remain the testable model of what the input does.

    override func mouseDown(with event: NSEvent) {
        guard uiInputEnabled else { return }
        window?.makeFirstResponder(self)
        handleMouseDown(at: convert(event.locationInWindow, from: nil), flags: event.modifierFlags, rightButton: false)
    }
    override func rightMouseDown(with event: NSEvent) {
        guard uiInputEnabled else { return }
        window?.makeFirstResponder(self)
        handleMouseDown(at: convert(event.locationInWindow, from: nil), flags: event.modifierFlags, rightButton: true)
    }
    override func mouseDragged(with event: NSEvent) {
        guard uiInputEnabled else { return }
        handleMouseDragged(to: convert(event.locationInWindow, from: nil))
    }
    override func mouseUp(with event: NSEvent) {
        guard uiInputEnabled else { return }
        handleMouseUp(at: convert(event.locationInWindow, from: nil))
    }
    override func rightMouseUp(with event: NSEvent) {}
    override func menu(for event: NSEvent) -> NSMenu? { nil }

    override func scrollWheel(with event: NSEvent) {
        guard uiInputEnabled, event.momentumPhase.isEmpty else { return }
        let consumed = handleScroll(at: convert(event.locationInWindow, from: nil), deltaY: event.scrollingDeltaY,
                                    precise: event.hasPreciseScrollingDeltas)
        if !consumed { super.scrollWheel(with: event) }
    }

    func handleMouseDown(at p: CGPoint, flags: NSEvent.ModifierFlags = [], rightButton: Bool = false) {
        if input.drag != nil { handleMouseUp(at: p) }
        if let pt = lcdFramebufferPoint(at: p, clamped: false) {
            input.drag = .lcd
            input.lastTouch = pt
            delegate?.panelTouch(x: pt.x, y: pt.y, action: 1)
            return
        }
        guard let id = hitTest(point: p), let c = controlIndex[id] else { return }
        input.residual[id] = 0
        switch c.kind {
        case .pad, .step, .bar, .button:
            if input.holds[id]?.contains(.latch) == true { removeSource(id, .latch); return }
            if rightButton || flags.contains(.control) || latchMode {
                addSource(id, .latch)
            } else {
                addSource(id, .mouse)
                input.drag = .hold(id)
            }
        case .selector:
            guard !rightButton else { return }
            input.lastPoint = p
            if let t = panelTransform, hypot(p.x - controlFrame(id).midX, p.y - controlFrame(id).midY) <= (c.capR ?? 0) * t.scale {
                addSource(id, .mouse)
                input.drag = .hold(id)
            } else {
                input.drag = .ring
            }
        case .knob:
            guard !rightButton else { return }
            input.lastPoint = p
            input.drag = .knob(id)
        case .analog:
            guard !rightButton else { return }
            input.lastPoint = p
            input.drag = .analog(id)
        case .strip:
            guard !rightButton else { return }
            let pos = stripPosition(at: p)
            input.drag = .strip
            input.lastStrip = pos
            state.stripTouch = pos
            addSource(id, .mouse, value: Int16(pos))
        case .lamp:
            break
        }
    }

    func handleMouseDragged(to p: CGPoint) {
        guard let drag = input.drag else { return }
        switch drag {
        case .hold:
            break
        case .knob(let id):
            let dy = input.lastPoint.y - p.y            // drag up = increase
            input.lastPoint = p
            emitDetents(id, from: dy / Self.dragPointsPerDetent, store: \.residual)
        case .ring:
            let dy = p.y - input.lastPoint.y            // drag down = next / positive
            input.lastPoint = p
            emitDetents(53, from: dy / Self.dragPointsPerDetent, store: \.residual)
        case .analog(let id):
            let dy = input.lastPoint.y - p.y
            input.lastPoint = p
            state.knobAngle[id] = min(135, max(-135, (state.knobAngle[id] ?? 0) + dy * 1.2))
            invalidate([id])
            emitAnalog(id)
        case .strip:
            let pos = stripPosition(at: p)
            guard pos != input.lastStrip else { return }
            input.lastStrip = pos
            state.stripTouch = pos
            emit(58, 3, Int16(pos))
            invalidate([58])
        case .lcd:
            guard let pt = lcdFramebufferPoint(at: p, clamped: true), pt != input.lastTouch ?? (-1, -1) else { return }
            input.lastTouch = pt
            delegate?.panelTouch(x: pt.x, y: pt.y, action: 0)
        }
    }

    func handleMouseUp(at p: CGPoint) {
        guard let drag = input.drag else { return }
        input.drag = nil
        switch drag {
        case .hold(let id):
            removeSource(id, .mouse)
        case .strip:
            state.stripTouch = nil
            removeSource(58, .mouse)
            invalidate([58])
        case .lcd:
            if let pt = lcdFramebufferPoint(at: p, clamped: true) { delegate?.panelTouch(x: pt.x, y: pt.y, action: 2) }
            input.lastTouch = nil
        case .knob, .ring, .analog:
            break
        }
    }

    /// Scroll over a knob turns it; over the selector or the LCD it steps the selector. Returns whether consumed.
    @discardableResult
    func handleScroll(at p: CGPoint, deltaY: CGFloat, precise: Bool) -> Bool {
        let unit: CGFloat = precise ? Self.scrollPointsPerDetent : 1
        if lcdFramebufferPoint(at: p, clamped: false) != nil || hitTest(point: p) == 53 {
            emitDetents(53, from: -deltaY / unit, store: \.scrollResidual)     // scroll down = next / positive
            return true
        }
        guard let id = hitTest(point: p), let c = controlIndex[id] else { return false }
        switch c.kind {
        case .knob:
            emitDetents(id, from: deltaY / unit, store: \.scrollResidual)       // scroll up = increase
            return true
        case .analog:
            state.knobAngle[id] = min(135, max(-135, (state.knobAngle[id] ?? 0) + deltaY / unit * 10))
            invalidate([id])
            emitAnalog(id)
            return true
        default:
            return false
        }
    }

    static let dragPointsPerDetent: CGFloat = 8
    static let scrollPointsPerDetent: CGFloat = 12
    static let knobDegreesPerDetent: CGFloat = 15
    static let selectorDegreesPerDetent: CGFloat = 12

    /// Adds fractional detents to a residual accumulator, emitting the integral part and keeping the remainder.
    private func emitDetents(_ id: UInt8, from amount: CGFloat, store: WritableKeyPath<InputState, [UInt8: CGFloat]>) {
        let total = (input[keyPath: store][id] ?? 0) + amount
        let whole = total.rounded(.towardZero)
        input[keyPath: store][id] = total - whole
        guard whole != 0 else { return }
        rotate(id, detents: Int(whole))
    }

    /// Sends a delta event and turns the local cosmetic indicator.
    private func rotate(_ id: UInt8, detents: Int) {
        let n = Int16(clamping: detents)
        emit(id, 2, n)
        if id == 53 {
            state.selectorAngle = (state.selectorAngle + CGFloat(detents) * Self.selectorDegreesPerDetent).truncatingRemainder(dividingBy: 360)
        } else {
            state.knobAngle[id] = ((state.knobAngle[id] ?? 0) + CGFloat(detents) * Self.knobDegreesPerDetent).truncatingRemainder(dividingBy: 360)
        }
        invalidate([id])
    }

    private func stripPosition(at p: CGPoint) -> Int {
        let f = controlFrame(58)
        guard f.height > 0 else { return 0 }
        let y = min(f.maxY, max(f.minY, p.y))
        return Int(((f.maxY - y) / f.height * 1000).rounded())
    }

    // MARK: Keyboard input

    private static let keyGrid: [Character: UInt8] = {
        var m: [Character: UInt8] = [:]
        for (rows, first) in [("zxcv", 1), ("asdf", 5), ("qwer", 9), ("1234", 13), ("7890", 27), ("uiop", 31), ("jkl;", 35), ("nm,.", 39)] as [(String, Int)] {
            for (i, ch) in rows.enumerated() { m[ch] = UInt8(first + i) }
        }
        m["`"] = 22
        return m
    }()
    private static let shiftedKeys: [Character: Character] = [
        "!": "1", "@": "2", "#": "3", "$": "4", "&": "7", "*": "8", "(": "9", ")": "0", ":": ";", "<": ",", ">": ".", "~": "`"
    ]

    // Bench mode swallows keys silently (no beep); menu shortcuts such as Cmd-Q reach the menu before keyDown.
    override func keyDown(with event: NSEvent) {
        guard uiInputEnabled else { return }
        let handled = handleKeyDown(keyCode: event.keyCode, characters: event.charactersIgnoringModifiers,
                                    isRepeat: event.isARepeat, flags: event.modifierFlags)
        if !handled { super.keyDown(with: event) }
    }
    override func keyUp(with event: NSEvent) {
        guard uiInputEnabled else { return }
        if !handleKeyUp(keyCode: event.keyCode) { super.keyUp(with: event) }
    }
    override func flagsChanged(with event: NSEvent) {
        if uiInputEnabled { handleFlagsChanged(event.modifierFlags) }    // Shift is the SHIFT button
        super.flagsChanged(with: event)
    }

    /// Up/Down arrows step the selector (Down = +1 = next, Up = -1); Return = selector push; Escape = Back;
    /// Space = Play; Delete = Stop; ` = Rec; letter/digit grids = pads and steps. Returns whether consumed.
    @discardableResult
    func handleKeyDown(keyCode: UInt16, characters: String?, isRepeat: Bool, flags: NSEvent.ModifierFlags) -> Bool {
        if flags.contains(.command) || flags.contains(.control) { return false }
        let target: UInt8
        switch keyCode {
        case 125, 126:
            rotate(53, detents: keyCode == 125 ? 1 : -1)      // arrow auto-repeat keeps stepping
            return true
        case 36, 76: target = 53
        case 53: target = 54
        case 51: target = 23
        case 49: target = 24
        default:
            guard let ch = characters?.lowercased().first, let id = Self.keyGrid[Self.shiftedKeys[ch] ?? ch] else { return false }
            target = id
        }
        if isRepeat || input.keysDown[keyCode] != nil { return true }
        input.keysDown[keyCode] = target
        addSource(target, .key(keyCode))
        return true
    }

    @discardableResult
    func handleKeyUp(keyCode: UInt16) -> Bool {
        guard let id = input.keysDown.removeValue(forKey: keyCode) else { return false }
        removeSource(id, .key(keyCode))
        return true
    }

    func handleFlagsChanged(_ flags: NSEvent.ModifierFlags) {
        let down = flags.contains(.shift)
        guard down != input.shiftDown else { return }
        input.shiftDown = down
        if down { addSource(21, .shift) } else { removeSource(21, .shift) }
    }

    // MARK: Toolbar actions

    @objc private func toolbarLatchChanged(_ sender: NSButton) {
        latchMode = sender.state == .on
        onToolbarChange?()
    }
    @objc private func toolbarVelocityChanged(_ sender: NSPopUpButton) {
        let i = sender.indexOfSelectedItem
        if PanelToolbar.velocities.indices.contains(i) { padVelocity = PanelToolbar.velocities[i].value }
        onToolbarChange?()
    }
    @objc private func toolbarReleaseAll(_ sender: NSButton) { releaseAll() }
    @objc private func toolbarLCDOnlyChanged(_ sender: NSButton) {
        lcdOnly = sender.state == .on
        onToolbarChange?()
    }

    // MARK: Tooltips

    private func rebuildToolTips() {
        removeAllToolTips()
        guard let layout = panelLayout, let t = panelTransform else { return }
        addToolTip(t.toView(layout.lcd.active.rect), owner: self, userData: nil)
        guard !lcdOnly else { return }
        for c in layout.controls where c.isInput {
            addToolTip(t.toView(c.frame), owner: self, userData: UnsafeMutableRawPointer(bitPattern: Int(c.id)))
        }
    }

    func view(_ view: NSView, stringForToolTip tag: NSView.ToolTipTag, point: NSPoint, userData data: UnsafeMutableRawPointer?) -> String {
        let id = UInt8(truncatingIfNeeded: Int(bitPattern: data))
        guard id != 0 else { return "OEM touch LCD" }
        guard let c = controlIndex[id] else { return "" }
        return analogNote(c)
    }

    func toolTipText(at p: NSPoint) -> String? {
        if lcdFramebufferPoint(at: p, clamped: false) != nil { return "OEM touch LCD" }
        guard let id = hitTest(point: p), let c = controlIndex[id] else { return nil }
        return analogNote(c)
    }

    // MARK: Accessibility

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .group }
    override func accessibilityLabel() -> String? { "SP-16 panel" }
    override func accessibilityChildren() -> [Any]? {
        if axElements == nil { axElements = makeAccessibilityElements() }
        return (axElements ?? []) + [toolbar]
    }

    private func makeAccessibilityElements() -> [PanelAccessibilityElement] {
        guard let layout = panelLayout else { return [] }
        var elements = [PanelAccessibilityElement(panel: self, id: nil, role: .image, label: "OEM touch LCD, 800 by 480")]
        guard !lcdOnly else { return elements }
        for c in layout.controls {
            let role: NSAccessibility.Role
            switch c.kind {
            case .knob, .analog, .selector, .strip: role = .slider
            case .lamp: role = .staticText
            default: role = .button
            }
            elements.append(PanelAccessibilityElement(panel: self, id: c.id, role: role, label: c.accessibility))
        }
        return elements
    }

    func screenFrame(forControl id: UInt8?) -> NSRect {
        let rect = id.map { controlFrame($0) } ?? lcdFrame
        guard let window else { return rect }
        return window.convertToScreen(convert(rect, to: nil))
    }

    func accessibilityEnabled(forControl id: UInt8?) -> Bool {
        guard uiInputEnabled else { return false }
        guard let id, let c = controlIndex[id] else { return true }
        return c.kind != .analog || Self.adcControls.contains(id)
    }

    func accessibilityValueText(forControl id: UInt8?) -> String? {
        guard let id, let c = controlIndex[id] else { return nil }
        switch c.kind {
        case .lamp:
            switch state.look(id) { case .unknown: return "unknown"; case .off: return "off"; case .lit: return "lit" }
        case .analog: return Self.adcControls.contains(c.id) ? "firmware ADC readback only" : "not emulated"
        default: return state.held.contains(id) ? "pressed" : "released"
        }
    }

    /// Accessibility press: press, then release after 40 ms. False (nothing sent) in bench mode.
    @discardableResult
    func accessibilityActivate(_ id: UInt8?) -> Bool {
        guard uiInputEnabled else { return false }
        guard let id else {
            let r = lcdFrame, mid = CGPoint(x: r.midX, y: r.midY)
            guard let pt = lcdFramebufferPoint(at: mid, clamped: true) else { return false }
            delegate?.panelTouch(x: pt.x, y: pt.y, action: 1)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.04) { [weak self] in self?.delegate?.panelTouch(x: pt.x, y: pt.y, action: 2) }
            return true
        }
        guard let c = controlIndex[id], c.kind != .lamp, c.kind != .analog, c.kind != .knob else { return false }
        if c.kind == .strip {
            addSource(id, .accessibility, value: 500)
        } else {
            addSource(id, .accessibility)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.04) { [weak self] in self?.removeSource(id, .accessibility) }
        return true
    }

    func accessibilityStep(_ id: UInt8?, by detents: Int) -> Bool {
        guard uiInputEnabled, let id, let c = controlIndex[id] else { return false }
        switch c.kind {
        case .knob, .selector: rotate(id, detents: detents)
        case .analog:
            state.knobAngle[id] = min(135, max(-135, (state.knobAngle[id] ?? 0) + CGFloat(detents) * 10))
            invalidate([id])
            emitAnalog(id)
        default: return false
        }
        return true
    }
}
