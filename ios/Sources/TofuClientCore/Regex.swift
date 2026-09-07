import Foundation

/// Thin NSRegularExpression helpers so the ported probes read like the Kotlin
/// originals (anchored structural regexes over wire payloads, never a JSON
/// parser — the same posture as TofuProbe.kt).
enum Rx {
    static func matches(
        _ pattern: String,
        _ options: NSRegularExpression.Options = [],
        in text: String
    ) -> Bool {
        guard let re = try? NSRegularExpression(pattern: pattern, options: options) else {
            return false
        }
        return re.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
    }

    /// Capture group [group] of the first match, or nil.
    static func group(
        _ pattern: String,
        _ options: NSRegularExpression.Options = [],
        in text: String,
        index: Int = 1
    ) -> String? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: options) else {
            return nil
        }
        let range = NSRange(text.startIndex..., in: text)
        guard let match = re.firstMatch(in: text, range: range),
              match.range(at: index).location != NSNotFound,
              let swiftRange = Range(match.range(at: index), in: text) else {
            return nil
        }
        return String(text[swiftRange])
    }
}
