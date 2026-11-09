import Foundation

/// Review metadata only: no password, supervisor project or implicit sign-in.
/// Drafts are created only by the credential-free connection parser.
public struct ConnectionDraft: Equatable, Sendable {
    public let alias: String
    public let baseUrl: String
    public let authType: AuthType
}

/// Inbound tofu://connect?url=<encoded-http(s)-url>&name=<optional-name> and
/// plain URL paste share the saved-URL admission boundary. No writes or I/O.
public enum ConnectionOnboarding {
    /// Connection-link protocol bounds, not retained queues or operation knobs.
    public static let maximumLinkBytes = 8192
    public static let maximumNameCharacters = 80

    public static func parse(_ input: String) -> ConnectionDraft? {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.utf8.count <= maximumLinkBytes, !ServerUrl.hasControls(text),
              let parts = URLComponents(string: text) else { return nil }
        var target = text
        var alias = ""
        if parts.scheme?.lowercased() == "tofu" {
            guard parts.host?.lowercased() == "connect", parts.user == nil, parts.password == nil,
                  parts.port == nil, parts.path.isEmpty || parts.path == "/", parts.fragment == nil,
                  let items = parts.queryItems, Set(items.map(\.name)).count == items.count,
                  items.allSatisfy({ $0.name == "url" || $0.name == "name" }),
                  let url = items.first(where: { $0.name == "url" })?.value else { return nil }
            target = url
            alias = (items.first(where: { $0.name == "name" })?.value ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard alias.count <= maximumNameCharacters, !ServerUrl.hasControls(alias),
              let server = ServerUrl.parse(target) else { return nil }
        return ConnectionDraft(alias: alias, baseUrl: server.raw, authType: ServerUrl.defaultAuthType(server.raw))
    }
}

/// An open form, including an untouched edit, can only be replaced explicitly.
/// Retains one pending metadata suggestion, never local fields or a password.
public struct ConnectionReviewState: Equatable, Sendable {
    public private(set) var accepted: ConnectionDraft?
    public private(set) var pending: ConnectionDraft?

    public init() {}

    @discardableResult
    public mutating func receive(_ draft: ConnectionDraft, formIsOpen: Bool) -> Bool {
        if formIsOpen {
            pending = draft
            return false
        }
        accepted = draft
        pending = nil
        return true
    }

    @discardableResult
    public mutating func usePending() -> Bool {
        guard let pending else { return false }
        accepted = pending
        self.pending = nil
        return true
    }

    public mutating func keepForm() { pending = nil }

    public mutating func reset() {
        accepted = nil
        pending = nil
    }
}

/// UI intent fencing only, not a second session engine. The app owns/cancels
/// its one Task and checks this generation after each suspension. Login stamps
/// are not configuration edits; a missing/replaced/edited row cannot complete.
public struct ProfileOperationGeneration: Sendable {
    public private(set) var current: UInt64 = 0

    public init() {}

    @discardableResult
    public mutating func advance() -> UInt64 {
        current &+= 1
        return current
    }

    public func isCurrent(_ generation: UInt64, isCancelled: Bool) -> Bool {
        !isCancelled && generation == current
    }

    public func canComplete(_ generation: UInt64, isCancelled: Bool, expected: Profile, stored: Profile?) -> Bool {
        guard isCurrent(generation, isCancelled: isCancelled), let stored else { return false }
        return expected.id == stored.id && expected.alias == stored.alias && expected.baseUrl == stored.baseUrl &&
            expected.authType == stored.authType && expected.projectPath == stored.projectPath
    }
}
