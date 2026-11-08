package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.SshEndpoint

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
     * @param secret optional saved code-server password; blank permits interactive sign-in
     * @param existingAliases aliases already taken (excluding the one being
     *        edited, which the caller passes in [editingAlias])
     * @param editingAlias the alias of the profile being edited, or null when adding
     * @param secretAlreadyStored true when editing a profile that already has a
     *        saved secret (so a blank secret field means "keep existing")
     */
    @Suppress("UNUSED_PARAMETER") // Stable form call contract; blank passwords now permit interaction.
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

        aliasError(alias, existingAliases, editingAlias)?.let { errors["alias"] = it }

        val u = baseUrl.trim()
        if (u.isEmpty()) {
            errors["baseUrl"] = "Server URL is required"
        } else if (ServerUrl.parse(u) == null) {
            errors["baseUrl"] = "Must be a full http(s):// URL"
        } else if (!u.startsWith("http://") && !u.startsWith("https://")) {
            errors["baseUrl"] = "Must start with http:// or https://"
        } else if (LocalKernelCookie.isReservedUrl(u)) {
            errors["baseUrl"] = LocalKernelCookie.RESERVED_HOST_MESSAGE
        }

        // A blank password selects interactive sign-in. The outer gateway may
        // be SSO even when its URL uses code-server's /proxy shape.

        return ValidationResult(errors)
    }

    /**
     * Validate the SSH kernel form. Ports arrive as the typed text; an ok result
     * guarantees [toSshEndpoint] can build the endpoint. Aliases share one
     * namespace with URL profiles (they key the vault).
     */
    fun validateSsh(
        alias: String,
        host: String,
        port: String,
        user: String,
        remotePort: String,
        existingAliases: Set<String>,
        editingAlias: String? = null,
    ): ValidationResult {
        val errors = mutableMapOf<String, String>()
        aliasError(alias, existingAliases, editingAlias)?.let { errors["alias"] = it }
        val h = host.trim()
        when {
            h.isEmpty() -> errors["host"] = "Host is required"
            h.any { it.isWhitespace() || it == '/' || it == '@' } -> errors["host"] = "Just the host name or IP address"
            // One colon is host:port; IPv6 literals have several.
            h.count { it == ':' } == 1 -> errors["host"] = "Put the port in its own field"
        }
        if (parsePort(port) == null) errors["port"] = PORT_RANGE_ERROR
        val u = user.trim()
        when {
            u.isEmpty() -> errors["user"] = "User is required"
            u.any { it.isWhitespace() || it == '@' } -> errors["user"] = "Just the user name"
        }
        if (parsePort(remotePort) == null) errors["remotePort"] = PORT_RANGE_ERROR
        return ValidationResult(errors)
    }

    /** The endpoint from fields that passed [validateSsh]; never carries a pinned key. */
    fun toSshEndpoint(host: String, port: String, user: String, remotePort: String): SshEndpoint =
        SshEndpoint(
            host = host.trim(),
            port = checkNotNull(parsePort(port)),
            user = user.trim(),
            remotePort = checkNotNull(parsePort(remotePort)),
        )

    private fun aliasError(alias: String, existingAliases: Set<String>, editingAlias: String?): String? {
        val a = alias.trim()
        return when {
            a.isEmpty() -> "Name is required"
            a != editingAlias && a in existingAliases -> "A server with this name already exists"
            else -> null
        }
    }

    /** TCP's port range. */
    private fun parsePort(raw: String): Int? = raw.trim().toIntOrNull()?.takeIf { it in 1..65535 }

    private const val PORT_RANGE_ERROR = "A port from 1 to 65535"

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
