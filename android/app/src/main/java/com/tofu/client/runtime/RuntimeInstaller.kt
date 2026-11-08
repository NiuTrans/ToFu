package com.tofu.client.runtime

import java.io.BufferedInputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.StandardCopyOption
import java.nio.file.attribute.FileTime
import java.nio.file.attribute.PosixFilePermission
import java.security.MessageDigest
import java.util.concurrent.CancellationException
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPInputStream

/** Read access to the APK's `assets/kernel/` (an AssetManager on device, a directory in tests). */
interface RuntimeAssets {
    /** Null when the build carries no such asset. */
    fun open(name: String): InputStream?
}

/** Which payload the next launch runs. [pending] = not yet committed by a successful readiness probe. */
data class PayloadLaunch(val version: String, val directory: File, val pending: Boolean)

/**
 * Installs the SHA-256-pinned runtime shipped in `assets/kernel/` into
 * [KernelLayout] and owns the payload pointers.
 *
 * - Each archive's size and SHA-256 are verified against the index BEFORE
 *   extraction; a mismatch is refused and nothing is extracted.
 * - Extraction goes to a staging directory, is checked entry-by-entry against
 *   the archive's `.manifest.json` (type, mode, size, SHA-256, link target, no
 *   extra or missing paths) and only then renamed into place.
 * - Android forbids hard links for apps (AOSP sepolicy `app_neverallows.te`:
 *   "neverallow all_untrusted_apps file_type:file link"), so a hard-link entry
 *   becomes a relative symlink to its target, the same emulation proot's
 *   `--link2symlink` applies at run time. Permission bits come from the archive;
 *   set-id/sticky bits are not representable through java.nio and are inert on
 *   `/data` (mounted nosuid) and under proot.
 * - A new payload is launched as *pending*. Only [commit] — called after the
 *   new kernel answered `/api/v4/meta` — switches `current` (one atomic
 *   rename of a symlink) and keeps the replaced payload as `previous`, the
 *   last-known-good. [reject] retires a pending payload that never became
 *   ready, so the next launch runs `current` again.
 * - The rootfs is replaced only when its pinned version changes; packages the
 *   user installed with apt are then lost (documented in the README).
 */
class RuntimeInstaller(private val layout: KernelLayout, private val assets: RuntimeAssets) {

    fun readIndex(): RuntimeIndex? {
        val stream = assets.open("${RuntimeIndex.ASSET_DIR}/${RuntimeIndex.INDEX_FILE}") ?: return null
        return RuntimeIndex.parse(stream.use { it.readBytes().toString(Charsets.UTF_8) })
    }

    /**
     * Make the runtime of [index] usable and describe the payload to launch.
     * Called only while no kernel generation runs, so proot's scratch and the
     * guest's `/dev/shm` are emptied of anything a killed generation left.
     * [cancelled] is polled between archive entries so a Stop during a long
     * first install takes effect promptly (the staging directory is removed).
     */
    fun prepare(index: RuntimeIndex, cancelled: () -> Boolean = { false }): PayloadLaunch {
        for (directory in listOf(layout.root, layout.payloadRoot, layout.data, layout.home, layout.prootTmp, layout.shm)) {
            if (!directory.isDirectory && !directory.mkdirs()) throw RuntimeInstallException("cannot create $directory")
        }
        for (scratch in listOf(layout.prootTmp, layout.shm)) scratch.listFiles()?.forEach(::removeTree)
        removeTree(layout.rootfsStaging)
        removeTree(layout.rootfsRetired)
        layout.payloadRoot.listFiles()?.filter { it.name.startsWith(".") }?.forEach(::removeTree)

        installRootfs(index.rootfs, cancelled)

        val current = pointer(layout.currentPointer)
        val wanted = index.payload.version
        if (current == wanted) return PayloadLaunch(wanted, layout.payload(wanted), pending = false)
        if (current != null && layout.rejectedMarker(wanted).exists()) {
            return PayloadLaunch(current, layout.payload(current), pending = false)
        }
        if (!layout.payload(wanted).isDirectory) {
            val staging = layout.payloadStaging(wanted)
            extractVerified(index.payload, staging, cancelled)
            move(staging, layout.payload(wanted))
        }
        return PayloadLaunch(wanted, layout.payload(wanted), pending = true)
    }

    /** The payload the `current` pointer designates, if it still exists. */
    fun currentVersion(): String? = pointer(layout.currentPointer)

    /** The last-known-good payload kept by the latest commit. */
    fun previousVersion(): String? = pointer(layout.previousPointer)

    /**
     * Switch `current` to [version] after its kernel answered `/api/v4/meta`.
     * The replaced payload becomes `previous` (last-known-good); payloads that
     * are neither are deleted.
     */
    fun commit(version: String) {
        if (!layout.payload(version).isDirectory) throw RuntimeInstallException("payload $version is not installed")
        val replaced = pointer(layout.currentPointer)
        if (replaced == version) return
        if (replaced != null) repoint(layout.previousPointer, replaced)
        repoint(layout.currentPointer, version)
        layout.rejectedMarker(version).delete()
        val keep = setOfNotNull(version, replaced ?: pointer(layout.previousPointer), KernelLayout.CURRENT, KernelLayout.PREVIOUS)
        layout.payloadRoot.listFiles()?.forEach { entry ->
            val name = entry.name.removeSuffix(".rejected")
            if (name !in keep) removeTree(entry)
        }
    }

    /**
     * Retire a pending payload that never became ready. A no-op without a
     * committed `current`: with nothing to fall back to, the next launch
     * retries the same payload rather than stranding the user.
     */
    fun reject(version: String) {
        val current = pointer(layout.currentPointer) ?: return
        if (current == version) return
        layout.rejectedMarker(version).writeText("")
    }

    private fun installRootfs(archive: RuntimeArchive, cancelled: () -> Boolean) {
        val installed = layout.rootfsVersionFile.takeIf { it.isFile }?.readText()?.trim()
        if (installed == archive.version && layout.rootfs.isDirectory) return
        extractVerified(archive, layout.rootfsStaging, cancelled)
        if (layout.rootfs.exists()) move(layout.rootfs, layout.rootfsRetired)
        move(layout.rootfsStaging, layout.rootfs)
        val temporary = File(layout.root, ".rootfs.version.tmp")
        temporary.writeText(archive.version)
        move(temporary, layout.rootfsVersionFile, replace = true)
        removeTree(layout.rootfsRetired)
    }

    private fun extractVerified(archive: RuntimeArchive, staging: File, cancelled: () -> Boolean) {
        val name = "${RuntimeIndex.ASSET_DIR}/${archive.file}"
        verifyArchive(name, archive)
        removeTree(staging)
        if (!staging.mkdirs()) throw RuntimeInstallException("cannot create $staging")
        try {
            val stream = assets.open(name) ?: throw RuntimeInstallException("asset $name disappeared")
            GZIPInputStream(BufferedInputStream(stream, IO_BUFFER), IO_BUFFER).use { gzip ->
                Extraction(TarReader(gzip), staging.toPath(), cancelled).run()
            }
        } catch (error: Throwable) {
            removeTree(staging)
            throw error
        }
    }

    private fun verifyArchive(name: String, archive: RuntimeArchive) {
        val digest = MessageDigest.getInstance("SHA-256")
        var size = 0L
        val stream = assets.open(name) ?: throw RuntimeInstallException("asset $name is missing from this build")
        stream.use {
            val buffer = ByteArray(IO_BUFFER)
            while (true) {
                val read = it.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
                size += read
            }
        }
        val actual = hex(digest.digest())
        if (size != archive.size || actual != archive.sha256) {
            throw RuntimeInstallException(
                "refusing ${archive.file}: expected ${archive.size} bytes sha256=${archive.sha256}, " +
                    "found $size bytes sha256=$actual"
            )
        }
    }

    private class Extraction(private val tar: TarReader, private val root: Path, private val cancelled: () -> Boolean) {
        private val seen = HashSet<String>()
        private val files = HashMap<String, String>()
        private val directories = ArrayList<Pair<Path, TarReader.Entry>>()
        private val verifiedParents = HashSet<Path>()

        fun run() {
            val first = tar.next() ?: throw RuntimeInstallException("empty runtime archive")
            if (first.path != RuntimeManifest.NAME || first.type != TarReader.Type.FILE) {
                throw RuntimeInstallException("runtime archive must start with ${RuntimeManifest.NAME}")
            }
            val manifest = RuntimeManifest.parse(tar.content().readBytes().toString(Charsets.UTF_8))
            while (true) {
                if (cancelled()) throw CancellationException("runtime install cancelled")
                val entry = tar.next() ?: break
                if (entry.path.isEmpty()) continue
                val expected = manifest.entries[entry.path]
                    ?: throw RuntimeInstallException("archive entry ${entry.path} is not in the manifest")
                if (!seen.add(entry.path)) throw RuntimeInstallException("archive lists ${entry.path} twice")
                if (expected.type != entry.type) throw RuntimeInstallException("${entry.path}: manifest says ${expected.type}, archive has ${entry.type}")
                val target = root.resolve(entry.path)
                ensureParent(target)
                when (entry.type) {
                    TarReader.Type.DIRECTORY -> {
                        checkMode(entry, expected)
                        if (!Files.isDirectory(target, LinkOption.NOFOLLOW_LINKS)) Files.createDirectory(target)
                        directories += target to entry
                    }
                    TarReader.Type.FILE -> {
                        checkMode(entry, expected)
                        val sha256 = writeFile(target, entry)
                        if (entry.size != expected.size || sha256 != expected.sha256) {
                            throw RuntimeInstallException("${entry.path}: content does not match the manifest")
                        }
                        Files.setPosixFilePermissions(target, permissions(entry.mode))
                        Files.setLastModifiedTime(target, FileTime.from(entry.mtimeSeconds, TimeUnit.SECONDS))
                        files[entry.path] = sha256
                    }
                    TarReader.Type.SYMLINK -> {
                        if (entry.linkTarget.isEmpty() || entry.linkTarget != expected.target) {
                            throw RuntimeInstallException("${entry.path}: symlink target does not match the manifest")
                        }
                        Files.createSymbolicLink(target, Paths.get(entry.linkTarget))
                    }
                    TarReader.Type.HARDLINK -> {
                        val linked = TarReader.normalize(entry.linkTarget)
                        if (linked != expected.target?.let(TarReader::normalize) || linked !in files) {
                            throw RuntimeInstallException("${entry.path}: hard link must name an earlier regular file")
                        }
                        Files.createSymbolicLink(target, target.parent.relativize(root.resolve(linked)))
                    }
                }
            }
            val missing = manifest.entries.keys - seen
            if (missing.isNotEmpty()) throw RuntimeInstallException("archive is missing ${missing.size} manifest entries, e.g. ${missing.first()}")
            // Deepest first, so restrictive directory modes cannot block their children.
            for ((path, entry) in directories.sortedByDescending { it.first.nameCount }) {
                Files.setPosixFilePermissions(path, permissions(entry.mode))
                Files.setLastModifiedTime(path, FileTime.from(entry.mtimeSeconds, TimeUnit.SECONDS))
            }
        }

        /** Every parent must be a real directory created by this extraction: a symlink parent could redirect writes outside [root]. */
        private fun ensureParent(target: Path) {
            val parent = target.parent
            if (parent == root || parent in verifiedParents) return
            if (!Files.isDirectory(parent, LinkOption.NOFOLLOW_LINKS)) {
                ensureParent(parent)
                if (Files.exists(parent, LinkOption.NOFOLLOW_LINKS)) throw RuntimeInstallException("${root.relativize(parent)} is not a directory")
                Files.createDirectory(parent)
            }
            verifiedParents.add(parent)
        }

        private fun writeFile(target: Path, entry: TarReader.Entry): String {
            val digest = MessageDigest.getInstance("SHA-256")
            FileOutputStream(target.toFile()).use { out ->
                val content = tar.content()
                val buffer = ByteArray(IO_BUFFER)
                while (true) {
                    val read = content.read(buffer)
                    if (read < 0) break
                    digest.update(buffer, 0, read)
                    out.write(buffer, 0, read)
                }
            }
            return hex(digest.digest())
        }

        private fun checkMode(entry: TarReader.Entry, expected: RuntimeManifest.Entry) {
            if (entry.mode != expected.mode) throw RuntimeInstallException("${entry.path}: mode does not match the manifest")
        }
    }

    private fun pointer(link: File): String? {
        val path = link.toPath()
        if (!Files.isSymbolicLink(path)) return null
        val version = Files.readSymbolicLink(path).toString()
        return version.takeIf { layout.payload(it).isDirectory }
    }

    private fun repoint(link: File, version: String) {
        val temporary = File(link.parentFile, ".${link.name}.tmp").toPath()
        Files.deleteIfExists(temporary)
        Files.createSymbolicLink(temporary, Paths.get(version))
        Files.move(temporary, link.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    }

    companion object {
        private const val IO_BUFFER = 64 * 1024

        private fun move(from: File, to: File, replace: Boolean = false) {
            if (replace) Files.move(from.toPath(), to.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            else Files.move(from.toPath(), to.toPath(), StandardCopyOption.ATOMIC_MOVE)
        }

        private fun permissions(mode: Int): Set<PosixFilePermission> {
            val all = PosixFilePermission.values() // OWNER_READ … OTHERS_EXECUTE, highest bit first
            return all.filterIndexed { index, _ -> mode and (1 shl (all.size - 1 - index)) != 0 }.toSet()
        }

        private fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

        /** Delete a tree without following symlinks; restores owner write access that archive modes may have removed. */
        internal fun removeTree(file: File) {
            val path = file.toPath()
            if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) return
            if (Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) {
                file.setWritable(true, true)
                file.setExecutable(true, true)
                file.listFiles()?.forEach(::removeTree)
            }
            Files.delete(path)
        }
    }
}
