import Foundation
import XCTest

/// A real local HTTP server; no URLProtocol interception or transport mock.
/// Python's standard library is available on the macOS/Linux core CI runners.
final class LoopbackHTTPFixture {
    let origin: String
    private let process: Process
    private let output: Pipe

    init() throws {
        process = Process()
        output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        let script = try XCTUnwrap(Bundle.module.url(forResource: "http_fixture", withExtension: "py", subdirectory: "Fixtures"))
        process.arguments = ["python3", "-u", script.path]
        process.standardOutput = output
        process.standardError = FileHandle.standardError
        try process.run()
        var line = Data()
        while line.count < 128 {
            let byte = output.fileHandleForReading.readData(ofLength: 1)
            if byte.isEmpty || byte == Data([10]) { break }
            line.append(byte)
        }
        guard let address = String(data: line, encoding: .utf8), address.hasPrefix("http://127.0.0.1:") else {
            if process.isRunning { process.terminate() }
            process.waitUntilExit()
            throw NSError(domain: "LoopbackHTTPFixture", code: 1)
        }
        origin = address
    }

    deinit {
        if process.isRunning { process.terminate() }
        process.waitUntilExit()
        try? output.fileHandleForReading.close()
    }
}
