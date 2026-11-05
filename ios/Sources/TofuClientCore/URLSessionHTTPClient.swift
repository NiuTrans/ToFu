import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Pooled URLSession transport. HTTPExchangeDelegate owns response lifetimes;
/// gateway policy chooses redirects and body bounds per request, not per pool.
public final class URLSessionHTTPClient: HTTPClient, @unchecked Sendable {
    private let session: URLSession
    private let exchanges: HTTPExchangeDelegate

    public init(timeoutSeconds: TimeInterval = 40) {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeoutSeconds
        config.timeoutIntervalForResource = timeoutSeconds
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        exchanges = HTTPExchangeDelegate()
        session = URLSession(configuration: config, delegate: exchanges, delegateQueue: nil)
    }

    deinit { session.invalidateAndCancel() }

    public func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        guard let server = ServerUrl.parse(request.url), let url = server.components.url else { throw URLError(.badURL) }
        var wire = URLRequest(url: url)
        wire.httpMethod = request.method
        for (name, value) in request.headers { wire.setValue(value, forHTTPHeaderField: name) }
        switch request.body {
        case .form(let pairs):
            wire.httpBody = Data(FormEncoding.encode(pairs).utf8)
            wire.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        case .json(let serialized):
            wire.httpBody = Data(serialized.utf8)
            wire.setValue("application/json", forHTTPHeaderField: "Content-Type")
        case nil: break
        }
        let cancellation = HTTPRequestCancellation()
        return try await withTaskCancellationHandler {
            try Task.checkCancellation()
            let response: HTTPResponse = try await withCheckedThrowingContinuation { continuation in
                let task = session.dataTask(with: wire)
                exchanges.register(task: task, request: request, continuation: continuation)
                cancellation.start(task)
            }
            try Task.checkCancellation()
            return response
        } onCancel: {
            cancellation.cancel()
        }
    }
}

/// Closes the race between cancellation and URLSession task creation.
private final class HTTPRequestCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var task: URLSessionTask?

    func start(_ task: URLSessionTask) {
        lock.lock()
        self.task = task
        let shouldCancel = cancelled
        lock.unlock()
        if shouldCancel { task.cancel() } else { task.resume() }
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let current = task
        lock.unlock()
        current?.cancel()
    }
}
