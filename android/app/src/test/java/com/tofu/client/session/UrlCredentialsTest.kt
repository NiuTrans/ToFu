package com.tofu.client.session

import com.tofu.client.data.AuthType
import org.junit.Assert.*
import org.junit.Test
import java.net.URLEncoder

/** No entry path may smuggle URL credentials into Room or a native display label. */
class UrlCredentialsTest {
    private val base = "https://[::1]:8443/dev/absproxy/15005/"

    @Test fun secret_query_and_spa_fragment_parameters_are_rejected_with_safe_errors() {
        for (suffix in listOf(
            "?token=fixture-value", "?ACCESS_TOKEN=fixture-value", "?api%5Fkey=fixture-value",
            "?password=fixture-value", "?client_secret=fixture-value", "?code=fixture-value",
            "#access_token=fixture-value", "#/chat?refresh_token=fixture-value",
            "?next=%2Flogin%3Ftoken%3Dfixture-value",
            "?next=%2F%2Ffixture-user%3Afixture-value%40host.test%2F",
            "#/chat?next=%2F%2Ffixture-user%3Afixture-value%40host.test%2F",
            "#next=//fixture-user:fixture-value@host.test/",
            "?next=https%3A%2F%2Ffixture-user%3Afixture-value%40host.test%2F",
            "?X-Api-Key=fixture-value", "?tofu_session=fixture-value",
            "?next=https%3A%2F%2Fhost.test%2F%3FX-Api-Key%3Dfixture-value",
            "?X-Amz-Credential=fixture-value", "?X-Amz-Signature=fixture-value",
            "?X-Goog-Credential=fixture-value", "?X-Goog-Signature=fixture-value",
            "?next=%2574%256f%256b%2565%256e%253Dfixture-value",
            "?next=%7B%22access_token%22%3A%22fixture-value%22%7D",
            "?return=https%3A%5C%5Cfixture-user%3Afixture-value%40host.test%2F",
            "?return=https%3A%5Cfixture-user%3Afixture-value%40host.test%2F",
            "?return=https%3A%2F%5Cfixture-user%3Afixture-value%40host.test%2F",
            "?return=https%3A%2Ffixture-user%3Afixture-value%40host.test%2F",
            "?return=https%3Afixture-user%3Afixture-value%40host.test%2F",
            "?return=%2F%2F%2Ffixture-user%3Afixture-value%40host.test%2F",
            "#return=https%3A%2F%2F%2F%2Ffixture-user%3Afixture-value%40host.test%2F",
            "?return=https%3A%2F%2Ffixture%09user%3Afixture-value%40host.test%2F",
            "?return=https%3A%2F%2Ffixture%20user%3Afixture-value%40host.test%2F",
            "?return=https%3A%2F%2Ffixture%252Fuser%3Afixture-value%40host.test%2F",
            "?label=fixture%0Avalue", "?label=fixture%7Fvalue",
            "?pass=fixture-value", "?ticket=fixture-value", "?oauth_code=fixture-value",
            "?auth_code=fixture-value", "?authorization_code=fixture-value", "?csrf=fixture-value",
            "?jwt=fixture-value", "?saml_response=fixture-value", "?access_key=fixture-value", "?private_key=fixture-value",
        )) {
            val url = base + suffix
            val result = ProfileForm.validate("test", url, AuthType.NONE, "", emptySet())
            assertFalse("credential suffix must not pass validation", result.ok)
            val error = result.errors["baseUrl"].orEmpty()
            assertTrue("error should explain how to correct the URL", error.contains("credential", ignoreCase = true))
            assertFalse("error must not echo a credential", error.contains("fixture-value"))
            assertNull("QR/paste must apply the same rule", DeepLink.parse(url))
            assertNull("encoded connect intents must apply the same rule", DeepLink.parse(
                "tofu://connect?url=" + URLEncoder.encode(url, "UTF-8")))
        }
    }

    @Test fun legacy_sanitization_preserves_nonsecret_route_fields_and_proxy_coordinates() {
        assertEquals(base + "?workspace=main#/chat?conversation=123&view=compact", ServerUrl.sanitizeForStorage(
            base + "?token=fixture-value&workspace=main#/chat?conversation=123&access_token=fixture-value&view=compact"))
        assertEquals("https://host.test:8443/dev/proxy/15005/?theme=dark#anchor", ServerUrl.sanitizeForStorage(
            "https://fixture-user:fixture-value@host.test:8443/dev/proxy/15005/?theme=dark#anchor"))
        assertEquals(base + "?label=50%25#token", ServerUrl.sanitizeForStorage(base + "?label=50%25#token"))
        assertEquals(base + "?theme=dark", ServerUrl.sanitizeForStorage(
            base + "?next=%7B%22access_token%22%3A%22fixture-value%22%7D&theme=dark"))
        assertEquals(base + "?theme=dark", ServerUrl.sanitizeForStorage(
            base + "?return=https%3A%5C%5Cfixture-user%3Afixture-value%40host.test%2F&theme=dark"))
    }

    @Test fun deeply_encoded_or_path_embedded_credentials_fail_closed() {
        var value = "token=fixture-value"
        repeat(8) { value = URLEncoder.encode(value, "UTF-8") }
        assertNull(DeepLink.parse(base + "?next=" + value))
        assertFalse(ProfileForm.validate("test", base + "%3Ftoken%3Dfixture-value", AuthType.NONE, "", emptySet()).ok)
    }

    @Test fun userinfo_is_rejected_without_echoing_it() {
        val result = ProfileForm.validate("test", "https://fixture-user:fixture-value@host.test/", AuthType.NONE, "", emptySet())
        assertFalse(result.ok)
        assertTrue(result.errors["baseUrl"].orEmpty().contains("credential", ignoreCase = true))
        assertFalse(result.errors.toString().contains("fixture-value"))
        assertNull(DeepLink.parse("https://fixture-user:fixture-value@host.test/"))
    }

    @Test fun persistence_checks_the_url_even_without_prior_ui_validation() {
        try {
            ProfileForm.toProfile(0, "test", base + "?token=fixture-value", AuthType.NONE, 0)
            fail("toProfile must enforce the storage boundary itself")
        } catch (error: IllegalArgumentException) {
            assertFalse(error.message.orEmpty().contains("fixture-value"))
        }
    }

    @Test fun nonsecret_query_fragment_and_proxy_coordinates_survive_all_entry_paths() {
        val url = base + "?workspace=main&theme=dark&tag=one&tag=two#/chat?conversation=123&view=compact"
        assertTrue(ProfileForm.validate("test", url, AuthType.NONE, "", emptySet()).ok)
        assertEquals(url, ProfileForm.toProfile(0, "test", url, AuthType.NONE, 0).baseUrl)
        assertEquals(DeepLink.Connect(url, null), DeepLink.parse(url))
        assertEquals(DeepLink.Connect(url, "test"), DeepLink.parse("tofu://connect?url=" +
            URLEncoder.encode(url, "UTF-8") + "&name=test"))
        val server = ServerUrl.parse(url)!!
        assertEquals("https://[::1]:8443/dev/login", server.loginUrl)
        assertEquals("https://[::1]:8443/dev/absproxy/15005/api/health", server.endpoint("api/health"))
    }

    @Test fun connect_requires_the_declared_route_and_unambiguous_parameters() {
        assertNull(DeepLink.parse("tofu://delete?url=https://host.test/"))
        assertNull(DeepLink.parse("tofu://connect?url=https://a.test/&url=https://b.test/"))
        assertNull(DeepLink.parse("tofu://connect?url=https://host.test/&token=fixture-value"))
    }

    @Test fun invalid_and_credential_bearing_addresses_are_never_echoed_as_labels() {
        for (url in listOf("https://fixture-user:fixture-value@host.test/", "not a URL?token=fixture-value")) {
            assertFalse(ServerUrl.displayLabel(url).contains("fixture-value"))
        }
    }
}
