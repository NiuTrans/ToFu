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
            await http.enqueue(makeResponse(200, body: gatewayForm(action: "/login"), url: "https://\(host)/login"))
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
        let actual1 = await rig.http.requests.count
        XCTAssertEqual(actual1, 0)
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
        let actual2 = await rig.http.requests.count
        XCTAssertEqual(actual2, 3)
    }

    /// The secret is alias-keyed: a rename must MOVE it or login silently
    /// loses the credential.
    func test_edit_rename_moves_the_secret_key() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        let current = Profile(id: 5, alias: "a", baseUrl: "https://h.example/proxy/15000/",
                              authType: .codeServerPassword, lastUsedAt: 9)
        await rig.store.seed(current)
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
        let actual3 = await rig.cookies.purgedHosts
        XCTAssertEqual(actual3, ["old.example"])
        XCTAssertNil(result.persisted.cookieHost)
        XCTAssertEqual(result.persisted.baseUrl, "https://new.example/proxy/15000/")
        let actual4 = await rig.store.getById(6)?.cookieHost
        XCTAssertEqual(actual4, "new.example")
    }

    func test_edit_cancellation_during_origin_purge_preserves_row_and_alias_secret() async throws {
        let store = try SQLiteProfileStore(path: ":memory:")
        var current = Profile(alias: "a", baseUrl: "https://old.example/proxy/15000/",
                              authType: .codeServerPassword, cookieHost: "old.example")
        current.id = store.insert(current)
        let http = FakeHTTPClient()
        let cookies = FakeCookieSink()
        let secrets = FakeSecretStore()
        secrets.putSecret("pw", for: current.alias)
        let manager = SessionManager(store: store, secrets: secrets, cookies: cookies, http: http)
        let controller = SessionController(store: store, secrets: secrets, session: manager)
        let gate = EditPersistenceGate()
        let arrived = expectation(description: "origin purge suspended before persistence")
        await cookies.onPurge { arrived.fulfill(); await gate.wait() }
        let snapshot = current
        let edit = Task {
            await controller.editProfile(current: snapshot, newAlias: "b",
                newUrl: "https://new.example/proxy/15000/", newAuthType: .codeServerPassword, newSecret: "")
        }
        await fulfillment(of: [arrived], timeout: 2)
        edit.cancel()
        await gate.release()
        let result = await edit.value
        let requests = await http.requests
        XCTAssertEqual(store.getById(current.id), current)
        XCTAssertEqual(result.persisted, current)
        XCTAssertEqual(secrets.secretFor("a"), "pw")
        XCTAssertNil(secrets.secretFor("b"))
        guard case .error = result.login else { return XCTFail("cancelled edit completed: \(result.login)") }
        XCTAssertTrue(requests.isEmpty)
    }

    func test_edit_deletion_during_nonthrowing_update_does_not_move_the_secret() async throws {
        for url in ["https://old.example/proxy/15001/", "https://new.example/proxy/15000/"] {
            let database = try SQLiteProfileStore(path: ":memory:")
            var current = Profile(alias: "a", baseUrl: "https://old.example/proxy/15000/", authType: .none)
            current.id = database.insert(current)
            let store = EditFaultStore(database: database,
                beforeUpdate: { row in database.deleteById(row.id) }, afterUpdate: { _ in })
            let http = FakeHTTPClient()
            let secrets = FakeSecretStore()
            secrets.putSecret("pw", for: "a")
            let manager = SessionManager(store: store, secrets: secrets, cookies: FakeCookieSink(), http: http)
            let controller = SessionController(store: store, secrets: secrets, session: manager)
            let result = await controller.editProfile(current: current, newAlias: "b", newUrl: url,
                newAuthType: .none, newSecret: "replacement")
            let requests = await http.requests
            XCTAssertNil(database.getById(current.id))
            XCTAssertEqual(secrets.secretFor("a"), "pw", url)
            XCTAssertNil(secrets.secretFor("b"), url)
            guard case .error = result.login else { return XCTFail("missing row completed: \(result.login)") }
            XCTAssertTrue(requests.isEmpty)
        }
    }

    func test_edit_alias_conflict_preserves_both_rows_and_credentials() async throws {
        for url in ["https://old.example/proxy/15001/", "https://new.example/proxy/15000/"] {
            let store = try SQLiteProfileStore(path: ":memory:")
            var current = Profile(alias: "a", baseUrl: "https://old.example/proxy/15000/", authType: .none)
            current.id = store.insert(current)
            var other = Profile(alias: "b", baseUrl: "https://other.example/", authType: .none)
            other.id = store.insert(other)
            let http = FakeHTTPClient()
            let secrets = FakeSecretStore()
            secrets.putSecret("pw", for: "a")
            secrets.putSecret("other-password", for: "b")
            let manager = SessionManager(store: store, secrets: secrets, cookies: FakeCookieSink(), http: http)
            let controller = SessionController(store: store, secrets: secrets, session: manager)
            let result = await controller.editProfile(current: current, newAlias: "b", newUrl: url,
                newAuthType: .none, newSecret: "replacement")
            let requests = await http.requests
            XCTAssertEqual(store.getById(current.id), current, url)
            XCTAssertEqual(store.getById(other.id), other, url)
            XCTAssertEqual(secrets.secretFor("a"), "pw", url)
            XCTAssertEqual(secrets.secretFor("b"), "other-password", url)
            guard case .error = result.login else { return XCTFail("alias conflict completed: \(result.login)") }
            XCTAssertTrue(requests.isEmpty)
        }
    }

    func test_edit_cancel_during_nonthrowing_commit_finishes_vault_reconciliation_without_login() async throws {
        for url in ["https://old.example/proxy/15001/", "https://new.example/proxy/15000/"] {
            let database = try SQLiteProfileStore(path: ":memory:")
            var current = Profile(alias: "a", baseUrl: "https://old.example/proxy/15000/", authType: .codeServerPassword)
            current.id = database.insert(current)
            let gate = EditPersistenceGate()
            let arrived = expectation(description: "nonthrowing update suspended before commit")
            let store = EditFaultStore(database: database,
                beforeUpdate: { _ in arrived.fulfill(); await gate.wait() }, afterUpdate: { _ in })
            let http = FakeHTTPClient()
            let secrets = FakeSecretStore()
            secrets.putSecret("pw", for: "a")
            let manager = SessionManager(store: store, secrets: secrets, cookies: FakeCookieSink(), http: http)
            let controller = SessionController(store: store, secrets: secrets, session: manager)
            let snapshot = current
            let edit = Task {
                await controller.editProfile(current: snapshot, newAlias: "b", newUrl: url,
                    newAuthType: .codeServerPassword, newSecret: "replacement")
            }
            await fulfillment(of: [arrived], timeout: 2)
            XCTAssertEqual(database.getById(current.id), current)
            XCTAssertEqual(secrets.secretFor("a"), "pw")
            XCTAssertNil(secrets.secretFor("b"))
            edit.cancel()
            await gate.release()
            let result = await edit.value
            let requests = await http.requests
            let saved = try XCTUnwrap(database.getById(current.id))
            XCTAssertEqual(saved.alias, "b")
            XCTAssertEqual(saved.baseUrl, url)
            XCTAssertEqual(result.persisted, saved)
            XCTAssertNil(secrets.secretFor("a"))
            XCTAssertEqual(secrets.secretFor("b"), "replacement")
            guard case .error = result.login else { return XCTFail("cancelled commit signed in: \(result.login)") }
            XCTAssertTrue(requests.isEmpty)
        }
    }

    func test_edit_commit_does_not_overwrite_concurrent_vault_changes_or_roll_back_row() async throws {
        for url in ["https://old.example/proxy/15001/", "https://new.example/proxy/15000/"] {
            for changedAlias in ["a", "b"] {
                let database = try SQLiteProfileStore(path: ":memory:")
                var current = Profile(alias: "a", baseUrl: "https://old.example/proxy/15000/", authType: .none)
                current.id = database.insert(current)
                let gate = EditPersistenceGate()
                let arrived = expectation(description: "committed update suspended before returning")
                let store = EditFaultStore(database: database, beforeUpdate: { _ in },
                    afterUpdate: { _ in arrived.fulfill(); await gate.wait() })
                let http = FakeHTTPClient()
                let secrets = FakeSecretStore()
                secrets.putSecret("pw", for: "a")
                let manager = SessionManager(store: store, secrets: secrets, cookies: FakeCookieSink(), http: http)
                let controller = SessionController(store: store, secrets: secrets, session: manager)
                let snapshot = current
                let edit = Task {
                    await controller.editProfile(current: snapshot, newAlias: "b", newUrl: url,
                        newAuthType: .none, newSecret: "replacement")
                }
                await fulfillment(of: [arrived], timeout: 2)
                secrets.putSecret("concurrent-password", for: changedAlias)
                await gate.release()
                let result = await edit.value
                let requests = await http.requests
                let saved = try XCTUnwrap(database.getById(current.id))
                XCTAssertEqual(saved.alias, "b")
                XCTAssertEqual(saved.baseUrl, url)
                XCTAssertEqual(result.persisted, saved)
                XCTAssertEqual(secrets.secretFor("a"), changedAlias == "a" ? "concurrent-password" : "pw")
                XCTAssertEqual(secrets.secretFor("b"), changedAlias == "b" ? "concurrent-password" : nil)
                guard case .error = result.login else { return XCTFail("vault conflict signed in: \(result.login)") }
                XCTAssertTrue(requests.isEmpty)
            }
        }
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
        let actual5 = await rig.store.touchCalls.map(\.at)
        XCTAssertEqual(actual5, [77])
        XCTAssertEqual(result.persisted.lastUsedAt, 77)
        XCTAssertNil(result.persisted.cookieHost)
        guard case .success = result.login else { return XCTFail("got \(result.login)") }
        let actual6 = await rig.http.requests.count
        XCTAssertEqual(actual6, 0)   // NONE is zero-request
    }

    func test_legacy_none_url_changes_only_on_explicit_save_not_activation() async throws {
        let rig = Rig()
        let legacy = Profile(id: 7, alias: "legacy", baseUrl: "https://user:secret@h.example/proxy/15000/?token=secret",
                             authType: .none)
        await rig.store.seed(legacy)
        let activation = await rig.controller.activate(legacy)
        guard case .error = activation.login else { return XCTFail("unsafe URL opened") }
        let untouched = await rig.store.getById(legacy.id)
        XCTAssertEqual(untouched?.baseUrl, legacy.baseUrl)
        XCTAssertEqual(untouched?.authType, AuthType.none)
        let safe = try XCTUnwrap(ServerUrl.sanitizeForStorage(legacy.baseUrl))
        let edited = await rig.controller.editProfile(current: legacy, newAlias: legacy.alias,
                                                      newUrl: safe, newAuthType: .none, newSecret: "")
        XCTAssertEqual(edited.persisted.baseUrl, "https://h.example/proxy/15000/")
        XCTAssertEqual(edited.persisted.authType, .none)
        let requests = await rig.http.requests
        XCTAssertTrue(requests.isEmpty)
    }

    func test_deletion_during_recency_touch_never_falls_back_to_snapshot() async throws {
        let store = DeleteOnTouchStore(database: try SQLiteProfileStore(path: ":memory:"))
        var profile = Profile(id: 0, alias: "deleted", baseUrl: "https://h.example/proxy/15000/", authType: .none)
        profile.id = await store.insert(profile)
        let http = FakeHTTPClient()
        let secrets = FakeSecretStore()
        let manager = SessionManager(store: store, secrets: secrets, cookies: FakeCookieSink(), http: http)
        let controller = SessionController(store: store, secrets: secrets, session: manager)
        let result = await controller.activate(profile)
        guard case .error = result.login else { return XCTFail("deleted row completed: \(result.login)") }
        let saved = await store.getById(profile.id)
        let requests = await http.requests
        XCTAssertNil(saved)
        XCTAssertTrue(requests.isEmpty)
    }

    func test_credential_bearing_add_or_edit_does_not_write_row_or_secret() async {
        let rig = Rig()
        let current = Profile(id: 7, alias: "existing", baseUrl: "https://h.example/proxy/15000/",
                              authType: .none)
        await rig.store.seed(current)
        rig.secrets.putSecret("existing-password", for: current.alias)
        _ = await rig.controller.addProfile(alias: "unsafe", baseUrl: "https://h.example/?token=secret",
                                            authType: .codeServerPassword, secret: "new-password")
        _ = await rig.controller.editProfile(current: current, newAlias: "renamed",
                                             newUrl: "https://h.example/#/chat?api_key=secret",
                                             newAuthType: .codeServerPassword, newSecret: "replacement-password")
        let rows = await rig.store.getAllOnce()
        let requests = await rig.http.requests
        XCTAssertEqual(rows, [current])
        XCTAssertEqual(rig.secrets.secretFor(current.alias), "existing-password")
        XCTAssertNil(rig.secrets.secretFor("unsafe"))
        XCTAssertNil(rig.secrets.secretFor("renamed"))
        XCTAssertTrue(requests.isEmpty)
    }

    func test_activate_deleted_none_snapshot_cannot_succeed_or_recreate_the_row() async {
        let rig = Rig()
        let deleted = Profile(id: 4, alias: "deleted", baseUrl: "https://h.example/proxy/15000/",
                              authType: .none)
        await rig.store.seed(deleted)
        await rig.store.deleteById(deleted.id)
        let result = await rig.controller.activate(deleted)
        guard case .error = result.login else { return XCTFail("deleted snapshot completed: \(result.login)") }
        let saved = await rig.store.getById(deleted.id)
        let requests = await rig.http.requests
        XCTAssertNil(saved)
        XCTAssertTrue(requests.isEmpty)
    }

    func test_delete_removes_secret_and_row() async {
        let rig = Rig()
        let p = Profile(id: 4, alias: "a", baseUrl: "https://h.example/proxy/15000/")
        await rig.store.seed(p)
        rig.secrets.putSecret("pw", for: "a")
        await rig.controller.deleteProfile(p)
        XCTAssertNil(rig.secrets.secretFor("a"))
        let actual8 = await rig.store.getById(4)
        XCTAssertNil(actual8)
    }
}

private actor EditPersistenceGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var released = false
    func wait() async {
        if released { return }
        await withCheckedContinuation { continuation = $0 }
    }
    func release() {
        released = true
        continuation?.resume(); continuation = nil
    }
}

/// Suspends or deletes around the actual nonthrowing SQLite update. The write
/// is never converted into an insert or an assumed-success test substitute.
private struct EditFaultStore: ProfileStore {
    let database: SQLiteProfileStore
    let beforeUpdate: @Sendable (Profile) async -> Void
    let afterUpdate: @Sendable (Profile) async -> Void
    func getAllOnce() async -> [Profile] { database.getAllOnce() }
    func getById(_ id: Int64) async -> Profile? { database.getById(id) }
    func getByAlias(_ alias: String) async -> Profile? { database.getByAlias(alias) }
    func insert(_ profile: Profile) async -> Int64 { database.insert(profile) }
    func update(_ profile: Profile) async {
        await beforeUpdate(profile)
        database.update(profile)
        await afterUpdate(profile)
    }
    func deleteById(_ id: Int64) async { database.deleteById(id) }
    func touchLastUsed(_ id: Int64, _ at: Int64) async { database.touchLastUsed(id, at) }
    func setCookieHostIfCurrent(_ profile: Profile, _ host: String) async -> Bool {
        database.setCookieHostIfCurrent(profile, host)
    }
}

/// A deterministic store fault over real SQLite, not a platform substitute.
private struct DeleteOnTouchStore: ProfileStore {
    let database: SQLiteProfileStore
    func getAllOnce() async -> [Profile] { database.getAllOnce() }
    func getById(_ id: Int64) async -> Profile? { database.getById(id) }
    func getByAlias(_ alias: String) async -> Profile? { database.getByAlias(alias) }
    func insert(_ profile: Profile) async -> Int64 { database.insert(profile) }
    func update(_ profile: Profile) async { database.update(profile) }
    func deleteById(_ id: Int64) async { database.deleteById(id) }
    func touchLastUsed(_ id: Int64, _ at: Int64) async {
        database.touchLastUsed(id, at)
        database.deleteById(id)
    }
    func setCookieHostIfCurrent(_ profile: Profile, _ host: String) async -> Bool {
        database.setCookieHostIfCurrent(profile, host)
    }
}
