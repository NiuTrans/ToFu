package com.tofu.client

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element

/** The zh-rCN UI never falls back to English: each translatable default string has a translation. */
class StringResourcesTranslationTest {

    private fun translatableNames(directory: String): Set<String> =
        File("src/main/res/$directory").listFiles { file: File -> file.extension == "xml" }.orEmpty()
            .flatMap { file ->
                val children = DocumentBuilderFactory.newInstance().newDocumentBuilder()
                    .parse(file).documentElement.childNodes
                (0 until children.length).map { children.item(it) }.filterIsInstance<Element>()
                    .filter { it.tagName in RESOURCE_TAGS && it.getAttribute("translatable") != "false" }
                    .map { it.getAttribute("name") }
            }.toSet()

    @Test
    fun every_default_string_has_a_chinese_translation() {
        val defaults = translatableNames("values")
        assertTrue("no default strings found under src/main/res/values", defaults.isNotEmpty())
        assertEquals("shown in English to zh-rCN users", emptySet<String>(),
            defaults - translatableNames("values-zh-rCN"))
    }

    private companion object {
        val RESOURCE_TAGS = setOf("string", "plurals", "string-array")
    }
}
