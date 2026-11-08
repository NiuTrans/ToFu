package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.SshEndpoint
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ProfileFormTest {

    private val goodUrl = "https://abc12345-vscode-dc1.codelab.example.com/proxy/15000/"

    @Test
    fun valid_form_passes() {
        val r = ProfileForm.validate(
            alias = "dc1", baseUrl = goodUrl,
            authType = AuthType.CODE_SERVER_PASSWORD, secret = "pw",
            existingAliases = emptySet(),
        )
        assertTrue(r.errors.toString(), r.ok)
    }

    @Test
    fun blank_alias_and_url_flagged() {
        val r = ProfileForm.validate("", "", AuthType.NONE, "", emptySet())
        assertTrue(r.errors.containsKey("alias"))
        assertTrue(r.errors.containsKey("baseUrl"))
    }

    @Test
    fun duplicate_alias_flagged_but_not_when_editing_self() {
        val taken = setOf("dc1", "prod")
        assertFalse(
            ProfileForm.validate("dc1", goodUrl, AuthType.NONE, "", taken).ok,
        )
        // Editing the same profile (editingAlias == alias) is allowed.
        assertTrue(
            ProfileForm.validate("dc1", goodUrl, AuthType.NONE, "", taken,
                editingAlias = "dc1").ok,
        )
    }

    @Test
    fun non_absolute_url_flagged() {
        val r = ProfileForm.validate("x", "not-a-url", AuthType.NONE, "", emptySet())
        assertTrue(r.errors.containsKey("baseUrl"))
    }

    @Test
    fun blank_password_permits_interactive_gateway_sign_in() {
        val r = ProfileForm.validate("x", goodUrl,
            AuthType.CODE_SERVER_PASSWORD, secret = "", existingAliases = emptySet())
        assertTrue(r.ok)
    }

    @Test
    fun blank_password_ok_when_editing_with_stored_secret() {
        val r = ProfileForm.validate("x", goodUrl,
            AuthType.CODE_SERVER_PASSWORD, secret = "", existingAliases = emptySet(),
            secretAlreadyStored = true)
        assertFalse(r.errors.containsKey("secret"))
    }

    @Test
    fun no_password_needed_for_sso_or_none() {
        assertTrue(ProfileForm.validate("x", goodUrl, AuthType.NONE, "", emptySet()).ok)
        assertTrue(ProfileForm.validate("x", goodUrl, AuthType.INTERACTIVE_SSO, "", emptySet()).ok)
    }

    @Test
    fun toProfile_extracts_instance_uuid() {
        val p = ProfileForm.toProfile(0, "dc1", goodUrl, AuthType.CODE_SERVER_PASSWORD, 0)
        assertEquals("abc12345", p.instanceUuid)
    }

    private fun ssh(
        alias: String = "box", host: String = "dev.example.com", port: String = "22",
        user: String = "me", remotePort: String = "15000", taken: Set<String> = emptySet(), editing: String? = null,
    ) = ProfileForm.validateSsh(alias, host, port, user, remotePort, taken, editing)

    @Test
    fun ssh_form_passes_with_defaults_and_ipv6_hosts() {
        assertTrue(ssh().errors.toString(), ssh().ok)
        assertTrue(ssh(host = "fe80::1").ok)
        assertTrue(ssh(host = " 10.0.0.5 ").ok)
    }

    @Test
    fun ssh_form_requires_name_host_and_user() {
        assertEquals(setOf("alias", "host", "user"), ssh(alias = " ", host = "", user = "").errors.keys)
    }

    @Test
    fun ssh_host_and_user_take_only_their_own_part() {
        assertTrue(ssh(host = "me@dev.example.com").errors.containsKey("host"))
        assertTrue(ssh(host = "ssh://dev.example.com").errors.containsKey("host"))
        assertEquals("Put the port in its own field", ssh(host = "dev.example.com:2222").errors["host"])
        assertTrue(ssh(user = "me@dev").errors.containsKey("user"))
        assertTrue(ssh(user = "two words").errors.containsKey("user"))
    }

    @Test
    fun ssh_ports_must_be_tcp_ports() {
        for (bad in listOf("", "0", "65536", "22a", "-1")) {
            assertTrue(bad, ssh(port = bad).errors.containsKey("port"))
            assertTrue(bad, ssh(remotePort = bad).errors.containsKey("remotePort"))
        }
        assertTrue(ssh(port = "65535", remotePort = "1").ok)
    }

    @Test
    fun ssh_alias_shares_the_profile_namespace() {
        assertFalse(ssh(taken = setOf("box")).ok)
        assertTrue(ssh(taken = setOf("box"), editing = "box").ok)
    }

    @Test
    fun ssh_endpoint_is_trimmed_and_never_pinned() {
        assertEquals(
            SshEndpoint(host = "dev.example.com", port = 2222, user = "me", hostKeySha256 = null, remotePort = 15001),
            ProfileForm.toSshEndpoint(" dev.example.com ", " 2222", "me ", "15001"),
        )
    }
}
