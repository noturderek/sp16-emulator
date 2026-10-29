import Foundation
import AVFoundation
import CoreAudio
import Darwin

/// Host audio output (`native.py build --audio coreaudio`). Pure parts first (jitter buffer with adaptive
/// resampling, drift controller), then the AVAudioEngine wrapper. Nothing here is created unless the build asked for it.
///
/// Timing model. The producer is the transport reader (bursts of 1-3 periods of 64 frames every ~2 ms, guest clock);
/// the consumer is the device callback (blocks of `block` frames, device clock). Prefill target =
/// one device block (the callback needs a whole block at once) + a jitter margin of 10 ms. 6.4 ms covers the measured
/// transport p95 (2.9 ms) plus the 2 ms forwarder poll plus one 64-frame period (1.45 ms). A PI controller on the
/// smoothed buffer depth trims the resampling ratio so the depth stays at the target whatever the clock mismatch is.

/// Tiny unfair lock; the render thread holds it for a memcpy-sized critical section only.
final class UnfairLock {
    private let pointer = UnsafeMutablePointer<os_unfair_lock>.allocate(capacity: 1)
    init() { pointer.initialize(to: os_unfair_lock()) }
    deinit { pointer.deallocate() }
    @inline(__always) func lock() { os_unfair_lock_lock(pointer) }
    @inline(__always) func unlock() { os_unfair_lock_unlock(pointer) }
}

/// PI controller from buffer depth error to a resampling-ratio trim. Frequency error appears as a constant depth
/// slope, so the integral term converges to the clock mismatch (`integralPPM`) and the proportional term only damps.
struct DriftController {
    var targetFrames: Double
    var timeConstant = 4.0          // seconds; P gain = 1/timeConstant, I gain = P^2/4 (critically damped)
    var smoothing = 0.5             // seconds, low-pass of the depth sample (producer bursts)
    var limitPPM = 5000.0
    private(set) var smoothed: Double? = nil
    private(set) var integral = 0.0 // fractional (1e-6 = 1 ppm)
    private(set) var adjust = 0.0

    var integralPPM: Double { integral * 1e6 }
    var adjustPPM: Double { adjust * 1e6 }

    init(targetFrames: Double) { self.targetFrames = targetFrames }

    mutating func reset() { smoothed = nil; adjust = 0 }   // the learned integral survives a re-prefill

    /// `depth` in input frames at callback start, `dt` seconds this callback covers, `inputRate` for unit conversion.
    mutating func update(depth: Double, dt: Double, inputRate: Double) -> Double {
        let alpha = dt / (smoothing + dt)
        let value = smoothed.map { $0 + alpha * (depth - $0) } ?? depth
        smoothed = value
        let error = (value - targetFrames) / inputRate          // seconds of excess buffer
        let kp = 1.0 / timeConstant
        if abs(error) < 0.015 { integral += kp * kp / 4 * error * dt }   // anti-windup: no integrating through a re-prefill transient
        integral = max(-limitPPM * 1e-6, min(limitPPM * 1e-6, integral))
        adjust = max(-limitPPM * 1e-6, min(limitPPM * 1e-6, kp * error + integral))
        return adjust
    }
}

/// Bounded stereo float jitter buffer with linear-interpolating variable-ratio reads.
/// Prefill before first output, zero-fill on underrun (then re-prefill), drop-oldest on overflow.
final class JitterBuffer {
    private(set) var capacity: Int
    let rate: Double
    private var left: [Float], right: [Float]
    private var readAbs: UInt64 = 0, writeAbs: UInt64 = 0
    private var frac = 0.0
    private(set) var playing = false
    private(set) var targetFrames: Int
    private var segments: [(start: UInt64, ns: UInt64)] = []
    private var segmentHead = 0
    private let lock = UnfairLock()

    // Counters (read through `counters()`).
    private var pushed: UInt64 = 0, consumed: UInt64 = 0, outFrames: UInt64 = 0, silentFrames: UInt64 = 0
    private var underrunEvents: UInt64 = 0, underrunFrames: UInt64 = 0
    private var overflowEvents: UInt64 = 0, overflowFrames: UInt64 = 0
    private var prefills: UInt64 = 0, flushes: UInt64 = 0
    private var depthMin = Int.max, depthMax = 0
    private var latency = AudioSink.Histogram()
    private var warmFrames: UInt64, warmBaseline: (underruns: UInt64, overflows: UInt64)? = nil
    private var playedOut: UInt64 = 0
    private var lastUnderrunPlayedS = -1.0, lastUnderrunDepth = 0

    init(capacity: Int, targetFrames: Int, rate: Double = 44100, warmupFrames: UInt64 = 0) {
        self.rate = rate; self.capacity = capacity; self.targetFrames = targetFrames; self.warmFrames = warmupFrames
        left = [Float](repeating: 0, count: capacity); right = left
    }

    var depthFrames: Int { lock.lock(); defer { lock.unlock() }; return Int(writeAbs - readAbs) }

    func setTarget(_ frames: Int) { lock.lock(); targetFrames = min(frames, capacity / 2); lock.unlock() }

    func flush() {
        lock.lock()
        readAbs = writeAbs; frac = 0; playing = false; segments.removeAll(); segmentHead = 0; flushes += 1
        lock.unlock()
    }

    private func copyIn(_ l: UnsafePointer<Float>?, _ r: UnsafePointer<Float>?, _ n: Int) {
        var done = 0
        while done < n {
            let at = Int((writeAbs + UInt64(done)) % UInt64(capacity)), run = min(n - done, capacity - at)
            for i in 0..<run {
                left[at + i] = l?[done + i] ?? 0; right[at + i] = r?[done + i] ?? 0
            }
            done += run
        }
    }

    /// `left`/`right` nil pushes silence. `writeNs` is when the guest produced the first frame (host clock).
    func push(_ l: UnsafePointer<Float>?, _ r: UnsafePointer<Float>?, count: Int, writeNs: UInt64) {
        guard count > 0 else { return }
        lock.lock(); defer { lock.unlock() }
        var l = l, r = r, n = count
        var drop = 0
        if n > capacity {   // keep the newest `capacity` frames only
            drop += n - capacity
            l = l.map { $0 + (n - capacity) }; r = r.map { $0 + (n - capacity) }; n = capacity
            readAbs = writeAbs   // everything buffered is older than the whole new block
            frac = 0
        }
        let depth = Int(writeAbs - readAbs)
        if depth + n > capacity {
            let extra = depth + n - capacity
            drop += extra; readAbs += UInt64(extra)
            frac = 0
        }
        if drop > 0 { overflowEvents += 1; overflowFrames += UInt64(drop) }
        segments.append((writeAbs, writeNs))
        copyIn(l, r, n)
        writeAbs += UInt64(n); pushed += UInt64(count)
    }

    private func latencyStart(_ position: UInt64) -> UInt64? {
        // segments are sorted by start; drop the ones wholly consumed
        while segments.count - segmentHead > 1 && segments[segmentHead + 1].start <= position { segmentHead += 1 }
        if segmentHead > 256 { segments.removeFirst(segmentHead); segmentHead = 0 }
        guard segmentHead < segments.count, segments[segmentHead].start <= position else { return nil }
        return segments[segmentHead].ns + UInt64(Double(position - segments[segmentHead].start) / rate * 1e9)
    }

    struct ReadResult { var depthBefore: Double; var playing: Bool; var underran: Bool; var prefilledNow: Bool }

    /// Fill `count` output frames consuming `ratio` input frames per output frame.
    @discardableResult
    func read(_ outL: UnsafeMutablePointer<Float>, _ outR: UnsafeMutablePointer<Float>, count: Int, ratio: Double,
              nowNs: UInt64) -> ReadResult {
        lock.lock(); defer { lock.unlock() }
        let depth = Int(writeAbs - readAbs)
        depthMin = min(depthMin, depth); depthMax = max(depthMax, depth)
        var result = ReadResult(depthBefore: Double(depth) - frac, playing: playing, underran: false, prefilledNow: false)
        outFrames += UInt64(count)
        if !playing {
            if depth >= targetFrames && depth > 2 { playing = true; prefills += 1; result.prefilledNow = true; result.playing = true }
            else {
                for i in 0..<count { outL[i] = 0; outR[i] = 0 }
                silentFrames += UInt64(count)
                return result
            }
        }
        if let ns = latencyStart(readAbs) { latency.add(nowNs > ns ? Double(nowNs - ns) / 1e9 : 0) }
        var position = frac, produced = 0
        let cap = UInt64(capacity)
        while produced < count {
            let index = Int(position), next = index + 1
            if UInt64(next) >= writeAbs - readAbs { break }
            let t = Float(position - Double(index))
            let a = Int((readAbs + UInt64(index)) % cap), b = Int((readAbs + UInt64(next)) % cap)
            outL[produced] = left[a] + (left[b] - left[a]) * t
            outR[produced] = right[a] + (right[b] - right[a]) * t
            produced += 1; position += ratio
        }
        let whole = min(Int(position), Int(writeAbs - readAbs))
        readAbs += UInt64(whole); consumed += UInt64(whole)
        frac = max(0, position - Double(whole))
        if produced < count {
            for i in produced..<count { outL[i] = 0; outR[i] = 0 }
            underrunEvents += 1; lastUnderrunPlayedS = Double(playedOut) / rate; lastUnderrunDepth = depth; underrunFrames += UInt64(count - produced); silentFrames += UInt64(count - produced)
            playing = false; frac = 0
            result.underran = true; result.playing = false
        }
        playedOut += UInt64(produced)
        if warmBaseline == nil && playedOut >= warmFrames { warmBaseline = (underrunEvents, overflowEvents) }
        return result
    }

    struct Counters {
        var depth: Int, target: Int, capacity: Int, playing: Bool
        var pushed: UInt64, consumed: UInt64, outFrames: UInt64
        var underruns: UInt64, underrunFrames: UInt64, overflows: UInt64, overflowFrames: UInt64
        var prefills: UInt64, flushes: UInt64, depthMin: Int, depthMax: Int
        var underrunsAfterWarmup: UInt64, overflowsAfterWarmup: UInt64
        var latency: [String: Double]
        var lastUnderrunAtPlayedS: Double, lastUnderrunDepth: Int, playedFrames: UInt64
    }

    func counters(resetDepthRange: Bool = false) -> Counters {
        lock.lock(); defer { lock.unlock() }
        let c = Counters(depth: Int(writeAbs - readAbs), target: targetFrames, capacity: capacity, playing: playing,
                         pushed: pushed, consumed: consumed, outFrames: outFrames, underruns: underrunEvents,
                         underrunFrames: underrunFrames, overflows: overflowEvents, overflowFrames: overflowFrames,
                         prefills: prefills, flushes: flushes, depthMin: depthMin == Int.max ? 0 : depthMin,
                         depthMax: depthMax,
                         underrunsAfterWarmup: warmBaseline.map { underrunEvents - $0.underruns } ?? 0,
                         overflowsAfterWarmup: warmBaseline.map { overflowEvents - $0.overflows } ?? 0,
                         latency: latency.summary(), lastUnderrunAtPlayedS: lastUnderrunPlayedS, lastUnderrunDepth: lastUnderrunDepth, playedFrames: playedOut)
        if resetDepthRange { depthMin = Int.max; depthMax = 0 }
        return c
    }
}

// MARK: - Output

/// `offline:<wav path>[:<device rate>[:<ppm>[:<block frames>]]]` renders the graph to a file on a software clock
/// (nothing reaches any device). Used for the verification runs; also selectable with `--audio-device`.
struct OfflineSpec {
    var path: String, rate: Double = 48000, ppm: Double = 0, block: Int = 256
    init?(_ text: String) {
        guard text.hasPrefix("offline:") else { return nil }
        let parts = text.dropFirst(8).split(separator: ":", omittingEmptySubsequences: false).map(String.init)
        guard let first = parts.first, !first.isEmpty else { return nil }
        path = first
        if parts.count > 1, let v = Double(parts[1]), v >= 8000 { rate = v }
        if parts.count > 2, let v = Double(parts[2]) { ppm = v }
        if parts.count > 3, let v = Int(parts[3]), v >= 32 { block = v }
    }
}

final class AudioOutput {
    enum State: String { case off, starting, running, unavailable, stopped }

    static let baseMarginSeconds = 0.010
    static let maxMarginSeconds = 0.040
    /// Jitter margin on top of one device block. Starts at 10 ms (measured transport p95 2.9 ms + 2 ms poll + one
    /// 64-frame period = 6.4 ms, plus headroom for the scheduling stalls of a loaded host) and grows by half again plus 2 ms on every underrun after the first prefill, to 40 ms: a loaded
    /// host trades latency for continuity and says so in the metrics (`margin_ms`). It never shrinks within a run.
    private(set) var marginSeconds = AudioOutput.baseMarginSeconds
    let requestedUID: String?
    let offline: OfflineSpec?
    private let queue = DispatchQueue(label: "sp16.audio.output")
    private let stateLock = NSLock()
    private var state = State.off
    private var engine: AVAudioEngine?
    private var bufferStorage: JitterBuffer?
    private var buffer: JitterBuffer? {
        get { stateLock.lock(); defer { stateLock.unlock() }; return bufferStorage }
        set { stateLock.lock(); bufferStorage = newValue; stateLock.unlock() }
    }
    private var controller = DriftController(targetFrames: 0)
    private var controllerLock = UnfairLock()
    private var inputRate = 44100.0
    private var deviceRate = 48000.0
    private var block = 128
    private var deviceName = "", deviceUID = "", deviceLatencyMs = 0.0
    private var stopped = false
    private var observer: NSObjectProtocol?
    private var offlineThread: Thread?
    private var retryTimer: DispatchSourceTimer?
    /// Power-on mute (the firmware's xMUTE, ~19.5 s after power-on). Read on the render thread, written once at
    /// launch and once at the unmute deadline, so it is guarded by its own unfair lock.
    private var mutedStorage = false
    private let mutedLock = UnfairLock()
    var muted: Bool {
        get { mutedLock.lock(); defer { mutedLock.unlock() }; return mutedStorage }
        set { mutedLock.lock(); mutedStorage = newValue; mutedLock.unlock() }
    }
    /// Verification only: build and prepare the real device graph (device selection, formats) but never start IO.
    var dryRun = false
    private var droppedUnavailable: UInt64 = 0, restarts: UInt64 = 0, lastError = ""
    private var ratioNow = 1.0
    // windowed rate measurement (host clock)
    private var windowStart = AudioSink.nowNs()
    private var windowPushed: UInt64 = 0, windowOut: UInt64 = 0
    private var guestRateHz = 0.0, deviceRateHz = 0.0
    private var lastRenderNs: UInt64 = 0, lastPushNs: UInt64 = 0
    private func tickNow() -> UInt64 { AudioSink.nowNs() }
    private var renderGapMaxMs = 0.0, pushGapMaxMs = 0.0
    private var eventLog: [[Double]] = []   // last 24 underruns: [uptime s, depth before, ms since previous callback, ms since last push, margin ms]

    init(deviceUID: String?, offline: OfflineSpec?) { requestedUID = deviceUID; self.offline = offline }

    // MARK: Device helpers

    static func deviceID(forUID uid: String) -> AudioDeviceID? {
        var cf = uid as CFString
        var device = AudioDeviceID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        let status = withUnsafeMutablePointer(to: &cf) {
            AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, UInt32(MemoryLayout<CFString>.size), $0, &size, &device)
        }
        return status == noErr && device != kAudioObjectUnknown ? device : nil
    }

    static func defaultOutputDevice() -> AudioDeviceID? {
        var device = AudioDeviceID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device)
        return status == noErr && device != kAudioObjectUnknown ? device : nil
    }

    static func stringProperty(_ device: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>? = nil
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value) == noErr, let v = value else { return "" }
        return v.takeRetainedValue() as String
    }

    static func uint32Property(_ device: AudioDeviceID, _ selector: AudioObjectPropertySelector,
                               scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
        var value = UInt32(0), size = UInt32(4)
        return AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value) == noErr ? value : nil
    }

    /// Output devices as (uid, name, hasOutput) for `--audio-device` discovery.
    static func outputDevices() -> [(uid: String, name: String)] {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(0)
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(system, &address, 0, nil, &size, &ids) == noErr else { return [] }
        return ids.compactMap { id in
            var out = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
            var s = UInt32(0)
            guard AudioObjectGetPropertyDataSize(id, &out, 0, nil, &s) == noErr, s > 0 else { return nil }
            return (stringProperty(id, kAudioDevicePropertyDeviceUID), stringProperty(id, kAudioObjectPropertyName))
        }
    }

    /// Time-constraint scheduling for the software device clock, as CoreAudio gives its IO thread; without it a loaded
    /// host stalls the clock and catches up in bursts, which a real device never does.
    static func makeRealtime(period: Double) {
        var base = mach_timebase_info_data_t(); mach_timebase_info(&base)
        func ticks(_ seconds: Double) -> UInt32 { UInt32(seconds * 1e9 * Double(base.denom) / Double(base.numer)) }
        var policy = thread_time_constraint_policy_data_t(period: ticks(period), computation: ticks(0.0005),
                                                          constraint: ticks(period * 0.5), preemptible: 1)
        let count = mach_msg_type_number_t(MemoryLayout<thread_time_constraint_policy_data_t>.size / MemoryLayout<integer_t>.size)
        _ = withUnsafeMutablePointer(to: &policy) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                thread_policy_set(mach_thread_self(), thread_policy_flavor_t(THREAD_TIME_CONSTRAINT_POLICY), $0, count)
            }
        }
    }

    /// Absolute-deadline sleep on the same clock as `AudioSink.nowNs` (CLOCK_UPTIME_RAW = mach_absolute_time).
    static func sleepUntil(uptimeNs: UInt64) {
        var base = mach_timebase_info_data_t(); mach_timebase_info(&base)
        mach_wait_until(uptimeNs &* UInt64(base.denom) / UInt64(base.numer))
    }

    // MARK: Lifecycle

    private func set(_ s: State) { stateLock.lock(); state = s; stateLock.unlock() }

    private var activity: NSObjectProtocol?

    func start() {
        // Audio is latency critical: no App Nap and no timer coalescing while it runs (a windowless or occluded app
        // is otherwise throttled, which stalls the software clock and any helper timers by tens of milliseconds).
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .latencyCritical],
                                                          reason: "SP-16 emulator audio output")
        queue.async { [weak self] in self?.bringUp() }
    }

    func stop() {
        if let a = activity { ProcessInfo.processInfo.endActivity(a); activity = nil }
        stateLock.lock(); stopped = true; stateLock.unlock()
        queue.sync { tearDown(); set(.stopped) }
    }

    private var isStopped: Bool { stateLock.lock(); defer { stateLock.unlock() }; return stopped }

    /// Producer entry. Safe from any thread; frames are dropped (and counted) while no device is running.
    func push(left: UnsafePointer<Float>?, right: UnsafePointer<Float>?, count: Int, rate: Double, writeNs: UInt64) {
        if rate != inputRate {   // format change: restart on the new input rate, discarding stale audio
            inputRate = rate
            queue.async { [weak self] in self?.reconfigureForRate() }
        }
        if let b = buffer, !isStopped {
            b.push(left, right, count: count, writeNs: writeNs)
            controllerLock.lock(); windowPushed += UInt64(count)
            let tick = AudioSink.nowNs()
            if lastPushNs != 0 { pushGapMaxMs = max(pushGapMaxMs, Double(tick &- lastPushNs) / 1e6) }
            lastPushNs = tick
            controllerLock.unlock()
        } else { droppedUnavailable += UInt64(count) }
    }

    func flush() { buffer?.flush(); controllerLock.lock(); controller.reset(); controllerLock.unlock() }

    private func reconfigureForRate() { if !isStopped, engine != nil || offlineThread != nil { tearDown(); bringUp() } }

    private func sizeBuffer() {
        let nominal = inputRate / deviceRate
        let target = Int(ceil(Double(block) * nominal + marginSeconds * inputRate)) + 2
        let capacity = max(4096, Int(ceil(Double(block) * nominal + AudioOutput.maxMarginSeconds * inputRate)) * 2 + 2048)
        buffer = JitterBuffer(capacity: capacity, targetFrames: target, rate: inputRate, warmupFrames: UInt64(deviceRate * 5))
        controllerLock.lock(); controller = DriftController(targetFrames: Double(target)); controllerLock.unlock()
    }

    /// Render callback body shared by the device and the offline clock.
    func render(_ outL: UnsafeMutablePointer<Float>, _ outR: UnsafeMutablePointer<Float>, count: Int) {
        guard let b = buffer else { for i in 0..<count { outL[i] = 0; outR[i] = 0 }; return }
        if count > block {   // callback bigger than planned: a whole block must be available at once
            block = count
            b.setTarget(Int(ceil(Double(count) * inputRate / deviceRate + marginSeconds * inputRate)) + 2)
            controllerLock.lock(); controller.targetFrames = Double(b.targetFrames); controllerLock.unlock()
        }
        controllerLock.lock()
        let nominal = inputRate / deviceRate
        let ratio = nominal * (1 + controller.adjust)
        controllerLock.unlock()
        let r = b.read(outL, outR, count: count, ratio: ratio, nowNs: AudioSink.nowNs())
        if r.underran {
            controllerLock.lock()
            eventLog.append([Double(tickNow()) / 1e9, r.depthBefore, Double(tickNow() &- lastRenderNs) / 1e6,
                             Double(tickNow() &- lastPushNs) / 1e6, marginSeconds * 1000])
            if eventLog.count > 24 { eventLog.removeFirst() }
            controllerLock.unlock()
            marginSeconds = min(AudioOutput.maxMarginSeconds, marginSeconds * 1.5 + 0.002)
            let t = Int(ceil(Double(block) * inputRate / deviceRate + marginSeconds * inputRate)) + 2
            b.setTarget(t)
            controllerLock.lock(); controller.targetFrames = Double(b.targetFrames); controllerLock.unlock()
        }
        controllerLock.lock()
        let tick = AudioSink.nowNs()
        if lastRenderNs != 0 { renderGapMaxMs = max(renderGapMaxMs, Double(tick &- lastRenderNs) / 1e6) }
        lastRenderNs = tick
        ratioNow = ratio
        if r.playing && !r.underran && !r.prefilledNow {
            _ = controller.update(depth: r.depthBefore, dt: Double(count) / deviceRate, inputRate: inputRate)
        } else if r.underran || !r.playing { controller.reset() }
        windowOut += UInt64(count)
        let now = AudioSink.nowNs()
        if now &- windowStart >= 5_000_000_000 {
            let seconds = Double(now &- windowStart) / 1e9
            guestRateHz = Double(windowPushed) / seconds; deviceRateHz = Double(windowOut) / seconds
            windowStart = now; windowPushed = 0; windowOut = 0
        }
        controllerLock.unlock()
        if muted { for i in 0..<count { outL[i] = 0; outR[i] = 0 } }
    }

    private func bringUp() {
        guard !isStopped else { return }
        set(.starting)
        if let spec = offline { bringUpOffline(spec); return }
        var deviceID: AudioDeviceID?
        if let uid = requestedUID, !uid.isEmpty {
            deviceID = AudioOutput.deviceID(forUID: uid)
            if deviceID == nil { markUnavailable("device \(uid) not present"); return }
        } else { deviceID = AudioOutput.defaultOutputDevice() }
        guard let device = deviceID else { markUnavailable("no default output device"); return }
        deviceName = AudioOutput.stringProperty(device, kAudioObjectPropertyName)
        deviceUID = AudioOutput.stringProperty(device, kAudioDevicePropertyDeviceUID)
        // Smaller IO buffer = less added latency; the device keeps whatever it can do.
        var want = UInt32(128)
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyBufferFrameSize, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        _ = AudioObjectSetPropertyData(device, &address, 0, nil, 4, &want)
        block = Int(AudioOutput.uint32Property(device, kAudioDevicePropertyBufferFrameSize) ?? 512)
        let outScope = kAudioObjectPropertyScopeOutput
        deviceLatencyMs = Double((AudioOutput.uint32Property(device, kAudioDevicePropertyLatency, scope: outScope) ?? 0)
                                 + (AudioOutput.uint32Property(device, kAudioDevicePropertySafetyOffset, scope: outScope) ?? 0)) // frames, scaled below
        let engine = AVAudioEngine()
        do {
            try engine.outputNode.auAudioUnit.setDeviceID(device)
        } catch { markUnavailable("cannot select device: \(error.localizedDescription)"); return }
        deviceRate = engine.outputNode.outputFormat(forBus: 0).sampleRate
        if deviceRate < 8000 { markUnavailable("device reports no sample rate"); return }
        deviceLatencyMs = deviceLatencyMs / deviceRate * 1000 + Double(block) / deviceRate * 1000
        sizeBuffer()
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: deviceRate, channels: 2, interleaved: false)!
        let node = makeSourceNode(format)
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
        self.engine = engine
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            // Device removed, default changed or rate changed: rebuild against whatever is there now, with stale audio dropped.
            self?.queue.async { guard let self = self, !self.isStopped else { return }; self.restarts += 1; self.tearDown(); self.bringUp() }
        }
        do { if dryRun { engine.prepare() } else { try engine.start() }; set(.running) } catch { markUnavailable("engine start failed: \(error.localizedDescription)") }
    }

    private func makeSourceNode(_ format: AVAudioFormat) -> AVAudioSourceNode {
        AVAudioSourceNode(format: format) { [unowned self] _, _, frames, list in
            let abl = UnsafeMutableAudioBufferListPointer(list)
            guard abl.count >= 2, let l = abl[0].mData?.assumingMemoryBound(to: Float.self),
                  let r = abl[1].mData?.assumingMemoryBound(to: Float.self) else { return noErr }
            self.render(l, r, count: Int(frames))
            return noErr
        }
    }

    private func bringUpOffline(_ spec: OfflineSpec) {
        deviceRate = spec.rate; block = spec.block; deviceName = "offline"; deviceUID = "offline"
        sizeBuffer()
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: deviceRate, channels: 2, interleaved: false)!
        let engine = AVAudioEngine()
        let node = makeSourceNode(format)
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)
        engine.connect(engine.mainMixerNode, to: engine.outputNode, format: format)
        do {
            try engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: AVAudioFrameCount(spec.block))
            try engine.start()
        } catch { markUnavailable("offline engine: \(error.localizedDescription)"); return }
        self.engine = engine
        let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: spec.rate, AVNumberOfChannelsKey: 2,
                                       AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false]
        guard let file = try? AVAudioFile(forWriting: URL(fileURLWithPath: spec.path), settings: settings,
                                          commonFormat: .pcmFormatFloat32, interleaved: false),
              let out = AVAudioPCMBuffer(pcmFormat: engine.manualRenderingFormat, frameCapacity: AVAudioFrameCount(spec.block)) else {
            markUnavailable("cannot create \(spec.path)"); return
        }
        let writer = DispatchQueue(label: "sp16.audio.offline-writer")
        set(.running)
        let thread = Thread { [weak self] in
            AudioOutput.makeRealtime(period: Double(spec.block) / spec.rate)   // like a device IO thread
            // Software device clock: block k is due at k * block / (rate * (1 + ppm)).
            let start = AudioSink.nowNs(), period = Double(spec.block) / (spec.rate * (1 + spec.ppm * 1e-6))
            var k = 0.0
            while let self = self, !self.isStopped, !Thread.current.isCancelled {
                k += 1
                let due = start &+ UInt64(k * period * 1e9)
                var now = AudioSink.nowNs()
                if due > now { AudioOutput.sleepUntil(uptimeNs: due) }
                now = AudioSink.nowNs()
                out.frameLength = out.frameCapacity
                guard (try? engine.renderOffline(AVAudioFrameCount(spec.block), to: out)) != nil else { continue }
                // The WAV goes out on its own queue: a disk stall must not look like a device stall.
                if let copy = AVAudioPCMBuffer(pcmFormat: out.format, frameCapacity: out.frameLength) {
                    copy.frameLength = out.frameLength
                    for ch in 0..<Int(out.format.channelCount) { memcpy(copy.floatChannelData![ch], out.floatChannelData![ch], Int(out.frameLength) * 4) }
                    writer.async { try? file.write(from: copy) }
                }
            }
        }
        thread.qualityOfService = .userInteractive
        offlineThread = thread
        thread.start()
    }

    private func markUnavailable(_ why: String) {
        lastError = why; set(.unavailable); buffer = nil
        // Poll for the device: after removal or before it is plugged in, no stale audio is kept.
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 2, repeating: 2)
        timer.setEventHandler { [weak self] in
            guard let self = self, !self.isStopped else { return }
            self.retryTimer?.cancel(); self.retryTimer = nil
            self.bringUp()
        }
        retryTimer?.cancel(); retryTimer = timer; timer.resume()
    }

    private func tearDown() {
        retryTimer?.cancel(); retryTimer = nil
        if let o = observer { NotificationCenter.default.removeObserver(o); observer = nil }
        let thread = offlineThread; offlineThread = nil
        // Offline thread exits on `stopped`; for a reconfigure it is replaced, so signal via a cancel flag.
        thread?.cancel()
        engine?.stop(); engine = nil
        buffer = nil
    }

    // MARK: Metrics

    func snapshot() -> [String: Any] {
        stateLock.lock(); let s = state; stateLock.unlock()
        var out: [String: Any] = ["state": s.rawValue, "muted": muted, "device_uid": deviceUID, "device_name": deviceName,
                                  "requested_device": requestedUID ?? "system default", "offline": offline != nil,
                                  "device_rate": deviceRate, "input_rate": inputRate, "block_frames": block,
                                  "dropped_while_unavailable": droppedUnavailable, "restarts": restarts, "last_error": lastError,
                                  "device_latency_ms": deviceLatencyMs, "margin_ms": marginSeconds * 1000]
        controllerLock.lock()
        out["resample_ratio"] = ratioNow; out["nominal_ratio"] = inputRate / deviceRate
        out["ratio_adjust_ppm"] = controller.adjustPPM; out["integral_ppm"] = controller.integralPPM
        out["underrun_log"] = eventLog
        out["render_gap_max_ms"] = renderGapMaxMs; out["push_gap_max_ms"] = pushGapMaxMs; renderGapMaxMs = 0; pushGapMaxMs = 0
        out["guest_rate_hz"] = guestRateHz; out["device_rate_hz"] = deviceRateHz
        if deviceRateHz > 0 { out["drift_ppm"] = (guestRateHz / deviceRateHz * deviceRate / inputRate - 1) * 1e6 }
        controllerLock.unlock()
        if let c = buffer?.counters(resetDepthRange: true) {
            out["depth_frames"] = c.depth; out["target_frames"] = c.target; out["capacity_frames"] = c.capacity
            out["depth_min"] = c.depthMin; out["depth_max"] = c.depthMax; out["playing"] = c.playing
            out["pushed_frames"] = c.pushed; out["underruns"] = c.underruns; out["underrun_frames"] = c.underrunFrames
            out["overflows"] = c.overflows; out["overflow_frames"] = c.overflowFrames; out["prefills"] = c.prefills
            out["flushes"] = c.flushes; out["last_underrun_played_s"] = c.lastUnderrunAtPlayedS; out["last_underrun_depth"] = c.lastUnderrunDepth; out["underruns_after_warmup"] = c.underrunsAfterWarmup
            out["overflows_after_warmup"] = c.overflowsAfterWarmup; out["write_to_render_ms"] = c.latency
        }
        return out
    }
}
