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
    private var hook: (@Sendable (HTTPRequest) async -> Void)?
    func onSend(_ hook: @escaping @Sendable (HTTPRequest) async -> Void) { self.hook = hook }

    func enqueue(_ response: HTTPResponse) { queue.append(.success(response)) }
    func enqueueError(_ error: Error) { queue.append(.failure(error)) }

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        requests.append(request)
        if let hook { await hook(request) }
        guard !queue.isEmpty else { throw FakeError.unexpectedRequest(request.url) }
        return try queue.removeFirst().get()
    }
}

actor FakeCookieSink: CookieSink {
    private(set) var injected: [(origin: String, cookies: [TofuCookie])] = []
    private(set) var purgedHosts: [String] = []
    private(set) var purgedOrigins: [String] = []
    private var entries: [(url: URL, cookie: TofuCookie)] = []
    private var purgeHook: (@Sendable () async -> Void)?
    func onPurge(_ hook: @escaping @Sendable () async -> Void) { purgeHook = hook }

    func inject(origin: String, cookies: [TofuCookie]) async {
        injected.append((origin, cookies))
        guard let url = URL(string: origin) else { return }
        for cookie in cookies {
            entries.removeAll { $0.cookie.name == cookie.name && $0.cookie.path == cookie.path &&
                ($0.cookie.domain ?? $0.url.host) == (cookie.domain ?? url.host) }
            entries.append((url, cookie))
        }
    }

    func purgeOrigin(_ origin: String, appURL: String) async {
        guard let url = URL(string: appURL), let host = url.host else { return }
        purgedHosts.append(host); purgedOrigins.append(origin)
        if let purgeHook { await purgeHook() }
        entries.removeAll { CookieScope.belongsToProfile(domain: $0.cookie.domain ?? $0.url.host ?? "", path: $0.cookie.path, appURL: url) }
    }
    func cookieHeader(_ target: String) async -> String? {
        guard let url = URL(string: target) else { return nil }
        let matching = entries.filter { entry in
            let c = entry.cookie
            return CookieScope.matches(domain: c.domain ?? entry.url.host ?? "", hostOnly: c.domain == nil,
                path: c.path, secure: c.secure, expires: c.expiresAtMs.map { Date(timeIntervalSince1970: Double($0) / 1000) }, url: url)
        }.sorted { $0.cookie.path.count > $1.cookie.path.count }
        return matching.isEmpty ? nil : matching.map { "\($0.cookie.name)=\($0.cookie.value)" }.joined(separator: "; ")
    }
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

    func applyProfileEdit(
        from oldAlias: String, to newAlias: String,
        expectedOldSecret: String?, expectedNewSecret: String?, secret: String?
    ) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard map[oldAlias] == expectedOldSecret, map[newAlias] == expectedNewSecret else { return false }
        if let secret { map[newAlias] = secret }
        if oldAlias != newAlias, expectedOldSecret != nil {
            guard let secret, map[newAlias] == secret else { return false }
            map[oldAlias] = nil
        }
        return true
    }
}

actor FakeProfileStore: ProfileStore {
    private var rows: [Int64: Profile] = [:]
    private var nextId: Int64 = 1
    private(set) var cookieHostWrites: [(id: Int64, host: String?)] = []
    private(set) var touchCalls: [(id: Int64, at: Int64)] = []

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

    func update(_ profile: Profile) async {
        guard rows[profile.id] != nil else { return }
        rows[profile.id] = profile
    }
    func deleteById(_ id: Int64) async { rows[id] = nil }

    func touchLastUsed(_ id: Int64, _ at: Int64) async {
        touchCalls.append((id, at))
        rows[id]?.lastUsedAt = at
    }

    func setCookieHostIfCurrent(_ profile: Profile, _ host: String) async -> Bool {
        guard let current = rows[profile.id], current.baseUrl == profile.baseUrl,
              current.alias == profile.alias, current.authType == profile.authType else { return false }
        if current.cookieHost != host { cookieHostWrites.append((profile.id, host)) }
        rows[profile.id]?.cookieHost = host
        return true
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
