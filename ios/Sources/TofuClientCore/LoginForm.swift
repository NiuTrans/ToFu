import Foundation

/// Recognizes the maintained code-server password form; ambiguous HTML belongs
/// in the browser. Entry point: discover. Dependencies: ServerUrl, Foundation.
public enum LoginForm {
    public struct PasswordForm: Sendable {
        public let target: URL
        public let fields: [String: String]
    }

    private static let forms = expression(#"<form\b([^>]*)>(.*?)</form\s*>"#)
    private static let inputs = expression(#"<input\b([^>]*)>"#)
    private static let attributes = expression(#"([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"#)
    private static let invisible = expression(#"<!--.*?-->|<script\b[^>]*>.*?</script\s*>"#)
    private static let entities = expression(#"&(?:amp|quot|apos|lt|gt|#\d+|#x[0-9a-f]+);"#)

    public static func discover(_ html: String, pageUrl: URL, server: ServerUrl) -> PasswordForm? {
        guard server.sameOrigin(pageUrl), html.utf8.count <= GatewayHTTP.maximumResponseBytes else { return nil }
        let visible = invisible.stringByReplacingMatches(in: html, range: NSRange(html.startIndex..., in: html), withTemplate: "")
        let candidates: [PasswordForm] = matches(forms, visible).compactMap { form in
            let attrs = parseAttributes(form[1])
            guard attrs["method"]?.lowercased() == "post",
                  attrs["class"]?.split(whereSeparator: { $0.isWhitespace }).contains("login-form") == true else { return nil }
            let fields = matches(inputs, form[2]).map { parseAttributes($0[1]) }
            guard fields.filter({ $0["name"] == "password" && $0["type"]?.lowercased() == "password" }).count == 1,
                  fields.contains(where: { $0["name"] == "base" && $0["type"]?.lowercased() == "hidden" }),
                  let target = URL(string: attrs["action"] ?? "", relativeTo: pageUrl)?.absoluteURL,
                  server.sameOrigin(target) else { return nil }
            var hidden: [String: String] = [:]
            for field in fields where field["type"]?.lowercased() == "hidden" {
                guard let name = field["name"] else { continue }
                guard name != "password", hidden[name] == nil else { return nil }
                hidden[name] = name == "href" ? pageUrl.absoluteString : field["value"] ?? ""
            }
            return PasswordForm(target: target, fields: hidden)
        }
        return candidates.count == 1 ? candidates[0] : nil
    }

    /// Syntax-only action resolution grants no permission to submit credentials.
    public static func resolveAction(_ html: String, pageUrl: URL) -> URL? {
        guard let form = matches(expression(#"<form\b([^>]*)>"#), html).first,
              let action = parseAttributes(form[1])["action"] else { return nil }
        return URL(string: action, relativeTo: pageUrl)?.absoluteURL
    }

    private static func expression(_ pattern: String) -> NSRegularExpression {
        // All patterns are owned constants, not server input.
        try! NSRegularExpression(pattern: pattern, options: [.caseInsensitive, .dotMatchesLineSeparators])
    }

    private static func matches(_ re: NSRegularExpression, _ text: String) -> [[String]] {
        re.matches(in: text, range: NSRange(text.startIndex..., in: text)).map { match in
            (0..<match.numberOfRanges).map { Range(match.range(at: $0), in: text).map { String(text[$0]) } ?? "" }
        }
    }

    private static func parseAttributes(_ text: String) -> [String: String] {
        var result: [String: String] = [:]
        for attr in matches(attributes, text) {
            result[attr[1].lowercased()] = decode(attr.dropFirst(2).first(where: { !$0.isEmpty }) ?? "")
        }
        return result
    }

    private static func decode(_ text: String) -> String {
        var decoded = text
        let named = ["&amp;": "&", "&quot;": "\"", "&apos;": "'", "&lt;": "<", "&gt;": ">"]
        for match in entities.matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed() {
            guard let range = Range(match.range, in: decoded) else { continue }
            let entity = String(decoded[range])
            let number = entity.dropFirst(2).dropLast()
            let code = number.hasPrefix("x") ? UInt32(number.dropFirst(), radix: 16) : UInt32(number)
            let replacement = named[entity] ?? code.flatMap(UnicodeScalar.init).map(String.init) ?? entity
            decoded.replaceSubrange(range, with: replacement)
        }
        return decoded
    }
}
