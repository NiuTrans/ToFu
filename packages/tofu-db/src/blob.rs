//! Owner-scoped, chunked blobs staged into immutable blocks and committed atomically.

use std::collections::BTreeSet;
use std::io::{self, Read};

use crate::block::BlockId;
use crate::engine::Engine;
#[path = "blob_decode_cache.rs"]
pub(crate) mod decode_cache;
#[path = "blob_whole_cache.rs"]
mod whole_cache;

pub(crate) enum ChunkBytes { Owned(Vec<u8>), Shared(std::sync::Arc<Vec<u8>>) }
impl ChunkBytes {
    fn as_slice(&self) -> &[u8] { match self { Self::Owned(bytes) => bytes, Self::Shared(bytes) => bytes } }
    fn into_vec(self) -> Vec<u8> { match self { Self::Owned(bytes) => bytes, Self::Shared(bytes) => std::sync::Arc::try_unwrap(bytes).unwrap_or_else(|bytes| bytes.as_ref().clone()) } }
}


const CHUNK_MAGIC: &[u8; 8] = b"TDBCHN01";
const MANIFEST_MAGIC: &[u8; 8] = b"TDBBLB01";
const VERSION: u32 = 1;
const CODEC_RAW: u8 = 0;
const CODEC_ZSTD: u8 = 1;
const CHUNK_HEADER_BYTES: usize = 8 + 4 + 1 + 3 + 4 + 4 + 32;
const MANIFEST_HEADER_BYTES: usize = 8 + 4 + 8 + 8 + 8 + 4 + 32;
const MANIFEST_ENTRY_BYTES: usize = 32 + 4;
const COMPRESSION_MINIMUM_SAVING_BYTES: usize = 64;
pub const BLOB_CHUNK_BYTES: usize = 1024 * 1024;
pub const MAX_BLOB_BYTES: u64 = 1024 * 1024 * 1024;
pub const MAX_BLOB_CHUNKS: usize = MAX_BLOB_BYTES as usize / BLOB_CHUNK_BYTES;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BlobId(pub [u8; 32]);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BlobReference {
    pub blob_id: BlobId,
    pub manifest_block_id: BlockId,
    pub logical_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StagedBlob {
    pub reference: BlobReference,
    pub transaction_block_ids: Vec<BlockId>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ChunkReference {
    block_id: BlockId,
    logical_bytes: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Manifest {
    tenant_id: u64,
    owner_user_id: u64,
    logical_bytes: u64,
    blob_id: BlobId,
    chunks: Vec<ChunkReference>,
}

pub struct BlobReader<'a> {
    backend: &'a dyn crate::entity::EntityBlockIo,
    decode_cache: Option<&'a decode_cache::DecodedChunkCache>,
    manifest: Manifest,
    manifest_block_id: BlockId,
    encoded_manifest: std::sync::Arc<Vec<u8>>,
    next_chunk: usize,
    logical_bytes_read: u64,
    content_hasher: blake3::Hasher,
    verified: bool,
}

/// Scope-bound identity minted only after a complete blob content check.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub(crate) struct VerifiedBlobIdentity {
    tenant_id: u64,
    owner_user_id: u64,
    content_hash: [u8; 32],
    logical_bytes: u64,
}

/// Immutable bytes bound to the completed reader's actual verified identity.
/// Consuming into a mutable Vec discards this witness; callers cannot attach it
/// to another buffer or mint it from a persisted BlobReference.
pub(crate) struct VerifiedBlobBytes {
    bytes: Vec<u8>,
    identity: VerifiedBlobIdentity,
}
impl VerifiedBlobBytes {
    pub(crate) fn bytes(&self) -> &[u8] {
        &self.bytes
    }
    pub(crate) fn identity(&self) -> VerifiedBlobIdentity {
        self.identity
    }
    pub(crate) fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct BlobReachabilityMetrics {
    pub block_count: u64,
    pub payload_bytes: u64,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn content_hasher(tenant_id: u64, owner_user_id: u64) -> blake3::Hasher {
    let mut hasher = blake3::Hasher::new();
    hasher.update(b"tofu-db:blob:v1\0");
    hasher.update(&tenant_id.to_le_bytes());
    hasher.update(&owner_user_id.to_le_bytes());
    hasher
}

#[cfg(test)]
pub(crate) fn encode_chunk(raw: &[u8]) -> io::Result<Vec<u8>> {
    if raw.is_empty() || raw.len() > BLOB_CHUNK_BYTES {
        return Err(invalid_input("blob chunk is empty or exceeds 1 MiB"));
    }
    let compressed = zstd::bulk::compress(raw, 3)?;
    encode_compressed_chunk(raw, &compressed)
}

/// One lazily initialized codec per authority, not per connection/thread.
/// The existing 1 MiB chunk bound caps its workspace; Engine drop owns it.
#[derive(Default)]
pub(crate) struct ChunkEncoder(std::sync::Mutex<Option<zstd::bulk::Compressor<'static>>>);

impl ChunkEncoder {
    pub(crate) fn encode(&self, raw: &[u8]) -> io::Result<Vec<u8>> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BLOB_ENCODE);
        if raw.is_empty() || raw.len() > BLOB_CHUNK_BYTES {
            return Err(invalid_input("blob chunk is empty or exceeds 1 MiB"));
        }
        let mut codec = self.0.lock().map_err(|_| io::Error::other("blob codec is poisoned"))?;
        if codec.is_none() {
            *codec = Some(zstd::bulk::Compressor::new(3)?);
        }
        let compressed = codec.as_mut().unwrap().compress(raw)?;
        encode_compressed_chunk(raw, &compressed)
    }
}

fn encode_compressed_chunk(raw: &[u8], compressed: &[u8]) -> io::Result<Vec<u8>> {
    let (codec, payload) = if compressed.len() + COMPRESSION_MINIMUM_SAVING_BYTES < raw.len() {
        (CODEC_ZSTD, compressed)
    } else {
        (CODEC_RAW, raw)
    };
    let payload_len = u32::try_from(payload.len())
        .map_err(|_| invalid_input("encoded blob chunk length overflow"))?;
    let mut encoded = Vec::with_capacity(CHUNK_HEADER_BYTES + payload.len());
    encoded.extend_from_slice(CHUNK_MAGIC);
    encoded.extend_from_slice(&VERSION.to_le_bytes());
    encoded.push(codec);
    encoded.extend_from_slice(&[0; 3]);
    encoded.extend_from_slice(&(raw.len() as u32).to_le_bytes());
    encoded.extend_from_slice(&payload_len.to_le_bytes());
    encoded.extend_from_slice(blake3::hash(raw).as_bytes());
    encoded.extend_from_slice(payload);
    Ok(encoded)
}

fn decode_chunk(encoded: &[u8], expected_logical_bytes: u32) -> io::Result<Vec<u8>> {
    if encoded.len() < CHUNK_HEADER_BYTES || &encoded[..8] != CHUNK_MAGIC {
        return Err(invalid_data("blob chunk magic mismatch"));
    }
    let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
    let codec = encoded[12];
    let reserved = &encoded[13..16];
    let logical_bytes = u32::from_le_bytes(encoded[16..20].try_into().unwrap());
    let payload_bytes = u32::from_le_bytes(encoded[20..24].try_into().unwrap()) as usize;
    let expected_hash: [u8; 32] = encoded[24..56].try_into().unwrap();
    if version != VERSION
        || reserved != [0, 0, 0]
        || logical_bytes != expected_logical_bytes
        || logical_bytes == 0
        || logical_bytes as usize > BLOB_CHUNK_BYTES
        || CHUNK_HEADER_BYTES.checked_add(payload_bytes) != Some(encoded.len())
    {
        return Err(invalid_data("blob chunk header or length mismatch"));
    }
    let payload = &encoded[CHUNK_HEADER_BYTES..];
    let decode_timer = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_CHUNK_DECODE);
    let raw = match codec {
        CODEC_RAW if payload.len() == logical_bytes as usize => payload.to_vec(),
        CODEC_ZSTD => zstd::bulk::decompress(payload, logical_bytes as usize)
            .map_err(|_| invalid_data("blob chunk decompression failed"))?,
        _ => return Err(invalid_data("unsupported blob chunk codec")),
    };
    drop(decode_timer);
    crate::request_profile::work(crate::request_profile::Work::BlobDecodedBytes, raw.len() as u64);
    let _hash = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_CHUNK_HASH);
    if raw.len() != logical_bytes as usize || blake3::hash(&raw).as_bytes() != &expected_hash {
        return Err(invalid_data("blob chunk logical hash mismatch"));
    }
    Ok(raw)
}

impl Manifest {
    fn encode(&self) -> io::Result<Vec<u8>> {
        if self.tenant_id == 0
            || self.logical_bytes > MAX_BLOB_BYTES
            || self.chunks.len() > MAX_BLOB_CHUNKS
            || (self.logical_bytes == 0) != self.chunks.is_empty()
        {
            return Err(invalid_input("invalid or unbounded blob manifest"));
        }
        let mut encoded =
            Vec::with_capacity(MANIFEST_HEADER_BYTES + self.chunks.len() * MANIFEST_ENTRY_BYTES);
        encoded.extend_from_slice(MANIFEST_MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&self.tenant_id.to_le_bytes());
        encoded.extend_from_slice(&self.owner_user_id.to_le_bytes());
        encoded.extend_from_slice(&self.logical_bytes.to_le_bytes());
        encoded.extend_from_slice(&(self.chunks.len() as u32).to_le_bytes());
        encoded.extend_from_slice(&self.blob_id.0);
        for chunk in &self.chunks {
            encoded.extend_from_slice(&chunk.block_id.0);
            encoded.extend_from_slice(&chunk.logical_bytes.to_le_bytes());
        }
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < MANIFEST_HEADER_BYTES || &encoded[..8] != MANIFEST_MAGIC {
            return Err(invalid_data("blob manifest magic mismatch"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let tenant_id = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
        let owner_user_id = u64::from_le_bytes(encoded[20..28].try_into().unwrap());
        let logical_bytes = u64::from_le_bytes(encoded[28..36].try_into().unwrap());
        let chunk_count = u32::from_le_bytes(encoded[36..40].try_into().unwrap()) as usize;
        let blob_id = BlobId(encoded[40..72].try_into().unwrap());
        let expected_len = chunk_count
            .checked_mul(MANIFEST_ENTRY_BYTES)
            .and_then(|length| MANIFEST_HEADER_BYTES.checked_add(length))
            .ok_or_else(|| invalid_data("blob manifest length overflow"))?;
        if version != VERSION
            || tenant_id == 0
            || logical_bytes > MAX_BLOB_BYTES
            || chunk_count > MAX_BLOB_CHUNKS
            || expected_len != encoded.len()
            || (logical_bytes == 0) != (chunk_count == 0)
        {
            return Err(invalid_data("invalid or unbounded blob manifest"));
        }
        let mut chunks = Vec::with_capacity(chunk_count);
        let mut offset = MANIFEST_HEADER_BYTES;
        let mut summed_bytes = 0_u64;
        for index in 0..chunk_count {
            let block_id = BlockId(encoded[offset..offset + 32].try_into().unwrap());
            let chunk_bytes =
                u32::from_le_bytes(encoded[offset + 32..offset + 36].try_into().unwrap());
            if chunk_bytes == 0
                || chunk_bytes as usize > BLOB_CHUNK_BYTES
                || (index + 1 < chunk_count && chunk_bytes as usize != BLOB_CHUNK_BYTES)
            {
                return Err(invalid_data(
                    "blob manifest contains an invalid chunk length",
                ));
            }
            summed_bytes = summed_bytes
                .checked_add(chunk_bytes as u64)
                .ok_or_else(|| invalid_data("blob manifest byte count overflow"))?;
            chunks.push(ChunkReference {
                block_id,
                logical_bytes: chunk_bytes,
            });
            offset += MANIFEST_ENTRY_BYTES;
        }
        if summed_bytes != logical_bytes {
            return Err(invalid_data("blob manifest logical length mismatch"));
        }
        Ok(Self {
            tenant_id,
            owner_user_id,
            logical_bytes,
            blob_id,
            chunks,
        })
    }
}

pub(crate) fn visit_blob_graph<ReadBlock, VisitBlock>(
    tenant_id: u64,
    owner_user_id: u64,
    reference: BlobReference,
    mut read_block: ReadBlock,
    mut visit_block: VisitBlock,
) -> io::Result<BlobReachabilityMetrics>
where
    ReadBlock: FnMut(BlockId) -> io::Result<Vec<u8>>,
    VisitBlock: FnMut(BlockId, &[u8]) -> io::Result<()>,
{
    let manifest_bytes = read_block(reference.manifest_block_id)?;
    let manifest = Manifest::decode(&manifest_bytes)?;
    if manifest.tenant_id != tenant_id || manifest.owner_user_id != owner_user_id {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "blob reachability crosses its owner scope",
        ));
    }
    if manifest.blob_id != reference.blob_id || manifest.logical_bytes != reference.logical_bytes {
        return Err(invalid_data("blob reference does not match its manifest"));
    }
    visit_block(reference.manifest_block_id, &manifest_bytes)?;
    let mut metrics = BlobReachabilityMetrics {
        block_count: 1,
        payload_bytes: manifest_bytes.len() as u64,
    };
    for chunk in manifest.chunks {
        let payload = read_block(chunk.block_id)?;
        metrics.block_count = metrics
            .block_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("blob reachability block count overflow"))?;
        metrics.payload_bytes = metrics
            .payload_bytes
            .checked_add(payload.len() as u64)
            .ok_or_else(|| invalid_data("blob reachability byte count overflow"))?;
        visit_block(chunk.block_id, &payload)?;
    }
    Ok(metrics)
}

impl StagedBlob {
    pub fn commit(self, engine: &mut Engine, inline_payload: &[u8]) -> io::Result<BlobReference> {
        engine.commit_references(inline_payload, &self.transaction_block_ids)?;
        Ok(self.reference)
    }
}

pub fn stage_blob<R: Read>(
    engine: &Engine,
    tenant_id: u64,
    owner_user_id: u64,
    reader: &mut R,
    maximum_bytes: u64,
) -> io::Result<StagedBlob> {
    stage_blob_with_backend(engine, tenant_id, owner_user_id, reader, maximum_bytes)
}

/// Encoding and block staging can precede the serialized commit. A preparation
/// backend retains private, accounted bytes; it never publishes storage state.
pub(crate) trait BlobStagingIo {
    fn encode_chunk(&self, raw: &[u8]) -> io::Result<Vec<u8>>;
    fn stage_block(&self, encoded: &[u8]) -> io::Result<BlockId>;
}

impl BlobStagingIo for Engine {
    fn encode_chunk(&self, raw: &[u8]) -> io::Result<Vec<u8>> { self.blob_chunk_encoder().encode(raw) }
    fn stage_block(&self, encoded: &[u8]) -> io::Result<BlockId> { self.write_blob_block(encoded) }
}

pub(crate) fn stage_blob_with_backend<R: Read>(
    backend: &dyn BlobStagingIo,
    tenant_id: u64,
    owner_user_id: u64,
    reader: &mut R,
    maximum_bytes: u64,
) -> io::Result<StagedBlob> {
    let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BLOB_STAGE);
    if tenant_id == 0 || owner_user_id == 0 {
        return Err(invalid_input(
            "tenant and owner identities must be positive",
        ));
    }
    if maximum_bytes > MAX_BLOB_BYTES {
        return Err(invalid_input("blob admission bound exceeds 1 GiB"));
    }
    let mut hasher = content_hasher(tenant_id, owner_user_id);
    let mut chunks = Vec::new();
    let mut unique_block_ids = BTreeSet::new();
    let mut logical_bytes = 0_u64;
    loop {
        // A small document needs its remaining admitted bytes plus one byte
        // to detect overflow, not a zero-filled 1 MiB allocation per read.
        let capacity = (maximum_bytes - logical_bytes).saturating_add(1)
            .min(BLOB_CHUNK_BYTES as u64) as usize;
        let mut raw = vec![0; capacity];
        let mut used = 0;
        while used < raw.len() {
            match reader.read(&mut raw[used..]) {
                Ok(0) => break,
                Ok(count) => used += count,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
        if used == 0 {
            break;
        }
        let reached_eof = used < raw.len();
        raw.truncate(used);
        logical_bytes = logical_bytes
            .checked_add(used as u64)
            .ok_or_else(|| invalid_input("blob length overflow"))?;
        if logical_bytes > maximum_bytes {
            return Err(invalid_input("blob exceeds its request admission bound"));
        }
        hasher.update(&raw);
        let block_id = backend.stage_block(&backend.encode_chunk(&raw)?)?;
        unique_block_ids.insert(block_id);
        chunks.push(ChunkReference {
            block_id,
            logical_bytes: used as u32,
        });
        if chunks.len() > MAX_BLOB_CHUNKS {
            return Err(invalid_input("blob exceeds its chunk count bound"));
        }
        if reached_eof { break; }
    }
    let blob_id = BlobId(*hasher.finalize().as_bytes());
    let manifest = Manifest {
        tenant_id,
        owner_user_id,
        logical_bytes,
        blob_id,
        chunks,
    };
    let manifest_block_id = backend.stage_block(&manifest.encode()?)?;
    let mut transaction_block_ids: Vec<_> = unique_block_ids.into_iter().collect();
    transaction_block_ids.push(manifest_block_id);
    Ok(StagedBlob {
        reference: BlobReference {
            blob_id,
            manifest_block_id,
            logical_bytes,
        },
        transaction_block_ids,
    })
}

impl<'a> BlobReader<'a> {
    pub(crate) fn with_decode_cache(mut self, cache: &'a decode_cache::DecodedChunkCache) -> Self {
        self.decode_cache = Some(cache);
        self
    }

    fn read_current_chunk(&self, chunk: &ChunkReference) -> io::Result<std::sync::Arc<Vec<u8>>> {
        let encoded = {
            let _source = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_CHUNK_SOURCE);
            self.backend.read_verified(chunk.block_id)?
        };
        crate::request_profile::work(crate::request_profile::Work::BlobCurrentEncodedBytes, encoded.len() as u64);
        Ok(encoded)
    }
    fn decode_current_source(&self, chunk: &ChunkReference, encoded: &std::sync::Arc<Vec<u8>>) -> io::Result<ChunkBytes> {
        match self.decode_cache {
            Some(cache) => cache.decode(chunk.block_id, encoded, chunk.logical_bytes),
            None => decode_chunk(encoded, chunk.logical_bytes).map(ChunkBytes::Owned),
        }
    }
    fn decode_current_chunk(&self, chunk: &ChunkReference) -> io::Result<ChunkBytes> {
        self.decode_current_source(chunk, &self.read_current_chunk(chunk)?)
    }
    fn identity(&self) -> VerifiedBlobIdentity {
        VerifiedBlobIdentity {
            tenant_id: self.manifest.tenant_id, owner_user_id: self.manifest.owner_user_id,
            content_hash: self.manifest.blob_id.0, logical_bytes: self.manifest.logical_bytes,
        }
    }
    // Only a fresh, complete materialization may reuse this proof. Streaming and
    // range APIs retain their original independent cursor/hash semantics.
    fn materialize_with_whole_cache(&self, cache: &whole_cache::WholeBlobCache, maximum_bytes: usize, bound_error: &impl Fn() -> io::Error) -> io::Result<VerifiedBlobBytes> {
        let certificate = cache.lookup(self.manifest_block_id, &self.encoded_manifest, self.identity());
        let mut matched = certificate.as_ref().is_some_and(|c| c.matches_count(self.manifest.chunks.len()));
        // Never pin an oversized body while attempting optional admission.
        // Format bounds cap vector metadata; encoded pins also fit the current
        // cache allowance. A concurrent resize may release entries while this
        // reader still owns its already bounded in-flight certificate.
        let allowance = cache.dependency_capacity();
        let mut charge = 4096usize.checked_add(512)
            .and_then(|n| n.checked_add(self.encoded_manifest.capacity()))
            .and_then(|n| n.checked_add(self.manifest.chunks.len().checked_mul(std::mem::size_of::<std::sync::Arc<Vec<u8>>>() + std::mem::size_of::<Vec<u8>>() + 2 * std::mem::size_of::<usize>())?));
        let mut sources = charge.filter(|&n| n <= allowance).map(|_| Vec::with_capacity(self.manifest.chunks.len()));
        let mut bytes = Vec::with_capacity(self.manifest.logical_bytes as usize);
        for (index, chunk) in self.manifest.chunks.iter().enumerate() {
            let encoded = self.read_current_chunk(chunk)?;
            let raw = self.decode_current_source(chunk, &encoded)?;
            if bytes.len().saturating_add(raw.as_slice().len()) > maximum_bytes { return Err(bound_error()); }
            {
                let _dependencies = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_WHOLE_PROOF_DEPENDENCIES);
                matched &= certificate.as_ref().is_some_and(|c| c.matches_chunk(index, &encoded));
                charge = charge.and_then(|n| n.checked_add(encoded.capacity()));
                if charge.is_some_and(|n| n <= allowance) {
                    if let Some(sources) = &mut sources { sources.push(encoded); }
                } else { sources = None; }
            }
            let _collect = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_BUFFER_COLLECT);
            bytes.extend_from_slice(raw.as_slice());
            crate::request_profile::work(crate::request_profile::Work::BlobCollectedBytes, raw.as_slice().len() as u64);
        }
        if bytes.len() as u64 != self.manifest.logical_bytes { return Err(invalid_data("blob logical content witness mismatch")); }
        if !matched {
            crate::request_profile::work(crate::request_profile::Work::BlobWholeProofMiss, 1);
            let _hash = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_CONTENT_HASH);
            let mut hasher = content_hasher(self.manifest.tenant_id, self.manifest.owner_user_id);
            hasher.update(&bytes);
            crate::request_profile::work(crate::request_profile::Work::BlobContentHashBytes, bytes.len() as u64);
            if BlobId(*hasher.finalize().as_bytes()) != self.manifest.blob_id { return Err(invalid_data("blob logical content witness mismatch")); }
        } else { cache.record_reuse(self.manifest.logical_bytes); }
        let verified = VerifiedBlobBytes { bytes, identity: self.identity() };
        if !matched {
            if let Some(sources) = sources { cache.remember(self.manifest_block_id, &self.encoded_manifest, sources, &verified); }
        }
        Ok(verified)
    }
    /// The caller owns the bound's public error message. Manifest/scope checks
    /// have already run when opening; actual payload reads and complete scoped
    /// content verification precede the immutable byte witness.
    pub(crate) fn materialize_verified(
        mut self,
        maximum_bytes: usize,
        bound_error: impl Fn() -> io::Error,
    ) -> io::Result<VerifiedBlobBytes> {
        if self.manifest.logical_bytes > maximum_bytes as u64 {
            return Err(bound_error());
        }
        if self.next_chunk == 0 && self.logical_bytes_read == 0 {
            if let Some(cache) = self.decode_cache.map(|cache| &cache.whole).filter(|cache| cache.enabled_for(self.manifest.logical_bytes)) {
                return self.materialize_with_whole_cache(cache, maximum_bytes, &bound_error);
            }
        }
        let mut bytes = Vec::with_capacity(self.manifest.logical_bytes as usize);
        while let Some(chunk) = self.next_chunk_bytes()? {
            if bytes.len().saturating_add(chunk.as_slice().len()) > maximum_bytes {
                return Err(bound_error());
            }
            let _collect = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_BUFFER_COLLECT);
            bytes.extend_from_slice(chunk.as_slice());
            crate::request_profile::work(crate::request_profile::Work::BlobCollectedBytes, chunk.as_slice().len() as u64);
        }
        // next_chunk(None) establishes the full scoped content witness. A
        // partially consumed reader cannot certify only its remaining suffix.
        if !self.verified || bytes.len() as u64 != self.manifest.logical_bytes {
            return Err(invalid_data("blob materialization is not a complete verified body"));
        }
        Ok(VerifiedBlobBytes {
            bytes,
            identity: VerifiedBlobIdentity {
                tenant_id: self.manifest.tenant_id,
                owner_user_id: self.manifest.owner_user_id,
                content_hash: self.manifest.blob_id.0,
                logical_bytes: self.manifest.logical_bytes,
            },
        })
    }

    /// `witness` is the writer persona's committed-transaction list used for
    /// the atomic-commit check; replicas pass `None` (reachable-open
    /// semantics — scope and content verification are unchanged).
    pub fn open(
        backend: &'a dyn crate::entity::EntityBlockIo,
        witness: Option<Vec<crate::engine::CommittedTransaction>>,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<Self> {
        Self::open_with_commit_validation(backend, witness, tenant_id, owner_user_id, reference)
    }

    pub(crate) fn open_reachable(
        backend: &'a dyn crate::entity::EntityBlockIo,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<Self> {
        Self::open_with_commit_validation(backend, None, tenant_id, owner_user_id, reference)
    }

    fn open_with_commit_validation(
        backend: &'a dyn crate::entity::EntityBlockIo,
        witness: Option<Vec<crate::engine::CommittedTransaction>>,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<Self> {
        let _manifest = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_MANIFEST_OPEN);
        let encoded_manifest = backend.read_verified(reference.manifest_block_id)?;
        crate::request_profile::work(crate::request_profile::Work::BlobCurrentEncodedBytes, encoded_manifest.len() as u64);
        let manifest = Manifest::decode(&encoded_manifest)?;
        if manifest.tenant_id != tenant_id || manifest.owner_user_id != owner_user_id {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "blob is outside the requested owner scope",
            ));
        }
        if manifest.blob_id != reference.blob_id
            || manifest.logical_bytes != reference.logical_bytes
        {
            return Err(invalid_data("blob reference does not match its manifest"));
        }
        if let Some(transactions) = witness {
            let required: BTreeSet<_> = manifest
                .chunks
                .iter()
                .map(|chunk| chunk.block_id)
                .chain(std::iter::once(reference.manifest_block_id))
                .collect();
            let committed = transactions.iter().any(|transaction| {
                let referenced: BTreeSet<_> =
                    transaction.envelope.block_ids.iter().copied().collect();
                required.is_subset(&referenced)
            });
            if !committed {
                return Err(invalid_data(
                    "blob manifest and chunks are not atomically committed",
                ));
            }
        }
        Ok(Self {
            backend,
            decode_cache: None,
            content_hasher: content_hasher(tenant_id, owner_user_id),
            manifest,
            manifest_block_id: reference.manifest_block_id,
            encoded_manifest,
            next_chunk: 0,
            logical_bytes_read: 0,
            verified: false,
        })
    }

    /// Random logical bytes from an owner-checked, reachable manifest. Each
    /// intersecting immutable block and its decoded chunk witness is verified;
    /// unlike a sequential read this does not claim a whole-blob hash check.
    /// It deliberately leaves the sequential reader/hash cursor unchanged.
    pub(crate) fn read_range(&self, offset: u64, length: usize) -> io::Result<Vec<u8>> {
        if length > 65_536
            || offset > self.manifest.logical_bytes
            || length as u64 > self.manifest.logical_bytes - offset
        {
            return Err(invalid_input("blob diagnostic range exceeds bound"));
        }
        if length == 0 {
            return Ok(Vec::new());
        }
        let end = offset + length as u64;
        let mut output = Vec::with_capacity(length);
        let mut start = 0_u64;
        for chunk in &self.manifest.chunks {
            let next = start + u64::from(chunk.logical_bytes);
            if start >= end {
                break;
            }
            if next > offset {
                let raw = self.decode_current_chunk(chunk)?;
                let first = offset.saturating_sub(start) as usize;
                let last = (end.min(next) - start) as usize;
                output.extend_from_slice(&raw.as_slice()[first..last]);
            }
            start = next;
        }
        if output.len() != length {
            return Err(invalid_data("blob range byte witness differs"));
        }
        Ok(output)
    }

    pub fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>> {
        self.next_chunk_bytes().map(|chunk| chunk.map(ChunkBytes::into_vec))
    }

    fn next_chunk_bytes(&mut self) -> io::Result<Option<ChunkBytes>> {
        if self.next_chunk == self.manifest.chunks.len() {
            if !self.verified {
                let _hash = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_CONTENT_HASH);
                let actual = BlobId(*self.content_hasher.finalize().as_bytes());
                if self.logical_bytes_read != self.manifest.logical_bytes
                    || actual != self.manifest.blob_id
                {
                    return Err(invalid_data("blob logical content witness mismatch"));
                }
                self.verified = true;
            }
            return Ok(None);
        }
        let chunk = &self.manifest.chunks[self.next_chunk];
        let raw = self.decode_current_chunk(chunk)?;
        self.logical_bytes_read = self
            .logical_bytes_read
            .checked_add(raw.as_slice().len() as u64)
            .ok_or_else(|| invalid_data("blob reader byte count overflow"))?;
        {
            let _hash = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_CONTENT_HASH);
            self.content_hasher.update(raw.as_slice());
            crate::request_profile::work(crate::request_profile::Work::BlobContentHashBytes, raw.as_slice().len() as u64);
        }
        self.next_chunk += 1;
        Ok(Some(raw))
    }
}

#[cfg(test)]
#[path = "blob_whole_cache_tests.rs"]
mod whole_cache_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn cached_blob_reader_keeps_source_errors_bounds_scope_and_complete_hash_checks() {
        use crate::entity::EntityBlockIo;
        use crate::vfs::{DeterministicVfs, Vfs};
        use std::{cell::{Cell, RefCell}, path::Path, sync::Arc};
        struct Source<'a> {
            engine: &'a Engine, chunk: BlockId, reads: Cell<usize>, fail: Cell<bool>,
            replacement: RefCell<Option<Arc<Vec<u8>>>>,
        }
        impl EntityBlockIo for Source<'_> {
            fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
                if id == self.chunk {
                    self.reads.set(self.reads.get()+1);
                    if self.fail.get() { return Err(io::Error::other("injected source failure")); }
                    if let Some(bytes) = self.replacement.borrow().as_ref() { return Ok(Arc::clone(bytes)); }
                }
                self.engine.read_block_shared(id)
            }
            fn write_fresh(&self, _: &[u8]) -> io::Result<BlockId> { panic!("read-only fixture") }
            fn write_fresh_owned(&self, _: Vec<u8>) -> io::Result<BlockId> { panic!("read-only fixture") }
        }
        let vfs=Arc::new(DeterministicVfs::new(None)); vfs.create_dir(Path::new("/data")).unwrap();
        let mut engine=Engine::initialize_with_vfs(Path::new("/data"),vfs).unwrap();
        let bytes=vec![b'a';32768];
        let reference=stage_blob(&engine,7,11,&mut Cursor::new(&bytes),bytes.len() as u64).unwrap().commit(&mut engine,b"cache-reader").unwrap();
        let manifest=Manifest::decode(&engine.read_block(reference.manifest_block_id).unwrap()).unwrap();
        let source=Source {engine:&engine,chunk:manifest.chunks[0].block_id,reads:Cell::new(0),fail:Cell::new(false),replacement:RefCell::new(None)};
        let cache=Arc::new(decode_cache::DecodedChunkCache::default());cache.configure_capacity(1024*1024);
        let metrics=cache.observer();
        let open=||BlobReader::open_reachable(&source,7,11,reference).unwrap().with_decode_cache(&cache);
        for _ in 0..2 { assert_eq!(open().materialize_verified(bytes.len(),||invalid_data("fixture bound")).unwrap().bytes(),bytes); }
        assert_eq!((source.reads.get(),metrics.metrics().hits),(2,1));
        let before=source.reads.get();
        assert_eq!(open().materialize_verified(bytes.len()-1,||invalid_data("fixture bound")).err().unwrap().to_string(),"fixture bound");
        assert_eq!(source.reads.get(),before);
        assert_eq!(BlobReader::open_reachable(&source,7,12,reference).err().unwrap().kind(),io::ErrorKind::PermissionDenied);
        source.fail.set(true);
        assert_eq!(open().materialize_verified(bytes.len(),||invalid_data("fixture bound")).err().unwrap().to_string(),"injected source failure");
        assert_eq!(metrics.metrics().hits,1);source.fail.set(false);
        let mut corrupted=source.engine.read_block(source.chunk).unwrap();corrupted[24]^=1;
        *source.replacement.borrow_mut()=Some(Arc::new(corrupted));
        assert_eq!(open().materialize_verified(bytes.len(),||invalid_data("fixture bound")).err().unwrap().to_string(),"blob chunk logical hash mismatch");
        assert_eq!(metrics.metrics().hits,1);*source.replacement.borrow_mut()=None;
        let mut reader=open();reader.manifest.blob_id=BlobId([0;32]);
        assert_eq!(reader.materialize_verified(bytes.len(),||invalid_data("fixture bound")).err().unwrap().to_string(),"blob logical content witness mismatch");
        assert_eq!(metrics.metrics().hits,2);
        let mut mutable=open().next_chunk().unwrap().unwrap();mutable[0]=b'z';
        assert_eq!(open().read_range(0,4).unwrap(),b"aaaa");
        assert_eq!(open().materialize_verified(bytes.len(),||invalid_data("fixture bound")).unwrap().bytes(),bytes);
    }

    #[test]
    fn verified_blob_materialization_requires_all_actual_chunks_and_preserves_bounds() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        let bytes = vec![b'x'; BLOB_CHUNK_BYTES + 17];
        let reference = stage_blob(&engine, 7, 11, &mut Cursor::new(&bytes), bytes.len() as u64)
            .unwrap()
            .commit(&mut engine, b"verified-materialize")
            .unwrap();
        let open = || {
            BlobReader::open(
                &engine,
                Some(engine.transaction_snapshot().unwrap()),
                7,
                11,
                reference,
            )
            .unwrap()
        };
        let output = open()
            .materialize_verified(bytes.len(), || invalid_data("fixture bound"))
            .unwrap();
        assert_eq!(output.bytes(), bytes);
        let bound = open()
            .materialize_verified(bytes.len() - 1, || invalid_data("fixture bound"))
            .err()
            .unwrap();
        assert_eq!(bound.kind(), io::ErrorKind::InvalidData);
        assert_eq!(bound.to_string(), "fixture bound");
        let mut partial = open();
        partial.next_chunk().unwrap().unwrap();
        assert_eq!(
            partial
                .materialize_verified(bytes.len(), || invalid_data("fixture bound"))
                .err()
                .unwrap()
                .to_string(),
            "blob materialization is not a complete verified body"
        );
        assert_eq!(
            BlobReader::open(
                &engine,
                Some(engine.transaction_snapshot().unwrap()),
                7,
                12,
                reference
            )
            .err()
            .unwrap()
            .kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn verified_blob_materialization_cannot_seal_a_mismatched_logical_content_hash() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        let reference = stage_blob(&engine, 7, 11, &mut Cursor::new(b"[1,2]"), 5)
            .unwrap()
            .commit(&mut engine, b"verified-hash")
            .unwrap();
        let mut reader = BlobReader::open(
            &engine,
            Some(engine.transaction_snapshot().unwrap()),
            7,
            11,
            reference,
        )
        .unwrap();
        // Inject a wrong expected logical identity after a valid manifest open.
        // Every chunk is valid; only the complete scoped content check defeats it.
        reader.manifest.blob_id = BlobId([0; 32]);
        let error = reader
            .materialize_verified(5, || invalid_data("fixture bound"))
            .err()
            .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert_eq!(error.to_string(), "blob logical content witness mismatch");
    }

    #[test]
    fn blob_staging_commit_faults_preserve_every_acknowledged_payload() {
        use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
        use std::{path::Path, sync::Arc};
        fn prepared() -> (Arc<DeterministicVfs>, Engine, BlobReference) {
            let vfs = Arc::new(DeterministicVfs::new(None));
            vfs.create_dir(Path::new("/data")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let first = stage_blob(&engine, 7, 11, &mut Cursor::new(vec![17; 9000]), 9000)
                .unwrap().commit(&mut engine, b"first").unwrap();
            (vfs, engine, first)
        }
        let (baseline_vfs, mut baseline, _) = prepared();
        let staged = stage_blob(&baseline, 7, 11, &mut Cursor::new(vec![23; 17000]), 17000).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        staged.commit(&mut baseline, b"second").unwrap();
        let trace = baseline_vfs.trace().unwrap();
        let mut faults = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            faults.push(FaultRule { operation_number: index as u64 + 1,
                action: FaultAction::ErrorBefore(io::ErrorKind::Other) });
            if *operation == Operation::Write {
                faults.push(FaultRule { operation_number: index as u64 + 1,
                    action: FaultAction::ShortWrite(7) });
            }
        }
        assert!(!faults.is_empty());
        for fault in faults {
            let (vfs, mut engine, first) = prepared();
            let staged = stage_blob(&engine, 7, 11, &mut Cursor::new(vec![23; 17000]), 17000).unwrap();
            vfs.arm_fault(Some(fault)).unwrap();
            let acknowledged = staged.commit(&mut engine, b"second").ok();
            // Snapshot BEFORE Drop: destructor catch-up must not rescue a
            // write whose required WAL barrier was absent at power loss.
            let crash = Arc::new(vfs.fork_durable().unwrap());
            let recovered = Engine::open_with_vfs(Path::new("/data"), crash).unwrap();
            for (reference, payload) in std::iter::once((first, vec![17; 9000]))
                .chain(acknowledged.map(|reference| (reference, vec![23; 17000]))) {
                let mut reader = BlobReader::open(&recovered,
                    Some(recovered.transaction_snapshot().unwrap()), 7, 11, reference).unwrap();
                let mut actual = Vec::new();
                while let Some(chunk) = reader.next_chunk().unwrap() { actual.extend(chunk); }
                assert_eq!(actual, payload);
            }
        }
    }

    #[test]
    fn multi_chunk_blob_round_trips_after_atomic_commit_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let reference;
        let payload = vec![b'x'; BLOB_CHUNK_BYTES * 2 + 17];
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            let staged = stage_blob(
                &engine,
                7,
                11,
                &mut Cursor::new(&payload),
                payload.len() as u64,
            )
            .unwrap();
            assert_eq!(staged.transaction_block_ids.len(), 3);
            let metrics = engine.block_write_metrics();
            assert_eq!(metrics.blocks_staged, 3);
            assert!(metrics.bytes_staged < payload.len() as u64 / 10);
            assert_eq!(metrics.blocks_written, 0);
            assert!(BlobReader::open(
                &engine,
                Some(engine.transaction_snapshot().unwrap()),
                7,
                11,
                staged.reference
            )
            .is_err());
            reference = staged.commit(&mut engine, b"blob.attach").unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        let mut reader = BlobReader::open(
            &engine,
            Some(engine.transaction_snapshot().unwrap()),
            7,
            11,
            reference,
        )
        .unwrap();
        let mut restored = Vec::new();
        while let Some(chunk) = reader.next_chunk().unwrap() {
            restored.extend_from_slice(&chunk);
        }
        assert_eq!(restored, payload);
        assert!(reader.next_chunk().unwrap().is_none());
    }

    #[test]
    fn blob_owner_scope_and_request_bound_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        assert_eq!(
            stage_blob(&engine, 1, 0, &mut Cursor::new(b"internal"), 8)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        let payload = vec![3; BLOB_CHUNK_BYTES + 1];
        let error = stage_blob(
            &engine,
            1,
            2,
            &mut Cursor::new(&payload),
            BLOB_CHUNK_BYTES as u64,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);

        let staged = stage_blob(&engine, 1, 2, &mut Cursor::new(b"secret"), 6).unwrap();
        let reference = staged.commit(&mut engine, b"blob.attach").unwrap();
        let error = BlobReader::open(
            &engine,
            Some(engine.transaction_snapshot().unwrap()),
            1,
            3,
            reference,
        )
        .err()
        .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);

        let other_owner = stage_blob(&engine, 1, 3, &mut Cursor::new(b"secret"), 6).unwrap();
        assert_ne!(reference.blob_id, other_owner.reference.blob_id);
    }

    #[test]
    fn diagnostic_blob_ranges_only_decode_intersecting_chunks() {
        use crate::entity::EntityBlockIo;
        use std::cell::Cell;
        use std::sync::Arc;

        struct Counting<'a> {
            engine: &'a Engine,
            reads: Cell<usize>,
        }
        impl EntityBlockIo for Counting<'_> {
            fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
                self.reads.set(self.reads.get() + 1);
                self.engine.read_block_shared(id)
            }
            fn write_fresh(&self, _: &[u8]) -> io::Result<BlockId> {
                panic!("read-only test")
            }
            fn write_fresh_owned(&self, _: Vec<u8>) -> io::Result<BlockId> {
                panic!("read-only test")
            }
        }
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        let payload: Vec<u8> = (0..(3 * BLOB_CHUNK_BYTES + 79))
            .map(|index| (index % 251) as u8)
            .collect();
        let staged = stage_blob(
            &engine,
            1,
            2,
            &mut Cursor::new(&payload),
            payload.len() as u64,
        )
        .unwrap();
        let reference = staged.commit(&mut engine, b"blob.attach").unwrap();
        let backend = Counting {
            engine: &engine,
            reads: Cell::new(0),
        };
        let mut reader = BlobReader::open(
            &backend,
            Some(engine.transaction_snapshot().unwrap()),
            1,
            2,
            reference,
        )
        .unwrap();
        backend.reads.set(0);
        let offset = BLOB_CHUNK_BYTES - 7;
        assert_eq!(
            reader.read_range(offset as u64, 23).unwrap(),
            payload[offset..offset + 23]
        );
        assert_eq!(backend.reads.get(), 2);
        backend.reads.set(0);
        assert!(reader
            .read_range(payload.len() as u64, 0)
            .unwrap()
            .is_empty());
        assert_eq!(backend.reads.get(), 0);
        assert!(reader.read_range(payload.len() as u64 + 1, 0).is_err());
        assert!(reader.read_range(0, 65_537).is_err());
        // Random reads leave the sequential whole-blob witness untouched.
        let mut all = Vec::new();
        while let Some(chunk) = reader.next_chunk().unwrap() {
            all.extend_from_slice(&chunk);
        }
        assert_eq!(all, payload);
    }

    #[test]
    fn reusable_encoder_preserves_exact_existing_chunk_bytes_across_sizes() {
        let encoder = ChunkEncoder::default();
        for size in [8192, BLOB_CHUNK_BYTES, 1, 32768, 8192] {
            let mut raw = vec![0; size];
            blake3::Hasher::new().update(&(size as u64).to_le_bytes())
                .finalize_xof().fill(&mut raw);
            for value in [raw, vec![b'x'; size]] {
                let encoded = encoder.encode(&value).unwrap();
                assert_eq!(encoded, encode_chunk(&value).unwrap());
                assert_eq!(decode_chunk(&encoded, size as u32).unwrap(), value);
            }
        }
        assert!(encoder.encode(&[]).is_err());
        assert!(encoder.encode(&vec![0; BLOB_CHUNK_BYTES + 1]).is_err());
    }

    #[test]
    fn blob_read_buffers_follow_remaining_admission_and_reject_overflow() {
        struct BoundedReader { input: Cursor<Vec<u8>>, maximum: usize }
        impl Read for BoundedReader {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                assert!(buffer.len() <= self.maximum,
                    "small blob exceeded its admitted read-buffer bound");
                self.input.read(buffer)
            }
        }
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        for size in [0, 8192, BLOB_CHUNK_BYTES, BLOB_CHUNK_BYTES + 1] {
            let payload = vec![b'b'; size];
            let mut input = BoundedReader { input: Cursor::new(payload.clone()),
                maximum: (size + 1).min(BLOB_CHUNK_BYTES) };
            let staged = stage_blob(&engine, 7, 11, &mut input, size as u64).unwrap();
            let reference = staged.commit(&mut engine, b"bounded-read").unwrap();
            let mut reader = BlobReader::open(&engine,
                Some(engine.transaction_snapshot().unwrap()), 7, 11, reference).unwrap();
            let mut restored = Vec::new();
            while let Some(chunk) = reader.next_chunk().unwrap() { restored.extend(chunk); }
            assert_eq!(restored, payload);
        }
        let before = engine.block_write_metrics().blocks_staged;
        let error = stage_blob(&engine, 7, 11, &mut BoundedReader {
            input: Cursor::new(vec![1; 8193]), maximum: 8193 }, 8192).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(engine.block_write_metrics().blocks_staged, before);
    }

    #[test]
    fn incompressible_chunk_uses_bounded_raw_encoding() {
        let mut raw = vec![0; BLOB_CHUNK_BYTES];
        for (index, byte) in raw.iter_mut().enumerate() {
            *byte = blake3::hash(&(index as u64).to_le_bytes()).as_bytes()[0];
        }
        let encoded = encode_chunk(&raw).unwrap();
        assert_eq!(encoded[12], CODEC_RAW);
        assert_eq!(
            decode_chunk(&encoded, BLOB_CHUNK_BYTES as u32).unwrap(),
            raw
        );
    }
}
