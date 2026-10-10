//! Volatile whole-blob certificates owned by BlobReader. Only a complete scoped
//! hash creates an entry. Every reuse follows successful current manifest/chunk
//! reads and exact immutable allocation comparisons; no BlockId-only shortcut.

use super::{VerifiedBlobBytes, VerifiedBlobIdentity};
use crate::block::BlockId;
use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

pub(crate) const HOT_CACHE_BUDGET_DIVISOR: usize = 64;
// Below one page, hashing is cheaper than admitting dependency metadata.
pub(super) const MINIMUM_LOGICAL_BYTES: u64 = 4096;
const BASE_BYTES: usize = 4096;
const ENTRY_BYTES: usize = 512;

pub(super) struct Certificate {
    manifest: Arc<Vec<u8>>,
    identity: VerifiedBlobIdentity,
    sources: Vec<Arc<Vec<u8>>>,
}
impl Certificate {
    pub(super) fn matches_chunk(&self, index: usize, encoded: &Arc<Vec<u8>>) -> bool {
        self.sources.get(index).is_some_and(|source| Arc::ptr_eq(source, encoded))
    }
    pub(super) fn matches_count(&self, count: usize) -> bool { self.sources.len() == count }
}
struct Entry { certificate: Arc<Certificate>, charge: usize, access: u64 }
#[derive(Default)]
struct State {
    entries: BTreeMap<BlockId, Entry>,
    recency: BTreeMap<u64, BlockId>,
    capacity: usize,
    payload_bytes: usize,
    tick: u64,
    lookups: u64,
    manifest_matches: u64,
    evictions: u64,
}
impl State {
    fn accounted(&self) -> usize {
        if self.entries.is_empty() { 0 } else { BASE_BYTES + self.payload_bytes }
    }
    fn clear_records(&mut self) {
        self.entries = BTreeMap::new(); self.recency = BTreeMap::new(); self.payload_bytes = 0;
    }
    fn next_access(&mut self) -> u64 {
        if self.tick == u64::MAX {
            self.evictions = self.evictions.saturating_add(self.entries.len() as u64);
            self.clear_records(); self.tick = 0;
        }
        self.tick += 1; self.tick
    }
    fn trim(&mut self) {
        while self.accounted() > self.capacity {
            let Some((_, id)) = self.recency.pop_first() else { break; };
            if let Some(entry) = self.entries.remove(&id) {
                self.payload_bytes -= entry.charge;
                self.evictions = self.evictions.saturating_add(1);
            }
        }
        if self.entries.is_empty() { self.clear_records(); }
    }
}
#[derive(Default)]
pub(super) struct WholeBlobCache {
    state: Mutex<State>,
    capacity: AtomicUsize,
    reused: AtomicU64,
}
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WholeBlobCacheMetrics {
    pub available: bool,
    pub capacity_bytes: usize,
    pub accounted_bytes: usize,
    pub entries: usize,
    pub recency_entries: usize,
    pub lookups: u64,
    pub manifest_matches: u64,
    pub reused: u64,
    pub evictions: u64,
}
impl WholeBlobCache {
    pub(super) fn configure_capacity(&self, capacity: usize) {
        if let Ok(mut state) = self.state.lock() {
            state.capacity = capacity; state.trim();
            self.capacity.store(capacity, Ordering::Relaxed);
        }
    }
    pub(super) fn enabled_for(&self, logical: u64) -> bool {
        logical >= MINIMUM_LOGICAL_BYTES && self.capacity.load(Ordering::Relaxed) > BASE_BYTES + ENTRY_BYTES
    }
    pub(super) fn dependency_capacity(&self) -> usize { self.capacity.load(Ordering::Relaxed) }
    pub(super) fn lookup(&self, id: BlockId, manifest: &Arc<Vec<u8>>, identity: VerifiedBlobIdentity) -> Option<Arc<Certificate>> {
        let _access = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_WHOLE_PROOF_LOOKUP);
        let mut state = self.state.lock().ok()?;
        state.lookups = state.lookups.saturating_add(1);
        let access = state.next_access();
        let entry = state.entries.get_mut(&id).filter(|entry| {
            Arc::ptr_eq(&entry.certificate.manifest, manifest) && entry.certificate.identity == identity
        })?;
        let old_access = entry.access;
        entry.access = access;
        let certificate = Arc::clone(&entry.certificate);
        state.recency.remove(&old_access); state.recency.insert(access, id);
        state.manifest_matches = state.manifest_matches.saturating_add(1);
        Some(certificate)
    }
    pub(super) fn record_reuse(&self, logical: u64) {
        self.reused.fetch_add(1, Ordering::Relaxed);
        crate::request_profile::work(crate::request_profile::Work::BlobWholeProofReuse, 1);
        crate::request_profile::work(crate::request_profile::Work::BlobWholeProofBytes, logical);
    }
    pub(super) fn remember(&self, id: BlockId, manifest: &Arc<Vec<u8>>, sources: Vec<Arc<Vec<u8>>>, verified: &VerifiedBlobBytes) {
        let _store = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_BLOB_WHOLE_PROOF_STORE);
        // Charge every retained encoded capacity, including repeated sources;
        // Arc/vector/control blocks plus two bounded tree records fit ENTRY_BYTES.
        let charge = sources.iter().try_fold(manifest.capacity(), |n, source| n.checked_add(source.capacity()))
            .and_then(|n| n.checked_add(sources.capacity().checked_mul(std::mem::size_of::<Arc<Vec<u8>>>() + std::mem::size_of::<Vec<u8>>() + 2 * std::mem::size_of::<usize>())?))
            .and_then(|n| n.checked_add(ENTRY_BYTES));
        let Some(charge) = charge else { return; };
        let Ok(mut state) = self.state.lock() else { return; };
        if charge > state.capacity.saturating_sub(BASE_BYTES) { return; }
        let access = state.next_access();
        if let Some(old) = state.entries.remove(&id) {
            state.recency.remove(&old.access); state.payload_bytes -= old.charge;
        }
        let certificate = Arc::new(Certificate { manifest: Arc::clone(manifest), identity: verified.identity(), sources });
        state.entries.insert(id, Entry { certificate, charge, access });
        state.recency.insert(access, id); state.payload_bytes += charge; state.trim();
    }
    pub(super) fn metrics(&self) -> WholeBlobCacheMetrics {
        let state = self.state.lock().ok();
        WholeBlobCacheMetrics {
            available: state.is_some(), capacity_bytes: state.as_ref().map_or(0, |s| s.capacity),
            accounted_bytes: state.as_ref().map_or(0, |s| s.accounted()),
            entries: state.as_ref().map_or(0, |s| s.entries.len()), recency_entries: state.as_ref().map_or(0, |s| s.recency.len()),
            lookups: state.as_ref().map_or(0, |s| s.lookups), manifest_matches: state.as_ref().map_or(0, |s| s.manifest_matches),
            reused: self.reused.load(Ordering::Relaxed), evictions: state.as_ref().map_or(0, |s| s.evictions),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn verified(byte: u8) -> VerifiedBlobBytes {
        VerifiedBlobBytes { bytes: vec![byte; 4096], identity: VerifiedBlobIdentity { tenant_id: 7, owner_user_id: 11, content_hash: [byte;32], logical_bytes: 4096 } }
    }
    #[test]
    fn whole_blob_cache_accounts_pins_evicts_and_releases_empty_roots() {
        let cache = WholeBlobCache::default(); cache.configure_capacity(6000);
        let manifest = Arc::new(vec![1;100]); let source = Arc::new(vec![2;100]);
        let weak = Arc::downgrade(&source); let first = BlockId::for_payload(&[1]); let second = BlockId::for_payload(&[2]);
        cache.remember(first, &manifest, vec![source.clone()], &verified(1));
        let expected = BASE_BYTES + ENTRY_BYTES + manifest.capacity() + source.capacity() + std::mem::size_of::<Arc<Vec<u8>>>() + std::mem::size_of::<Vec<u8>>() + 2*std::mem::size_of::<usize>();
        assert_eq!(cache.metrics().accounted_bytes, expected);
        let in_flight = cache.lookup(first, &manifest, verified(1).identity()).unwrap();
        cache.configure_capacity(expected);
        cache.remember(second, &manifest, vec![Arc::new(vec![3;100])], &verified(2));
        assert_eq!(cache.metrics().entries, 1); assert_eq!(cache.metrics().recency_entries, 1);
        assert!(cache.lookup(first, &manifest, verified(1).identity()).is_none());
        assert!(in_flight.matches_chunk(0, &source));
        drop(source); cache.configure_capacity(0);
        assert_eq!(cache.metrics().accounted_bytes, 0); assert_eq!(cache.metrics().entries, 0); assert_eq!(cache.metrics().recency_entries, 0);
        assert!(weak.upgrade().is_some()); drop(in_flight); assert!(weak.upgrade().is_none());
        cache.configure_capacity(6000);
        cache.remember(first, &manifest, vec![Arc::new(vec![0;6000])], &verified(1));
        assert_eq!(cache.metrics().entries, 0);
    }
    #[test]
    fn whole_blob_cache_exact_manifest_identity_clock_reset_and_poison_fallback() {
        let cache = WholeBlobCache::default(); cache.configure_capacity(1024*1024);
        let manifest = Arc::new(vec![1;100]); let id = BlockId::for_payload(&manifest); let source = Arc::new(vec![2;100]);
        cache.remember(id, &manifest, vec![source.clone()], &verified(1));
        assert!(cache.lookup(id, &Arc::new(manifest.as_ref().clone()), verified(1).identity()).is_none());
        assert!(cache.lookup(id, &manifest, verified(2).identity()).is_none());
        let proof = cache.lookup(id, &manifest, verified(1).identity()).unwrap();
        assert!(!proof.matches_chunk(0, &Arc::new(source.as_ref().clone())));
        assert!(!proof.matches_chunk(1, &source)); assert!(!proof.matches_count(2));
        cache.state.lock().unwrap().tick = u64::MAX;
        assert!(cache.lookup(id, &manifest, verified(1).identity()).is_none());
        assert_eq!(cache.metrics().accounted_bytes, 0); assert_eq!(cache.metrics().recency_entries, 0);
        let _ = std::panic::catch_unwind(|| { let _guard = cache.state.lock().unwrap(); panic!("optional metadata poison"); });
        assert!(cache.lookup(id, &manifest, verified(1).identity()).is_none());
        cache.remember(id, &manifest, vec![source], &verified(1));
        assert!(!cache.metrics().available);
    }
}
