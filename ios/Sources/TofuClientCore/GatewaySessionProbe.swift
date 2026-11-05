import Foundation

/// Proves access through the outer gate using the protected, owner-scoped API.
/// Public health/meta and arbitrary JSON never grant authenticated status.
struct GatewaySessionProbe: Sendable {
    let transport: GatewayHTTP

    static func url(_ server: ServerUrl) -> String { server.endpoint("api/v4/conversations") + "?limit=1&meta=1" }

    func verify(_ profile: Profile) async throws -> Bool {
        guard let server = ServerUrl.parse(profile.baseUrl) else { return false }
        do {
            if Self.provesTofu(try await transport.send(Self.url(server))) { return true }
        } catch {
            if GatewayHTTP.isCancellation(error) { throw CancellationError() }
        }
        guard let project = profile.projectPath, !project.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let supervisor = SupervisorUrl.fromServerUrl(profile.baseUrl) else { return false }
        do {
            let response = try await transport.send(SupervisorUrl.endpoint(supervisor, SupervisorUrl.status, projectPathForQuery: project))
            guard response.status == 200, Self.isJSON(response),
                  let status = try? JSONDecoder().decode(SupervisorStatus.self, from: Data(response.body.utf8)) else { return false }
            return status.ok && status.supervisorProtocolVersion > 0 && !status.supervisorSourceFingerprint.isEmpty
        } catch {
            if GatewayHTTP.isCancellation(error) { throw CancellationError() }
            return false
        }
    }

    static func provesTofu(_ response: HTTPResponse) -> Bool {
        guard isJSON(response) else { return false }
        let data = Data(response.body.utf8)
        if response.status == 200, let page = try? JSONDecoder().decode(MetadataEnvelope.self, from: data) {
            return !page.requestId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        if [401, 403].contains(response.status), let problem = try? JSONDecoder().decode(AuthProblem.self, from: data) {
            return problem.status == response.status && problem.instance == "/api/v4/conversations" &&
                problem.type.hasPrefix("urn:tofu:problem:") && !problem.requestId.isEmpty
        }
        return false
    }

    private static func isJSON(_ response: HTTPResponse) -> Bool {
        let type = response.header("content-type")?.split(separator: ";").first?.trimmingCharacters(in: .whitespaces).lowercased() ?? ""
        return type == "application/json" || type.hasSuffix("+json")
    }

    private struct MetadataEnvelope: Decodable {
        let requestId: String
        let data: Page
        let meta: Clock
        struct Page: Decodable { let items: [IgnoredValue]; let page: Object }
        struct Clock: Decodable { let serverTimeMs: Int64 }
        struct IgnoredValue: Decodable { init(from decoder: Decoder) throws {} }
        struct Object: Decodable {
            private enum Keys: String, CodingKey { case unused }
            init(from decoder: Decoder) throws { _ = try decoder.container(keyedBy: Keys.self) }
        }
    }
    private struct AuthProblem: Decodable {
        let type: String; let instance: String; let status: Int; let requestId: String
    }
    private struct SupervisorStatus: Decodable {
        let ok: Bool; let running: Bool
        let supervisorProtocolVersion: Int; let supervisorSourceFingerprint: String
    }
}
