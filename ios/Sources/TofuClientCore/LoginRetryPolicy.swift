import Foundation

/// Bounded retry for transient transport failures of the login POST.
/// Port of LoginRetryPolicy.kt.
public enum LoginRetryPolicy {
    public static let maxAttempts = 3

    /// Longer ladder for a WARMING sandbox: a cold container behind the
    /// vscode proxy routinely takes tens of seconds before anything listens,
    /// so the short 3-attempt/3.5s ladder would hand the user back a dead
    /// page long before the host is up.
    public static let maxWarmingAttempts = 6

    public static func maxAttempts(warming: Bool) -> Int {
        warming ? maxWarmingAttempts : maxAttempts
    }

    /// Delay before the next attempt, after [failedAttempts] failures.
    public static func backoffMs(_ failedAttempts: Int, warming: Bool = false) -> Int64 {
        if warming { return min(2_000 * Int64(failedAttempts), 8_000) }
        return failedAttempts <= 1 ? 1_000 : 2_500
    }

    /// The wire contract for "sandbox waking" lives in ``TofuProbe``.
    public static func isWarmingStatus(_ code: Int) -> Bool {
        TofuProbe.isWakingStatus(code)
    }

    /// A socket timeout against the proxy means the edge is up but the
    /// sandbox behind the tunnel isn't answering yet — same as its 5xx.
    /// Refused / reset / DNS failures stay on the short ladder.
    public static func isWarmingTransport(_ error: Error) -> Bool {
        if let urlError = error as? URLError {
            return urlError.code == .timedOut
        }
        let message = error.localizedDescription.lowercased()
        return message.contains("timed out") || message.contains("timeout")
    }

    /// User-facing text for a warming retry that eventually exhausted.
    public static func warmingMessage(_ cause: String) -> String {
        "The sandbox is still waking up (\(cause)) — it usually comes up within " +
            "half a minute. Give it a few more seconds and tap Open again."
    }
}
