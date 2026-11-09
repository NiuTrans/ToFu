import XCTest
@testable import TofuClientCore

/// Stored connections cannot be credentials, including encoded redirect/SPA fields.
final class SecurityRegressionsTests: XCTestCase {
    func test_saved_urls_and_forms_reject_encoded_or_nested_credentials() {
        for raw in [
            "https://user:secret@h.example/", "https://user@h.example/",
            "https://h.example/?access_token=secret", "https://h.example/#/chat?password=secret",
            "https://h.example/?%74oken=secret", "https://h.example/?%2574oken=secret",
            "https://h.example/?next=%2F%2Fuser%3Asecret%40other.example",
            "https://h.example/?next=https%3A%2F%2Fidp.example%2F%3Fpassword%3Dsecret",
            "https://h.example/?next=%252Fcallback%253Fcode%253Dsecret",
            "https://h.example/#access_token=secret?/chat", "https://h.example/?X-Amz-Signature=secret",
            "https://h.example/?X-Goog-Credential=secret", "https://h.example/?X-Api-Key=secret",
            "https://h.example/?tofu_session=secret", "https://h.example/#/chat?gateway_cookie=secret",
            "https://h.example/?workspace=%0Asecret"
        ] {
            XCTAssertNil(ServerUrl.parse(raw), raw)
            let form = ProfileForm.validate(alias: "a", baseUrl: raw, authType: .none,
                                            secret: "", existingAliases: [])
            XCTAssertFalse(form.ok, raw)
        }
    }

    func test_safe_spa_parameters_are_preserved_and_profile_url_is_canonical() {
        let raw = " HTTPS://H.EXAMPLE:8443/team/proxy/15000/?workspace=one%20two#/chat?conversation=c1 "
        let validation = ProfileForm.validate(alias: "a", baseUrl: raw, authType: .none,
                                              secret: "", existingAliases: [])
        XCTAssertTrue(validation.ok)
        let profile = ProfileForm.toProfile(id: 1, alias: " a ", baseUrl: raw,
                                             authType: .none, lastUsedAt: 0)
        XCTAssertEqual(profile.baseUrl,
                       "https://h.example:8443/team/proxy/15000/?workspace=one%20two#/chat?conversation=c1")
        XCTAssertEqual(profile.authType, .none)
        XCTAssertNotNil(ServerUrl.parse("https://h.example/?workspace=50%25&label=one%20two"))
    }

    func test_nested_field_assignments_and_deep_encoding_cannot_hide_credentials() {
        var deeplyEncoded = "%74oken"
        for _ in 0..<8 { deeplyEncoded = deeplyEncoded.replacingOccurrences(of: "%", with: "%25") }
        for raw in [
            "https://h.example/?next=access_token%3Dsecret",
            "https://h.example/?next%3D%2574oken%253Dsecret",
            "https://h.example/#/chat?next=api_key%3Dsecret",
            "https://h.example/?\(deeplyEncoded)=secret"
        ] {
            XCTAssertNil(ServerUrl.parse(raw), raw)
            XCTAssertNil(ConnectionOnboarding.parse(raw), raw)
        }
        let legacy = "https://h.example/?workspace=50%25&next=access_token%3Dsecret#/chat?conversation=c1&next=api_key%3Dsecret"
        XCTAssertEqual(ServerUrl.sanitizeForStorage(legacy),
                       "https://h.example/?workspace=50%25#/chat?conversation=c1")
        XCTAssertNotNil(ServerUrl.parse("https://h.example/?workspace=50%25&label=one%2520two"))
    }

    func test_navigation_can_carry_sso_callbacks_but_storage_cannot() throws {
        let callback = "https://h.example/team/proxy/15000/?code=transient&state=one#access_token=transient"
        let server = try XCTUnwrap(ServerUrl.parse("https://h.example/team/proxy/15000/"))
        XCTAssertNotNil(ServerUrl.parseNavigation(callback))
        XCTAssertNil(ServerUrl.parse(callback))
        XCTAssertTrue(server.sameOrigin(try XCTUnwrap(URL(string: callback))))
        XCTAssertTrue(TrustedWebBridge.allowsURL(URL(string: callback), serverURL: server.raw, isMainFrame: true))
        let profile = Profile(id: 1, alias: "sso", baseUrl: server.raw, authType: .interactiveSso)
        XCTAssertTrue(InteractiveSso.isCompletionCandidate(profile: profile, finishedUrl: callback, cookieHeader: "idp=opaque"))
        XCTAssertTrue(server.isLoginURL("https://h.example/team/login?code=transient"))
        XCTAssertNil(ConnectionOnboarding.parse(callback))
        XCTAssertEqual(ProfileForm.toProfile(id: 1, alias: "sso", baseUrl: callback,
                                             authType: .none, lastUsedAt: 0).baseUrl, "")
        XCTAssertNil(ServerUrl.parseNavigation("https://user:secret@h.example/callback?code=transient"))
    }

    func test_legacy_url_is_sanitized_only_for_an_explicit_edit_draft() {
        let legacy = "https://user:secret@h.example:8443/team/proxy/15000/?workspace=one%20two&token=secret&next=https%3A%2F%2Fidp.example%2F%3Fpassword%3Dsecret#/chat?conversation=c1&api_key=secret"
        XCTAssertNil(ServerUrl.parse(legacy))
        XCTAssertEqual(ServerUrl.sanitizeForStorage(legacy),
                       "https://h.example:8443/team/proxy/15000/?workspace=one%20two#/chat?conversation=c1")
        XCTAssertEqual(ServerUrl.sanitizeForStorage("https://h.example/?workspace=one"),
                       "https://h.example/?workspace=one")
        XCTAssertNil(ServerUrl.sanitizeForStorage("javascript:alert(1)"))
    }

    func test_nested_browser_authorities_are_inspected_before_each_decode() throws {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        for nested in [
            #"https:\\user:fixture-password@other.example/"#,
            #"https:/\user:fixture-password@other.example/"#,
            #"\\user:fixture-password@other.example/"#,
            "https:///user:fixture-password@other.example/",
            "https:user:fixture-password@other.example/",
            "https://us\ter:fixture-password@other.example/",
            "https://user name:fixture-password@other.example/",
            "https://user%2Fname:fixture-password@other.example/"
        ] {
            let encoded = try XCTUnwrap(nested.addingPercentEncoding(withAllowedCharacters: allowed))
            let raw = "https://h.example/?workspace=one&return=" + encoded
            XCTAssertNil(ServerUrl.parse(raw), nested)
            XCTAssertNil(ConnectionOnboarding.parse(raw), nested)
            XCTAssertFalse(ProfileForm.validate(alias: "a", baseUrl: raw, authType: .none,
                                                 secret: "", existingAliases: []).ok, nested)
            XCTAssertEqual(ServerUrl.sanitizeForStorage(raw), "https://h.example/?workspace=one", nested)
        }
    }

    func test_known_credential_aliases_are_not_saved_as_url_parameters() {
        for name in ["pass", "ticket", "oauth_code", "auth_code", "authorization_code",
                     "csrf", "jwt", "saml_response", "access_key", "private_key"] {
            let raw = "https://h.example/?" + name + "=fixture-secret"
            XCTAssertNil(ServerUrl.parse(raw), name)
            XCTAssertNil(ConnectionOnboarding.parse(raw), name)
        }
    }

    func test_invalid_url_labels_never_echo_credentials_or_untrusted_input() {
        for raw in ["https://user:secret@h.example/", "https://h.example/?token=secret", "junk"] {
            XCTAssertEqual(ServerUrl.displayLabel(raw), "Invalid server URL")
        }
        XCTAssertEqual(ServerUrl.displayLabel("https://h.example:8443/app/?workspace=one"), "h.example:8443")
    }
}
