import Foundation

/// Native code-server handshake. Every redirect is inspected before fetching;
/// only LoginForm's recognized same-origin form can receive a saved password.
struct GatewayLogin: Sendable {
    enum Discovery {
        case form(LoginForm.PasswordForm)
        case unrecognized
        case interactive
        case warming(Int)
        case rateLimited
    }
    enum Submission {
        case accepted
        case rejected
        case interactive
        case warming(Int)
        case rateLimited
    }
    let transport: GatewayHTTP
    private static let redirects: Set<Int> = [301, 302, 303, 307, 308]
    private static let maximumRedirects = 8

    func discover(_ server: ServerUrl) async throws -> Discovery {
        var target = server.components.url!
        for hop in 0...Self.maximumRedirects {
            let response = try await transport.send(target.absoluteString)
            _ = try await transport.receiveCookies(response, url: target)
            if LoginRetryPolicy.isWarmingStatus(response.status) { return .warming(response.status) }
            if response.status == 429 { return .rateLimited }
            if Self.redirects.contains(response.status) {
                guard hop < Self.maximumRedirects,
                      let location = response.header("location"),
                      let next = URL(string: location, relativeTo: target)?.absoluteURL,
                      server.sameOrigin(next) else { return .interactive }
                target = next
            } else {
                if response.status == 200, let form = LoginForm.discover(response.body, pageUrl: target, server: server) { return .form(form) }
                return .unrecognized
            }
        }
        return .interactive
    }

    func submit(_ form: LoginForm.PasswordForm, secret: String, server: ServerUrl) async throws -> Submission {
        let fields = form.fields.sorted { $0.key < $1.key }.map { ($0.key, $0.value) } + [("password", secret)]
        let response = try await transport.send(form.target.absoluteString, method: "POST", body: .form(fields))
        if LoginRetryPolicy.isWarmingStatus(response.status) { return .warming(response.status) }
        if response.status == 429 { return .rateLimited }
        let issued = try await transport.receiveCookies(response, url: form.target)
        if let location = response.header("location") {
            guard let next = URL(string: location, relativeTo: form.target)?.absoluteURL,
                  server.sameOrigin(next) else { return .interactive }
        }
        if [200, 204, 302, 303].contains(response.status), issued.contains(where: {
            $0.name == SessionManager.sessionCookieName && !$0.value.isEmpty && ($0.expiresAtMs ?? Int64.max) > currentTimeMs()
        }) { return .accepted }
        if response.status == 200, LoginForm.discover(response.body, pageUrl: form.target, server: server) != nil { return .rejected }
        return .interactive
    }
}
