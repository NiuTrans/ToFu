import XCTest
@testable import TofuClientCore

final class SessionControllerTests: XCTestCase {

    private struct Rig {
        let http = FakeHTTPClient()
        let cookies = FakeCookieSink()
        let store = FakeProfileStore()
        let secrets = FakeSecretStore()
        let controller: SessionController

        init(clock: @escaping @Sendable () -> Int64 = { 42 }) {
            let manager = SessionManager(
                store: store, secrets: secrets, cookies: cookies, http: http
            )
            controller = SessionController(
                store: store, secrets: secrets, session: manager, clock: clock
            )
        }

        /// GET /login 404 (form fallback) → POST 302 + session cookie → meta 404.
        func scriptSuccessfulLogin(host: String = "h.example") async {
            await http.enqueue(makeResponse(404, url: "https://\(host)/login"))
            await http.enqueue(makeResponse(302, url: "https://\(host)/login", headers: [
                "set-cookie": ["code-server-session=abc; Path=/"],
            ]))
            await http.enqueue(makeResponse(404))
        }
    }

    func test_add_duplicate_alias_short_circuits() async {
        let rig = Rig()
        await rig.store.seed(Profile(id: 1, alias: "dup", baseUrl: "https://h.example/proxy/15000/"))
        let result = await rig.controller.addProfile(
            alias: "dup", baseUrl: "https://h.example/proxy/15000/",
            authType: .codeServerPassword, secret: "pw"
        )
        guard case .duplicateAlias = result else { return XCTFail("expected duplicateAlias") }
        XCTAssertEqual(await rig.http.requests.count, 0)
    }

    /// code-server auth is per-HOST: a blank password field reuses the secret
    /// already stored for another profile on the same host.
    func test_add_blank_secret_reuses_same_host_password() async {
        let rig = Rig()
        await rig.store.seed(Profile(id: 9, alias: "old", baseUrl: "https://h.example/proxy/15000/",
                                     authType: .codeServerPassword))
        rig.secrets.putSecret("shared", for: "old")
        await rig.scriptSuccessfulLogin()

        let result = await rig.controller.addProfile(
            alias: "new", baseUrl: "https://h.example/proxy/15001/",
            authType: .codeServerPassword, secret: ""
        )
        guard case .added(let saved, let login) = result else { return XCTFail("expected added") }
        guard case .success = login else { return XCTFail("expected success, got \(login)") }
        XCTAssertEqual(saved.alias, "new")
        XCTAssertEqual(rig.secrets.secretFor("new"), "shared")
        let requests = await rig.http.requests
        guard case .form(let fields)? = requests[1].body else { return XCTFail("expected form") }
        XCTAssertTrue(fields.contains { $0.0 == "password" && $0.1 == "shared" })
    }

    func test_add_stores_secret_before_login() async {
        let rig = Rig()
        await rig.scriptSuccessfulLogin()
        let result = await rig.controller.addProfile(
            alias: "a", baseUrl: "https://h.example/proxy/15000/",
            authType: .codeServerPassword, secret: "pw"
        )
        guard case .added = result else { return XCTFail("expected added") }
        XCTAssertEqual(rig.secrets.secretFor("a"), "pw")
        // The login consumed the stored credential (form discovery + POST).
        XCTAssertEqual(await rig.http.requests.count, 3)
    }

    /// The secret is alias-keyed: a rename must MOVE it or login silently
    /// loses the credential.
    func test_edit_rename_moves_the_secret_key() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        let current = Profile(id: 5, alias: "a", baseUrl: "https://h.example/proxy/15000/",
                              authType: .codeServerPassword, lastUsedAt: 9)
        await rig.scriptSuccessfulLogin()

        let result = await rig.controller.editProfile(
            current: current, newAlias: "b", newUrl: current.baseUrl,
            newAuthType: .codeServerPassword, newSecret: ""
        )
        XCTAssertEqual(rig.secrets.secretFor("a"), nil)
        XCTAssertEqual(rig.secrets.secretFor("b"), "pw")
        XCTAssertEqual(result.persisted.alias, "b")
        XCTAssertEqual(result.persisted.id, 5)
        guard case .success = result.login else { return XCTFail("got \(result.login)") }
    }

    /// Host change delegates to the purge-and-relogin path, which owns what
    /// the persisted row looks like (cookieHost cleared, then re-stamped).
    func test_edit_host_change_purges_and_relogs() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        let current = Profile(id: 6, alias: "a", baseUrl: "https://old.example/proxy/15000/",
                              authType: .codeServerPassword, cookieHost: "old.example")
        await rig.store.seed(current)
        await rig.scriptSuccessfulLogin(host: "new.example")

        let result = await rig.controller.editProfile(
            current: current, newAlias: "a", newUrl: "https://new.example/proxy/15000/",
            newAuthType: .codeServerPassword, newSecret: ""
        )
        XCTAssertEqual(await rig.cookies.purgedHosts, ["old.example"])
        XCTAssertNil(result.persisted.cookieHost)
        XCTAssertEqual(result.persisted.baseUrl, "https://new.example/proxy/15000/")
        XCTAssertEqual(await rig.store.getById(6)?.cookieHost, "new.example")
    }

    /// The UI's list-rendered row may lag the store: activate must bump
    /// recency with a targeted write and RE-READ the row before login.
    func test_activate_rereads_the_store_row() async {
        let rig = Rig(clock: { 77 })
        await rig.store.seed(Profile(id: 9, alias: "a", baseUrl: "https://h.example:15000",
                                     authType: .none, lastUsedAt: 1))
        // A stale, list-rendered copy: authType none either way, but the
        // persisted result must come from the store (fresh lastUsedAt).
        let stale = Profile(id: 9, alias: "a", baseUrl: "https://h.example:15000",
                            authType: .none, cookieHost: "junk", lastUsedAt: 0)
        let result = await rig.controller.activate(stale)
        XCTAssertEqual(await rig.store.touchCalls.map(\.at), [77])
        XCTAssertEqual(result.persisted.lastUsedAt, 77)
        XCTAssertNil(result.persisted.cookieHost)
        guard case .success = result.login else { return XCTFail("got \(result.login)") }
        XCTAssertEqual(await rig.http.requests.count, 0)   // NONE is zero-request
    }

    func test_migrate_fixes_only_proxy_urls_with_stale_none_auth() async {
        let rig = Rig()
        await rig.store.seed(Profile(id: 1, alias: "p1", baseUrl: "https://h.example/proxy/15000/",
                                     authType: .none))
        await rig.store.seed(Profile(id: 2, alias: "p2", baseUrl: "https://h.example/proxy/15000/",
                                     authType: .codeServerPassword))
        await rig.store.seed(Profile(id: 3, alias: "p3", baseUrl: "https://h.example:15000",
                                     authType: .none))
        let fixed = await rig.controller.migrateProxyAuthDefaults()
        XCTAssertEqual(fixed, 1)
        let writes = await rig.store.authTypeWrites
        XCTAssertEqual(writes.count, 1)
        XCTAssertEqual(writes[0].id, 1)
        XCTAssertEqual(writes[0].authType, .codeServerPassword)
        // Idempotent: a second pass fixes nothing.
        XCTAssertEqual(await rig.controller.migrateProxyAuthDefaults(), 0)
    }

    func test_delete_removes_secret_and_row() async {
        let rig = Rig()
        let p = Profile(id: 4, alias: "a", baseUrl: "https://h.example/proxy/15000/")
        await rig.store.seed(p)
        rig.secrets.putSecret("pw", for: "a")
        await rig.controller.deleteProfile(p)
        XCTAssertNil(rig.secrets.secretFor("a"))
        XCTAssertNil(await rig.store.getById(4))
    }
}
