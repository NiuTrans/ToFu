package com.tofu.client.session

import com.tofu.client.data.AuthType
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
    fun password_required_for_codeserver_when_no_stored_secret() {
        val r = ProfileForm.validate("x", goodUrl,
            AuthType.CODE_SERVER_PASSWORD, secret = "", existingAliases = emptySet())
        assertTrue(r.errors.containsKey("secret"))
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
}
