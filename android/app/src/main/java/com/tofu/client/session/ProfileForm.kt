package com.tofu.client.session

import com.tofu.client.data.AuthType

/**
 * Pure (Android-free) validation for the add / edit-server form, so the rules
 * are unit-testable without Compose. The UI binds field state to
 * [validate] and shows [ValidationResult.errors] inline.
 */
object ProfileForm {

    /** Field-level validation errors, keyed by field name for inline display. */
    data class ValidationResult(val errors: Map<String, String>) {
        val ok: Boolean get() = errors.isEmpty()
    }

    /**
     * Validate a submitted form.
     *
     * @param alias human name / switcher key (required, unique)
     * @param baseUrl the server URL (required, must be absolute http(s))
     * @param authType chosen auth mode
     * @param secret the password — required ONLY for CODE_SERVER_PASSWORD *when*
     *        [secretAlreadyStored] is false (editing keeps an existing secret)
     * @param existingAliases aliases already taken (excluding the one being
     *        edited, which the caller passes in [editingAlias])
     * @param editingAlias the alias of the profile being edited, or null when adding
     * @param secretAlreadyStored true when editing a profile that already has a
     *        saved secret (so a blank secret field means "keep existing")
     */
    fun validate(
        alias: String,
        baseUrl: String,
        authType: AuthType,
        secret: String,
        existingAliases: Set<String>,
        editingAlias: String? = null,
        secretAlreadyStored: Boolean = false,
    ): ValidationResult {
        val errors = mutableMapOf<String, String>()

        val a = alias.trim()
        if (a.isEmpty()) {
            errors["alias"] = "Name is required"
        } else if (a != editingAlias && a in existingAliases) {
            errors["alias"] = "A server with this name already exists"
        }

        val u = baseUrl.trim()
        if (u.isEmpty()) {
            errors["baseUrl"] = "Server URL is required"
        } else if (ServerUrl.parse(u) == null) {
            errors["baseUrl"] = "Must be a full http(s):// URL"
        } else if (!u.startsWith("http://") && !u.startsWith("https://")) {
            errors["baseUrl"] = "Must start with http:// or https://"
        }

        if (authType == AuthType.CODE_SERVER_PASSWORD &&
            secret.isEmpty() && !secretAlreadyStored
        ) {
            errors["secret"] = "Password is required"
        }

        return ValidationResult(errors)
    }

    /**
     * Build the [Profile] to persist from validated form fields. Parses the
     * instanceUuid from the URL host so the re-provision detection works.
     * Caller must have validated first.
     */
    fun toProfile(
        id: Long,
        alias: String,
        baseUrl: String,
        authType: AuthType,
        lastUsedAt: Long,
        projectPath: String? = null,
    ): com.tofu.client.data.Profile {
        val u = baseUrl.trim()
        return com.tofu.client.data.Profile(
            id = id,
            alias = alias.trim(),
            baseUrl = u,
            authType = authType,
            instanceUuid = ServerUrl.parse(u)?.instanceUuid,
            lastUsedAt = lastUsedAt,
            projectPath = projectPath?.trim()?.ifEmpty { null },
        )
    }
}
