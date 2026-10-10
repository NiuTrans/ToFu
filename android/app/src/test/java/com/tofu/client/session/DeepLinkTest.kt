package com.tofu.client.session

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Pins the onboarding payload parser (scan / paste / deep link). */
class DeepLinkTest {

    @Test
    fun bare_http_url_is_accepted_without_a_name() {
        assertEquals(
            DeepLink.Connect("https://host.test/proxy/15000/", null),
            DeepLink.parse("https://host.test/proxy/15000/"),
        )
        assertEquals(
            DeepLink.Connect("http://192.168.1.20:8080", null),
            DeepLink.parse("  http://192.168.1.20:8080  "),
        )
    }

    @Test
    fun connect_intent_carries_url_and_name() {
        val payload = "tofu://connect?url=https%3A%2F%2Fhost.test%2Fproxy%2F15000%2F" +
            "&name=Shanghai%20sandbox"
        assertEquals(
            DeepLink.Connect("https://host.test/proxy/15000/", "Shanghai sandbox"),
            DeepLink.parse(payload),
        )
    }

    @Test
    fun connect_intent_without_a_valid_url_is_rejected() {
        assertNull(DeepLink.parse("tofu://connect?name=NoUrl"))
        assertNull(DeepLink.parse("tofu://connect?url=ftp://host.test"))
        assertNull(DeepLink.parse("tofu://connect?url=not-a-url"))
    }

    @Test
    fun unrelated_payloads_are_rejected() {
        assertNull(DeepLink.parse(null))
        assertNull(DeepLink.parse(""))
        assertNull(DeepLink.parse("just some text"))
        assertNull(DeepLink.parse("WIFI:S:home;T:WPA;P:secret;;"))
        assertNull(DeepLink.parse("https://"))
    }

    @Test
    fun http_url_validation_matches_server_parse() {
        org.junit.Assert.assertTrue(DeepLink.isHttpUrl("https://a.test/"))
        org.junit.Assert.assertFalse(DeepLink.isHttpUrl("mailto:a@b.test"))
        org.junit.Assert.assertFalse(DeepLink.isHttpUrl("https://"))
    }
}
