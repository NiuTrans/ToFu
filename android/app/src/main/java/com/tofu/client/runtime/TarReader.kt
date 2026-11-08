package com.tofu.client.runtime

import java.io.EOFException
import java.io.IOException
import java.io.InputStream

/**
 * Minimal tar reader over JDK streams for the runtime archives (runtime
 * contract v1). Reads POSIX ustar headers, PAX per-entry headers (`path`,
 * `linkpath`, `size`, `mtime`) and GNU long names/links. Only regular files,
 * directories, symlinks and hard links exist in the on-device layout; any
 * other entry type (devices, FIFOs, sparse files, global PAX headers) fails
 * loudly instead of being skipped. No dependency: the JDK's gzip stream plus
 * this reader cover the producer's deterministic archives.
 */
internal class TarReader(private val input: InputStream) {
    enum class Type { FILE, DIRECTORY, SYMLINK, HARDLINK }

    class Entry(
        val path: String,
        val type: Type,
        val mode: Int,
        val size: Long,
        val linkTarget: String,
        val mtimeSeconds: Long,
    )

    private val header = ByteArray(BLOCK)
    private var remaining = 0L
    private var padding = 0L

    /** The next entry, or null at the end-of-archive marker. Unread data of the previous entry is skipped. */
    fun next(): Entry? {
        skipFully(remaining + padding)
        remaining = 0
        padding = 0
        var pax: Map<String, String> = emptyMap()
        var gnuName: String? = null
        var gnuLink: String? = null
        while (true) {
            if (!readBlock()) throw IOException("truncated tar archive (no end-of-archive marker)")
            if (header.all { it == ZERO }) return null
            verifyChecksum()
            val flag = header[TYPEFLAG].toInt().toChar()
            val headerSize = octal(SIZE_OFFSET, SIZE_LENGTH)
            when (flag) {
                'x' -> { pax = parsePax(readExtension(headerSize)); continue }
                'L' -> { gnuName = cString(readExtension(headerSize)); continue }
                'K' -> { gnuLink = cString(readExtension(headerSize)); continue }
            }
            val type = when (flag) {
                '0', '\u0000', '7' -> Type.FILE
                '5' -> Type.DIRECTORY
                '2' -> Type.SYMLINK
                '1' -> Type.HARDLINK
                else -> throw IOException("unsupported tar entry type '$flag'")
            }
            val rawName = pax["path"] ?: gnuName ?: ustarName()
            val size = pax["size"]?.toLongOrNull() ?: headerSize
            remaining = size
            padding = (BLOCK - size % BLOCK) % BLOCK
            return Entry(
                path = normalize(rawName),
                type = type,
                mode = (octal(MODE_OFFSET, 8) and RuntimeManifest.MODE_BITS.toLong()).toInt(),
                size = if (type == Type.FILE) size else 0L,
                linkTarget = pax["linkpath"] ?: gnuLink ?: field(LINK_OFFSET, 100),
                mtimeSeconds = pax["mtime"]?.substringBefore('.')?.toLongOrNull() ?: octal(MTIME_OFFSET, 12),
            )
        }
    }

    /** The current entry's data; valid until the next [next] call. */
    fun content(): InputStream = object : InputStream() {
        override fun read(): Int {
            if (remaining <= 0) return -1
            val value = input.read()
            if (value < 0) throw EOFException("truncated tar entry")
            remaining--
            return value
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
            if (remaining <= 0) return -1
            val count = input.read(buffer, offset, minOf(length.toLong(), remaining).toInt())
            if (count < 0) throw EOFException("truncated tar entry")
            remaining -= count
            return count
        }
    }

    private fun readBlock(): Boolean {
        var read = 0
        while (read < BLOCK) {
            val count = input.read(header, read, BLOCK - read)
            if (count < 0) {
                if (read == 0) return false
                throw EOFException("truncated tar header")
            }
            read += count
        }
        return true
    }

    private fun readExtension(size: Long): ByteArray {
        if (size > MAX_EXTENSION_BYTES) throw IOException("tar extension header of $size bytes")
        val bytes = ByteArray(size.toInt())
        var read = 0
        while (read < bytes.size) {
            val count = input.read(bytes, read, bytes.size - read)
            if (count < 0) throw EOFException("truncated tar extension header")
            read += count
        }
        skipFully((BLOCK - size % BLOCK) % BLOCK)
        return bytes
    }

    private fun skipFully(count: Long) {
        var left = count
        val scratch = ByteArray(BLOCK * 16)
        while (left > 0) {
            val read = input.read(scratch, 0, minOf(left, scratch.size.toLong()).toInt())
            if (read < 0) throw EOFException("truncated tar archive")
            left -= read
        }
    }

    private fun verifyChecksum() {
        val expected = octal(CHECKSUM_OFFSET, 8)
        var unsigned = 0L
        var signed = 0L
        for (index in 0 until BLOCK) {
            val byte = if (index in CHECKSUM_OFFSET until CHECKSUM_OFFSET + 8) SPACE else header[index]
            unsigned += byte.toInt() and 0xFF
            signed += byte.toInt()
        }
        if (expected != unsigned && expected != signed) throw IOException("tar header checksum mismatch")
    }

    private fun ustarName(): String {
        val name = field(0, 100)
        // POSIX ustar ("ustar\0") stores a path prefix; GNU ("ustar ") uses that area for other fields.
        val posix = header[257] == 'u'.code.toByte() && header[262] == ZERO
        val prefix = if (posix) field(PREFIX_OFFSET, 155) else ""
        return if (prefix.isEmpty()) name else "$prefix/$name"
    }

    private fun field(offset: Int, length: Int): String {
        var end = offset
        while (end < offset + length && header[end] != ZERO) end++
        return String(header, offset, end - offset, Charsets.UTF_8)
    }

    private fun octal(offset: Int, length: Int): Long {
        if (header[offset].toInt() and 0x80 != 0) throw IOException("base-256 tar numbers are not supported")
        val text = field(offset, length).trim()
        if (text.isEmpty()) return 0
        return text.toLongOrNull(8) ?: throw IOException("malformed tar number '$text'")
    }

    companion object {
        const val BLOCK = 512
        private const val ZERO: Byte = 0
        private const val SPACE: Byte = 0x20
        private const val MODE_OFFSET = 100
        private const val SIZE_OFFSET = 124
        private const val SIZE_LENGTH = 12
        private const val MTIME_OFFSET = 136
        private const val CHECKSUM_OFFSET = 148
        private const val TYPEFLAG = 156
        private const val LINK_OFFSET = 157
        private const val PREFIX_OFFSET = 345
        /** PAX/GNU extension records hold one path or a few keywords; anything larger is not a runtime archive. */
        private const val MAX_EXTENSION_BYTES = 1L shl 20

        /**
         * Archive-relative path without `./` or a trailing slash ("" is the
         * archive root). Absolute paths and `..` segments are refused so no
         * entry can land outside the extraction root.
         */
        fun normalize(raw: String): String {
            var path = raw
            while (path.startsWith("./")) path = path.substring(2)
            path = path.trimEnd('/')
            if (path == ".") path = ""
            if (path.startsWith("/") || path.contains('\u0000') ||
                path.split('/').any { it == ".." || (it.isEmpty() && path.isNotEmpty()) }
            ) {
                throw IOException("unsafe archive path '$raw'")
            }
            return path
        }

        private fun cString(bytes: ByteArray): String {
            val end = bytes.indexOf(ZERO).let { if (it < 0) bytes.size else it }
            return String(bytes, 0, end, Charsets.UTF_8)
        }

        /** PAX records: "<length> <key>=<value>\n", length counting the whole record. */
        private fun parsePax(bytes: ByteArray): Map<String, String> {
            val values = HashMap<String, String>()
            var position = 0
            while (position < bytes.size) {
                var space = position
                while (space < bytes.size && bytes[space] != SPACE) space++
                val length = String(bytes, position, space - position, Charsets.US_ASCII).toIntOrNull()
                    ?: throw IOException("malformed PAX record")
                if (length <= 0 || position + length > bytes.size) throw IOException("malformed PAX record length")
                val record = String(bytes, space + 1, position + length - space - 2, Charsets.UTF_8)
                val equals = record.indexOf('=')
                if (equals <= 0) throw IOException("malformed PAX record")
                values[record.substring(0, equals)] = record.substring(equals + 1)
                position += length
            }
            return values
        }
    }
}
