//! Committed live projections retained between Turn event commits.
//!
//! Entry points: `ResidentProjections::{take, apply_committed}` on the shared
//! authority state, `PendingResidency::{forget, publish, clear}` on one
//! `AuthorityTransaction`, and `process_metrics` for `system.metrics`.
//! Depends only on the Turn document key and the complete Turn header.
//!
//! A compact projection head keeps a live Turn as an immutable checkpoint, a
//! bounded patch chain and text pages. Without this owner every structural
//! event decoded that whole history again (~4 MB per event in production).
//! An entry holds the exact public projection one committed transaction
//! produced, its exact encoded size and that commit's complete Turn header.
//! A later event consumes it only when its own transactional read returns the
//! identical header: the in-transaction adjacency proof, extended across
//! commits. Publication happens only after `AuthorityDatabase::commit`
//! succeeded, and `metadata_summary::put_document` (the single Turn document
//! writer) forgets the entry in every other committed write, so a committed
//! header never names another projection. A take removes the entry; a failed
//! or conflicting commit loses it and the next event materializes the durable
//! head. Nothing here is durable or authoritative.
//!
//! The lifetime is the live head's own: the terminal event consumes the entry
//! and publishes nothing; a lifecycle write forgets it. There is no count,
//! byte or age bound to tune. Resident memory is the committed live
//! projections whose next event would otherwise decode them again, and is
//! reported in `system.metrics.durability`. A live Turn removed without a Turn
//! document write (a purge of a still-running conversation) keeps its entry
//! until the next event of that key or process exit.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::{Map, Value};

use crate::entity::EntityKey;

static HITS: AtomicU64 = AtomicU64::new(0);
static STALE: AtomicU64 = AtomicU64::new(0);
static ENTRIES: AtomicU64 = AtomicU64::new(0);
static BYTES: AtomicU64 = AtomicU64::new(0);

/// Process totals: reused projections, stale entries dropped, and the
/// current resident entries with their exact encoded projection bytes.
pub(crate) fn process_metrics() -> (u64, u64, u64, u64) {
    (HITS.load(Ordering::Relaxed), STALE.load(Ordering::Relaxed),
        ENTRIES.load(Ordering::Relaxed), BYTES.load(Ordering::Relaxed))
}

/// The committed public projection of one live head, its exact serialized
/// size and the complete Turn document header that commit stored.
pub(crate) struct ResidentProjection {
    pub header: Map<String, Value>,
    pub projection: Map<String, Value>,
    pub projection_bytes: usize,
}

fn revision(header: &Map<String, Value>) -> Option<u64> {
    header.get("projectionRevision").and_then(Value::as_u64)
}

#[derive(Default)]
pub(crate) struct ResidentProjections {
    entries: Mutex<BTreeMap<EntityKey, ResidentProjection>>,
}

impl ResidentProjections {
    fn entries(&self) -> std::sync::MutexGuard<'_, BTreeMap<EntityKey, ResidentProjection>> {
        self.entries.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn released(entry: &ResidentProjection) {
        ENTRIES.fetch_sub(1, Ordering::Relaxed);
        BYTES.fetch_sub(entry.projection_bytes as u64, Ordering::Relaxed);
    }

    /// The entry for exactly this header, consumed. An entry for an older or
    /// unknown revision is stale and dropped; one newer than a lagging
    /// snapshot stays for the reader that will see it.
    pub(crate) fn take(&self, key: &EntityKey, header: &Map<String, Value>) -> Option<ResidentProjection> {
        let mut entries = self.entries();
        let entry = entries.remove(key)?;
        if entry.header == *header {
            Self::released(&entry);
            HITS.fetch_add(1, Ordering::Relaxed);
            return Some(entry);
        }
        if revision(&entry.header).zip(revision(header)).is_some_and(|(entry, read)| entry > read) {
            entries.insert(key.clone(), entry);
        } else {
            Self::released(&entry);
            STALE.fetch_add(1, Ordering::Relaxed);
        }
        None
    }

    /// Apply one committed transaction's residency changes. Called only after
    /// its commit succeeded; a rolled back transaction drops them unapplied.
    pub(crate) fn apply_committed(&self, pending: PendingResidency) {
        if pending.forget.is_empty() && pending.publish.is_empty() {
            return;
        }
        let mut entries = self.entries();
        for key in &pending.forget {
            if let Some(entry) = entries.remove(key) {
                Self::released(&entry);
            }
        }
        for (key, entry) in pending.publish {
            ENTRIES.fetch_add(1, Ordering::Relaxed);
            BYTES.fetch_add(entry.projection_bytes as u64, Ordering::Relaxed);
            entries.insert(key, entry);
        }
    }

    #[cfg(test)]
    pub(crate) fn resident_keys(&self) -> usize {
        self.entries().len()
    }

    #[cfg(test)]
    pub(crate) fn forget_all(&self) {
        let mut entries = self.entries();
        for entry in entries.values() {
            Self::released(entry);
        }
        entries.clear();
    }
}

impl Drop for ResidentProjections {
    fn drop(&mut self) {
        for entry in self.entries().values() {
            Self::released(entry);
        }
    }
}

/// Residency changes one transaction applies if, and only if, it commits.
#[derive(Default)]
pub(crate) struct PendingResidency {
    forget: BTreeSet<EntityKey>,
    publish: BTreeMap<EntityKey, ResidentProjection>,
}

impl PendingResidency {
    /// A Turn document write that does not carry its successor projection.
    pub(crate) fn forget(&mut self, key: &EntityKey) {
        self.publish.remove(key);
        self.forget.insert(key.clone());
    }

    /// The exact projection this transaction's own Turn write produced.
    pub(crate) fn publish(&mut self, key: EntityKey, entry: ResidentProjection) {
        self.forget.insert(key.clone());
        self.publish.insert(key, entry);
    }

    /// Drop every publication; its forget remains. A later step of the same
    /// transaction may have changed head state the projection depends on.
    pub(crate) fn clear(&mut self) {
        self.publish.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn key(name: &str) -> EntityKey {
        EntityKey::new(7, 11, "turn", name.as_bytes()).unwrap()
    }

    fn entry(revision: u64) -> ResidentProjection {
        ResidentProjection {
            header: json!({"projectionRevision": revision}).as_object().unwrap().clone(),
            projection: json!({"content": format!("r{revision}")}).as_object().unwrap().clone(),
            projection_bytes: 16,
        }
    }

    fn committed(resident: &ResidentProjections, name: &str, revision: u64) {
        let mut pending = PendingResidency::default();
        pending.publish(key(name), entry(revision));
        resident.apply_committed(pending);
    }

    #[test]
    fn only_a_committed_publication_with_the_identical_header_is_taken_once() {
        let resident = ResidentProjections::default();
        let mut pending = PendingResidency::default();
        pending.publish(key("a"), entry(1));
        drop(pending); // A rolled back transaction publishes nothing.
        assert!(resident.take(&key("a"), &entry(1).header).is_none());

        committed(&resident, "a", 2);
        assert!(resident.take(&key("a"), &entry(1).header).is_none(), "a lagging snapshot");
        assert_eq!(resident.resident_keys(), 1, "the newer entry stays for its reader");
        assert!(resident.take(&key("a"), &entry(3).header).is_none(), "a newer committed header");
        assert_eq!(resident.resident_keys(), 0, "an older entry is stale and dropped");

        committed(&resident, "a", 4);
        assert_eq!(resident.take(&key("a"), &entry(4).header).unwrap().projection["content"], "r4");
        assert!(resident.take(&key("a"), &entry(4).header).is_none(), "a take consumes the entry");
    }

    #[test]
    fn a_later_turn_write_or_cleared_step_forgets_the_committed_entry() {
        let resident = ResidentProjections::default();
        committed(&resident, "a", 1);
        committed(&resident, "b", 1);
        let mut pending = PendingResidency::default();
        pending.publish(key("a"), entry(2));
        pending.forget(&key("a")); // the same transaction wrote the Turn again
        pending.publish(key("b"), entry(2));
        pending.clear(); // an intervening non-event step
        resident.apply_committed(pending);
        assert_eq!(resident.resident_keys(), 0);
    }
}
