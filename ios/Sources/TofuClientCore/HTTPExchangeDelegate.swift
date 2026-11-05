import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// URLSession callback owner. The registry lock covers creation/completion;
/// response buffers are accessed only by URLSession's serial delegate queue.
/// Cancellation always reaches didComplete, which removes and resumes once.
final class HTTPExchangeDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private final class Exchange {
        let request: HTTPRequest
        let continuation: CheckedContinuation<HTTPResponse, Error>
        var response: HTTPURLResponse?
        var data = Data()
        var failure: Error?
        init(request: HTTPRequest, continuation: CheckedContinuation<HTTPResponse, Error>) {
            self.request = request; self.continuation = continuation
        }
    }
    private let lock = NSLock()
    private var active: [Int: Exchange] = [:]

    func register(task: URLSessionTask, request: HTTPRequest, continuation: CheckedContinuation<HTTPResponse, Error>) {
        lock.lock(); defer { lock.unlock() }
        active[task.taskIdentifier] = Exchange(request: request, continuation: continuation)
    }

    private func exchange(_ task: URLSessionTask) -> Exchange? {
        lock.lock(); defer { lock.unlock() }
        return active[task.taskIdentifier]
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(exchange(task)?.request.followRedirects == true ? request : nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let state = exchange(dataTask), let response = response as? HTTPURLResponse else {
            completionHandler(.cancel); return
        }
        state.response = response
        if let limit = state.request.maximumResponseBytes, response.expectedContentLength > limit || limit < 0 {
            state.failure = URLError(.dataLengthExceedsMaximum)
            completionHandler(.cancel)
        } else { completionHandler(.allow) }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let state = exchange(dataTask), state.failure == nil else { return }
        if let limit = state.request.maximumResponseBytes, data.count > limit - state.data.count {
            state.failure = URLError(.dataLengthExceedsMaximum)
            dataTask.cancel()
        } else { state.data.append(data) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let state = active.removeValue(forKey: task.taskIdentifier)
        lock.unlock()
        guard let state else { return }
        if let error = state.failure ?? error {
            state.continuation.resume(throwing: error)
        } else if let response = state.response, let url = response.url {
            var headers: [String: [String]] = [:]
            for (name, value) in response.allHeaderFields {
                guard let name = name as? String else { continue }
                headers[name.lowercased()] = (value as? [String]) ?? [String(describing: value)]
            }
            state.continuation.resume(returning: HTTPResponse(status: response.statusCode, headers: headers,
                body: String(data: state.data, encoding: .utf8) ?? "", finalUrl: url))
        } else { state.continuation.resume(throwing: URLError(.badServerResponse)) }
    }
}
