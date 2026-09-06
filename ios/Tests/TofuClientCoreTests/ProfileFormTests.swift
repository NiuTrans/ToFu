import XCTest
@testable import TofuClientCore

final class ProfileFormTests: XCTestCase {

    private let url = "https://h.example/proxy/15000/"

    func test_valid_form_passes() {
        let result = ProfileForm.validate(
            alias: "ml-gpu", baseUrl: url, authType: .codeServerPassword,
            secret: "pw", existingAliases: []
        )
        XCTAssertTrue(result.ok)
    }

    func test_alias_required_and_unique() {
        var result = ProfileForm.validate(
            alias: "  ", baseUrl: url, authType: .none, secret: "", existingAliases: []
        )
        XCTAssertNotNil(result.errors["alias"])

        result = ProfileForm.validate(
            alias: "ml", baseUrl: url, authType: .none, secret: "",
            existingAliases: ["ml"]
        )
        XCTAssertNotNil(result.errors["alias"])

        // Editing keeps its own alias.
        result = ProfileForm.validate(
            alias: "ml", baseUrl: url, authType: .none, secret: "",
            existingAliases: ["ml"], editingAlias: "ml"
        )
        XCTAssertNil(result.errors["alias"])
    }

    func test_url_required_and_absolute() {
        var result = ProfileForm.validate(
            alias: "a", baseUrl: "", authType: .none, secret: "", existingAliases: []
        )
        XCTAssertNotNil(result.errors["baseUrl"])

        result = ProfileForm.validate(
            alias: "a", baseUrl: "h.example/proxy/15000/", authType: .none,
            secret: "", existingAliases: []
        )
        XCTAssertNotNil(result.errors["baseUrl"])
    }

    /// Secret required only for codeServerPassword, and only when none is
    /// already stored (editing keeps the existing one).
    func test_secret_requirement() {
        var result = ProfileForm.validate(
            alias: "a", baseUrl: url, authType: .codeServerPassword,
            secret: "", existingAliases: []
        )
        XCTAssertNotNil(result.errors["secret"])

        result = ProfileForm.validate(
            alias: "a", baseUrl: url, authType: .codeServerPassword,
            secret: "", existingAliases: [], secretAlreadyStored: true
        )
        XCTAssertNil(result.errors["secret"])

        result = ProfileForm.validate(
            alias: "a", baseUrl: url, authType: .interactiveSso,
            secret: "", existingAliases: []
        )
        XCTAssertNil(result.errors["secret"])
    }

    func test_toProfile_trims_parses_uuid_and_normalizes_project_path() {
        let p = ProfileForm.toProfile(
            id: 0, alias: "  ml ",
            baseUrl: "  https://5665bc99-279b-4edf-8553-c7b7804c6e02-vscode-zw05.mlp.sankuai.com/proxy/15000/  ",
            authType: .codeServerPassword, lastUsedAt: 5, projectPath: "  "
        )
        XCTAssertEqual(p.alias, "ml")
        XCTAssertEqual(p.instanceUuid, "5665bc99-279b-4edf-8553-c7b7804c6e02")
        XCTAssertNil(p.projectPath)
        XCTAssertEqual(p.lastUsedAt, 5)
    }
}
