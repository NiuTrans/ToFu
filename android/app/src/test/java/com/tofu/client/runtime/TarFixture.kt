package com.tofu.client.runtime

import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.security.MessageDigest
import java.util.zip.GZIPOutputStream

/** Builds deterministic runtime archives (ustar + PAX long paths, manifest first) the way the producer lane does. */
object TarFixture {
    const val MTIME = 1_700_000_000L

    sealed class Item(val path: String) {
        class Dir(path: String, val mode: Int = 0b111_101_101) : Item(path)
        class Regular(path: String, val content: ByteArray, val mode: Int = 0b110_100_100) : Item(path)
        class Symlink(path: String, val target: String) : Item(path)
        class Hardlink(path: String, val target: String, val mode: Int = 0b110_100_100) : Item(path)
    }

    /**
     * The producer's format (android/runtime/README.md §2): compact JSON with
     * sorted keys, `mode` as a string of four octal digits, and the producer's
     * extra top-level keys, which the consumer ignores.
     */
    fun manifest(items: List<Item>): String {
        val entries = JSONArray()
        for (item in items) {
            entries.put(
                when (item) {
                    is Item.Dir -> JSONObject().put("mode", octal(item.mode)).put("path", item.path).put("type", "dir")
                    is Item.Regular -> JSONObject().put("mode", octal(item.mode)).put("path", item.path)
                        .put("sha256", sha256(item.content)).put("size", item.content.size).put("type", "file")
                    is Item.Symlink -> JSONObject().put("mode", octal(0b111_111_111)).put("path", item.path).put("target", item.target).put("type", "symlink")
                    is Item.Hardlink -> JSONObject().put("mode", octal(item.mode)).put("path", item.path).put("target", item.target).put("type", "hardlink")
                }
            )
        }
        return JSONObject().put("abi", RuntimeIndex.ABI).put("entries", entries).put("format", 1)
            .put("inputs", JSONObject().put("fixture", true)).put("kind", "payload").put("version", "fixture").toString()
    }

    fun octal(mode: Int): String = "%04o".format(mode)

    fun tarGz(items: List<Item>, manifest: String = manifest(items)): ByteArray {
        val tar = ByteArrayOutputStream()
        entry(tar, ".manifest.json", '0', 0b110_100_100, manifest.toByteArray(Charsets.UTF_8))
        for (item in items) {
            when (item) {
                is Item.Dir -> entry(tar, item.path + "/", '5', item.mode, ByteArray(0))
                is Item.Regular -> entry(tar, item.path, '0', item.mode, item.content)
                is Item.Symlink -> entry(tar, item.path, '2', 0b111_111_111, ByteArray(0), item.target)
                is Item.Hardlink -> entry(tar, item.path, '1', item.mode, ByteArray(0), item.target)
            }
        }
        tar.write(ByteArray(1024))
        val gzip = ByteArrayOutputStream()
        GZIPOutputStream(gzip).use { it.write(tar.toByteArray()) }
        return gzip.toByteArray()
    }

    fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun entry(out: ByteArrayOutputStream, path: String, type: Char, mode: Int, data: ByteArray, link: String = "") {
        val name = path.toByteArray(Charsets.UTF_8)
        if (name.size > 100) {
            val body = " path=$path\n".toByteArray(Charsets.UTF_8).size
            var length = body + 1
            while (length.toString().length + body != length) length = length.toString().length + body
            val record = "$length path=$path\n".toByteArray(Charsets.UTF_8)
            out.write(header("././@PaxHeader", 'x', 0b110_100_100, record.size.toLong(), ""))
            writeData(out, record)
        }
        out.write(header(String(name.copyOfRange(maxOf(0, name.size - 100), name.size), Charsets.UTF_8), type, mode, data.size.toLong(), link))
        writeData(out, data)
    }

    private fun writeData(out: ByteArrayOutputStream, data: ByteArray) {
        out.write(data)
        val padding = (512 - data.size % 512) % 512
        out.write(ByteArray(padding))
    }

    private fun header(name: String, type: Char, mode: Int, size: Long, link: String): ByteArray {
        val block = ByteArray(512)
        fun put(offset: Int, text: String) = text.toByteArray(Charsets.UTF_8).copyInto(block, offset)
        put(0, name)
        put(100, "%07o\u0000".format(mode))
        put(108, "0000000\u0000")
        put(116, "0000000\u0000")
        put(124, "%011o\u0000".format(size))
        put(136, "%011o\u0000".format(MTIME))
        put(148, "        ")
        block[156] = type.code.toByte()
        put(157, link)
        put(257, "ustar\u0000")
        put(263, "00")
        val checksum = block.sumOf { it.toInt() and 0xFF }
        put(148, "%06o\u0000 ".format(checksum))
        return block
    }
}

/** `assets/` backed by a directory. */
class DirectoryAssets(private val root: File) : RuntimeAssets {
    override fun open(name: String): InputStream? = File(root, name).takeIf { it.isFile }?.inputStream()
}
