import Foundation

/// URLSession-backed transport — the production ``HTTPClient``.
///
/// Redirect policy is per-SESSION in URLSession, not per-request, so this
/// client holds two sessions: one that follows redirects and one whose
/// delegate swallows them (the login handshake needs the raw 302 +
/// Set-Cookie). Timeouts mirror the Android client: 10s connect, 40s overall
/// (the supervisor's read budget).
public final class URLSessionHTTPClient: HTTPClient, @unchecked Sendable {

    private let following: URLSession
    private let notFollowing: URLSession

    public init(timeoutSeconds: TimeInterval = 40) {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeoutSeconds
        config.timeoutIntervalForResource = timeoutSeconds
        // The WebView jar is the cookie authority; the transport must not
        // keep its own store or a stale transport cookie would ride along.
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        self.following = URLSession(configuration: config)
        self.notFollowing = URLSession(
            configuration: config,
            delegate: NoRedirectDelegate(),
            delegateQueue: nil
        )
    }

    public func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        guard let url = URL(string: request.url) else {
            throw URLError(.badURL)
        }
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = request.method
        for (name, value) in request.headers {
            urlRequest.setValue(value, forHTTPHeaderField: name)
        }
        if let body = request.body {
            switch body {
            case .form(let pairs):
                let encoded = pairs
                    .map { "\(formEscape($0.0))=\(formEscape($0.1))" }
                    .joined(separator: "&")
                urlRequest.httpBody = Data(encoded.utf8)
                urlRequest.setValue(
                    "application/x-www-form-urlencoded",
                    forHTTPHeaderField: "Content-Type"
                )
            case .json(let serialized):
                urlRequest.httpBody = Data(serialized.utf8)
                urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
            }
        }

        let session = request.followRedirects ? following : notFollowing
        let (data, response) = try await session.data(for: urlRequest)
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        var headers: [String: [String]] = [:]
        for (name, value) in http.allHeaderFields {
            guard let name = name as? String, let value = value as? String else { continue }
            headers[name.lowercased(), default: []].append(value)
        }
        let body = String(data: data, encoding: .utf8) ?? ""
        return HTTPResponse(
            status: http.statusCode,
            headers: headers,
            body: body,
            finalUrl: http.url ?? url
        )
    }

    private func formEscape(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? value
    }
}

/// Swallows redirects so the caller sees the raw 3xx (and its Set-Cookie).
private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}
