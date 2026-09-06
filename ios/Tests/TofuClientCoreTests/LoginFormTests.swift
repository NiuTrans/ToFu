import XCTest
@testable import TofuClientCore

final class LoginFormTests: XCTestCase {

    private let page = URL(string: "https://h.example/some/prefix/")!

    func test_relative_action_resolves_against_page_url() {
        let html = "<html><form method=\"post\" action=\"./login\">"
        XCTAssertEqual(
            LoginForm.resolveAction(html, pageUrl: page)?.absoluteString,
            "https://h.example/some/login"
        )
    }

    func test_root_relative_action() {
        XCTAssertEqual(
            LoginForm.resolveAction("<form action='/login'>", pageUrl: page)?.absoluteString,
            "https://h.example/login"
        )
    }

    func test_absolute_action_wins() {
        XCTAssertEqual(
            LoginForm.resolveAction("<form action=\"https://other.example/login\">", pageUrl: page)?.absoluteString,
            "https://other.example/login"
        )
    }

    /// An empty action legitimately means "post to this page".
    func test_empty_action_posts_to_page_itself() {
        XCTAssertEqual(
            LoginForm.resolveAction("<form action=\"\">", pageUrl: page)?.absoluteString,
            page.absoluteString
        )
    }

    func test_no_form_returns_nil_so_caller_falls_back() {
        XCTAssertNil(LoginForm.resolveAction("<html>no form</html>", pageUrl: page))
    }

    func test_single_quotes_and_attributes_before_action() {
        let html = "<form class='big' method='post' action='./login'>"
        XCTAssertEqual(
            LoginForm.resolveAction(html, pageUrl: page)?.absoluteString,
            "https://h.example/some/login"
        )
    }
}
