import Foundation

/// application/x-www-form-urlencoded encoding shared by POST bodies and
/// supervisor query fields. Reserved separators must never become fields.
enum FormEncoding {
    private static let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_*")
    static func component(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: allowed)!.replacingOccurrences(of: "%20", with: "+")
    }
    static func encode(_ pairs: [(String, String)]) -> String {
        pairs.map { "\(component($0.0))=\(component($0.1))" }.joined(separator: "&")
    }
}
