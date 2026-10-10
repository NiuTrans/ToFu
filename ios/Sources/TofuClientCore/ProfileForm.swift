import Foundation

/// Pure validation for the add / edit-server form. Port of ProfileForm.kt.
/// The UI binds field state to ``validate`` and shows the errors inline.
public enum ProfileForm {

    /// Field-level validation errors, keyed by field name for inline display.
    public struct ValidationResult: Equatable, Sendable {
        public let errors: [String: String]
        public var ok: Bool { errors.isEmpty }
    }

    /// Validate a submitted form.
    /// - [secret] is required ONLY for codeServerPassword when
    ///   [secretAlreadyStored] is false (editing keeps an existing secret).
    /// - [existingAliases] excludes the profile being edited ([editingAlias]).
    public static func validate(
        alias: String,
        baseUrl: String,
        authType: AuthType,
        secret: String,
        existingAliases: Set<String>,
        editingAlias: String? = nil,
        secretAlreadyStored: Bool = false
    ) -> ValidationResult {
        var errors: [String: String] = [:]

        let a = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        if a.isEmpty {
            errors["alias"] = "Name is required"
        } else if a != editingAlias && existingAliases.contains(a) {
            errors["alias"] = "A server with this name already exists"
        }

        let u = baseUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        if u.isEmpty {
            errors["baseUrl"] = "Server URL is required"
        } else if ServerUrl.parse(u) == nil {
            errors["baseUrl"] = "Must be a full http(s):// URL"
        } else if !u.hasPrefix("http://") && !u.hasPrefix("https://") {
            errors["baseUrl"] = "Must start with http:// or https://"
        }

        if authType == .codeServerPassword && secret.isEmpty && !secretAlreadyStored {
            errors["secret"] = "Password is required"
        }

        return ValidationResult(errors: errors)
    }

    /// Build the ``Profile`` to persist from validated form fields. Parses the
    /// instanceUuid from the URL host so re-provision detection works.
    /// Caller must have validated first.
    public static func toProfile(
        id: Int64,
        alias: String,
        baseUrl: String,
        authType: AuthType,
        lastUsedAt: Int64,
        projectPath: String? = nil
    ) -> Profile {
        let u = baseUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        let pp = projectPath?.trimmingCharacters(in: .whitespacesAndNewlines)
        return Profile(
            id: id,
            alias: alias.trimmingCharacters(in: .whitespacesAndNewlines),
            instanceUuid: ServerUrl.parse(u)?.instanceUuid,
            baseUrl: u,
            authType: authType,
            lastUsedAt: lastUsedAt,
            projectPath: (pp?.isEmpty == false) ? pp : nil
        )
    }
}
