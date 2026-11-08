package com.tofu.client.runtime

import java.io.File
import java.io.IOException

/** A runtime install step was refused (checksum, manifest, layout or I/O); nothing is switched. */
class RuntimeInstallException(message: String, cause: Throwable? = null) : IOException(message, cause)

/**
 * App-private on-device layout of the local kernel (runtime contract v1, see
 * android/README.md "Local kernel"). Every path lives under the app's
 * credential-encrypted `files` directory; nothing is shared storage, and
 * nothing a running kernel uses lives in `cache`, which Android may clear at
 * any time. The rootfs path never moves: proot's `--link2symlink` stores
 * absolute host paths in the symlinks that stand in for hard links.
 */
class KernelLayout(filesDir: File) {
    val root = File(filesDir, "kernel")
    /** Persistent Ubuntu userland; replaced only when its pinned version changes. */
    val rootfs = File(root, "rootfs")
    val rootfsVersionFile = File(root, "rootfs.version")
    val rootfsStaging = File(root, "rootfs.staging")
    val rootfsRetired = File(root, "rootfs.old")
    /** Replaceable payloads, one directory per version, plus the `current`/`previous` pointers. */
    val payloadRoot = File(root, "payload")
    val currentPointer = File(payloadRoot, CURRENT)
    /** The last-known-good payload: the one `current` designated before the latest commit. */
    val previousPointer = File(payloadRoot, PREVIOUS)
    /** Tofu's data directory (bind-mounted at [GuestPaths.DATA]); never touched by the installer. */
    val data = File(root, "data")
    /** The guest user's home (bind-mounted at [GuestPaths.HOME]): uv/npm caches, MCP servers, skills. */
    val home = File(root, "home")
    /** proot's scratch directory (`PROOT_TMP_DIR`); emptied before each generation. */
    val prootTmp = File(root, "proot-tmp")
    /** Backs the guest's `/dev/shm` (glibc `shm_open`/`sem_open`); emptied before each generation. */
    val shm = File(root, "shm")

    fun payload(version: String): File = File(payloadRoot, version)
    fun payloadStaging(version: String): File = File(payloadRoot, ".staging-$version")
    fun rejectedMarker(version: String): File = File(payloadRoot, "$version.rejected")

    companion object {
        const val CURRENT = "current"
        const val PREVIOUS = "previous"
    }
}

/** Mount points inside the guest (runtime contract v1). */
object GuestPaths {
    const val PAYLOAD = "/opt/tofu"
    const val DATA = "/var/lib/tofu"
    const val HOME = "/root"
    const val ENTRY = "/opt/tofu/bin/tofu-local-kernel"
    const val SHM = "/dev/shm"
}
