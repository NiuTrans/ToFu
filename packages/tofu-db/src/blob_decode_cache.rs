//! Budgeted decompression/hash certificates bound to the exact current immutable
//! encoded block. Entry: DecodedChunkCache::decode after source reading succeeds.
//! Scope/manifest checks and whole-blob proof ownership stay in BlobReader.

use super::{decode_chunk, ChunkBytes, CHUNK_HEADER_BYTES, CODEC_ZSTD};
use crate::block::BlockId;
use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

pub(crate) const HOT_CACHE_BUDGET_DIVISOR: usize = 16;
pub(crate) fn whole_cache_budget_divisor() -> usize { super::whole_cache::HOT_CACHE_BUDGET_DIVISOR }
const BASE_BYTES: usize = 4096;
const ENTRY_BYTES: usize = 512;
struct Entry {
    encoded: Arc<Vec<u8>>,
    decoded: Arc<Vec<u8>>,
    charge: usize,
    access: u64,
}
#[derive(Default)]
struct State {
    entries: BTreeMap<BlockId, Entry>,
    recency: BTreeMap<u64, BlockId>,
    capacity: usize,
    payload_bytes: usize,
    tick: u64,
    hits: u64,
    misses: u64,
    evictions: u64,
}
impl State {
    fn accounted(&self) -> usize {
        if self.entries.is_empty() {
            0
        } else {
            BASE_BYTES + self.payload_bytes
        }
    }
    fn clear_records(&mut self) {
        self.entries = BTreeMap::new();
        self.recency = BTreeMap::new();
        self.payload_bytes = 0;
    }
    fn next_access(&mut self) -> u64 {
        if self.tick == u64::MAX {
            self.evictions = self.evictions.saturating_add(self.entries.len() as u64);
            self.clear_records();
            self.tick = 0;
        }
        self.tick += 1;
        self.tick
    }
    fn get(&mut self, id: BlockId, encoded: &Arc<Vec<u8>>, logical: u32) -> Option<Arc<Vec<u8>>> {
        let access = self.next_access();
        let Some(entry) = self.entries.get_mut(&id).filter(|entry| {
            Arc::ptr_eq(&entry.encoded, encoded) && entry.decoded.len() == logical as usize
        }) else {
            self.misses = self.misses.saturating_add(1);
            return None;
        };
        self.recency.remove(&entry.access);
        entry.access = access;
        self.recency.insert(access, id);
        self.hits = self.hits.saturating_add(1);
        Some(Arc::clone(&entry.decoded))
    }
    fn trim(&mut self) {
        while self.accounted() > self.capacity {
            let Some((_, id)) = self.recency.pop_first() else {
                break;
            };
            if let Some(entry) = self.entries.remove(&id) {
                self.payload_bytes -= entry.charge;
                self.evictions = self.evictions.saturating_add(1);
            }
        }
        if self.entries.is_empty() {
            self.clear_records();
        }
    }
    fn insert(&mut self, id: BlockId, encoded: &Arc<Vec<u8>>, decoded: &Arc<Vec<u8>>) {
        let Some(charge) = encoded
            .capacity()
            .checked_add(decoded.capacity())
            .and_then(|n| n.checked_add(ENTRY_BYTES))
        else {
            return;
        };
        if charge > self.capacity.saturating_sub(BASE_BYTES) {
            return;
        }
        let access = self.next_access();
        if let Some(old) = self.entries.remove(&id) {
            self.recency.remove(&old.access);
            self.payload_bytes -= old.charge;
        }
        self.entries.insert(
            id,
            Entry {
                encoded: Arc::clone(encoded),
                decoded: Arc::clone(decoded),
                charge,
                access,
            },
        );
        self.recency.insert(access, id);
        self.payload_bytes += charge;
        self.trim();
    }
}
#[derive(Default)]
pub(crate) struct DecodedChunkCache {
    state: Mutex<State>,
    pub(super) whole: super::whole_cache::WholeBlobCache,
}
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DecodedChunkCacheMetrics {
    pub whole_blob: super::whole_cache::WholeBlobCacheMetrics,
    pub available: bool,
    pub capacity_bytes: usize,
    pub accounted_bytes: usize,
    pub entries: usize,
    pub recency_entries: usize,
    pub hits: u64,
    pub misses: u64,
    pub evictions: u64,
}
#[derive(Clone)]
pub struct DecodedChunkCacheObserver(Arc<DecodedChunkCache>);
impl DecodedChunkCacheObserver {
    pub fn metrics(&self) -> DecodedChunkCacheMetrics {
        self.0.metrics()
    }
}
impl DecodedChunkCache {
    pub(crate) fn observer(self: &Arc<Self>) -> DecodedChunkCacheObserver {
        DecodedChunkCacheObserver(Arc::clone(self))
    }
    pub(crate) fn configure_capacity(&self, capacity: usize) {
        self.whole.configure_capacity(capacity / (super::whole_cache::HOT_CACHE_BUDGET_DIVISOR / HOT_CACHE_BUDGET_DIVISOR));
        if let Ok(mut state) = self.state.lock() {
            state.capacity = capacity;
            state.trim();
        }
    }
    pub(crate) fn decode(
        &self,
        id: BlockId,
        encoded: &Arc<Vec<u8>>,
        logical: u32,
    ) -> std::io::Result<ChunkBytes> {
        // Raw chunks gain no decompression reuse; malformed headers still enter
        // the original decoder. Never infer a certificate from a header alone.
        if encoded.len() < CHUNK_HEADER_BYTES || encoded[12] != CODEC_ZSTD {
            return decode_chunk(encoded, logical).map(ChunkBytes::Owned);
        }
        let decoded = {
            let _access = crate::fsync_stats::StageTimer::diagnostic(
                crate::fsync_stats::STAGE_BLOB_CHUNK_CACHE_ACCESS,
            );
            self.state
                .lock()
                .ok()
                .and_then(|mut state| state.get(id, encoded, logical))
        };
        if let Some(decoded) = decoded {
            crate::request_profile::work(crate::request_profile::Work::BlobChunkProofReuse, 1);
            crate::request_profile::work(
                crate::request_profile::Work::BlobChunkProofBytes,
                decoded.len() as u64,
            );
            return Ok(ChunkBytes::Shared(decoded));
        }
        // Validation/decompression never holds the optional metadata lock.
        let decoded = decode_chunk(encoded, logical)?;
        let mut state = match self.state.lock() {
            Ok(state) => state,
            Err(_) => return Ok(ChunkBytes::Owned(decoded)),
        };
        let fits = encoded
            .capacity()
            .checked_add(decoded.capacity())
            .and_then(|bytes| bytes.checked_add(BASE_BYTES + ENTRY_BYTES))
            .is_some_and(|bytes| bytes <= state.capacity);
        if !fits {
            return Ok(ChunkBytes::Owned(decoded));
        }
        let decoded = Arc::new(decoded);
        state.insert(id, encoded, &decoded);
        Ok(ChunkBytes::Shared(decoded))
    }
    fn metrics(&self) -> DecodedChunkCacheMetrics {
        let state = self.state.lock().ok();
        DecodedChunkCacheMetrics {
            whole_blob: self.whole.metrics(),
            available: state.is_some(),
            capacity_bytes: state.as_ref().map_or(0, |s| s.capacity),
            accounted_bytes: state.as_ref().map_or(0, |s| s.accounted()),
            entries: state.as_ref().map_or(0, |s| s.entries.len()),
            recency_entries: state.as_ref().map_or(0, |s| s.recency.len()),
            hits: state.as_ref().map_or(0, |s| s.hits),
            misses: state.as_ref().map_or(0, |s| s.misses),
            evictions: state.as_ref().map_or(0, |s| s.evictions),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn encoded(byte: u8) -> Arc<Vec<u8>> {
        Arc::new(super::super::encode_chunk(&vec![byte; 32768]).unwrap())
    }
    #[test]
    fn decoded_chunk_cache_binds_current_immutable_source_and_logical_length() {
        let cache = DecodedChunkCache::default();
        cache.configure_capacity(1024 * 1024);
        let encoded = encoded(b'a');
        let id = BlockId::for_payload(&encoded);
        assert_eq!(
            cache.decode(id, &encoded, 32768).unwrap().as_slice(),
            vec![b'a'; 32768]
        );
        let certificate = cache.decode(id, &encoded, 32768).unwrap();
        assert_eq!(certificate.as_slice(), vec![b'a'; 32768]);
        assert_eq!(cache.metrics().hits, 1);
        let other_allocation = Arc::new(encoded.as_ref().clone());
        cache.decode(id, &other_allocation, 32768).unwrap();
        assert_eq!(cache.metrics().hits, 1);
        assert_eq!(
            cache
                .decode(id, &other_allocation, 32767)
                .err()
                .unwrap()
                .to_string(),
            "blob chunk header or length mismatch"
        );
        let mut corrupted = other_allocation.as_ref().clone();
        corrupted[24] ^= 1;
        assert_eq!(
            cache
                .decode(id, &Arc::new(corrupted), 32768)
                .err()
                .unwrap()
                .to_string(),
            "blob chunk logical hash mismatch"
        );
        assert_eq!(certificate.as_slice(), vec![b'a'; 32768]);
    }
    #[test]
    fn decoded_chunk_cache_bounds_payload_capacity_recency_and_releases_all_pins() {
        let cache = DecodedChunkCache::default();
        let a = encoded(b'a');
        let b = encoded(b'b');
        let ai = BlockId::for_payload(&a);
        let bi = BlockId::for_payload(&b);
        cache.configure_capacity(BASE_BYTES + ENTRY_BYTES + a.capacity() + 32768);
        let decoded = cache.decode(ai, &a, 32768).unwrap();
        let decoded = match decoded {
            ChunkBytes::Shared(p) => p,
            _ => panic!("not admitted"),
        };
        let weak = Arc::downgrade(&decoded);
        drop(decoded);
        for _ in 0..10000 {
            cache.decode(ai, &a, 32768).unwrap();
        }
        assert_eq!(cache.metrics().recency_entries, 1);
        assert_eq!(cache.metrics().entries, 1);
        cache.decode(bi, &b, 32768).unwrap();
        assert!(weak.upgrade().is_none());
        assert!(cache.metrics().accounted_bytes <= cache.metrics().capacity_bytes);
        cache.state.lock().unwrap().tick = u64::MAX;
        cache.decode(ai, &a, 32768).unwrap();
        assert_eq!(cache.metrics().entries, 1);
        cache.configure_capacity(0);
        assert_eq!(
            (
                cache.metrics().entries,
                cache.metrics().recency_entries,
                cache.metrics().accounted_bytes
            ),
            (0, 0, 0)
        );
        cache.decode(ai, &a, 32768).unwrap();
        assert_eq!(cache.metrics().accounted_bytes, 0);
    }
    #[test]
    fn decoded_chunk_cache_poison_falls_back_and_never_admits_invalid_chunks() {
        let cache = DecodedChunkCache::default();
        cache.configure_capacity(1024 * 1024);
        let encoded = encoded(b'a');
        let id = BlockId::for_payload(&encoded);
        cache.decode(id, &encoded, 32768).unwrap();
        let _ = std::panic::catch_unwind(|| {
            let _state = cache.state.lock().unwrap();
            panic!("fixture poison")
        });
        assert!(!cache.metrics().available);
        assert_eq!(
            cache.decode(id, &encoded, 32768).unwrap().as_slice(),
            vec![b'a'; 32768]
        );
        assert!(cache.decode(id, &encoded, 1).is_err());
    }
}
