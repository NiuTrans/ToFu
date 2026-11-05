import XCTest
@testable import TofuClientCore

final class SQLiteProfileStoreTests: XCTestCase {
    func test_conditional_stamp_rejects_edits_and_deletion_from_another_connection() throws {
        let path = FileManager.default.temporaryDirectory.appendingPathComponent("tofu-profiles-\(UUID().uuidString).sqlite")
        defer { try? FileManager.default.removeItem(at: path) }
        let login = try SQLiteProfileStore(path: path.path)
        let editor = try SQLiteProfileStore(path: path.path)
        for mutation in ["url", "alias", "auth", "delete"] {
            var snapshot = Profile(id: 0, alias: "original", baseUrl: "https://h.example/app/", authType: .codeServerPassword)
            snapshot.id = editor.insert(snapshot)
            var changed = snapshot
            switch mutation {
            case "url": changed.baseUrl = "https://new.example/app/"
            case "alias": changed.alias = "renamed"
            case "auth": changed.authType = .none
            default: break
            }
            if mutation == "delete" { editor.deleteById(snapshot.id) } else { editor.update(changed) }
            XCTAssertFalse(login.setCookieHostIfCurrent(snapshot, "h.example"), mutation)
            XCTAssertNil(editor.getById(snapshot.id)?.cookieHost)
            if mutation == "delete" { XCTAssertNil(editor.getById(snapshot.id)) }
            else { XCTAssertEqual(editor.getById(snapshot.id), changed) }
        }
    }

    func test_conditional_stamp_preserves_new_recency_and_project_path() throws {
        let store = try SQLiteProfileStore(path: ":memory:")
        var snapshot = Profile(id: 0, alias: "a", baseUrl: "https://h.example/app/", authType: .codeServerPassword)
        snapshot.id = store.insert(snapshot)
        var edited = snapshot; edited.projectPath = "/new/project"; edited.lastUsedAt = 99
        store.update(edited)
        XCTAssertTrue(store.setCookieHostIfCurrent(snapshot, "h.example"))
        let saved = store.getById(snapshot.id)
        XCTAssertEqual(saved?.projectPath, "/new/project")
        XCTAssertEqual(saved?.lastUsedAt, 99)
        XCTAssertEqual(saved?.cookieHost, "h.example")
    }
}
