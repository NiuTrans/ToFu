//! Reconstructible positive hints for payload-segment point reads across opens.
//!
//! A hint only orders a verified read. It never proves presence or absence,
//! cannot name a segment outside CONTROL's current catalog, and every payload
//! still passes the segment's CRC/content-hash checks. Missing, stale, torn or
//! unwritable cache files leave the ordinary catalog lookup authoritative.

use std::collections::{BTreeMap, VecDeque};
use std::io;
use std::path::Path;

use uuid::Uuid;

use crate::block::BlockId;
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::vfs::{OpenRequest, Vfs};

const MAGIC: &[u8; 8] = b"TDBHNT01";
const CACHE_FILE: &str = "payload-read-hints.cache";
const TEMP_FILE: &str = "payload-read-hints.cache.tmp";
const ENTRY_BYTES: usize = 32 + 16;
const HEADER_BYTES: usize = 12;
const CHECKSUM_BYTES: usize = 32;
const MAX_ENTRIES: usize = crate::generated_tofudb_ir::MAX_RECOVERY_PACKED_LOCATIONS;
const MAX_FILE_BYTES: usize = HEADER_BYTES + MAX_ENTRIES * ENTRY_BYTES + CHECKSUM_BYTES;

pub(crate) struct PayloadHints {
    entries: BTreeMap<BlockId, Uuid>,
    insertion_order: VecDeque<BlockId>,
    capacity: usize,
    dirty: bool,
}

impl PayloadHints {
    pub(crate) fn new(routing_budget_bytes: usize) -> Self {
        Self {
            entries: BTreeMap::new(),
            insertion_order: VecDeque::new(),
            // Reserve a small share of the existing probe-sized routing budget.
            // Account conservatively for tree nodes and the eviction queue.
            capacity: (routing_budget_bytes / 16 / 192).min(MAX_ENTRIES),
            dirty: false,
        }
    }

    pub(crate) fn resize(&mut self, routing_budget_bytes: usize) {
        self.capacity = (routing_budget_bytes / 16 / 192).min(MAX_ENTRIES);
        self.evict();
    }

    pub(crate) fn resize_with_saved(&mut self, root: &Path, vfs: &dyn Vfs, budget: usize) {
        let capacity = (budget / 16 / 192).min(MAX_ENTRIES);
        if capacity <= self.capacity {
            self.resize(budget);
            return;
        }
        // The launch probe runs after the minimal open. Re-admit saved hints
        // under its larger budget, keeping this open's recent reads newest.
        let mut expanded = Self::load(root, vfs, budget);
        for id in &self.insertion_order {
            expanded.remember(*id, self.entries[id]);
        }
        *self = expanded;
    }

    fn evict(&mut self) {
        while self.entries.len() > self.capacity {
            if let Some(id) = self.insertion_order.pop_front() {
                self.entries.remove(&id);
                self.dirty = true;
            }
        }
    }

    pub(crate) fn remember(&mut self, id: BlockId, segment_id: Uuid) {
        if self.capacity == 0 || self.entries.get(&id) == Some(&segment_id) {
            return;
        }
        if self.entries.insert(id, segment_id).is_none() {
            self.insertion_order.push_back(id);
        }
        self.dirty = true;
        self.evict();
    }

    pub(crate) fn candidate(
        &self, id: BlockId, manifest: &PayloadManifest,
    ) -> Option<PayloadSegmentReference> {
        let segment_id = self.entries.get(&id)?;
        manifest.segments.iter()
            .find(|reference| reference.segment_id == *segment_id && reference.may_contain(id))
            .copied()
    }

    pub(crate) fn load(root: &Path, vfs: &dyn Vfs, budget: usize) -> Self {
        let mut hints = Self::new(budget);
        let read = || -> io::Result<Vec<u8>> {
            let mut file = vfs.open(&root.join(CACHE_FILE), OpenRequest {
                read: true, ..OpenRequest::default()
            })?;
            file.read_all(MAX_FILE_BYTES)
        };
        // Cache I/O or format failures cannot fail an authority open.
        if let Ok(bytes) = read() {
            hints.decode(&bytes);
        }
        hints
    }

    fn decode(&mut self, bytes: &[u8]) {
        if bytes.len() < HEADER_BYTES + CHECKSUM_BYTES || &bytes[..8] != MAGIC {
            return;
        }
        let count = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
        if count > MAX_ENTRIES || bytes.len() != HEADER_BYTES + count * ENTRY_BYTES + CHECKSUM_BYTES {
            return;
        }
        let end = bytes.len() - CHECKSUM_BYTES;
        if blake3::hash(&bytes[..end]).as_bytes() != &bytes[end..] {
            return;
        }
        for entry in bytes[HEADER_BYTES..end].chunks_exact(ENTRY_BYTES) {
            self.remember(BlockId(entry[..32].try_into().unwrap()),
                Uuid::from_bytes(entry[32..].try_into().unwrap()));
        }
        self.dirty = false;
    }

    pub(crate) fn save(&mut self, root: &Path, vfs: &dyn Vfs) -> io::Result<()> {
        if !self.dirty { return Ok(()); }
        let mut bytes = Vec::with_capacity(HEADER_BYTES + self.entries.len() * ENTRY_BYTES + CHECKSUM_BYTES);
        bytes.extend_from_slice(MAGIC);
        bytes.extend_from_slice(&(self.entries.len() as u32).to_le_bytes());
        // Preserve admission order so reopens do not evict by hash ordering.
        for id in &self.insertion_order {
            bytes.extend_from_slice(&id.0);
            bytes.extend_from_slice(self.entries[id].as_bytes());
        }
        bytes.extend_from_slice(blake3::hash(&bytes).as_bytes());
        let temporary = root.join(TEMP_FILE);
        // The engine's exclusive lease owns this one bounded scratch slot.
        match vfs.remove_file(&temporary) {
            Ok(()) => {},
            Err(error) if error.kind() == io::ErrorKind::NotFound => {},
            Err(error) => return Err(error),
        }
        let result = (|| {
            let mut file = vfs.open(&temporary, OpenRequest {
                write: true, create_new: true, ..OpenRequest::default()
            })?;
            file.write_all_at(0, &bytes)?;
            drop(file);
            // Reconstructible hints require no durability barrier. A power
            // loss may discard them; bounded reads/checksums reject torn data.
            vfs.rename(&temporary, &root.join(CACHE_FILE))
        })();
        if result.is_err() {
            let _ = vfs.remove_file(&temporary); // Optional cache scratch only.
        } else {
            self.dirty = false;
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::RealVfs;

    #[test]
    fn hints_are_bounded_and_torn_cache_is_discarded() {
        let root = tempfile::tempdir().unwrap();
        let mut hints = PayloadHints::new(16 * 192 * 2);
        let segment = Uuid::new_v4();
        for n in 0..3 { hints.remember(BlockId([n; 32]), segment); }
        assert_eq!(hints.entries.len(), 2);
        assert!(!hints.entries.contains_key(&BlockId([0; 32])));
        hints.save(root.path(), &RealVfs).unwrap();
        let loaded = PayloadHints::load(root.path(), &RealVfs, 16 * 192 * 2);
        assert_eq!(loaded.entries, hints.entries);
        assert_eq!(loaded.insertion_order, hints.insertion_order);
        let path = root.path().join(CACHE_FILE);
        let mut bytes = std::fs::read(&path).unwrap();
        bytes[HEADER_BYTES] ^= 1;
        std::fs::write(&path, bytes).unwrap();
        assert!(PayloadHints::load(root.path(), &RealVfs, usize::MAX).entries.is_empty());
    }

    #[test]
    fn launch_budget_growth_recovers_saved_hints_and_keeps_recent_reads() {
        let root = tempfile::tempdir().unwrap();
        let segment = Uuid::new_v4();
        let mut hints = PayloadHints::new(16 * 192 * 4);
        for n in 0..4 { hints.remember(BlockId([n; 32]), segment); }
        hints.save(root.path(), &RealVfs).unwrap();
        let mut lean = PayloadHints::load(root.path(), &RealVfs, 16 * 192);
        lean.remember(BlockId([4; 32]), segment);
        lean.resize_with_saved(root.path(), &RealVfs, 16 * 192 * 4);
        assert_eq!(lean.entries.len(), 4);
        assert!(lean.entries.contains_key(&BlockId([4; 32])));
        assert!(lean.entries.contains_key(&BlockId([1; 32])));
        assert!(!lean.entries.contains_key(&BlockId([0; 32])));
        lean.resize_with_saved(root.path(), &RealVfs, 0);
        assert!(lean.entries.is_empty());
    }

    #[test]
    fn failed_optional_save_preserves_previous_cache() {
        let root = tempfile::tempdir().unwrap();
        let mut hints = PayloadHints::new(16 * 192);
        let segment = Uuid::new_v4();
        hints.remember(BlockId([0; 32]), segment);
        hints.save(root.path(), &RealVfs).unwrap();
        hints.remember(BlockId([1; 32]), segment);
        // A non-file scratch path forces an I/O error even under root.
        std::fs::create_dir(root.path().join(TEMP_FILE)).unwrap();
        assert!(hints.save(root.path(), &RealVfs).is_err());
        let saved = PayloadHints::load(root.path(), &RealVfs, 16 * 192);
        assert!(saved.entries.contains_key(&BlockId([0; 32])));
        assert!(hints.entries.contains_key(&BlockId([1; 32])));
    }

    #[test]
    fn another_catalog_never_accepts_a_stale_hint() {
        let mut hints = PayloadHints::new(usize::MAX);
        let id = BlockId([7; 32]);
        hints.remember(id, Uuid::new_v4());
        assert!(hints.candidate(id, &PayloadManifest::default()).is_none());
        hints.resize(0);
        assert!(hints.entries.is_empty());
    }
}
