// system-audio-capture — streams computer audio (what the Mac is playing)
// as s16le 16 kHz mono PCM to stdout, using ScreenCaptureKit.
//
// Requires macOS 13+ and the Screen & System Audio Recording permission for
// the invoking process (System Settings → Privacy & Security). Errors are
// reported on stderr with a non-zero exit status; the backend surfaces them
// to the UI as a clear unavailable/device-lost state.
//
// Build: swiftc -O -framework ScreenCaptureKit -framework CoreMedia \
//        -framework AVFoundation main.swift -o system-audio-capture

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(("system-audio-capture: " + message + "\n").data(using: .utf8)!)
    exit(code)
}

@available(macOS 13.0, *)
final class AudioStreamer: NSObject, SCStreamOutput, SCStreamDelegate {
    private let stdout = FileHandle.standardOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        var ablPointer: UnsafeMutablePointer<AudioBufferList>?
        var blockBuffer: CMBlockBuffer?
        var ablSize = 0
        // Query required size first, then fetch the audio buffer list.
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer, bufferListSizeNeededOut: &ablSize, bufferListOut: nil,
            bufferListSize: 0, blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: 0, blockBufferOut: nil)
        let ablMemory = UnsafeMutableRawPointer.allocate(byteCount: ablSize, alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { ablMemory.deallocate() }
        ablPointer = ablMemory.assumingMemoryBound(to: AudioBufferList.self)
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer, bufferListSizeNeededOut: nil, bufferListOut: ablPointer!,
            bufferListSize: ablSize, blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0,
            blockBufferOut: &blockBuffer)
        guard status == noErr else { return }

        let abl = UnsafeMutableAudioBufferListPointer(ablPointer!)
        guard let first = abl.first, let data = first.mData else { return }
        // The stream is configured for mono Float32 at 16 kHz, so the first
        // buffer holds everything we need.
        let sampleCount = Int(first.mDataByteSize) / MemoryLayout<Float32>.size
        let floats = data.assumingMemoryBound(to: Float32.self)
        var pcm = [Int16](repeating: 0, count: sampleCount)
        for i in 0..<sampleCount {
            let v = max(-1.0, min(1.0, floats[i]))
            pcm[i] = Int16(v * 32767.0)
        }
        pcm.withUnsafeBufferPointer { buf in
            stdout.write(Data(buffer: buf))
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("capture stream stopped: \(error.localizedDescription)", code: 3)
    }
}

guard #available(macOS 13.0, *) else {
    fail("requires macOS 13 or newer (ScreenCaptureKit audio capture)", code: 2)
}

if #available(macOS 13.0, *) {
    let streamer = AudioStreamer()
    var streamRef: SCStream?

    let sem = DispatchSemaphore(value: 0)
    Task {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else {
                fail("no display available for the capture filter", code: 2)
            }
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let cfg = SCStreamConfiguration()
            cfg.capturesAudio = true
            cfg.excludesCurrentProcessAudio = true
            cfg.sampleRate = 16000
            cfg.channelCount = 1
            // Minimal video output; only the audio stream is consumed.
            cfg.width = 2
            cfg.height = 2
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            let stream = SCStream(filter: filter, configuration: cfg, delegate: streamer)
            try stream.addStreamOutput(streamer, type: .audio,
                                       sampleHandlerQueue: DispatchQueue(label: "audio"))
            try await stream.startCapture()
            streamRef = stream
            FileHandle.standardError.write("capturing\n".data(using: .utf8)!)
        } catch {
            fail("cannot start capture (is Screen & System Audio Recording "
                 + "permission granted?): \(error.localizedDescription)", code: 2)
        }
        sem.signal()
    }
    sem.wait()
    _ = streamRef  // retained for the lifetime of the process

    let stop: @convention(c) (Int32) -> Void = { _ in
        exit(0)
    }
    signal(SIGINT, stop)
    signal(SIGTERM, stop)
    dispatchMain()
}
