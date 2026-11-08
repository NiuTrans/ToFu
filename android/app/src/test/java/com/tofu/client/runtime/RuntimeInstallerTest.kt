package com.tofu.client.runtime

import android.app.Application
import com.tofu.client.runtime.TarFixture.Item
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Paths
import java.nio.file.attribute.PosixFilePermissions
import java.util.concurrent.CancellationException

/**
 * The installer against real archives: SHA-256 refusal before extraction,
 * manifest-checked extraction (symlinks, hard links, modes), the atomic
 * `current` switch on commit, last-known-good, and rootfs replacement only on
 * a version change. Robolectric supplies the platform's org.json.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], application = Application::class)
class RuntimeInstallerTest {
    private lateinit var root: File
    private lateinit var layout: KernelLayout
    private lateinit var assets: File

    private val longPath = "usr/share/doc/" + "a-deliberately-long-directory-name/".repeat(3) + "file-past-the-ustar-name-limit.txt"
    private val rootfsItems = listOf(
        Item.Dir("usr"),
        Item.Dir("usr/bin"),
        Item.Regular("usr/bin/git", "#!git".toByteArray(), mode = 0b111_101_101),
        Item.Dir("usr/lib"),
        Item.Dir("usr/lib/git-core"),
        Item.Regular("usr/lib/git-core/git", "git-binary".toByteArray(), mode = 0b111_101_101),
        Item.Hardlink("usr/lib/git-core/git-add", "usr/lib/git-core/git", mode = 0b111_101_101),
        Item.Symlink("bin", "usr/bin"),
        Item.Symlink("etc-alternatives-vi", "/etc/alternatives/vi"),
        Item.Dir("etc"),
        Item.Regular("etc/os-release", "NAME=Ubuntu\n".toByteArray()),
        Item.Dir("root-only", mode = 0b111_000_000),
        Item.Regular("usr/bin/sudo", "#!sudo".toByteArray(), mode = 0b100_111_101_101), // 4755
        Item.Dir("tmp", mode = 0b001_111_111_111), // 1777
        Item.Dir("usr/share"),
        Item.Dir("usr/share/doc"),
        Item.Regular(longPath, "long".toByteArray()),
    )

    private fun payloadItems(version: String) = listOf(
        Item.Dir("bin"),
        Item.Regular("bin/tofu-local-kernel", "#!/bin/sh\nexec python3 local_kernel.py \"$@\"\n".toByteArray(), mode = 0b111_101_101),
        Item.Regular("local_kernel.py", "# payload $version\n".toByteArray()),
    )

    @Before
    fun setUp() {
        root = Files.createTempDirectory("installer").toFile()
        layout = KernelLayout(File(root, "files"))
        assets = File(root, "apk-assets/kernel").apply { mkdirs() }
    }

    @After
    fun tearDown() = RuntimeInstaller.removeTree(root)

    private fun publish(role: String, version: String, items: List<Item>, manifest: String = TarFixture.manifest(items)): RuntimeArchive {
        val bytes = TarFixture.tarGz(items, manifest)
        val file = "$role-$version.tgz"
        File(assets, file).writeBytes(bytes)
        return RuntimeArchive(version, file, TarFixture.sha256(bytes), bytes.size.toLong())
    }

    private fun installer() = RuntimeInstaller(layout, DirectoryAssets(assets.parentFile!!))

    private fun index(rootfs: String = "24.04.1", payload: String) =
        RuntimeIndex(publish("rootfs", rootfs, rootfsItems), publish("payload", payload, payloadItems(payload)))

    private fun permissions(file: File) = PosixFilePermissions.toString(Files.getPosixFilePermissions(file.toPath(), LinkOption.NOFOLLOW_LINKS))

    @Test
    fun refuses_a_sha256_or_size_mismatch_before_extracting() {
        val good = index(payload = "v1")
        try {
            installer().prepare(RuntimeIndex(good.rootfs.copy(sha256 = "f".repeat(64)), good.payload))
            fail("expected a refusal")
        } catch (expected: RuntimeInstallException) {
            assertTrue(expected.message!!.startsWith("refusing rootfs-24.04.1.tgz"))
            assertFalse(layout.rootfs.exists())
            assertFalse(layout.rootfsStaging.exists())
        }
        for (bad in listOf(good.payload.copy(sha256 = "0".repeat(64)), good.payload.copy(size = good.payload.size + 1))) {
            try {
                installer().prepare(RuntimeIndex(good.rootfs, bad))
                fail("expected a refusal")
            } catch (expected: RuntimeInstallException) {
                assertTrue(expected.message!!.startsWith("refusing payload-v1.tgz"))
            }
            assertFalse("nothing extracted", layout.payload("v1").exists())
            assertTrue("no staging left", layout.payloadRoot.list()!!.none { it.startsWith(".staging") })
        }
    }

    @Test
    fun extracts_files_symlinks_hard_links_and_modes() {
        val launch = installer().prepare(index(payload = "v1"))
        assertEquals(PayloadLaunch("v1", layout.payload("v1"), pending = true), launch)
        assertNull("current moves only on commit", installer().currentVersion())

        val rootfs = layout.rootfs
        assertEquals("#!git", File(rootfs, "usr/bin/git").readText())
        assertEquals("rwxr-xr-x", permissions(File(rootfs, "usr/bin/git")))
        assertEquals("rw-r--r--", permissions(File(rootfs, "etc/os-release")))
        assertEquals("rwx------", permissions(File(rootfs, "root-only")))
        assertEquals("setuid is matched against the manifest, then dropped", "rwxr-xr-x", permissions(File(rootfs, "usr/bin/sudo")))
        assertEquals("sticky is matched against the manifest, then dropped", "rwxrwxrwx", permissions(File(rootfs, "tmp")))
        assertEquals(Paths.get("usr/bin"), Files.readSymbolicLink(File(rootfs, "bin").toPath()))
        assertEquals("absolute targets resolve inside the guest", Paths.get("/etc/alternatives/vi"), Files.readSymbolicLink(File(rootfs, "etc-alternatives-vi").toPath()))
        val hardLink = File(rootfs, "usr/lib/git-core/git-add").toPath()
        assertEquals("hard link materialized as a relative symlink", Paths.get("git"), Files.readSymbolicLink(hardLink))
        assertEquals("git-binary", hardLink.toFile().readText())
        assertEquals("long", File(rootfs, longPath).readText())
        assertEquals(TarFixture.MTIME * 1000, File(rootfs, "etc/os-release").lastModified())
        assertEquals("24.04.1", layout.rootfsVersionFile.readText())
        assertEquals("#!/bin/sh\nexec python3 local_kernel.py \"$@\"\n", File(layout.payload("v1"), "bin/tofu-local-kernel").readText())
        assertEquals("rwxr-xr-x", permissions(File(layout.payload("v1"), "bin/tofu-local-kernel")))
        for (directory in listOf(layout.data, layout.home, layout.prootTmp, layout.shm)) assertTrue(directory.isDirectory)
    }

    @Test
    fun scratch_directories_are_emptied_before_each_generation_and_data_is_kept() {
        installer().prepare(index(payload = "v1"))
        File(layout.prootTmp, "proot-1234-glue").mkdirs()
        File(layout.shm, "sem.mp-abc").writeText("stale")
        File(layout.data, "tofu.db").writeText("durable")
        File(layout.home, ".gitconfig").writeText("durable")

        installer().prepare(index(payload = "v1"))
        assertTrue(layout.prootTmp.list()!!.isEmpty())
        assertTrue(layout.shm.list()!!.isEmpty())
        assertEquals("durable", File(layout.data, "tofu.db").readText())
        assertEquals("durable", File(layout.home, ".gitconfig").readText())
    }

    @Test
    fun commit_switches_current_atomically_and_keeps_the_last_known_good() {
        installer().prepare(index(payload = "v1"))
        installer().commit("v1")
        assertTrue(Files.isSymbolicLink(layout.currentPointer.toPath()))
        assertEquals("v1", installer().currentVersion())
        assertEquals(PayloadLaunch("v1", layout.payload("v1"), pending = false), installer().prepare(index(payload = "v1")))

        val upgrade = installer().prepare(index(payload = "v2"))
        assertEquals(PayloadLaunch("v2", layout.payload("v2"), pending = true), upgrade)
        assertEquals("the running pointer is untouched until v2 proves ready", "v1", installer().currentVersion())

        installer().commit("v2")
        assertEquals("v2", installer().currentVersion())
        assertEquals("v1", installer().previousVersion())
        assertTrue(layout.payload("v1").isDirectory)

        installer().prepare(index(payload = "v3"))
        installer().commit("v3")
        assertEquals("v3", installer().currentVersion())
        assertEquals("v2", installer().previousVersion())
        assertFalse("payloads older than last-known-good are pruned", layout.payload("v1").exists())
        assertEquals(
            setOf("v2", "v3", KernelLayout.CURRENT, KernelLayout.PREVIOUS),
            layout.payloadRoot.list()!!.toSet(),
        )
    }

    @Test
    fun a_rejected_payload_falls_back_to_the_last_known_good() {
        installer().prepare(index(payload = "v1"))
        installer().commit("v1")
        installer().prepare(index(payload = "v2"))

        installer().reject("v2")
        File(layout.payload("v2"), "local_kernel.py").writeText("marker: not re-extracted")
        assertEquals(PayloadLaunch("v1", layout.payload("v1"), pending = false), installer().prepare(index(payload = "v2")))
        assertEquals("marker: not re-extracted", File(layout.payload("v2"), "local_kernel.py").readText())

        installer().prepare(index(payload = "v3"))
        installer().commit("v3")
        assertFalse(layout.rejectedMarker("v2").exists())
    }

    @Test
    fun rejecting_the_only_payload_keeps_retrying_it() {
        installer().prepare(index(payload = "v1"))
        installer().reject("v1")
        assertEquals(PayloadLaunch("v1", layout.payload("v1"), pending = true), installer().prepare(index(payload = "v1")))
    }

    @Test
    fun the_rootfs_is_replaced_only_when_its_version_changes() {
        installer().prepare(index(rootfs = "24.04.1", payload = "v1"))
        val userInstalled = File(layout.rootfs, "usr/bin/node").apply { writeText("apt-installed") }

        installer().prepare(index(rootfs = "24.04.1", payload = "v2"))
        assertTrue("same pinned rootfs: user changes survive", userInstalled.isFile)

        installer().prepare(index(rootfs = "24.04.2", payload = "v2"))
        assertFalse("a new pinned rootfs replaces the old one", userInstalled.exists())
        assertEquals("24.04.2", layout.rootfsVersionFile.readText())
        assertFalse(layout.rootfsRetired.exists())
        assertTrue(File(layout.rootfs, "usr/bin/git").isFile)
    }

    @Test
    fun an_archive_that_disagrees_with_its_manifest_is_refused() {
        val items = payloadItems("v1")
        val cases = mapOf(
            "content" to TarFixture.manifest(items.map { if (it.path == "local_kernel.py") Item.Regular(it.path, "other".toByteArray()) else it }),
            "mode" to TarFixture.manifest(items.map { if (it is Item.Regular && it.path == "local_kernel.py") Item.Regular(it.path, it.content, mode = 0b111_111_111) else it }),
            "missing entry" to TarFixture.manifest(items + Item.Regular("absent.py", ByteArray(0))),
            "extra entry" to TarFixture.manifest(items.filter { it.path != "local_kernel.py" }),
            "type" to TarFixture.manifest(items.map { if (it.path == "bin") Item.Regular("bin", ByteArray(0)) else it }),
        )
        val rootfs = publish("rootfs", "24.04.1", rootfsItems)
        for ((case, manifest) in cases) {
            val payload = publish("payload", "v1", items, manifest)
            try {
                installer().prepare(RuntimeIndex(rootfs, payload))
                fail("expected a refusal for $case")
            } catch (expected: RuntimeInstallException) {
                assertFalse(case, layout.payload("v1").exists())
                assertTrue(case, layout.payloadRoot.list()!!.none { it.startsWith(".staging") })
            }
        }
    }

    @Test
    fun the_manifest_is_read_in_the_producers_format() {
        // An excerpt in android/runtime/README.md §2's exact form: one line, sorted
        // keys, `mode` as four octal digits, extra top-level keys, dirs and
        // symlinks without size/sha256, files without a target.
        val sha = "0123456789abcdef".repeat(4)
        val excerpt = """{"abi":"arm64-v8a","entries":[""" +
            """{"mode":"0755","path":"bin","type":"dir"},""" +
            """{"mode":"0755","path":"bin/tofu-local-kernel","sha256":"$sha","size":1234,"type":"file"},""" +
            """{"mode":"0777","path":"python/bin/python3","target":"python3.12","type":"symlink"},""" +
            """{"mode":"4755","path":"usr/bin/sudo","sha256":"$sha","size":7,"type":"file"},""" +
            """{"mode":"1777","path":"tmp","type":"dir"}""" +
            """],"format":1,"inputs":{"ubuntu":"24.04.5"},"kind":"rootfs","version":"24.04.5-0123456789ab"}""" + "\n"

        val entries = RuntimeManifest.parse(excerpt).entries
        assertEquals(RuntimeManifest.Entry(TarReader.Type.DIRECTORY, 0b111_101_101, 0, null, null), entries["bin"])
        assertEquals(RuntimeManifest.Entry(TarReader.Type.FILE, 0b111_101_101, 1234, sha, null), entries["bin/tofu-local-kernel"])
        assertEquals(RuntimeManifest.Entry(TarReader.Type.SYMLINK, 0b111_111_111, 0, null, "python3.12"), entries["python/bin/python3"])
        assertEquals("setuid", RuntimeManifest.Entry(TarReader.Type.FILE, 0b100_111_101_101, 7, sha, null), entries["usr/bin/sudo"])
        assertEquals("sticky", RuntimeManifest.Entry(TarReader.Type.DIRECTORY, 0b001_111_111_111, 0, null, null), entries["tmp"])
        assertEquals(5, entries.size)

        // Anything but a JSON string of four octal digits is a malformed manifest, never a silent mode mismatch.
        for (bad in listOf("\"755\"", "\"0855\"", "\"0x1ed\"", "\"+755\"", "493", "755")) {
            try {
                RuntimeManifest.parse("""{"entries":[{"mode":$bad,"path":"bin","type":"dir"}],"format":1}""")
                fail("accepted mode $bad")
            } catch (expected: RuntimeInstallException) {
                assertTrue(expected.message!!, expected.message!!.contains("mode"))
            }
        }
    }

    @Test
    fun unsafe_paths_and_writes_through_symlinks_are_refused() {
        val rootfs = publish("rootfs", "24.04.1", rootfsItems)
        val escapes = listOf(
            listOf(Item.Regular("../escape", "x".toByteArray())),
            listOf(Item.Symlink("link", root.path), Item.Regular("link/planted", "x".toByteArray())),
            listOf(Item.Regular("data", "x".toByteArray()), Item.Hardlink("alias", "../../outside")),
        )
        for (items in escapes) {
            val payload = publish("payload", "v1", items)
            try {
                installer().prepare(RuntimeIndex(rootfs, payload))
                fail("expected a refusal for ${items.map { it.path }}")
            } catch (expected: IOException) {
                assertFalse(File(root, "planted").exists())
                assertFalse(layout.payload("v1").exists())
            }
        }
    }

    @Test
    fun a_cancelled_install_removes_its_staging_directory() {
        try {
            installer().prepare(index(payload = "v1")) { true }
            fail("expected cancellation")
        } catch (expected: CancellationException) {
            assertFalse(layout.rootfsStaging.exists())
            assertFalse(layout.rootfs.exists())
        }
    }

    @Test
    fun the_index_is_validated_before_any_io() {
        val valid = """{"format":1,"abi":"arm64-v8a","rootfs":{"version":"24.04.1","file":"rootfs-24.04.1.tgz","sha256":"${"a".repeat(64)}","size":10},"payload":{"version":"v1","file":"payload-v1.tgz","sha256":"${"b".repeat(64)}","size":20}}"""
        val index = RuntimeIndex.parse(valid)
        assertEquals(RuntimeArchive("v1", "payload-v1.tgz", "b".repeat(64), 20), index.payload)
        val invalid = listOf(
            valid.replace("\"format\":1", "\"format\":2"),
            valid.replace("arm64-v8a", "x86_64"),
            valid.replace("\"version\":\"v1\"", "\"version\":\"current\""),
            valid.replace("\"version\":\"v1\"", "\"version\":\"../v1\""),
            valid.replace("payload-v1.tgz", "../payload-v1.tgz"),
            valid.replace("payload-v1.tgz", "payload-v1.tar.gz"), // AGP would gunzip a *.gz asset
            valid.replace("b".repeat(64), "B".repeat(64)),
            valid.replace("\"size\":20", "\"size\":0"),
            "{}",
        )
        for (text in invalid) {
            try {
                RuntimeIndex.parse(text)
                fail("accepted $text")
            } catch (expected: RuntimeInstallException) {
                // refused before any extraction
            }
        }
        File(assets, "index.json").writeText(valid)
        assertEquals(index, installer().readIndex())
    }
}
