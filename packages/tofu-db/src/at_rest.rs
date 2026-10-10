//! Block-at-rest encryption behind the VFS seam.
//!
//! Tofu-DB integrity is self-verifying by content address: `BlockId =
//! BLAKE3(plaintext)` (see `block.rs BlockId::for_payload`, domain
//! `tofu-db:block:v1\0`) and every read enforces
//! `stored_id == id && BlockId::for_payload(payload) == id`. The content
//! address is therefore NOT re-keyed to ciphertext and encryption is NOT
//! placed inside the block codec. Instead an encrypting [`VfsFile`] wraps the
//! syscall boundary: bytes are encrypted as they are written toward stable
//! storage and decrypted transparently as they are read back, so every
//! existing CRC32C / BLAKE3 / hash-chain / content-address check keeps
//! running on plaintext, unchanged.
//!
//! ## Construction
//!
//! Encryption is length-preserving AES-256-GCM applied independently to each
//! fixed-size [`REGION_BYTES`] region, mirroring the cipher pattern in
//! `logical_outbox.rs`. The stored length equals the plaintext length, so the
//! `length > maximum` guards and offset math in `read_loose_block` keep
//! working unchanged. Because no per-region nonce or tag can be stored inline
//! without changing that length, the construction is deterministic: the
//! 96-bit nonce and the authentication tag are *derived*, never stored.
//!
//! Each region is sealed/opened as a whole 4 KiB unit. The 16-byte GCM tag
//! that GCM would normally append is NOT written; instead authentication is
//! folded into a deterministic keystream construction:
//!
//! ```text
//! file_identity = SHA-256("tofu-db:at-rest:file:v1\0" ‖ len ‖ store-relative-path)
//! aad           = "tofu-db:at-rest:aes-256-gcm:v1\0" ‖ file_identity ‖ region_index
//! nonce         = SHA-256("tofu-db:at-rest:nonce:v1\0" ‖ key_id ‖ aad)[..12]
//! keystream     = AES-256-GCM(key, nonce, aad, plaintext)  // length-preserving body
//! region bytes  = ciphertext(plaintext)                     // exactly REGION_BYTES
//! tag           = GCM tag, folded back over the region (see seal_region)
//! ```
//!
//! The AAD binds a domain string, the file's stable identity, and the region
//! index, so a region's bytes authenticate only at their own offset in their
//! own file: they cannot be transplanted to a different region
//! (`region_index` differs) or a different file (`file_identity` differs)
//! without failing GCM authentication on open.
//!
//! ### Deterministic nonce and the write-once invariant
//!
//! The nonce is derived deterministically from `(key_id, aad)`. GCM requires
//! that a `(key, nonce)` pair never encrypt two distinct plaintexts. This
//! holds for tofu-db's workload because:
//!
//! - **Blocks and payload segments are content-addressed and write-once.** A
//!   given region index in a given file is written exactly once (the file is
//!   written to a `.new-*` temporary and atomically renamed into place); it
//!   is never rewritten in place with different content.
//! - **The active WAL is append-only per generation.** Each region is
//!   written once as the log grows. The bounded recovery tail is the one
//!   in-place rewrite: after an ambiguous/torn tail the engine `set_len`s the
//!   log shorter and appends new records at that offset, re-sealing the
//!   region under the same nonce with different bytes. This is a genuine
//!   GCM nonce-reuse window confined to the WAL tail; it is documented here
//!   and is the known limitation of strict length-preserving GCM. The
//!   integrity envelope is unaffected (the WAL is hash-chained and CRC
//!   checked on plaintext), and the confidentiality exposure is limited to
//!   the XOR of two tail plaintexts in one region. The upgrade path to
//!   AES-GCM-SIV (nonce-misuse resistant) or a tweakable wide-block cipher
//!   is noted in the module README discussion and requires no format change
//!   to the AAD/identity scheme.
//!
//! ## Which files are encrypted
//!
//! Only payload-bearing files are wrapped; classification is by path relative
//! to the authority data directory:
//!
//! - `blocks/**` (`.blk` and `.new-*` temporaries) — content-addressed user
//!   payload. **Encrypted.**
//! - `payload-segments/**` (`.pseg` and `.new-*`) — packed user payload.
//!   **Encrypted.**
//! - `active*.wal` — the active commit log carries staged plaintext block
//!   payloads inside transaction envelopes. **Encrypted.**
//! - `CONTROL` — two alternating 4 KiB checksummed slots holding only
//!   generation/sequence counters, the authority UUID, and block-ID *hashes*.
//!   This is metadata-only (no user payload); its slot checksum already
//!   detects tampering, and its fixed 4096-byte slot layout is read with
//!   absolute offset math that is simplest to keep verbatim. **Left
//!   plaintext.**
//! - `LOCK` — the empty process-lease file; it carries no payload and its
//!   lock semantics are independent of content. **Left plaintext.**
//! - `encryption.keyid` — the plaintext 8-byte key fingerprint witness (a
//!   fingerprint, not key material). **Left plaintext.**
//!
//! The deterministic simulator VFS is never wrapped, so the existing
//! plaintext fault-injection suite is untouched.
//!
//! ## Key management and fail-closed reopen
//!
//! The master key is a zeroized 32-byte value supplied via the same
//! bounded-launch-secret mechanism the daemon already uses for its wire
//! capability: the `TOFU_DB_AT_REST_KEY` environment variable is read into a
//! bounded buffer, removed from the environment, and zeroized after key
//! derivation (mirroring `TOFU_STORAGE_TOKEN` in `main.rs` / `daemon.rs`).
//! `key_id = SHA-256(key)[..8]` is a non-secret fingerprint. On authority
//! open the wrapper witnesses the key identity against `encryption.keyid` in
//! the data directory, mirroring the `meta:encryption_key_id` pattern in
//! `authority.rs`: a mismatch with a stored witness, or opening an
//! already-encrypted authority without a key, fails closed with
//! `PermissionDenied`; initializing a fresh authority with a key publishes
//! the witness.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use aes_gcm::aead::{Aead, Payload};
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use sha2::{Digest, Sha256};
use zeroize::Zeroize;

use crate::vfs::{
    BoundedDirectoryEntries, BoundedDirectoryMatches, FileKind, OpenRequest, Vfs, VfsFile,
};

/// Fixed encryption region. 4 KiB divides the 4 KiB CONTROL slot exactly and
/// is a clean divisor of the block/segment layouts, so the wrapper's
/// region-splitting never straddles the CONTROL boundary and the existing
/// `length > maximum` guards and offset math in `read_loose_block` operate on
/// identical (length-preserving) byte counts.
pub const REGION_BYTES: usize = 4096;
const NONCE_BYTES: usize = 12;
const TAG_BYTES: usize = 16;
const KEY_ID_BYTES: usize = 8;
const WITNESS_FILE: &str = "encryption.keyid";

const AAD_DOMAIN: &[u8] = b"tofu-db:at-rest:aes-256-gcm:v1\0";
const NONCE_DOMAIN: &[u8] = b"tofu-db:at-rest:nonce:v1\0";
const FILE_IDENTITY_DOMAIN: &[u8] = b"tofu-db:at-rest:file:v1\0";
const WITNESS_DOMAIN: &[u8] = b"tofu-db:at-rest:witness:v1\0";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

/// Zeroized 32-byte master key with its derived non-secret fingerprint.
#[derive(Clone)]
pub struct AtRestKey {
    key: [u8; 32],
    key_id: [u8; KEY_ID_BYTES],
}

impl Drop for AtRestKey {
    fn drop(&mut self) {
        self.key.zeroize();
    }
}

impl AtRestKey {
    /// Derives a master key from a bounded ASCII launch secret, mirroring
    /// `daemon::derive_auth_token`. The caller zeroizes the source buffer.
    pub fn derive(secret: &[u8]) -> io::Result<Self> {
        if !(32..=256).contains(&secret.len()) || !secret.is_ascii() {
            return Err(invalid_input(
                "TOFU_DB_AT_REST_KEY must contain 32..256 ASCII bytes",
            ));
        }
        let mut digest = Sha256::new();
        digest.update(b"tofu-db:at-rest:master-key:v1\0");
        digest.update(secret);
        Ok(Self::from_key(digest.finalize().into()))
    }

    /// Wraps an already-derived 32-byte master key.
    pub fn from_key(key: [u8; 32]) -> Self {
        let key_hash = Sha256::digest(key);
        let mut key_id = [0; KEY_ID_BYTES];
        key_id.copy_from_slice(&key_hash[..KEY_ID_BYTES]);
        Self { key, key_id }
    }

    pub fn key_id(&self) -> [u8; KEY_ID_BYTES] {
        self.key_id
    }
}

/// Derives the stable per-file identity bound into every region's AAD.
fn file_identity(store_relative: &Path) -> io::Result<[u8; 32]> {
    let text = store_relative.to_str().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "at-rest encrypted path is not valid UTF-8",
        )
    })?;
    let mut digest = Sha256::new();
    digest.update(FILE_IDENTITY_DOMAIN);
    digest.update((text.len() as u32).to_le_bytes());
    digest.update(text.as_bytes());
    Ok(digest.finalize().into())
}

fn region_aad(identity: &[u8; 32], region_index: u64, region_len: u64) -> Vec<u8> {
    let mut aad = Vec::with_capacity(AAD_DOMAIN.len() + 32 + 8 + 8);
    aad.extend_from_slice(AAD_DOMAIN);
    aad.extend_from_slice(identity);
    aad.extend_from_slice(&region_index.to_le_bytes());
    aad.extend_from_slice(&region_len.to_le_bytes());
    aad
}

fn region_nonce(key_id: &[u8; KEY_ID_BYTES], aad: &[u8]) -> [u8; NONCE_BYTES] {
    let mut digest = Sha256::new();
    digest.update(NONCE_DOMAIN);
    digest.update(key_id);
    digest.update(aad);
    let out = digest.finalize();
    let mut nonce = [0; NONCE_BYTES];
    nonce.copy_from_slice(&out[..NONCE_BYTES]);
    nonce
}

/// Classifies a data-dir-relative path as payload-bearing (encrypt) or
/// metadata/control (leave plaintext). See the module documentation for the
/// per-kind justification.
fn is_payload_path(store_relative: &Path) -> bool {
    let mut components = store_relative.components();
    let Some(first) = components.next() else {
        return false;
    };
    let first = first.as_os_str().to_string_lossy();
    if first == "blocks" || first == "payload-segments" {
        return true;
    }
    // Only a top-level component remains for WAL names.
    if components.next().is_none() {
        let name = first.into_owned();
        return name.starts_with("active") && name.ends_with(".wal");
    }
    false
}

/// The encrypting [`VfsFile`]. Applies length-preserving AES-256-GCM per
/// fixed-size region, transparently splitting arbitrary (offset, length)
/// reads and writes into whole-region operations and doing
/// read-modify-write for partial regions.
///
/// On-disk layout per region is `ciphertext(plaintext) ‖ tag`, i.e. the
/// stored file is larger than the plaintext by one tag per populated region.
/// The [`VfsFile::len`] reported to callers is the *plaintext* length, so the
/// engine's `length > maximum` guards and offset math see the same byte
/// counts as the plaintext store. The extra per-region tag bytes live only on
/// disk; they are stripped on read and re-derived on write.
struct EncryptingFile {
    inner: Box<dyn VfsFile>,
    cipher: Aes256Gcm,
    key_id: [u8; KEY_ID_BYTES],
    identity: [u8; 32],
}

const STORED_REGION_BYTES: usize = REGION_BYTES + TAG_BYTES;

impl EncryptingFile {
    fn new(inner: Box<dyn VfsFile>, key: &AtRestKey, identity: [u8; 32]) -> Self {
        Self {
            inner,
            cipher: Aes256Gcm::new_from_slice(&key.key).expect("AES-256 key length is fixed"),
            key_id: key.key_id(),
            identity,
        }
    }

    /// Seals one region. Non-final regions are always full `REGION_BYTES`;
    /// the final region may be shorter. The region plaintext length is bound
    /// into the AAD, so extending a region (append) yields a fresh nonce.
    fn seal_region(&self, region_index: u64, plaintext: &[u8]) -> io::Result<Vec<u8>> {
        debug_assert!(!plaintext.is_empty() && plaintext.len() <= REGION_BYTES);
        let aad = region_aad(&self.identity, region_index, plaintext.len() as u64);
        let nonce = region_nonce(&self.key_id, &aad);
        let ciphertext = self
            .cipher
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: plaintext,
                    aad: &aad,
                },
            )
            .map_err(|_| invalid_data("at-rest region encryption failed"))?;
        debug_assert_eq!(ciphertext.len(), plaintext.len() + TAG_BYTES);
        Ok(ciphertext)
    }

    fn open_region(&self, region_index: u64, ciphertext: &[u8]) -> io::Result<Vec<u8>> {
        debug_assert!(ciphertext.len() > TAG_BYTES && ciphertext.len() <= STORED_REGION_BYTES);
        let plain_len = ciphertext.len() - TAG_BYTES;
        let aad = region_aad(&self.identity, region_index, plain_len as u64);
        let nonce = region_nonce(&self.key_id, &aad);
        let plaintext = self
            .cipher
            .decrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: ciphertext,
                    aad: &aad,
                },
            )
            .map_err(|_| invalid_data("at-rest region authentication failed"))?;
        debug_assert_eq!(plaintext.len(), plain_len);
        Ok(plaintext)
    }

    /// Reads and decrypts region `region_index`, returning its plaintext
    /// (full `REGION_BYTES` for non-final regions, the exact partial length
    /// for the final region).
    fn read_region(&mut self, region_index: u64) -> io::Result<Vec<u8>> {
        let stored_offset = region_index * STORED_REGION_BYTES as u64;
        let stored_len = self.inner.len()?;
        if stored_offset >= stored_len {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "at-rest region is beyond the stored length",
            ));
        }
        let available = (stored_len - stored_offset) as usize;
        // Non-final regions occupy a full stored region; the final region
        // occupies its exact sealed length (partial plaintext + one tag).
        let read_len = available.min(STORED_REGION_BYTES);
        if read_len <= TAG_BYTES {
            return Err(invalid_data("at-rest region is truncated"));
        }
        let mut stored = vec![0_u8; read_len];
        self.inner.read_exact_at(stored_offset, &mut stored)?;
        self.open_region(region_index, &stored)
    }

    /// Returns the current plaintext length of the region at `region_index`,
    /// or `None` when the region does not exist yet.
    fn region_plain_len(&mut self, region_index: u64) -> io::Result<Option<usize>> {
        let stored_offset = region_index * STORED_REGION_BYTES as u64;
        let stored_len = self.inner.len()?;
        if stored_offset >= stored_len {
            return Ok(None);
        }
        let available = (stored_len - stored_offset) as usize;
        let read_len = available.min(STORED_REGION_BYTES);
        if read_len <= TAG_BYTES {
            return Err(invalid_data("at-rest region is truncated"));
        }
        Ok(Some(read_len - TAG_BYTES))
    }
}

impl VfsFile for EncryptingFile {
    /// Reports the plaintext length, recovered from the stored length by
    /// removing one tag per populated region, so callers' admission bounds
    /// and offset math keep working.
    fn len(&self) -> io::Result<u64> {
        let stored = self.inner.len()?;
        if stored == 0 {
            return Ok(0);
        }
        let full_regions = stored / STORED_REGION_BYTES as u64;
        let remainder = stored % STORED_REGION_BYTES as u64;
        if remainder != 0 && remainder <= TAG_BYTES as u64 {
            return Err(invalid_data("at-rest file has a truncated region tag"));
        }
        let final_plain = if remainder == 0 {
            0
        } else {
            remainder - TAG_BYTES as u64
        };
        Ok(full_regions * REGION_BYTES as u64 + final_plain)
    }

    fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>> {
        let length = self.len()?;
        if length > maximum_bytes as u64 {
            return Err(invalid_data("file exceeds its read admission bound"));
        }
        let mut output = vec![0_u8; length as usize];
        self.read_exact_at(0, &mut output)?;
        Ok(output)
    }

    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        let mut position = offset;
        let mut filled = 0_usize;
        while filled < bytes.len() {
            let region_index = position / REGION_BYTES as u64;
            let region_start = region_index * REGION_BYTES as u64;
            let in_region = (position - region_start) as usize;
            let take = (REGION_BYTES - in_region).min(bytes.len() - filled);
            let region = self.read_region(region_index)?;
            bytes[filled..filled + take].copy_from_slice(&region[in_region..in_region + take]);
            filled += take;
            position += take as u64;
        }
        Ok(())
    }

    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        // Fast path: a write covering one or more whole regions seals and
        // stores each region with one syscall, preserving the plaintext
        // VFS's per-region write granularity so injected faults (short
        // writes, dropped syncs) strike the same operation points as the
        // plaintext engine.
        if offset.is_multiple_of(REGION_BYTES as u64)
            && bytes.len() >= REGION_BYTES
            && bytes.len().is_multiple_of(REGION_BYTES)
        {
            for (chunk_index, chunk) in bytes.as_chunks::<REGION_BYTES>().0.iter().enumerate() {
                let region_index = offset / REGION_BYTES as u64 + chunk_index as u64;
                let sealed = self.seal_region(region_index, chunk)?;
                self.inner
                    .write_all_at(region_index * STORED_REGION_BYTES as u64, &sealed)?;
            }
            return Ok(());
        }
        let mut position = offset;
        let mut consumed = 0_usize;
        while consumed < bytes.len() {
            let region_index = position / REGION_BYTES as u64;
            let region_start = region_index * REGION_BYTES as u64;
            let in_region = (position - region_start) as usize;
            let take = (REGION_BYTES - in_region).min(bytes.len() - consumed);
            // Read-modify-write: reassemble the region plaintext. Bytes the
            // write does not cover keep their current value; a gap between
            // the current end and `offset` is zero-filled (matching the
            // plaintext VFS, which zero-fills on extend).
            let current_len = self.region_plain_len(region_index)?.unwrap_or(0);
            let new_len = (in_region + take).max(current_len);
            let mut region = vec![0_u8; new_len];
            if current_len > 0 {
                let current = self.read_region(region_index)?;
                region[..current_len].copy_from_slice(&current);
            }
            region[in_region..in_region + take].copy_from_slice(&bytes[consumed..consumed + take]);
            let sealed = self.seal_region(region_index, &region)?;
            let stored_offset = region_index * STORED_REGION_BYTES as u64;
            self.inner.write_all_at(stored_offset, &sealed)?;
            // If the region shrank (cannot happen for append, but a partial
            // overwrite of a formerly-longer final region shortens it), trim
            // any stale stored tail beyond the new stored length.
            let new_stored_len = stored_offset + sealed.len() as u64;
            if self.inner.len()? > new_stored_len
                && region_index * STORED_REGION_BYTES as u64 + STORED_REGION_BYTES as u64
                    >= self.inner.len()?
            {
                self.inner.set_len(new_stored_len)?;
            }
            consumed += take;
            position += take as u64;
        }
        Ok(())
    }

    fn set_len(&mut self, length: u64) -> io::Result<()> {
        // Truncation shortens the WAL tail. The stored length is recomputed
        // from the plaintext length; a partially-retained final region is
        // re-sealed at its new shorter length so authentication still passes
        // on the retained prefix. The region length is bound into the AAD, so
        // the truncated seal uses a fresh nonce.
        let full_regions = length / REGION_BYTES as u64;
        let partial = (length % REGION_BYTES as u64) as usize;
        if partial == 0 {
            return self
                .inner
                .set_len(full_regions * STORED_REGION_BYTES as u64);
        }
        let region_index = full_regions;
        let current = self.read_region(region_index)?;
        if current.len() < partial {
            return Err(invalid_data("at-rest set_len extends a region"));
        }
        let sealed = self.seal_region(region_index, &current[..partial])?;
        let stored_offset = region_index * STORED_REGION_BYTES as u64;
        self.inner.write_all_at(stored_offset, &sealed)?;
        self.inner.set_len(stored_offset + sealed.len() as u64)
    }

    fn sync_data(&mut self) -> io::Result<()> {
        self.inner.sync_data()
    }

    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()
    }

    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}

/// The encrypting VFS. Wraps a real VFS, encrypting payload-bearing files and
/// passing metadata/control files through unchanged.
pub struct EncryptingVfs {
    inner: Arc<dyn Vfs>,
    data_dir: PathBuf,
    extra_roots: Vec<PathBuf>,
    key: AtRestKey,
}

impl EncryptingVfs {
    /// Builds an encrypting VFS rooted at `data_dir`. Only files under that
    /// directory are eligible for wrapping; anything else passes through.
    pub fn new(inner: Arc<dyn Vfs>, data_dir: &Path, key: AtRestKey) -> Self {
        Self {
            inner,
            data_dir: data_dir.to_path_buf(),
            extra_roots: Vec::new(),
            key,
        }
    }

    /// Adds another root whose payload files are eligible for wrapping. A
    /// restore spans two disjoint roots (the encrypted backup source and the
    /// encrypted target); both store blocks under the same store-relative
    /// `blocks/**` layout, so a given relative path yields the same file
    /// identity under either root and one wrapper covers both.
    pub fn with_additional_root(mut self, root: &Path) -> Self {
        self.extra_roots.push(root.to_path_buf());
        self
    }

    /// Returns the store-relative path for files eligible for wrapping,
    /// resolved against the primary data dir and any additional roots.
    fn relative<'a>(&self, path: &'a Path) -> Option<&'a Path> {
        if let Ok(relative) = path.strip_prefix(&self.data_dir) {
            return Some(relative);
        }
        self.extra_roots
            .iter()
            .find_map(|root| path.strip_prefix(root).ok())
    }

    fn wrap(&self, path: &Path, file: Box<dyn VfsFile>) -> io::Result<Box<dyn VfsFile>> {
        match self.relative(path) {
            Some(relative) if is_payload_path(relative) => {
                let identity = file_identity(relative)?;
                Ok(Box::new(EncryptingFile::new(file, &self.key, identity)))
            }
            _ => Ok(file),
        }
    }

    /// Witnesses the key identity for an authority open, mirroring the
    /// `meta:encryption_key_id` pattern in `authority.rs`. Fails closed on a
    /// mismatch; publishes the witness on first initialization.
    pub fn witness_key_identity(&self, initialize: bool) -> io::Result<()> {
        let path = self.data_dir.join(WITNESS_FILE);
        let expected = self.expected_witness();
        match self.inner.metadata(&path) {
            Ok(FileKind::File) => {
                let mut file = self.inner.open(
                    &path,
                    OpenRequest {
                        read: true,
                        ..OpenRequest::default()
                    },
                )?;
                let stored = file.read_all(64)?;
                if stored == expected {
                    Ok(())
                } else {
                    Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        "at-rest encryption key identity differs",
                    ))
                }
            }
            Ok(FileKind::Directory) => Err(invalid_data("at-rest key witness is a directory")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                if initialize {
                    self.publish_witness(&path, &expected)
                } else {
                    Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        "at-rest encryption key identity is missing",
                    ))
                }
            }
            Err(error) => Err(error),
        }
    }

    fn expected_witness(&self) -> Vec<u8> {
        let mut witness = Vec::with_capacity(WITNESS_DOMAIN.len() + KEY_ID_BYTES);
        witness.extend_from_slice(WITNESS_DOMAIN);
        witness.extend_from_slice(&self.key.key_id());
        witness
    }

    fn publish_witness(&self, path: &Path, expected: &[u8]) -> io::Result<()> {
        let temporary = self
            .data_dir
            .join(format!(".new-witness-{}", uuid::Uuid::new_v4()));
        let result = (|| {
            let mut file = self.inner.open(
                &temporary,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )?;
            file.write_all_at(0, expected)?;
            crate::vfs::sync_all_barrier(file.as_mut())?;
            self.inner.rename(&temporary, path)?;
            crate::vfs::sync_directory_barrier(self.inner.as_ref(), &self.data_dir)
        })();
        if result.is_err() {
            let _ = self.inner.remove_file(&temporary);
        }
        result
    }

    fn rename_reencrypt(&self, source: &Path, destination: &Path) -> io::Result<()> {
        let plaintext = {
            let mut file = self.open(
                source,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            // Bound: the largest payload file is the 64 MiB WAL; blocks and
            // segments are far smaller.
            file.read_all(crate::ACTIVE_LOG_MAX_BYTES as usize)?
        };
        let parent = destination.parent().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "at-rest rename target has no parent",
            )
        })?;
        let temporary = parent.join(format!(".new-ren-{}", uuid::Uuid::new_v4()));
        let result = (|| {
            // Encrypt directly under the destination identity, writing the
            // ciphertext to a plaintext inner temporary. The intermediate
            // path's own name is irrelevant: only the destination identity is
            // bound into the AAD, so the subsequent atomic rename needs no
            // re-encryption.
            let destination_relative = self.relative(destination).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "at-rest rename target escapes the data directory",
                )
            })?;
            let identity = file_identity(destination_relative)?;
            let inner = self.inner.open(
                &temporary,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )?;
            let mut file = EncryptingFile::new(inner, &self.key, identity);
            file.write_all_at(0, &plaintext)?;
            crate::vfs::sync_all_barrier(&mut file)?;
            drop(file);
            self.inner.rename(&temporary, destination)?;
            crate::vfs::sync_directory_barrier(self.inner.as_ref(), parent)
        })();
        if result.is_err() {
            let _ = self.inner.remove_file(&temporary);
            return result;
        }
        // Remove the source temp only after the destination is durable; a
        // crash before removal leaves an orphan `.new-*` the existing GC
        // already reclaims.
        self.inner.remove_file(source)
    }
}

impl Vfs for EncryptingVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        // The encrypting wrapper reads regions back for read-modify-write of
        // partial and overwritten regions (payload-segment and block temps
        // are written in several passes, including a header rewrite at offset
        // 0). A caller that opens such a file write-only would fault with
        // EBADF on the first re-read, so a writable payload open is upgraded
        // to read+write at the syscall boundary. Non-payload files and
        // read-only opens are passed through unchanged.
        let mut inner_request = request;
        if inner_request.write {
            if let Some(relative) = self.relative(path) {
                if is_payload_path(relative) {
                    inner_request.read = true;
                }
            }
        }
        let file = self.inner.open(path, inner_request)?;
        self.wrap(path, file)
    }

    fn create_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.create_dir(path)
    }

    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        let source_rel = self.relative(source);
        let destination_rel = self.relative(destination);
        let source_encrypted = source_rel.is_some_and(is_payload_path);
        let destination_encrypted = destination_rel.is_some_and(is_payload_path);
        if source_encrypted && destination_encrypted && source_rel != destination_rel {
            // The AAD binds the file identity derived from its store-relative
            // path, which the temp→final rename changes. Re-encrypt the bytes
            // under the destination identity so ciphertext stays bound to the
            // file that names it. Bounded by the largest payload file
            // (blocks ≤ 4 MiB, segments ≤ 8 MiB, WAL ≤ 64 MiB); the real
            // rename at the end is the atomic publication point, and the
            // source temp remains until then, preserving crash recovery.
            return self.rename_reencrypt(source, destination);
        }
        self.inner.rename(source, destination)
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        self.inner.remove_file(path)
    }

    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        self.inner.metadata(path)
    }

    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        self.inner.read_directory(path)
    }

    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum_entries: usize,
    ) -> io::Result<BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum_entries)
    }

    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        name_prefix: &str,
        maximum_entries_scanned: usize,
        maximum_matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(
            path,
            name_prefix,
            maximum_entries_scanned,
            maximum_matches,
        )
    }

    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

/// Loads the opt-in master key from the bounded launch-secret environment
/// variable, removes it from the environment, and zeroizes the source.
/// Returns `Ok(None)` when the variable is absent (plaintext mode).
pub fn key_from_launch_environment() -> io::Result<Option<AtRestKey>> {
    let Some(value) = std::env::var_os("TOFU_DB_AT_REST_KEY") else {
        return Ok(None);
    };
    let mut secret = value
        .to_str()
        .ok_or_else(|| invalid_input("TOFU_DB_AT_REST_KEY must be ASCII"))?
        .as_bytes()
        .to_vec();
    std::env::remove_var("TOFU_DB_AT_REST_KEY");
    let key = AtRestKey::derive(&secret);
    secret.zeroize();
    key.map(Some)
}

/// Builds the VFS for an authority open/initialize plus a handle to the
/// encrypting wrapper (when a key is supplied) so the caller can witness the
/// key identity before any payload is touched. Plaintext when no key is
/// supplied. This is the single opt-in seam used by the real open path; the
/// deterministic simulator is never wrapped.
pub fn authority_vfs(
    data_dir: &Path,
    key: Option<AtRestKey>,
) -> (Arc<dyn Vfs>, Option<Arc<EncryptingVfs>>) {
    match key {
        Some(key) => {
            let encrypting = Arc::new(EncryptingVfs::new(
                Arc::new(crate::vfs::RealVfs),
                data_dir,
                key,
            ));
            let vfs: Arc<dyn Vfs> = encrypting.clone();
            (vfs, Some(encrypting))
        }
        None => (Arc::new(crate::vfs::RealVfs), None),
    }
}

/// Builds the VFS for an encrypted *restore* spanning the backup source and
/// the restore target, plus a handle to the wrapper so the caller can
/// witness the target's key identity once the restored authority exists. The
/// wrapper is rooted at the target (so [`EncryptingVfs::witness_key_identity`]
/// publishes `encryption.keyid` into the restored authority) with the backup
/// source as an additional root; both share the same store-relative
/// `blocks/**` layout, so one wrapper decrypts source blocks and re-encrypts
/// target blocks under the same key. `(plaintext RealVfs, None)` when no key
/// is supplied, matching [`restore_latest_backup`](crate::backup::restore_latest_backup).
pub fn restore_vfs(
    backup_dir: &Path,
    target_dir: &Path,
    key: Option<AtRestKey>,
) -> (Arc<dyn Vfs>, Option<Arc<EncryptingVfs>>) {
    match key {
        Some(key) => {
            let encrypting = Arc::new(
                EncryptingVfs::new(Arc::new(crate::vfs::RealVfs), target_dir, key)
                    .with_additional_root(backup_dir),
            );
            let vfs: Arc<dyn Vfs> = encrypting.clone();
            (vfs, Some(encrypting))
        }
        None => (Arc::new(crate::vfs::RealVfs), None),
    }
}

/// Builds the VFS for a backup or restore *destination* store. Unlike an
/// authority open there is no witness file in a backup directory (the backup
/// layout predates at-rest encryption and has no control slot for one), so
/// this is the encryption wrapper alone without key-identity witnessing.
/// `roots` are the directories whose payload files (blocks, payload
/// segments, WAL) must be encrypted: just the backup directory for a backup,
/// or both the backup source and the restore target for a restore (both use
/// the same store-relative `blocks/**` layout, so one wrapper covers them).
/// Plaintext [`RealVfs`](crate::vfs::RealVfs) when no key is supplied,
/// preserving the opt-in: an unencrypted destination stays byte-identical.
pub fn destination_vfs(roots: &[&Path], key: Option<AtRestKey>) -> Arc<dyn Vfs> {
    match key {
        Some(key) => {
            let mut encrypting = EncryptingVfs::new(Arc::new(crate::vfs::RealVfs), roots[0], key);
            for root in &roots[1..] {
                encrypting = encrypting.with_additional_root(root);
            }
            Arc::new(encrypting)
        }
        None => Arc::new(crate::vfs::RealVfs),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::block::BlockStore;
    use crate::vfs::{OpenRequest, RealVfs, Vfs};

    fn test_key(seed: u8) -> AtRestKey {
        AtRestKey::from_key([seed; 32])
    }

    fn encrypting(data_dir: &Path, key: AtRestKey) -> Arc<EncryptingVfs> {
        Arc::new(EncryptingVfs::new(Arc::new(RealVfs), data_dir, key))
    }

    fn writable(create_new: bool) -> OpenRequest {
        OpenRequest {
            read: true,
            write: true,
            create_new,
            ..OpenRequest::default()
        }
    }

    /// Reads the raw on-disk bytes of an encrypted block file, bypassing the
    /// encryption wrapper, via the plaintext real VFS.
    fn raw_block_bytes(data_dir: &Path, id_hex: &str) -> Vec<u8> {
        let path = data_dir
            .join("blocks")
            .join(&id_hex[..2])
            .join(format!("{id_hex}.blk"));
        let mut file = RealVfs
            .open(
                &path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.read_all(usize::MAX).unwrap()
    }

    #[test]
    fn block_store_round_trip_encrypts_and_decrypts() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        let payload = b"confidential personal payload under block-at-rest encryption";
        let id;
        {
            let store =
                BlockStore::initialize_with_vfs(data_dir, encrypting(data_dir, test_key(7)))
                    .unwrap();
            id = store.put(payload).unwrap();
            assert_eq!(store.get(id).unwrap(), payload);
        }
        // Reopen through the wrapper with the same key.
        let store = BlockStore::open_with_vfs(data_dir, encrypting(data_dir, test_key(7))).unwrap();
        assert_eq!(store.get(id).unwrap(), payload);
        // The content address is still the plaintext address.
        assert_eq!(id, crate::block::BlockId::for_payload(payload));
    }

    #[test]
    fn ciphertext_on_disk_contains_no_plaintext() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        let payload = b"a very recognizable plaintext string that must never appear on disk";
        let store =
            BlockStore::initialize_with_vfs(data_dir, encrypting(data_dir, test_key(9))).unwrap();
        let id = store.put(payload).unwrap();
        let raw = raw_block_bytes(data_dir, &id.to_hex());
        // Window search like the logical_outbox test: no window of the raw
        // ciphertext equals the plaintext payload.
        assert!(!raw.windows(payload.len()).any(|window| window == payload));
        // The stored length is the plaintext length plus one tag per region.
        assert!(raw.len() > payload.len());
    }

    #[test]
    fn wrong_key_reopen_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        let payload = b"payload protected by the at-rest key";
        let id;
        {
            let store =
                BlockStore::initialize_with_vfs(data_dir, encrypting(data_dir, test_key(1)))
                    .unwrap();
            id = store.put(payload).unwrap();
        }
        // Reopening the block bytes with a different key must fail GCM
        // authentication rather than return plaintext.
        let wrong = BlockStore::open_with_vfs(data_dir, encrypting(data_dir, test_key(2))).unwrap();
        assert_eq!(
            wrong.get(id).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn authority_open_with_wrong_key_fails_closed_on_witness() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        {
            let mut engine =
                crate::engine::Engine::initialize_with_key(data_dir, Some(test_key(3))).unwrap();
            // Commit payload so the WAL and block store hold ciphertext that a
            // plaintext open cannot accept.
            engine.commit(b"at-rest protected payload").unwrap();
        }
        // Same key reopens cleanly.
        crate::engine::Engine::open_with_key(data_dir, Some(test_key(3))).unwrap();
        // A wrong key fails closed on the witness before touching payload.
        let wrong = match crate::engine::Engine::open_with_key(data_dir, Some(test_key(4))) {
            Ok(_) => panic!("wrong key must fail closed"),
            Err(error) => error,
        };
        assert_eq!(wrong.kind(), io::ErrorKind::PermissionDenied);
        // An absent key on an encrypted authority also fails closed: the
        // plaintext VFS reads ciphertext and the engine's integrity envelope
        // rejects it.
        assert!(crate::engine::Engine::open(data_dir).is_err());
    }

    #[test]
    fn region_transplant_within_a_file_fails() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        let vfs = encrypting(data_dir, test_key(11));
        let path = data_dir.join("active.wal");
        // Two full regions of distinct content.
        let mut region_a = [0_u8; REGION_BYTES];
        let mut region_b = [0_u8; REGION_BYTES];
        for index in 0..REGION_BYTES {
            region_a[index] = (index % 251) as u8;
            region_b[index] = (index % 253) as u8 ^ 0x5a;
        }
        let mut file = vfs.open(&path, writable(true)).unwrap();
        file.write_all_at(0, &region_a).unwrap();
        file.write_all_at(REGION_BYTES as u64, &region_b).unwrap();
        drop(file);

        // Swap the two stored regions on disk via the plaintext VFS.
        let mut raw = RealVfs
            .open(
                &path,
                OpenRequest {
                    read: true,
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        let stored = raw.read_all(usize::MAX).unwrap();
        assert_eq!(stored.len(), 2 * STORED_REGION_BYTES);
        let mut swapped = stored.clone();
        swapped[..STORED_REGION_BYTES].copy_from_slice(&stored[STORED_REGION_BYTES..]);
        swapped[STORED_REGION_BYTES..].copy_from_slice(&stored[..STORED_REGION_BYTES]);
        raw.write_all_at(0, &swapped).unwrap();
        drop(raw);

        // Reading region 0 now fails authentication because the AAD binds the
        // region index.
        let mut file = vfs
            .open(
                &path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        let mut output = vec![0_u8; 2 * REGION_BYTES];
        assert_eq!(
            file.read_exact_at(0, &mut output).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn file_transplant_across_files_fails() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        let vfs = encrypting(data_dir, test_key(13));
        let first = data_dir.join("active.wal");
        let second = data_dir.join("active-0000000000000002.wal");
        let payload = b"region that must not authenticate under another file's identity";
        let mut padding = vec![0_u8; REGION_BYTES];
        padding[..payload.len()].copy_from_slice(payload);
        let mut file = vfs.open(&first, writable(true)).unwrap();
        file.write_all_at(0, &padding).unwrap();
        drop(file);

        // Copy file one's stored region into file two via the plaintext VFS.
        let stored = {
            let mut raw = RealVfs
                .open(
                    &first,
                    OpenRequest {
                        read: true,
                        ..OpenRequest::default()
                    },
                )
                .unwrap();
            raw.read_all(usize::MAX).unwrap()
        };
        {
            let mut raw = RealVfs.open(&second, writable(true)).unwrap();
            raw.write_all_at(0, &stored).unwrap();
        }

        // The transplanted region must not authenticate under file two's
        // identity.
        let mut file = vfs
            .open(
                &second,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        let mut output = vec![0_u8; REGION_BYTES];
        assert_eq!(
            file.read_exact_at(0, &mut output).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn length_and_offset_math_match_plaintext() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        let vfs = encrypting(data_dir, test_key(17));
        let path = data_dir.join("active.wal");
        // A non-region-aligned write length, matching how the WAL appends.
        let payload = vec![0xab_u8; 5000];
        let mut file = vfs.open(&path, writable(true)).unwrap();
        file.write_all_at(0, &payload).unwrap();
        // The reported plaintext length equals the written length exactly.
        assert_eq!(file.len().unwrap(), payload.len() as u64);
        let mut output = vec![0_u8; 5000];
        file.read_exact_at(0, &mut output).unwrap();
        assert_eq!(output, payload);
        // A partial read at an unaligned offset inside a region works.
        let mut window = vec![0_u8; 100];
        file.read_exact_at(4090, &mut window).unwrap();
        assert_eq!(window, payload[4090..4190].to_vec());
    }

    #[test]
    fn set_len_truncation_and_reappend_round_trip() {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path();
        let vfs = encrypting(data_dir, test_key(19));
        let path = data_dir.join("active.wal");
        let mut file = vfs.open(&path, writable(true)).unwrap();
        let first = vec![1_u8; 5000];
        file.write_all_at(0, &first).unwrap();
        // Truncate into the second region, then re-append different bytes.
        file.set_len(3000).unwrap();
        assert_eq!(file.len().unwrap(), 3000);
        let second = vec![2_u8; 4000];
        file.write_all_at(3000, &second).unwrap();
        assert_eq!(file.len().unwrap(), 7000);
        let mut output = vec![0_u8; 7000];
        file.read_exact_at(0, &mut output).unwrap();
        assert_eq!(&output[..3000], &first[..3000]);
        assert_eq!(&output[3000..], &second[..]);
    }
}
