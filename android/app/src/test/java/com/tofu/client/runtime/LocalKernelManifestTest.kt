package com.tofu.client.runtime

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import java.util.Properties
import javax.xml.parsers.DocumentBuilderFactory

/**
 * The merged manifest AGP produced for this variant (from the unit-test config
 * Gradle generates) and the backup rules it references. Gradle tier only.
 */
class LocalKernelManifestTest {
    private val androidNs = "http://schemas.android.com/apk/res/android"

    private fun parse(file: File): Element =
        DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }.newDocumentBuilder().parse(file).documentElement

    private fun mergedManifest(): Element {
        val config = Properties()
        javaClass.classLoader!!.getResourceAsStream("com/android/tools/test_config.properties")!!.use(config::load)
        return parse(File(config.getProperty("android_merged_manifest")))
    }

    private fun Element.children(tag: String): List<Element> {
        val nodes = getElementsByTagName(tag)
        return (0 until nodes.length).map { nodes.item(it) as Element }
    }

    private fun Element.android(name: String): String = getAttributeNS(androidNs, name)

    @Test
    fun the_service_is_a_special_use_foreground_service_with_its_subtype() {
        val service = mergedManifest().children("service").single { it.android("name") == LocalKernelService::class.java.name }
        assertEquals("false", service.android("exported"))
        assertEquals("specialUse", service.android("foregroundServiceType"))
        val property = service.children("property").single()
        assertEquals("android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE", property.android("name"))
        assertEquals("Runs the user's local Tofu agent server on 127.0.0.1", property.android("value"))
    }

    @Test
    fun the_permissions_backup_rules_and_native_library_extraction_are_declared() {
        val manifest = mergedManifest()
        val permissions = manifest.children("uses-permission").map { it.android("name") }.toSet()
        for (permission in listOf("FOREGROUND_SERVICE", "FOREGROUND_SERVICE_SPECIAL_USE", "POST_NOTIFICATIONS", "WAKE_LOCK")) {
            assertTrue(permission, "android.permission.$permission" in permissions)
        }
        val application = manifest.children("application").single()
        assertEquals("false", application.android("allowBackup"))
        assertEquals("@xml/data_extraction_rules", application.android("dataExtractionRules"))
        // AGP writes useLegacyPackaging into the packaged manifest: proot must be extracted into nativeLibraryDir.
        assertEquals("true", application.android("extractNativeLibs"))
    }

    @Test
    fun every_domain_is_excluded_from_cloud_backup_and_device_transfer() {
        val rules = parse(File("src/main/res/xml/data_extraction_rules.xml"))
        val domains = setOf("root", "file", "database", "sharedpref", "external", "device_root", "device_file", "device_database", "device_sharedpref")
        for (section in listOf("cloud-backup", "device-transfer")) {
            val block = rules.children(section).single()
            assertTrue("$section has no includes", block.children("include").isEmpty())
            val excluded = block.children("exclude").filter { it.getAttribute("path") == "." }.map { it.getAttribute("domain") }.toSet()
            assertEquals(section, domains, excluded)
        }
    }
}
