import Foundation

/// Transport request in a Foundation-only shape, so the session/supervisor
/// logic is testable without URLSession. The production client (app target)
/// wraps URLSession — with a no-redirect delegate when ``followRedirects`` is
/// false, because the login handshake needs the raw 302 + Set-Cookie.
public struct HTTPRequest: Sendable {
    public enum Body: Sendable {
        /// application/x-www-form-urlencoded, order-stable.
        case form([(String, String)])
        /// application/json, pre-serialised.
        case json(String)
    }

    public var url: String
    public var method: String
    public var headers: [String: String]
    public var body: Body?
    /// false = expose the raw 3xx (login handshake); true = resolve the
    /// redirect chain before the caller parses the body (login-form discovery,
    /// meta preflight).
    public var followRedirects: Bool

    public init(
        url: String,
        method: String = "GET",
        headers: [String: String] = [:],
        body: Body? = nil,
        followRedirects: Bool = true
    ) {
        self.url = url
        self.method = method
        self.headers = headers
        self.body = body
        self.followRedirects = followRedirects
    }
}

public struct HTTPResponse: Sendable, Equatable {
    public let status: Int
    /// Lowercased header names → ALL values (Set-Cookie repeats).
    public let headers: [String: [String]]
    public let body: String
    /// URL after any redirects (== request URL when followRedirects == false).
    public let finalUrl: URL

    public init(status: Int, headers: [String: [String]], body: String, finalUrl: URL) {
        self.status = status
        self.headers = headers
        self.body = body
        self.finalUrl = finalUrl
    }

    public func header(_ name: String) -> String? {
        headers[name.lowercased()]?.first
    }

    public func headerValues(_ name: String) -> [String] {
        headers[name.lowercased()] ?? []
    }
}

/// The transport seam. Blocking-callable from async context; the production
/// impl applies the Android timeouts (connect 10s / read 40s for supervisor).
public protocol HTTPClient: Sendable {
    func send(_ request: HTTPRequest) async throws -> HTTPResponse
}
