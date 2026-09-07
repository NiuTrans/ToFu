//! Owner-scoped, chunked blobs staged into immutable blocks and committed atomically.

use std::collections::BTreeSet;
use std::io::{self, Read};

use crate::block::BlockId;
use crate::engine::Engine;

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
    engine: &'a Engine,
    manifest: Manifest,
    next_chunk: usize,
    logical_bytes_read: u64,
    content_hasher: blake3::Hasher,
    verified: bool,
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

fn encode_chunk(raw: &[u8]) -> io::Result<Vec<u8>> {
    if raw.is_empty() || raw.len() > BLOB_CHUNK_BYTES {
        return Err(invalid_input("blob chunk is empty or exceeds 1 MiB"));
    }
    let compressed = zstd::bulk::compress(raw, 3)?;
    let (codec, payload) = if compressed.len() + COMPRESSION_MINIMUM_SAVING_BYTES < raw.len() {
        (CODEC_ZSTD, compressed.as_slice())
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
    let raw = match codec {
        CODEC_RAW if payload.len() == logical_bytes as usize => payload.to_vec(),
        CODEC_ZSTD => zstd::bulk::decompress(payload, logical_bytes as usize)
            .map_err(|_| invalid_data("blob chunk decompression failed"))?,
        _ => return Err(invalid_data("unsupported blob chunk codec")),
    };
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
        let mut raw = vec![0; BLOB_CHUNK_BYTES];
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
        raw.truncate(used);
        logical_bytes = logical_bytes
            .checked_add(used as u64)
            .ok_or_else(|| invalid_input("blob length overflow"))?;
        if logical_bytes > maximum_bytes {
            return Err(invalid_input("blob exceeds its request admission bound"));
        }
        hasher.update(&raw);
        let block_id = engine.write_block(&encode_chunk(&raw)?)?;
        unique_block_ids.insert(block_id);
        chunks.push(ChunkReference {
            block_id,
            logical_bytes: used as u32,
        });
        if chunks.len() > MAX_BLOB_CHUNKS {
            return Err(invalid_input("blob exceeds its chunk count bound"));
        }
    }
    let blob_id = BlobId(*hasher.finalize().as_bytes());
    let manifest = Manifest {
        tenant_id,
        owner_user_id,
        logical_bytes,
        blob_id,
        chunks,
    };
    let manifest_block_id = engine.write_block(&manifest.encode()?)?;
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
    pub fn open(
        engine: &'a Engine,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<Self> {
        Self::open_with_commit_validation(engine, tenant_id, owner_user_id, reference, true)
    }

    pub(crate) fn open_reachable(
        engine: &'a Engine,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<Self> {
        Self::open_with_commit_validation(engine, tenant_id, owner_user_id, reference, false)
    }

    fn open_with_commit_validation(
        engine: &'a Engine,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
        require_transaction_witness: bool,
    ) -> io::Result<Self> {
        let manifest = Manifest::decode(&engine.read_block(reference.manifest_block_id)?)?;
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
        if require_transaction_witness {
            let required: BTreeSet<_> = manifest
                .chunks
                .iter()
                .map(|chunk| chunk.block_id)
                .chain(std::iter::once(reference.manifest_block_id))
                .collect();
            let transactions = engine.transaction_snapshot()?;
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
            engine,
            content_hasher: content_hasher(tenant_id, owner_user_id),
            manifest,
            next_chunk: 0,
            logical_bytes_read: 0,
            verified: false,
        })
    }

    pub fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>> {
        if self.next_chunk == self.manifest.chunks.len() {
            if !self.verified {
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
        let raw = decode_chunk(
            &self.engine.read_block(chunk.block_id)?,
            chunk.logical_bytes,
        )?;
        self.logical_bytes_read = self
            .logical_bytes_read
            .checked_add(raw.len() as u64)
            .ok_or_else(|| invalid_data("blob reader byte count overflow"))?;
        self.content_hasher.update(&raw);
        self.next_chunk += 1;
        Ok(Some(raw))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

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
            assert_eq!(metrics.blocks_written, 3);
            assert!(metrics.bytes_written < payload.len() as u64 / 10);
            assert!(BlobReader::open(&engine, 7, 11, staged.reference).is_err());
            reference = staged.commit(&mut engine, b"blob.attach").unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        let mut reader = BlobReader::open(&engine, 7, 11, reference).unwrap();
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
        let error = BlobReader::open(&engine, 1, 3, reference).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);

        let other_owner = stage_blob(&engine, 1, 3, &mut Cursor::new(b"secret"), 6).unwrap();
        assert_ne!(reference.blob_id, other_owner.reference.blob_id);
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
