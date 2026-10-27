import Foundation
import Darwin

/// Host side of the audio transport (`native.py build --audio file`): reads the dedicated virtio-serial
/// port (`<socket>.pcm`), writes one WAV per playback stream into the session directory and keeps counters.
/// File sink only: nothing here touches CoreAudio or any output device. See hardware_stub.c for the packet layout.
final class AudioSink {
    static let headerSize = 48
    static let magic: [UInt8] = Array("SPCM".utf8)

    struct StreamState {
        var name: String
        var format: Int, channels: Int, firstChannel: Int, totalChannels: Int, rate: Int
        /// Channels of the payload that go into the WAV: the whole payload, except that the 16-channel master stream
        /// keeps its historical file (channels 12...15) when the guest sends all 16 for the output monitor.
        var wavFirst = 0, wavChannels = 0
        var fd: Int32 = -1
        var path: String
        var dataBytes: UInt64 = 0
        var frames: UInt64 = 0
        var nextFrame: UInt64? = nil
        var gapFrames: UInt64 = 0
        var overlapFrames: UInt64 = 0
        var guestDrops: UInt64 = 0
        var packets: UInt64 = 0
        var peak: Double = 0
        var nonzeroFrames: UInt64 = 0
        var lastHeaderUpdate: UInt64 = 0
    }

    /// Latency histogram over the whole run (50 microsecond bins to 250 ms), so percentiles are not windowed.
    struct Histogram {
        static let binSeconds = 0.00005
        var bins = [UInt64](repeating: 0, count: 5000)
        var count: UInt64 = 0, sum = 0.0, maximum = 0.0
        mutating func add(_ seconds: Double) {
            let value = max(0, seconds)
            let bin = min(bins.count - 1, Int(value / Histogram.binSeconds))
            bins[bin] += 1; count += 1; sum += value; maximum = max(maximum, value)
        }
        func percentile(_ p: Double) -> Double {
            guard count > 0 else { return 0 }
            let target = UInt64(Double(count) * p), limit = min(target, count - 1)
            var seen: UInt64 = 0
            for (index, value) in bins.enumerated() {
                seen += value
                if seen > limit { return Double(index + 1) * Histogram.binSeconds }
            }
            return maximum
        }
        func summary() -> [String: Double] {
            guard count > 0 else { return ["count": 0] }
            return ["count": Double(count), "p50": percentile(0.5) * 1000, "p95": percentile(0.95) * 1000,
                    "p99": percentile(0.99) * 1000, "max": maximum * 1000, "mean": sum / Double(count) * 1000]
        }
    }

    let socketPath: String
    let directory: String
    let lock = NSLock()
    var streams: [Int: StreamState] = [:]
    var wavIndex: [Int: Int] = [:]
    var connected = false
    var packets: UInt64 = 0, bytes: UInt64 = 0, seqGaps: UInt64 = 0, badPackets: UInt64 = 0
    var expectedSequence: UInt32? = nil
    var latency = Histogram()
    var late20: UInt64 = 0
    var lateLog: [[Double]] = []   // last 8 packets later than 20 ms: [host uptime s, latency ms]
    var minRTT = Double.infinity
    var offsetNs: Double? = nil      // guest monotonic ns minus host ns (NTP midpoint, best round trip)
    var stopping = false
    var pendingWriteNs: UInt64 = 0    // host-clock time the guest wrote the packet being handled
    var isRunning: () -> Bool
    let writeFiles: Bool
    let output: AudioOutput?
    /// Output jacks the host can monitor (the API of the output monitor menu). `source` says where the jack's stereo
    /// pair lives in the transport: the stream id (0 ak4458audio-0, 1 ak4452audio-0 = PHONES) and the left and right
    /// channel inside that stream's own channel numbering (absolute, as the guest has them). The AK4458 stream is 16
    /// S16 channels, 8 DAC slots of two channels each; only the even channel of a slot carries audio. Grades: MAIN and
    /// OUT7-8 observed with AUDIO TEST MODE3 and MODE2, OUT3-4 and OUT5-6 inferred (a regular slot order), PHONES
    /// observed (MODE1, MODE2). See audio-jack-map.json.
    enum OutputJack: String, CaseIterable {
        case main = "MAIN", out34 = "OUT3-4", out56 = "OUT5-6", out78 = "OUT7-8", phones = "PHONES"
        var source: (stream: Int, left: Int, right: Int) {
            switch self {
            case .main: return (0, 12, 14)
            case .out34: return (0, 8, 10)
            case .out56: return (0, 4, 6)
            case .out78: return (0, 0, 2)
            case .phones: return (1, 0, 1)
            }
        }
    }
    private var monitorJack = OutputJack.main
    /// The jack the CoreAudio output plays (MAIN by default). Thread safe; takes effect on the next packet. The WAVs
    /// never depend on it.
    var monitor: OutputJack {
        get { lock.lock(); defer { lock.unlock() }; return monitorJack }
        set {
            lock.lock(); let changed = monitorJack != newValue; monitorJack = newValue; lock.unlock()
            if changed { output?.flush() }
        }
    }

    init(socketPath: String, directory: String, writeFiles: Bool = true, output: AudioOutput? = nil,
         isRunning: @escaping () -> Bool) {
        self.socketPath = socketPath; self.directory = directory; self.isRunning = isRunning
        self.writeFiles = writeFiles; self.output = output
    }

    func start() {
        // Receipt latency is measured here, so the reader must not wait behind other host work.
        let thread = Thread { [weak self] in self?.run() }
        thread.qualityOfService = .userInteractive
        thread.start()
    }

    func stop() {
        lock.lock(); stopping = true; lock.unlock()
    }

    static func nowNs() -> UInt64 { clock_gettime_nsec_np(CLOCK_UPTIME_RAW) }

    static func formatName(_ format: Int) -> String {
        switch format { case 2: return "S16_LE"; case 10: return "S32_LE"; case 14: return "FLOAT_LE"; default: return "ALSA_\(format)" }
    }
    static func sampleWidth(_ format: Int) -> Int? {
        switch format { case 2: return 2; case 10, 14: return 4; default: return nil }
    }
    static let streamNames = [0: "ak4458audio-0", 1: "ak4452audio-0", 2: "ak4452audio-1"]

    // MARK: Reader

    func connect() -> Int32? {
        for _ in 0..<1200 {
            lock.lock(); let quit = stopping; lock.unlock()
            if quit || !isRunning() { return nil }
            let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
            guard fd >= 0 else { return nil }
            var address = sockaddr_un()
            address.sun_family = sa_family_t(AF_UNIX)
            let path = Array(socketPath.utf8)
            guard path.count < MemoryLayout.size(ofValue: address.sun_path) else { Darwin.close(fd); return nil }
            withUnsafeMutableBytes(of: &address.sun_path) { raw in for (i, b) in path.enumerated() { raw[i] = b } }
            let rc = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
            }
            if rc == 0 {
                var noSignal: Int32 = 1
                setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
                return fd
            }
            Darwin.close(fd)
            Thread.sleep(forTimeInterval: 0.1)
        }
        return nil
    }

    func sendPing(_ fd: Int32) {
        var message = [UInt8](repeating: 0, count: 16)
        message.replaceSubrange(0..<4, with: Array("SPNG".utf8))
        var t0 = AudioSink.nowNs().littleEndian
        withUnsafeBytes(of: &t0) { message.replaceSubrange(8..<16, with: $0) }
        _ = message.withUnsafeBytes { Darwin.write(fd, $0.baseAddress!, 16) }
    }

    func run() {
        guard let fd = connect() else { return }
        lock.lock(); connected = true; lock.unlock()
        var pending = Data()
        var lastPing = 0.0, pings = 0
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            lock.lock(); let quit = stopping; lock.unlock()
            if quit { break }
            let now = ProcessInfo.processInfo.systemUptime
            if now - lastPing >= (pings < 20 ? 0.05 : 1.0) { sendPing(fd); lastPing = now; pings += 1 }
            var poller = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&poller, 1, 50)
            if ready < 0 && errno == EINTR { continue }
            if ready <= 0 { continue }
            let count = Darwin.read(fd, &chunk, chunk.count)
            if count < 0 && errno == EINTR { continue }
            if count <= 0 { break }
            let received = AudioSink.nowNs()
            pending.append(contentsOf: chunk[0..<count])
            consume(&pending, received: received)
        }
        Darwin.close(fd)
        lock.lock()
        connected = false
        for id in Array(streams.keys) { updateHeader(id, force: true); if streams[id]!.fd >= 0 { Darwin.close(streams[id]!.fd); streams[id]!.fd = -1 } }
        lock.unlock()
    }

    func u32(_ d: Data, _ o: Int) -> UInt32 { (0..<4).reduce(UInt32(0)) { $0 | UInt32(d[d.startIndex + o + $1]) << UInt32(8 * $1) } }
    func u64(_ d: Data, _ o: Int) -> UInt64 { (0..<8).reduce(UInt64(0)) { $0 | UInt64(d[d.startIndex + o + $1]) << UInt64(8 * $1) } }

    func consume(_ pending: inout Data, received: UInt64) {
        while pending.count >= AudioSink.headerSize {
            let head = pending.prefix(AudioSink.headerSize)
            guard Array(head.prefix(4)) == AudioSink.magic else {
                lock.lock(); badPackets += 1; lock.unlock()
                pending.removeAll()   // lost framing: wait for a fresh connection rather than guess
                return
            }
            let payload = Int(u32(pending, 44))
            guard payload <= 1 << 20 else { lock.lock(); badPackets += 1; lock.unlock(); pending.removeAll(); return }
            guard pending.count >= AudioSink.headerSize + payload else { return }
            let packet = Data(pending.prefix(AudioSink.headerSize + payload))
            pending.removeFirst(AudioSink.headerSize + payload)
            handle(packet, received: received)
        }
    }

    // MARK: Packets

    func handle(_ p: Data, received: UInt64) {
        let type = Int(p[p.startIndex + 4]), stream = Int(p[p.startIndex + 5])
        let sequence = u32(p, 16), frames = Int(u32(p, 20)), guestNs = u64(p, 24), index = u64(p, 32)
        lock.lock(); defer { lock.unlock() }
        packets += 1; bytes += UInt64(p.count)
        if let expected = expectedSequence, expected != sequence { seqGaps += UInt64(sequence &- expected) }
        expectedSequence = sequence &+ 1
        switch type {
        case 4: // pong: index echoes the host's send time
            let rtt = Double(received &- index) / 1e9
            if rtt < minRTT { minRTT = rtt }
            if rtt <= minRTT * 1.5 + 0.0002 {
                offsetNs = Double(guestNs) - (Double(index) + Double(received)) / 2
            }
        case 1, 2:
            let format = Int(p[p.startIndex + 6]), channels = Int(p[p.startIndex + 7]), total = Int(p[p.startIndex + 8])
            let first = Int(p[p.startIndex + 9]), rate = Int(u32(p, 12))
            guard AudioSink.sampleWidth(format) != nil, channels > 0 else { badPackets += 1; return }
            ensureStream(stream, format: format, channels: channels, total: total, first: first, rate: rate)
            guard type == 2 else { return }
            streams[stream]!.guestDrops = UInt64(u32(p, 40))
            pendingWriteNs = offsetNs.map { UInt64(max(0, Double(guestNs) - $0)) } ?? received
            write(stream, payload: p.dropFirst(AudioSink.headerSize), frames: frames, index: index)
            if let offset = offsetNs {
                let seconds = (Double(received) - (Double(guestNs) - offset)) / 1e9
                latency.add(seconds)
                if seconds > 0.02 {
                    late20 += 1
                    lateLog.append([ProcessInfo.processInfo.systemUptime, (seconds * 1000).rounded()])
                    if lateLog.count > 8 { lateLog.removeFirst() }
                }
            }
        default: badPackets += 1
        }
    }

    func ensureStream(_ id: Int, format: Int, channels: Int, total: Int, first: Int, rate: Int) {
        if let s = streams[id], s.format == format, s.channels == channels, s.rate == rate, s.firstChannel == first { return }
        if let s = streams[id], s.fd >= 0 { updateHeader(id, force: true); Darwin.close(s.fd) }
        let serial = (wavIndex[id] ?? 0) + 1
        wavIndex[id] = serial
        let base = AudioSink.streamNames[id] ?? "stream\(id)"
        let path = directory + "/audio-" + base + (serial > 1 ? "-\(serial)" : "") + ".wav"
        let fd = writeFiles ? Darwin.open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0o600) : -1
        var state = StreamState(name: base, format: format, channels: channels, firstChannel: first, totalChannels: total,
                                rate: rate, fd: fd, path: path)
        state.nextFrame = nil
        if id == 0 && first == 0 && channels == 16 { state.wavFirst = 12; state.wavChannels = 4 } else { state.wavFirst = 0; state.wavChannels = channels }
        if let old = streams[id] { state.guestDrops = old.guestDrops }
        streams[id] = state
        updateHeader(id, force: true)
        // The header goes in with pwrite, which leaves the file offset at 0: move it past the header, or the first
        // sample bytes overwrite it and the data ends up displaced by 44 bytes (4 bytes mod the frame size).
        if fd >= 0 { _ = lseek(fd, 44, SEEK_SET) }
    }

    func updateHeader(_ id: Int, force: Bool) {
        guard var s = streams[id], s.fd >= 0, let width = AudioSink.sampleWidth(s.format) else { return }
        if !force && s.dataBytes - s.lastHeaderUpdate < UInt64(s.rate * s.wavChannels * width) { return }
        s.lastHeaderUpdate = s.dataBytes
        streams[id] = s
        let dataSize = UInt32(truncatingIfNeeded: min(s.dataBytes, 0xFFFF_FFFF - 36))
        var h = [UInt8]()
        func put32(_ v: UInt32) { for i in 0..<4 { h.append(UInt8((v >> UInt32(8 * i)) & 255)) } }
        func put16(_ v: UInt16) { h.append(UInt8(v & 255)); h.append(UInt8(v >> 8)) }
        h += Array("RIFF".utf8); put32(36 + dataSize); h += Array("WAVEfmt ".utf8); put32(16)
        put16(s.format == 14 ? 3 : 1); put16(UInt16(s.wavChannels)); put32(UInt32(s.rate))
        put32(UInt32(s.rate * s.wavChannels * width)); put16(UInt16(s.wavChannels * width)); put16(UInt16(width * 8))
        h += Array("data".utf8); put32(dataSize)
        _ = h.withUnsafeBytes { Darwin.pwrite(s.fd, $0.baseAddress!, 44, 0) }
    }

    func write(_ id: Int, payload: Data, frames: Int, index: UInt64) {
        guard var s = streams[id], let width = AudioSink.sampleWidth(s.format) else { return }
        let bpf = s.channels * width, wavBpf = s.wavChannels * width
        guard frames > 0, payload.count >= frames * bpf else { badPackets += 1; return }
        var gapFrames: UInt64 = 0
        if let next = s.nextFrame {
            if index > next {
                gapFrames = index - next
                // frames the guest had to drop: keep the timeline with silence (bounded to 10 s per gap)
                var gap = min(index - next, UInt64(s.rate) * 10)
                s.gapFrames += index - next
                let zeros = [UInt8](repeating: 0, count: 65536)
                while gap > 0 {
                    let step = min(UInt64(zeros.count / wavBpf), gap)
                    if s.fd >= 0 { _ = zeros.withUnsafeBytes { Darwin.write(s.fd, $0.baseAddress!, Int(step) * wavBpf) } }
                    s.dataBytes += step * UInt64(wavBpf); s.frames += step; gap -= step
                }
            } else if index < next { s.overlapFrames += next - index }
        }
        if let output = output, id == monitorJack.source.stream { feedOutput(output, s, jack: monitorJack, payload: payload, frames: frames, index: index, gap: gapFrames) }
        s.nextFrame = index + UInt64(frames)
        payload.withUnsafeBytes { raw in
            if s.fd >= 0 {
                if s.wavChannels == s.channels { _ = Darwin.write(s.fd, raw.baseAddress!, frames * bpf) }
                else {
                    var sliced = [UInt8](repeating: 0, count: frames * wavBpf)
                    for f in 0..<frames {
                        for b in 0..<wavBpf { sliced[f * wavBpf + b] = raw.load(fromByteOffset: f * bpf + s.wavFirst * width + b, as: UInt8.self) }
                    }
                    _ = sliced.withUnsafeBytes { Darwin.write(s.fd, $0.baseAddress!, frames * wavBpf) }
                }
            }
            let base = raw.baseAddress!
            for f in 0..<frames {
                var any = false
                for c in s.wavFirst..<(s.wavFirst + s.wavChannels) {
                    let o = (f * s.channels + c) * width
                    var v = 0.0
                    switch s.format {
                    case 2: v = abs(Double(base.loadUnaligned(fromByteOffset: o, as: Int16.self).littleEndian)) / 32768
                    case 10: v = abs(Double(base.loadUnaligned(fromByteOffset: o, as: Int32.self).littleEndian)) / 2147483648
                    default:
                        let bits = base.loadUnaligned(fromByteOffset: o, as: UInt32.self).littleEndian
                        v = abs(Double(Float(bitPattern: bits))); if !v.isFinite { v = 0 }
                    }
                    if v > 0 { any = true; if v > s.peak { s.peak = v } }
                }
                if any { s.nonzeroFrames += 1 }
            }
        }
        s.dataBytes += UInt64(frames * wavBpf); s.frames += UInt64(frames); s.packets += 1
        streams[id] = s
        updateHeader(id, force: false)
    }

    /// Decode the monitored jack's pair (S16, or float for the PHONES stream) and hand it to the CoreAudio path. A guest
    /// drop leaves a gap in the absolute frame index: short gaps are bridged with silence to keep the timeline, long
    /// ones flush the buffer.
    func feedOutput(_ output: AudioOutput, _ s: StreamState, jack: OutputJack, payload: Data, frames: Int, index: UInt64, gap: UInt64) {
        guard s.format == 2 || s.format == 14 else { return }   // S16 (AK4458 master) or float (the AK4452 playback)
        let pair = jack.source
        let li = pair.left - s.firstChannel, ri = pair.right - s.firstChannel
        guard li >= 0, ri >= 0, li < s.channels, ri < s.channels else { return }
        var left = [Float](repeating: 0, count: frames), right = left
        let width = s.format == 2 ? 2 : 4
        payload.withUnsafeBytes { raw in
            func sample(_ offset: Int) -> Float {
                if s.format == 2 { return Float(raw.loadUnaligned(fromByteOffset: offset, as: Int16.self).littleEndian) / 32768 }
                let value = Float(bitPattern: raw.loadUnaligned(fromByteOffset: offset, as: UInt32.self).littleEndian)
                return value.isFinite ? value : 0
            }
            for f in 0..<frames {
                let base = (f * s.channels) * width
                left[f] = sample(base + li * width)
                right[f] = sample(base + ri * width)
            }
        }
        if gap > 0 {
            if gap > UInt64(s.rate / 10) { output.flush() }
            else { output.push(left: nil, right: nil, count: Int(gap), rate: Double(s.rate), writeNs: AudioSink.nowNs()) }
        }
        left.withUnsafeBufferPointer { l in right.withUnsafeBufferPointer { r in
            output.push(left: l.baseAddress, right: r.baseAddress, count: frames, rate: Double(s.rate), writeNs: pendingWriteNs)
        } }
    }

    // MARK: Metrics

    func snapshot() -> [String: Any] {
        lock.lock(); defer { lock.unlock() }
        var out: [String: Any] = [:]
        for (id, s) in streams {
            out[AudioSink.streamNames[id] ?? "stream\(id)"] = [
                "format": AudioSink.formatName(s.format), "channels": s.wavChannels, "first_channel": s.firstChannel + s.wavFirst,
                "transport_channels": s.channels, "transport_first_channel": s.firstChannel,
                "total_channels": s.totalChannels, "rate": s.rate, "frames": s.frames, "packets": s.packets,
                "gap_frames": s.gapFrames, "overlap_frames": s.overlapFrames, "guest_drops": s.guestDrops,
                "peak": s.peak, "nonzero_frames": s.nonzeroFrames, "wav": writeFiles ? s.path : ""] as [String: Any]
        }
        var clock: [String: Any] = [:]
        if minRTT.isFinite { clock["min_rtt_ms"] = minRTT * 1000 }
        if let offset = offsetNs { clock["offset_ns"] = offset }
        var result: [String: Any] = ["mode": output == nil ? (writeFiles ? "file" : "counters") : (writeFiles ? "both" : "coreaudio"), "connected": connected, "packets": packets, "bytes": bytes, "seq_gaps": seqGaps,
                "bad_packets": badPackets, "clock": clock, "latency_ms": latency.summary(), "late_over_20ms": late20,
                "late_log": lateLog, "streams": out]
        if let output = output { result["output"] = output.snapshot() }
        return result
    }
}
