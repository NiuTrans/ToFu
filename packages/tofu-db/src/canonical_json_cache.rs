//! Volatile canonical JSON certificates for fully verified, scoped blob bytes.
//! Entry: CanonicalProofCache::canonicalize_blob. Only strict canonical success
//! populates the cache; legacy conversion/errors retain their original path.
//! Complete blob reading precedes lookup, so this cache never substitutes for
//! scope, manifest, chunk, length or full content-hash verification.

use super::{canonicalize_with_depth, CanonicalJsonBody};
use crate::blob::{VerifiedBlobBytes, VerifiedBlobIdentity};
use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

/// A small share of the existing probe-derived hot block-cache budget. This
/// changes metadata capacity, never eagerly allocates that many bytes.
pub(crate) const HOT_CACHE_BUDGET_DIVISOR: usize = 64;
const BASE_ACCOUNTED_BYTES: usize = 4096;
const ENTRY_ACCOUNTED_BYTES: usize = 512;

#[derive(Clone, Copy)]
struct Entry {
    depth: usize,
    access: u64,
}

#[derive(Default)]
struct State {
    entries: BTreeMap<VerifiedBlobIdentity, Entry>,
    recency: BTreeMap<u64, VerifiedBlobIdentity>,
    capacity_bytes: usize,
    tick: u64,
    hits: u64,
    misses: u64,
    evictions: u64,
}
impl State {
    fn maximum_entries(&self) -> usize {
        self.capacity_bytes.saturating_sub(BASE_ACCOUNTED_BYTES) / ENTRY_ACCOUNTED_BYTES
    }
    fn accounted_bytes(&self) -> usize {
        if self.entries.is_empty() {
            0
        } else {
            BASE_ACCOUNTED_BYTES + self.entries.len() * ENTRY_ACCOUNTED_BYTES
        }
    }
    fn next_access(&mut self) -> u64 {
        if self.tick == u64::MAX {
            // A clock reset drops reconstructible certificates, never bytes or
            // durable state. No old access stamp can alias a new one.
            self.evictions = self.evictions.saturating_add(self.entries.len() as u64);
            self.entries.clear();
            self.recency.clear();
            self.tick = 0;
        }
        self.tick += 1;
        self.tick
    }
    fn get(&mut self, key: VerifiedBlobIdentity) -> Option<usize> {
        let access = self.next_access();
        let Some(entry) = self.entries.get_mut(&key) else {
            self.misses = self.misses.saturating_add(1);
            return None;
        };
        self.recency.remove(&entry.access);
        entry.access = access;
        self.recency.insert(access, key);
        self.hits = self.hits.saturating_add(1);
        Some(entry.depth)
    }
    fn insert(&mut self, key: VerifiedBlobIdentity, depth: usize) {
        if self.maximum_entries() == 0 {
            return;
        }
        let access = self.next_access();
        if let Some(old) = self.entries.insert(key, Entry { depth, access }) {
            self.recency.remove(&old.access);
        }
        self.recency.insert(access, key);
        self.trim();
    }
    fn trim(&mut self) {
        while self.entries.len() > self.maximum_entries() {
            let Some((_, key)) = self.recency.pop_first() else {
                break;
            };
            self.entries.remove(&key);
            self.evictions = self.evictions.saturating_add(1);
        }
        if self.entries.is_empty() {
            // Removing the final entry leaves allocated empty BTreeMap roots.
            // Drop those roots so disabled/empty caches really release their
            // indexed allocations and the zero-byte accounting stays true.
            self.entries = BTreeMap::new();
            self.recency = BTreeMap::new();
        }
    }
}

#[derive(Default)]
pub(crate) struct CanonicalProofCache {
    state: Mutex<State>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CanonicalProofCacheMetrics {
    pub available: bool,
    pub capacity_bytes: usize,
    pub accounted_bytes: usize,
    pub maximum_entries: usize,
    pub entries: usize,
    pub recency_entries: usize,
    pub hits: u64,
    pub misses: u64,
    pub evictions: u64,
}

/// Anonymous counters/capacity only; no authority lock or source identifiers.
#[derive(Clone)]
pub struct CanonicalProofCacheObserver(Arc<CanonicalProofCache>);
impl CanonicalProofCacheObserver {
    pub fn metrics(&self) -> CanonicalProofCacheMetrics {
        self.0.metrics()
    }
}
impl CanonicalProofCache {
    pub(crate) fn observer(self: &Arc<Self>) -> CanonicalProofCacheObserver {
        CanonicalProofCacheObserver(Arc::clone(self))
    }
    pub(crate) fn configure_capacity(&self, capacity_bytes: usize) {
        if let Ok(mut state) = self.state.lock() {
            state.capacity_bytes = capacity_bytes;
            state.trim();
        }
    }
    pub(crate) fn canonicalize_blob<'a>(
        &self,
        blob: &'a VerifiedBlobBytes,
    ) -> Result<CanonicalJsonBody<'a>, serde_json::Error> {
        let depth = self
            .state
            .lock()
            .ok()
            .and_then(|mut state| state.get(blob.identity()));
        if let Some(depth) = depth {
            crate::request_profile::work(crate::request_profile::Work::JsonCanonicalProofReuse, 1);
            crate::request_profile::work(
                crate::request_profile::Work::JsonCanonicalProofBytes,
                blob.bytes().len() as u64,
            );
            return Ok(CanonicalJsonBody {
                bytes: std::borrow::Cow::Borrowed(blob.bytes()),
                maximum_container_depth: Some(depth),
            });
        }
        crate::request_profile::work(crate::request_profile::Work::JsonCanonicalProofMiss, 1);
        // Expensive recognition never holds the cache lock. Racing cold reads
        // may duplicate validation, but can only publish the same certificate.
        let body = canonicalize_with_depth(blob.bytes())?;
        if let Some(depth) = body.maximum_container_depth() {
            if let Ok(mut state) = self.state.lock() {
                state.insert(blob.identity(), depth);
            }
        }
        Ok(body)
    }
    fn metrics(&self) -> CanonicalProofCacheMetrics {
        let state = self.state.lock().ok();
        CanonicalProofCacheMetrics {
            available: state.is_some(),
            capacity_bytes: state.as_ref().map_or(0, |s| s.capacity_bytes),
            accounted_bytes: state.as_ref().map_or(0, |s| s.accounted_bytes()),
            maximum_entries: state.as_ref().map_or(0, |s| s.maximum_entries()),
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
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{io::Cursor, path::Path};

    fn verified(tenant: u64, owner: u64, bytes: &[u8]) -> VerifiedBlobBytes {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        let mut engine =
            crate::engine::Engine::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        let reference = crate::blob::stage_blob(
            &engine,
            tenant,
            owner,
            &mut Cursor::new(bytes),
            bytes.len() as u64,
        )
        .unwrap()
        .commit(&mut engine, b"canonical-proof-fixture")
        .unwrap();
        crate::blob::BlobReader::open(
            &engine,
            Some(engine.transaction_snapshot().unwrap()),
            tenant,
            owner,
            reference,
        )
        .unwrap()
        .materialize_verified(bytes.len(), || std::io::Error::other("fixture bound"))
        .unwrap()
    }

    #[test]
    fn canonical_proof_cache_preserves_exact_bytes_and_scoped_content_identity() {
        let cache = CanonicalProofCache::default();
        cache.configure_capacity(BASE_ACCOUNTED_BYTES + 8 * ENTRY_ACCOUNTED_BYTES);
        let a = verified(7, 11, r#"{"items":[1,true,{"x":"你好"}]}"#.as_bytes());
        let same = verified(7, 11, a.bytes());
        let other_owner = verified(7, 12, a.bytes());
        let other_tenant = verified(8, 11, a.bytes());
        let changed = verified(7, 11, br#"{"items":[[[null]]]}"#);
        assert_eq!(a.identity(), same.identity());
        assert_ne!(a.identity(), other_owner.identity());
        assert_ne!(a.identity(), other_tenant.identity());
        assert_ne!(a.identity(), changed.identity());
        for blob in [&a, &same, &other_owner, &other_tenant, &changed, &a] {
            let body = cache.canonicalize_blob(blob).unwrap();
            let expected = canonicalize_with_depth(blob.bytes()).unwrap();
            assert_eq!(body.bytes(), expected.bytes());
            assert_eq!(
                body.maximum_container_depth(),
                expected.maximum_container_depth()
            );
        }
        let metrics = cache.metrics();
        assert_eq!((metrics.entries, metrics.hits, metrics.misses), (4, 2, 4));
        assert_eq!(metrics.recency_entries, metrics.entries);
    }

    #[test]
    fn canonical_proof_cache_keeps_lru_metadata_bounded_through_reuse_resize_and_clock_reset() {
        let cache = CanonicalProofCache::default();
        cache.configure_capacity(BASE_ACCOUNTED_BYTES + 2 * ENTRY_ACCOUNTED_BYTES);
        let a = verified(7, 11, b"[1]");
        let b = verified(7, 11, b"[2]");
        let c = verified(7, 11, b"[3]");
        cache.canonicalize_blob(&a).unwrap();
        cache.canonicalize_blob(&b).unwrap();
        for _ in 0..10_000 {
            cache.canonicalize_blob(&a).unwrap();
        }
        assert_eq!(cache.metrics().recency_entries, 2);
        cache.canonicalize_blob(&c).unwrap();
        assert!(cache
            .state
            .lock()
            .unwrap()
            .entries
            .contains_key(&a.identity()));
        assert!(!cache
            .state
            .lock()
            .unwrap()
            .entries
            .contains_key(&b.identity()));
        let metrics = cache.metrics();
        assert_eq!(metrics.evictions, 1);
        assert!(metrics.accounted_bytes <= metrics.capacity_bytes);
        cache.configure_capacity(BASE_ACCOUNTED_BYTES + ENTRY_ACCOUNTED_BYTES);
        assert_eq!(cache.metrics().entries, 1);
        cache.state.lock().unwrap().tick = u64::MAX;
        cache.canonicalize_blob(&a).unwrap();
        assert_eq!(
            (cache.metrics().entries, cache.metrics().recency_entries),
            (1, 1)
        );
        cache.configure_capacity(0);
        cache.canonicalize_blob(&a).unwrap();
        assert_eq!(
            (cache.metrics().entries, cache.metrics().accounted_bytes),
            (0, 0)
        );
    }

    #[test]
    fn canonical_proof_cache_never_remembers_legacy_conversion_or_malformed_json() {
        let cache = CanonicalProofCache::default();
        cache.configure_capacity(16 * 1024);
        for bytes in [
            b" null ".as_slice(),
            b"-0",
            br#"{"z":1,"a":2}"#,
            br#"{"a":1,"a":2}"#,
            b"not JSON",
        ] {
            let blob = verified(7, 11, bytes);
            for _ in 0..2 {
                let output = cache.canonicalize_blob(&blob);
                let expected = canonicalize_with_depth(bytes);
                assert_eq!(output.is_ok(), expected.is_ok());
                if let (Ok(output), Ok(expected)) = (output, expected) {
                    assert_eq!(output.bytes(), expected.bytes());
                    assert_eq!(output.maximum_container_depth(), None);
                }
            }
        }
        assert_eq!((cache.metrics().entries, cache.metrics().hits), (0, 0));
    }

    #[test]
    fn canonical_proof_cache_poison_reverts_to_strict_recognition() {
        let cache = CanonicalProofCache::default();
        cache.configure_capacity(16 * 1024);
        let blob = verified(7, 11, b"[1,true]");
        cache.canonicalize_blob(&blob).unwrap();
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = cache.state.lock().unwrap();
            panic!("injected cache poison");
        }));
        assert!(!cache.metrics().available);
        let body = cache.canonicalize_blob(&blob).unwrap();
        assert_eq!(body.bytes(), blob.bytes());
        assert_eq!(body.maximum_container_depth(), Some(1));
        let malformed = verified(7, 11, b"[1,]");
        assert!(cache.canonicalize_blob(&malformed).is_err());
    }
}
