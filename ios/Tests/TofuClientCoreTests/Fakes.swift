import Foundation
import XCTest
@testable import TofuClientCore

enum FakeError: Error, Equatable {
    case transport(String)
    case unexpectedRequest(String)
}

func makeResponse(
    _ status: Int,
    body: String = "",
    url: String = "https://h.example/login",
    headers: [String: [String]] = [:]
) -> HTTPResponse {
    HTTPResponse(status: status, headers: headers, body: body, finalUrl: URL(string: url)!)
}

/// Scripted transport: FIFO of responses/errors, records every request.
actor FakeHTTPClient: HTTPClient {
    private var queue: [Result<HTTPResponse, Error>] = []
    private(set) var requests: [HTTPRequest] = []

    func enqueue(_ response: HTTPResponse) { queue.append(.success(response)) }
    func enqueueError(_ error: Error) { queue.append(.failure(error)) }

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        requests.append(request)
        guard !queue.isEmpty else { throw FakeError.unexpectedRequest(request.url) }
        return try queue.removeFirst().get()
    }
}

actor FakeCookieSink: CookieSink {
    private(set) var injected: [(origin: String, cookies: [TofuCookie])] = []
    private(set) var purgedHosts: [String] = []
    /// origin → Cookie: header value.
    var jar: [String: String] = [:]

    func inject(origin: String, cookies: [TofuCookie]) async {
        injected.append((origin, cookies))
        jar[origin] = cookies.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")
    }

    func purgeHost(_ host: String) async {
        purgedHosts.append(host)
        jar["https://\(host)"] = nil
    }

    func cookieHeader(_ origin: String) async -> String? { jar[origin] }
}

final class FakeSecretStore: SecretStore, @unchecked Sendable {
    private var map: [String: String] = [:]
    private let lock = NSLock()

    func secretFor(_ alias: String) -> String? {
        lock.lock(); defer { lock.unlock() }
        return map[alias]
    }

    func putSecret(_ secret: String, for alias: String) {
        lock.lock(); defer { lock.unlock() }
        map[alias] = secret
    }

    func removeSecret(_ alias: String) {
        lock.lock(); defer { lock.unlock() }
        map[alias] = nil
    }
}

actor FakeProfileStore: ProfileStore {
    private var rows: [Int64: Profile] = [:]
    private var nextId: Int64 = 1
    private(set) var cookieHostWrites: [(id: Int64, host: String?)] = []
    private(set) var touchCalls: [(id: Int64, at: Int64)] = []
    private(set) var authTypeWrites: [(id: Int64, authType: AuthType)] = []

    func seed(_ profile: Profile) { rows[profile.id] = profile }

    func getAllOnce() async -> [Profile] { rows.values.sorted { $0.id < $1.id } }
    func getById(_ id: Int64) async -> Profile? { rows[id] }
    func getByAlias(_ alias: String) async -> Profile? { rows.values.first { $0.alias == alias } }

    func insert(_ profile: Profile) async -> Int64 {
        let id = nextId
        nextId += 1
        var row = profile
        row.id = id
        rows[id] = row
        return id
    }

    func update(_ profile: Profile) async { rows[profile.id] = profile }
    func deleteById(_ id: Int64) async { rows[id] = nil }

    func touchLastUsed(_ id: Int64, _ at: Int64) async {
        touchCalls.append((id, at))
        rows[id]?.lastUsedAt = at
    }

    func setAuthType(_ id: Int64, _ authType: AuthType) async {
        authTypeWrites.append((id, authType))
        rows[id]?.authType = authType
    }

    func setCookieHost(_ id: Int64, _ host: String?) async {
        cookieHostWrites.append((id, host))
        rows[id]?.cookieHost = host
    }
}

/// Records the backoff schedule instead of sleeping.
final class SleepRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var _delays: [Int64] = []
    var delays: [Int64] { lock.lock(); defer { lock.unlock() }; return _delays }

    var hook: @Sendable (Int64) async -> Void {
        { ms in self.record(ms) }
    }

    private func record(_ ms: Int64) {
        lock.lock(); defer { lock.unlock() }
        _delays.append(ms)
    }
}
