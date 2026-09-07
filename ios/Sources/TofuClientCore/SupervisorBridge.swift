import Foundation

/// The shell↔page supervisor handshake — constants shared with the web side
/// (frontend native-bridge contract), so both clients and the SPA can never
/// drift apart on the wire names.
///
/// The protocol exists because a headless START's login bounce is
/// indistinguishable from a stored-credential failure at the page layer:
/// start → 401 → login → reload lands on `/?login=BAD` — code-server saying
/// "wrong password" about a credential that is correct. The page would
/// normally surface that as a fix-your-password banner (and a hostile web
/// build could use it to clobber the Keychain entry). The bridge arms a
/// bounded expectation instead: for TTL after a Start click, the page treats
/// the first login bounce as the start handshake and keeps quiet.
public enum SupervisorBridge {

    /// How long after a Start click the page forgives the login bounce.
    /// Start polls up to 30 s; this must outlive that plus a reload.
    public static let ttlMs: Int64 = 45_000

    /// UserDefaults key under which the armed expectation is parked. The
    /// WebView layer reads it on page load and stamps `window.tofuStartPending`
    /// BEFORE the page's own scripts run (WKUserScript atDocumentStart).
    public static let pendingDefaultsKey = "tofu.supervisor.startPending.untilMs"

    /// JS expression evaluated at document start to arm the page side.
    /// `window.tofuStartPending = <untilMs>;` — the SPA reads it once and
    /// clears it. Absent/expired → property missing or past.
    public static func armingScript(untilMs: Int64) -> String {
        "window.tofuStartPending=\(untilMs);"
    }

    /// Parse the page-side marker back out of a raw `window.tofuStartPending`
    /// value (string form, as JSON.stringify/evaluateJavascript hands it to
    /// the native side). Returns the deadline in epoch ms, or nil when the
    /// marker is absent or malformed.
    public static func parsePending(_ raw: String?) -> Int64? {
        guard let raw else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        return Int64(trimmed)
    }

    /// Is an armed marker still live at `nowMs`?
    public static func isArmed(rawPending: String?, nowMs: Int64) -> Bool {
        guard let until = parsePending(rawPending) else { return false }
        return until > nowMs
    }
}
