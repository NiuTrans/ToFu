import Foundation

/// Mirror of the desktop agent's reachability probe (lib/desktop_agent/_probe.py)
/// — the contract that settles "does this URL actually reach Tofu?" at PASTE
/// time rather than after hours of silent retrying. Port of TofuProbe.kt.
///
/// The discrimination that matters behind a VS Code port-forwarding proxy: the
/// gateway edge refuses EVERY unauthenticated request with 401 — including
/// /api/health — so a bare 401 says nothing about whether the URL is right.
///  - Tofu's OWN refusal carries the api_error envelope
///    `{"ok":false,"error":{…}}` — error is a JSON OBJECT;
///  - the proxy edge answers `{"error":"Unauthorized"}` — error is a STRING —
///    and a gateway landing page is 200 HTML with no bootId;
///  - the positive proof of "this is Tofu" is 200 + a bootId in the health JSON;
///  - a COLD sandbox behind the proxy edge surfaces as 502/503/504 — the edge
///    answers but nothing listens behind the tunnel yet; that is "waking up",
///    not "wrong URL", and the honest guidance is to wait, not to edit.
public enum TofuProbe {

    public enum Verdict: String, Sendable {
        /// 200 + health JSON carries bootId — this IS Tofu.
        case tofu
        /// 401/403 with Tofu's api_error envelope — Tofu answered; its own gate refused.
        case tofuAuth
        /// 401/403 WITHOUT the Tofu envelope — the proxy edge bounced it before Tofu.
        case gateway
        /// 200 (or anything else) that is not Tofu's health JSON — landing page / wrong server.
        case notTofu
        /// 502/503/504 from the proxy edge — the sandbox behind the tunnel is still booting.
        case waking
        /// No usable HTTP response (transport failure / non-edge 5xx).
        case unreachable
    }

    /// Proxy-edge statuses meaning "nothing listens behind the tunnel yet".
    public static let wakingStatuses: Set<Int> = [502, 503, 504]

    public static func isWakingStatus(_ status: Int) -> Bool {
        wakingStatuses.contains(status)
    }

    private static let okFalse = #""ok"\s*:\s*false"#
    private static let errorObject = #""error"\s*:\s*\{"#
    private static let bootId = #""bootId"\s*:\s*""#

    /// Tofu's api_error envelope: `{"ok":false,"error":{…}}` (error is an
    /// OBJECT). The gateway's `{"error":"Unauthorized"}` (STRING) does NOT
    /// match — that asymmetry is the whole discrimination.
    public static func isTofuErrorEnvelope(_ body: String?) -> Bool {
        guard let body else { return false }
        return Rx.matches(okFalse, in: body) && Rx.matches(errorObject, in: body)
    }

    /// The positive Tofu signal from /api/health: a bootId field.
    public static func hasBootId(_ body: String?) -> Bool {
        guard let body else { return false }
        return Rx.matches(bootId, in: body)
    }

    /// Classify an /api/health response. [status] 0 is the caller's sentinel
    /// for "no HTTP response at all" (transport failure).
    public static func classify(status: Int, body: String?) -> Verdict {
        if status == 0 { return .unreachable }
        if isWakingStatus(status) { return .waking }
        if (500...599).contains(status) { return .unreachable }
        if status == 401 || status == 403 {
            return isTofuErrorEnvelope(body) ? .tofuAuth : .gateway
        }
        if status == 200 { return hasBootId(body) ? .tofu : .notTofu }
        return .notTofu
    }

    /// True when [verdict] is something the user must fix (not just informational).
    public static func isProblem(_ verdict: Verdict, authType: AuthType, hasSecret: Bool) -> Bool {
        switch verdict {
        case .tofu, .tofuAuth:
            return false
        case .gateway:
            switch authType {
            case .none: return true
            case .codeServerPassword: return !hasSecret
            case .interactiveSso: return false
            }
        case .notTofu, .waking, .unreachable:
            return true
        }
    }

    /// The honest one-line explanation shown next to the URL field after a test.
    public static func guidance(_ verdict: Verdict, authType: AuthType, hasSecret: Bool) -> String {
        switch verdict {
        case .tofu:
            return "Tofu answered — this URL is correct."
        case .tofuAuth:
            return "Tofu answered but requires sign-in — it will be handled on open."
        case .gateway:
            switch authType {
            case .codeServerPassword:
                return hasSecret
                    ? "Behind the code-server gate (expected for /proxy/ URLs) — " +
                      "the saved password signs in on open."
                    : "Behind the code-server gate — enter the password, " +
                      "or this URL can't be reached."
            case .interactiveSso:
                return "Behind a sign-in gateway — you'll complete sign-in once in the app."
            case .none:
                return "A gateway refused the request — this URL needs sign-in; " +
                    "switch the auth mode above."
            }
        case .notTofu:
            return "Something answered, but it isn't Tofu — check the host and the /proxy/<port>/ prefix."
        case .waking:
            return "The proxy answered, but the sandbox behind it is still waking up — " +
                "wait half a minute and re-test, don't edit the URL."
        case .unreachable:
            return "No answer from this URL — check the network and the address."
        }
    }
}
