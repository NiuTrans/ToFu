import Foundation

/// Pure discovery of the code-server login POST target. Port of LoginForm.kt.
///
/// We must NOT assume the login endpoint is the origin-root `/login`: a
/// code-server deployed behind a path prefix serves a login page whose
/// `<form action>` is relative (e.g. `./login` under `/some/prefix/`). Posting
/// to the assumed origin-root would silently fail auth there. Parses the first
/// `<form … action="…">` out of the fetched login page and resolves it against
/// the page URL; the caller falls back to origin-root when no action is found.
public enum LoginForm {

    // <form ... action="X"> — captures the quote char and the value.
    private static let formAction = #"<form\b[^>]*\baction\s*=\s*(["'])(.*?)\1"#

    /// Resolve the login POST URL from [html] served at [pageUrl].
    /// Returns nil when no `<form action>` is found (caller falls back to the
    /// origin-root `/login`). An empty `action=""` legitimately means "post to
    /// this page" and resolves to [pageUrl] itself.
    public static func resolveAction(_ html: String, pageUrl: URL) -> URL? {
        guard let re = try? NSRegularExpression(
            pattern: formAction,
            options: [.caseInsensitive, .dotMatchesLineSeparators]
        ) else { return nil }
        let range = NSRange(html.startIndex..., in: html)
        guard let match = re.firstMatch(in: html, range: range),
              let actionRange = Range(match.range(at: 2), in: html) else { return nil }
        let action = html[actionRange].trimmingCharacters(in: .whitespacesAndNewlines)
        // Handles absolute, root-relative, ./relative, and "" (→ pageUrl).
        return URL(string: action, relativeTo: pageUrl)?.absoluteURL
    }
}
