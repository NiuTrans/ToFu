package com.tofu.client.runtime

import android.content.Context
import android.content.res.AssetManager
import android.os.Process
import java.io.File
import java.io.FileNotFoundException
import java.io.InputStream

/** `android.os.Process` signals; same-uid processes are the only ones an app may signal. */
class AndroidProcessSignals(private val procRoot: File = File("/proc")) : ProcessSignals {
    override fun terminate(pid: Int) = Process.sendSignal(pid, LaunchProtocol.SIGTERM)

    override fun forceStop(): Int {
        val prootPids = AppProcessTable.childPids(procRoot, Process.myPid(), Process.myUid())
        prootPids.forEach { Process.sendSignal(it, Process.SIGNAL_QUIT) }
        return prootPids.size
    }

    override fun killAppChildren(): Int {
        val pids = AppProcessTable.otherPids(procRoot, Process.myPid(), Process.myUid())
        pids.forEach(Process::killProcess)
        return pids.size
    }
}

class AssetManagerRuntimeAssets(private val assets: AssetManager) : RuntimeAssets {
    override fun open(name: String): InputStream? = try {
        assets.open(name, AssetManager.ACCESS_STREAMING)
    } catch (missing: FileNotFoundException) {
        null // this build carries no such asset
    }
}

/** Whether this build and device can run the local kernel at all (no I/O beyond two directory lookups). */
sealed interface LocalKernelAvailability {
    data object Available : LocalKernelAvailability

    /** The build was assembled without `-Ptofu.localKernel.assetsDir`, or without the arm64 proot libraries. */
    data class NotInThisBuild(val missing: String) : LocalKernelAvailability

    companion object {
        fun of(context: Context): LocalKernelAvailability {
            val assets = context.assets.list(RuntimeIndex.ASSET_DIR).orEmpty()
            if (RuntimeIndex.INDEX_FILE !in assets) return NotInThisBuild("assets/${RuntimeIndex.ASSET_DIR}/${RuntimeIndex.INDEX_FILE}")
            val nativeLibraries = File(context.applicationInfo.nativeLibraryDir)
            for (library in listOf(KernelCommandLine.PROOT, KernelCommandLine.PROOT_LOADER)) {
                if (!File(nativeLibraries, library).isFile) return NotInThisBuild("${RuntimeIndex.ABI}/$library")
            }
            return Available
        }
    }
}

/**
 * The device implementation of [KernelRuntime]: APK assets, `nativeLibraryDir`,
 * and the process-wide [secret] store that the cookie adapter reads as well.
 */
class AndroidKernelRuntime(private val context: Context, private val secret: KernelSecret) : KernelRuntime {
    private val layout = KernelLayout(context.filesDir)
    private val installer = RuntimeInstaller(layout, AssetManagerRuntimeAssets(context.assets))

    override fun prepare(cancelled: () -> Boolean): PreparedLaunch {
        val availability = LocalKernelAvailability.of(context)
        if (availability is LocalKernelAvailability.NotInThisBuild) throw LocalRuntimeUnavailableException("missing ${availability.missing}")
        val index = installer.readIndex() ?: throw LocalRuntimeUnavailableException("missing runtime index")
        val payload = installer.prepare(index, cancelled)
        val command = KernelCommandLine.build(layout, File(context.applicationInfo.nativeLibraryDir), payload.directory)
        return PreparedLaunch(command, secret.load(), payload.version, payload.pending)
    }

    override fun commit(launch: PreparedLaunch) = installer.commit(launch.payloadVersion)

    override fun reject(launch: PreparedLaunch) = installer.reject(launch.payloadVersion)
}
