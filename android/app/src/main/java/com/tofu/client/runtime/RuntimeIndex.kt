package com.tofu.client.runtime

import org.json.JSONException
import org.json.JSONObject

/** One SHA-256-pinned archive named by `assets/kernel/index.json`. */
data class RuntimeArchive(val version: String, val file: String, val sha256: String, val size: Long)

/**
 * `assets/kernel/index.json` (runtime contract v1):
 * `{"format":1,"abi":"arm64-v8a","rootfs":{version,file,sha256,size},"payload":{…}}`.
 *
 * The index is build-time pinned, but versions become directory names and
 * file names are opened from assets, so both are validated here instead of
 * trusting the producer: a malformed index fails loudly before any I/O.
 */
data class RuntimeIndex(val rootfs: RuntimeArchive, val payload: RuntimeArchive) {
    companion object {
        const val FORMAT = 1
        const val ABI = "arm64-v8a"
        const val ASSET_DIR = "kernel"
        const val INDEX_FILE = "index.json"

        private val VERSION = Regex("[A-Za-z0-9][A-Za-z0-9._+-]*")
        /** `.tgz`, never `.gz`: AGP's asset merger gunzips `*.gz` assets and drops the extension. */
        private val FILE_NAME = Regex("[A-Za-z0-9][A-Za-z0-9._+-]*\\.tgz")
        private val SHA256 = Regex("[0-9a-f]{64}")

        fun parse(json: String): RuntimeIndex = try {
            val root = JSONObject(json)
            val format = root.getInt("format")
            if (format != FORMAT) throw RuntimeInstallException("unsupported runtime index format $format")
            val abi = root.getString("abi")
            if (abi != ABI) throw RuntimeInstallException("runtime index is for $abi, not $ABI")
            RuntimeIndex(archive(root.getJSONObject("rootfs"), "rootfs"), archive(root.getJSONObject("payload"), "payload"))
        } catch (error: JSONException) {
            throw RuntimeInstallException("malformed runtime index: ${error.message}")
        }

        private fun archive(json: JSONObject, role: String): RuntimeArchive {
            val archive = RuntimeArchive(
                version = json.getString("version"),
                file = json.getString("file"),
                sha256 = json.getString("sha256"),
                size = json.getLong("size"),
            )
            if (!VERSION.matches(archive.version) || archive.version == KernelLayout.CURRENT || archive.version == KernelLayout.PREVIOUS) {
                throw RuntimeInstallException("invalid $role version '${archive.version}'")
            }
            if (!FILE_NAME.matches(archive.file)) throw RuntimeInstallException("invalid $role file name '${archive.file}'")
            if (!SHA256.matches(archive.sha256)) throw RuntimeInstallException("invalid $role sha256")
            if (archive.size <= 0) throw RuntimeInstallException("invalid $role size ${archive.size}")
            return archive
        }
    }
}

/**
 * The archive's first entry, `.manifest.json` (runtime contract v1, frozen by
 * its producer in android/runtime/README.md §2):
 * `{"format":1,"entries":[{"path","type","mode","size","sha256","target"}]}`.
 * `type` is one of file/dir/symlink/hardlink; `mode` is a JSON string of four
 * octal digits, equal to the tar header's mode (`"0755"`, `"4755"`, `"1777"`);
 * `size`+`sha256` are required for files, `target` for links. Paths are
 * archive-relative POSIX paths. Other keys (`abi`, `kind`, `version`,
 * `inputs`) are the producer's and are ignored.
 */
internal class RuntimeManifest private constructor(val entries: Map<String, Entry>) {
    data class Entry(val type: TarReader.Type, val mode: Int, val size: Long, val sha256: String?, val target: String?)

    companion object {
        const val NAME = ".manifest.json"

        fun parse(json: String): RuntimeManifest = try {
            val root = JSONObject(json)
            val format = root.getInt("format")
            if (format != RuntimeIndex.FORMAT) throw RuntimeInstallException("unsupported manifest format $format")
            val array = root.getJSONArray("entries")
            val entries = HashMap<String, Entry>(array.length() * 2)
            for (index in 0 until array.length()) {
                val item = array.getJSONObject(index)
                val path = TarReader.normalize(item.getString("path"))
                val type = when (val name = item.getString("type")) {
                    "file" -> TarReader.Type.FILE
                    "dir" -> TarReader.Type.DIRECTORY
                    "symlink" -> TarReader.Type.SYMLINK
                    "hardlink" -> TarReader.Type.HARDLINK
                    else -> throw RuntimeInstallException("manifest entry $path has unknown type '$name'")
                }
                val entry = Entry(
                    type = type,
                    mode = mode(path, item.get("mode")),
                    size = if (type == TarReader.Type.FILE) item.getLong("size") else 0L,
                    sha256 = if (type == TarReader.Type.FILE) item.getString("sha256") else null,
                    target = if (type == TarReader.Type.SYMLINK || type == TarReader.Type.HARDLINK) item.getString("target") else null,
                )
                if (entries.put(path, entry) != null) throw RuntimeInstallException("manifest lists $path twice")
            }
            entries.remove(NAME)
            entries.remove("")
            RuntimeManifest(entries)
        } catch (error: JSONException) {
            throw RuntimeInstallException("malformed archive manifest: ${error.message}")
        }

        /** Permission and set-id/sticky bits; the file type lives in `type`. */
        const val MODE_BITS = 0xFFF

        private val OCTAL_MODE = Regex("[0-7]{4}")

        /** A string, never a JSON number: org.json would read `"0755"` as decimal 755. */
        private fun mode(path: String, value: Any): Int {
            if (value !is String || !OCTAL_MODE.matches(value)) {
                throw RuntimeInstallException("manifest entry $path: mode must be a string of four octal digits")
            }
            return value.toInt(8) and MODE_BITS
        }
    }
}
