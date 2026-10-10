//! Whole-blob proof fences exercised against a replaceable, failing current source.
use super::*;
use std::{cell::{Cell, RefCell}, collections::BTreeMap, sync::Arc};
struct Source {
    blocks: RefCell<BTreeMap<BlockId, Arc<Vec<u8>>>>,
    reads: Cell<usize>,
    failure: Cell<Option<BlockId>>,
}
impl crate::entity::EntityBlockIo for Source {
    fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.reads.set(self.reads.get() + 1);
        if self.failure.get() == Some(id) { return Err(io::Error::other("current source failed")); }
        self.blocks.borrow().get(&id).cloned().ok_or_else(|| invalid_data("missing current source"))
    }
    fn write_fresh(&self, _: &[u8]) -> io::Result<BlockId> { panic!("read only") }
    fn write_fresh_owned(&self, _: Vec<u8>) -> io::Result<BlockId> { panic!("read only") }
}
fn fixture() -> (Source, BlobReference, Vec<u8>, Vec<BlockId>) {
    let chunks = [vec![b'a'; BLOB_CHUNK_BYTES], vec![b'b'; BLOB_CHUNK_BYTES]];
    let bytes = chunks.concat();
    let mut hasher = content_hasher(7, 11); hasher.update(&bytes);
    let mut blocks = BTreeMap::new(); let mut references = Vec::new(); let mut ids = Vec::new();
    for raw in chunks {
        let encoded = Arc::new(encode_chunk(&raw).unwrap());
        let id = BlockId::for_payload(&encoded);
        blocks.insert(id, encoded); ids.push(id);
        references.push(ChunkReference { block_id: id, logical_bytes: raw.len() as u32 });
    }
    let manifest = Manifest { tenant_id: 7, owner_user_id: 11, logical_bytes: bytes.len() as u64, blob_id: BlobId(*hasher.finalize().as_bytes()), chunks: references };
    let encoded = Arc::new(manifest.encode().unwrap()); let id = BlockId::for_payload(&encoded);
    let reference = BlobReference { blob_id: manifest.blob_id, manifest_block_id: id, logical_bytes: manifest.logical_bytes };
    blocks.insert(id, encoded);
    (Source { blocks: RefCell::new(blocks), reads: Cell::new(0), failure: Cell::new(None) }, reference, bytes, ids)
}
fn read(source: &Source, cache: &decode_cache::DecodedChunkCache, reference: BlobReference) -> io::Result<VerifiedBlobBytes> {
    BlobReader::open_reachable(source, 7, 11, reference)?.with_decode_cache(cache).materialize_verified(reference.logical_bytes as usize, || invalid_data("bound"))
}
#[test]
fn whole_blob_proof_requires_current_complete_dependencies_and_preserves_streaming() {
    let (source, reference, bytes, _) = fixture();
    let cache = decode_cache::DecodedChunkCache::default(); cache.configure_capacity(16 * 1024 * 1024);
    assert_eq!(read(&source, &cache, reference).unwrap().bytes(), bytes);
    assert_eq!(read(&source, &cache, reference).unwrap().bytes(), bytes);
    assert_eq!(source.reads.get(), 6);
    assert_eq!(cache.whole.metrics().reused, 1);
    let mut output = read(&source, &cache, reference).unwrap().into_bytes(); output[0] = b'z';
    assert_eq!(read(&source, &cache, reference).unwrap().bytes(), bytes);
    let before = cache.whole.metrics().reused;
    let mut reader = BlobReader::open_reachable(&source, 7, 11, reference).unwrap().with_decode_cache(&cache);
    assert_eq!(reader.read_range(BLOB_CHUNK_BYTES as u64 - 2, 4).unwrap(), b"aabb");
    assert_eq!(reader.next_chunk().unwrap().unwrap(), bytes[..BLOB_CHUNK_BYTES]);
    assert_eq!(reader.materialize_verified(bytes.len(), || invalid_data("bound")).err().unwrap().to_string(), "blob materialization is not a complete verified body");
    assert_eq!(cache.whole.metrics().reused, before);
}
#[test]
fn whole_blob_proof_cannot_hide_source_failure_replacement_or_bad_scoped_content() {
    let (source, reference, bytes, ids) = fixture();
    let cache = decode_cache::DecodedChunkCache::default(); cache.configure_capacity(16 * 1024 * 1024);
    read(&source, &cache, reference).unwrap();
    for id in [reference.manifest_block_id, ids[0], ids[1]] {
        source.failure.set(Some(id));
        assert_eq!(read(&source, &cache, reference).err().unwrap().to_string(), "current source failed");
        assert_eq!(cache.whole.metrics().reused, 0);
    }
    source.failure.set(None);
    // Equal bytes in a fresh allocation must be checked and certified anew.
    let old = source.blocks.borrow()[&ids[1]].clone();
    source.blocks.borrow_mut().insert(ids[1], Arc::new(old.as_ref().clone()));
    assert_eq!(read(&source, &cache, reference).unwrap().bytes(), bytes);
    assert_eq!(cache.whole.metrics().reused, 0);
    read(&source, &cache, reference).unwrap(); assert_eq!(cache.whole.metrics().reused, 1);
    let mut invalid = old.as_ref().clone(); invalid[24] ^= 1;
    source.blocks.borrow_mut().insert(ids[1], Arc::new(invalid));
    assert_eq!(read(&source, &cache, reference).err().unwrap().to_string(), "blob chunk logical hash mismatch");
    // A valid decoded replacement with a different body still fails BlobId.
    source.blocks.borrow_mut().insert(ids[1], Arc::new(encode_chunk(&vec![b'c'; BLOB_CHUNK_BYTES]).unwrap()));
    assert_eq!(read(&source, &cache, reference).err().unwrap().to_string(), "blob logical content witness mismatch");
    assert_eq!(cache.whole.metrics().reused, 1);
    source.blocks.borrow_mut().insert(ids[1], old);
    let original = source.blocks.borrow()[&reference.manifest_block_id].clone();
    let mut manifest = Manifest::decode(&original).unwrap(); manifest.chunks.swap(0, 1);
    source.blocks.borrow_mut().insert(reference.manifest_block_id, Arc::new(manifest.encode().unwrap()));
    assert_eq!(read(&source, &cache, reference).err().unwrap().to_string(), "blob logical content witness mismatch");
    source.blocks.borrow_mut().insert(reference.manifest_block_id, Arc::new(original.as_ref().clone()));
    assert_eq!(read(&source, &cache, reference).unwrap().bytes(), bytes);
    assert_eq!(cache.whole.metrics().reused, 1);
}
#[test]
fn whole_blob_proof_keeps_scope_bounds_atomic_commit_and_small_body_fences() {
    let (source, reference, bytes, _) = fixture();
    let cache = decode_cache::DecodedChunkCache::default(); cache.configure_capacity(16 * 1024 * 1024);
    read(&source, &cache, reference).unwrap();
    let before = source.reads.get();
    let reader = BlobReader::open_reachable(&source, 7, 11, reference).unwrap().with_decode_cache(&cache);
    assert_eq!(reader.materialize_verified(bytes.len()-1, || invalid_data("bound")).err().unwrap().to_string(), "bound");
    assert_eq!(source.reads.get(), before+1);
    assert_eq!(BlobReader::open_reachable(&source, 8, 11, reference).err().unwrap().kind(), io::ErrorKind::PermissionDenied);
    assert_eq!(BlobReader::open_reachable(&source, 7, 12, reference).err().unwrap().kind(), io::ErrorKind::PermissionDenied);
    assert_eq!(BlobReader::open(&source, Some(vec![]), 7, 11, reference).err().unwrap().to_string(), "blob manifest and chunks are not atomically committed");
    let mut reader = BlobReader::open_reachable(&source, 7, 11, reference).unwrap().with_decode_cache(&cache);
    reader.manifest.blob_id = BlobId([0;32]);
    assert_eq!(reader.materialize_verified(bytes.len(), || invalid_data("bound")).err().unwrap().to_string(), "blob logical content witness mismatch");
    assert_eq!(cache.whole.metrics().reused, 0);
    assert!(!cache.whole.enabled_for(4095)); assert!(cache.whole.enabled_for(4096));
    cache.configure_capacity(0); assert!(!cache.whole.enabled_for(bytes.len() as u64));
    assert_eq!(read(&source, &cache, reference).unwrap().bytes(), bytes);
    assert_eq!(cache.whole.metrics().entries, 0);
}

#[test]
fn whole_blob_proof_oversized_dependencies_fall_back_without_retained_pins() {
    let (source, reference, bytes, _) = fixture();
    let cache = decode_cache::DecodedChunkCache::default(); cache.configure_capacity(4609 * 4);
    assert!(cache.whole.enabled_for(reference.logical_bytes));
    for _ in 0..2 { assert_eq!(read(&source, &cache, reference).unwrap().bytes(), bytes); }
    assert_eq!(cache.whole.metrics().reused, 0); assert_eq!(cache.whole.metrics().accounted_bytes, 0);
    for encoded in source.blocks.borrow().values() { assert_eq!(Arc::strong_count(encoded), 1); }
}
