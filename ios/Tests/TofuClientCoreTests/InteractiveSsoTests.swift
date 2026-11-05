import XCTest
@testable import TofuClientCore

final class InteractiveSsoTests: XCTestCase {
    func test_handoff_changes_only_active_profile_and_needs_verified_completion() {
        let stored = Profile(id: 1, alias: "a", baseUrl: "https://own.example/proxy/15000/", authType: .codeServerPassword)
        let active = InteractiveSso.profileForWebView(stored, result: .needsInteractiveSso(url: stored.baseUrl))
        XCTAssertEqual(active.authType, .interactiveSso)
        XCTAssertEqual(stored.authType, .codeServerPassword)
        XCTAssertEqual(InteractiveSso.profileForWebView(stored, result: .success(host: "own.example")), stored)
        XCTAssertFalse(InteractiveSso.completedSignIn(profile: active, finishedUrl: active.baseUrl, cookieHeader: "idp=opaque"))
    }

    private func ssoProfile(_ url: String = "https://own.example/proxy/15000/") -> Profile {
        Profile(id: 1, alias: "a", baseUrl: url, authType: .interactiveSso)
    }

    /// needsInteractiveSso MUST open the WebView — its whole point is that
    /// sign-in can only complete there.
    func test_shouldOpenWebView() {
        XCTAssertTrue(InteractiveSso.shouldOpenWebView(.success(host: "h")))
        XCTAssertTrue(InteractiveSso.shouldOpenWebView(.needsInteractiveSso(url: "u")))
        XCTAssertFalse(InteractiveSso.shouldOpenWebView(.badCredentials))
        XCTAssertFalse(InteractiveSso.shouldOpenWebView(.error(message: "x")))
        XCTAssertFalse(InteractiveSso.shouldOpenWebView(.incompatible(message: "x")))
    }

    func test_completedSignIn_requires_own_host_and_a_cookie() {
        let p = ssoProfile()
        // On own host with a cookie, off the login page → signed in.
        XCTAssertTrue(InteractiveSso.completedSignIn(
            profile: p, finishedUrl: "https://own.example/proxy/15000/", cookieHeader: "code-server-session=x", gatewayVerified: true
        ))
        // Still on the IdP → not done.
        XCTAssertFalse(InteractiveSso.completedSignIn(
            profile: p, finishedUrl: "https://idp.foreign.example/callback", cookieHeader: "x=y", gatewayVerified: true
        ))
        // On own host but back on the login page → gate not passed.
        XCTAssertFalse(InteractiveSso.completedSignIn(
            profile: p, finishedUrl: "https://own.example/login", cookieHeader: "x=y", gatewayVerified: true
        ))
        // No cookie in the jar → nothing to record.
        XCTAssertFalse(InteractiveSso.completedSignIn(
            profile: p, finishedUrl: "https://own.example/proxy/15000/", cookieHeader: nil
        ))
        XCTAssertFalse(InteractiveSso.completedSignIn(
            profile: p, finishedUrl: "https://own.example/proxy/15000/", cookieHeader: "  "
        ))
    }

    func test_completedSignIn_ignores_non_sso_profiles() {
        let p = Profile(id: 1, alias: "a", baseUrl: "https://own.example/proxy/15000/", authType: .codeServerPassword)
        XCTAssertFalse(InteractiveSso.completedSignIn(
            profile: p, finishedUrl: "https://own.example/proxy/15000/", cookieHeader: "x=y", gatewayVerified: true
        ))
    }

    func test_hostToStamp() {
        XCTAssertEqual(InteractiveSso.hostToStamp(ssoProfile()), "own.example")
        XCTAssertNil(InteractiveSso.hostToStamp(ssoProfile("junk")))
    }
}
