//! Content-addressed history manifest pages. Leaves retain versions 1–4;
//! version 5 joins ordered children without a lifetime segment-count ceiling.
//! Publication writes children before parents and CONTROL remains the commit.

use super::*;
use crate::block::BlockStore;
use std::collections::BTreeSet;

const PAGED_MANIFEST_VERSION: u32 = 5;
// magic, version, checkpoint sequence/hash, segment count, root presence/id,
// child count. The fixed-size absent root keeps framing unambiguous.
const PAGED_HEADER_BYTES: usize = 8 + 4 + 8 + 32 + 8 + 1 + 32 + 4;
const CHILDREN_PER_PAGE: usize = (HISTORY_SEGMENT_MAX_BYTES - PAGED_HEADER_BYTES) / 32;
// A writer-built tree fans out by at least two; 64 levels already exceed the
// u64 sequence space. This is a malformed-format recursion guard, not admission.
const MAX_MANIFEST_DEPTH: usize = u64::BITS as usize;

#[derive(Clone, Copy)]
struct PageReference {
    id: BlockId,
    segment_count: u64,
    checkpoint_sequence: u64,
    checkpoint_hash: [u8; 32],
}

impl HistoryManifest {
    /// Encode all immutable pages in child-before-parent order. No I/O runs
    /// during preparation; a failed publication cannot expose an incomplete root.
    pub(crate) fn encode_pages(&mut self) -> io::Result<Vec<Vec<u8>>> {
        self.encode_pages_with_bounds(
            if self.payload_index_root_id.is_some() {
                MAX_INDEXED_HISTORY_SEGMENTS
            } else {
                MAX_HISTORY_SEGMENTS
            },
            CHILDREN_PER_PAGE,
        )
    }

    #[cfg(test)]
    pub(crate) fn encode_test_pages(&mut self) -> io::Result<Vec<Vec<u8>>> {
        self.encode_pages_with_bounds(2, 2)
    }

    fn encode_pages_with_bounds(
        &mut self,
        segments_per_page: usize,
        children_per_page: usize,
    ) -> io::Result<Vec<Vec<u8>>> {
        self.validate_chain(false)?;
        if segments_per_page == 0 || children_per_page < 2 {
            return Err(invalid_input("invalid history page fanout"));
        }
        let mut pages = Vec::new();
        let mut level = Vec::new();
        for segments in self.segments.chunks(segments_per_page) {
            let last = segments.last().unwrap();
            let leaf = HistoryManifest {
                checkpoint_sequence: last.last_sequence,
                checkpoint_hash: last.terminal_hash,
                payload_index_root_id: self.payload_index_root_id,
                manifest_page_ids: Vec::new(),
                segments: segments.to_vec(),
            };
            let encoded = leaf.encode()?;
            level.push(PageReference {
                id: BlockId::for_payload(&encoded),
                segment_count: segments
                    .len()
                    .try_into()
                    .map_err(|_| invalid_input("history segment count overflow"))?,
                checkpoint_sequence: leaf.checkpoint_sequence,
                checkpoint_hash: leaf.checkpoint_hash,
            });
            pages.push(encoded);
        }
        while level.len() > 1 {
            let mut next = Vec::new();
            for children in level.chunks(children_per_page) {
                if children.len() == 1 {
                    next.push(children[0]);
                    continue;
                }
                let last = children.last().unwrap();
                let count = children.iter().try_fold(0_u64, |count, child| {
                    count
                        .checked_add(child.segment_count)
                        .ok_or_else(|| invalid_input("history segment count overflow"))
                })?;
                let mut encoded = Vec::with_capacity(PAGED_HEADER_BYTES + children.len() * 32);
                encoded.extend_from_slice(MANIFEST_MAGIC);
                encoded.extend_from_slice(&PAGED_MANIFEST_VERSION.to_le_bytes());
                encoded.extend_from_slice(&last.checkpoint_sequence.to_le_bytes());
                encoded.extend_from_slice(&last.checkpoint_hash);
                encoded.extend_from_slice(&count.to_le_bytes());
                encoded.push(u8::from(self.payload_index_root_id.is_some()));
                encoded
                    .extend_from_slice(&self.payload_index_root_id.unwrap_or(BlockId([0; 32])).0);
                encoded.extend_from_slice(&(children.len() as u32).to_le_bytes());
                for child in children {
                    encoded.extend_from_slice(&child.id.0);
                }
                if encoded.len() > HISTORY_SEGMENT_MAX_BYTES {
                    return Err(invalid_input("history manifest page exceeds block bound"));
                }
                next.push(PageReference {
                    id: BlockId::for_payload(&encoded),
                    segment_count: count,
                    checkpoint_sequence: last.checkpoint_sequence,
                    checkpoint_hash: last.checkpoint_hash,
                });
                pages.push(encoded);
            }
            level = next;
        }
        // Every page except the final root is a retained metadata dependency.
        self.manifest_page_ids = pages[..pages.len() - 1]
            .iter()
            .map(|bytes| BlockId::for_payload(bytes))
            .collect();
        Ok(pages)
    }

    pub(crate) fn store(&mut self, blocks: &BlockStore) -> io::Result<BlockId> {
        let pages = self.encode_pages()?;
        Self::publish_pages(&pages, blocks)
    }

    pub(crate) fn publish_pages(pages: &[Vec<u8>], blocks: &BlockStore) -> io::Result<BlockId> {
        let mut root = None;
        for page in pages {
            root = Some(blocks.put_loose(page)?);
        }
        root.ok_or_else(|| invalid_input("history manifest has no pages"))
    }

    /// Read exactly the selected root and all authenticated children. A missing,
    /// reordered, corrupt or duplicate child fails the entire recovery/GC walk.
    pub(crate) fn load(
        root: BlockId,
        mut read: impl FnMut(BlockId) -> io::Result<Vec<u8>>,
    ) -> io::Result<Self> {
        let mut seen = BTreeSet::new();
        Self::load_page(root, &mut read, &mut seen, 0)
    }

    fn load_page(
        id: BlockId,
        read: &mut impl FnMut(BlockId) -> io::Result<Vec<u8>>,
        seen: &mut BTreeSet<BlockId>,
        depth: usize,
    ) -> io::Result<Self> {
        if depth >= MAX_MANIFEST_DEPTH || !seen.insert(id) {
            return Err(invalid_data(
                "history manifest page cycle, duplicate or depth overflow",
            ));
        }
        let encoded = read(id)?;
        if encoded.len() < 12
            || encoded.len() > HISTORY_SEGMENT_MAX_BYTES
            || BlockId::for_payload(&encoded) != id
            || &encoded[..8] != MANIFEST_MAGIC
        {
            return Err(invalid_data(
                "history manifest page identity or size mismatch",
            ));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        if version != PAGED_MANIFEST_VERSION {
            return Self::decode(&encoded);
        }
        if encoded.len() < PAGED_HEADER_BYTES {
            return Err(invalid_data("truncated paged history manifest"));
        }
        let checkpoint_sequence = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
        let checkpoint_hash = encoded[20..52].try_into().unwrap();
        let count = u64::from_le_bytes(encoded[52..60].try_into().unwrap());
        let root = BlockId(encoded[61..93].try_into().unwrap());
        let payload_index_root_id = match encoded[60] {
            0 if root == BlockId([0; 32]) => None,
            1 => Some(root),
            _ => return Err(invalid_data("invalid paged history payload root presence")),
        };
        let children = u32::from_le_bytes(encoded[93..97].try_into().unwrap()) as usize;
        if checkpoint_sequence == 0
            || count == 0
            || count > checkpoint_sequence
            || children < 2
            || children > CHILDREN_PER_PAGE
            || children as u64 > count
            || encoded.len() != PAGED_HEADER_BYTES + children * 32
        {
            return Err(invalid_data("invalid paged history manifest bounds"));
        }
        let mut manifest = HistoryManifest {
            checkpoint_sequence,
            checkpoint_hash,
            payload_index_root_id,
            manifest_page_ids: Vec::new(),
            segments: Vec::new(),
        };
        for raw in encoded[PAGED_HEADER_BYTES..].chunks_exact(32) {
            let child_id = BlockId(raw.try_into().unwrap());
            let mut child = Self::load_page(child_id, read, seen, depth + 1)?;
            if child.payload_index_root_id != payload_index_root_id
                || (child.segments.len() as u64) >= count
                || manifest.segments.len() as u64 + child.segments.len() as u64 > count
            {
                return Err(invalid_data("paged history child witness mismatch"));
            }
            manifest.manifest_page_ids.push(child_id);
            manifest
                .manifest_page_ids
                .append(&mut child.manifest_page_ids);
            manifest.segments.append(&mut child.segments);
        }
        if manifest.segments.len() as u64 != count {
            return Err(invalid_data("paged history segment count mismatch"));
        }
        manifest
            .validate_chain(false)
            .map_err(|_| invalid_data("invalid paged history manifest chain"))?;
        Ok(manifest)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn manifest(count: usize) -> HistoryManifest {
        let mut segments = Vec::new();
        let mut parent_hash = [0; 32];
        for sequence in 1..=count as u64 {
            let terminal_hash = *blake3::hash(&sequence.to_le_bytes()).as_bytes();
            segments.push(HistorySegmentReference {
                block_id: BlockId(terminal_hash),
                payload_index_block_id: Some(BlockId(parent_hash)),
                first_sequence: sequence,
                last_sequence: sequence,
                parent_hash,
                terminal_hash,
            });
            parent_hash = terminal_hash;
        }
        HistoryManifest {
            checkpoint_sequence: count as u64,
            checkpoint_hash: parent_hash,
            payload_index_root_id: Some(BlockId([7; 32])),
            manifest_page_ids: Vec::new(),
            segments,
        }
    }

    fn encoded(
        manifest: &mut HistoryManifest,
        leaf: usize,
        fanout: usize,
    ) -> (BlockId, BTreeMap<BlockId, Vec<u8>>) {
        let pages = manifest.encode_pages_with_bounds(leaf, fanout).unwrap();
        let root = BlockId::for_payload(pages.last().unwrap());
        (
            root,
            pages
                .into_iter()
                .map(|page| (BlockId::for_payload(&page), page))
                .collect(),
        )
    }

    fn load(root: BlockId, pages: &BTreeMap<BlockId, Vec<u8>>) -> io::Result<HistoryManifest> {
        HistoryManifest::load(root, |id| {
            pages
                .get(&id)
                .cloned()
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
        })
    }

    #[test]
    fn history_larger_than_a_full_compact_page_roundtrips() {
        let mut source = manifest(MAX_INDEXED_HISTORY_SEGMENTS + 1);
        let (root, pages) = encoded(&mut source, MAX_INDEXED_HISTORY_SEGMENTS, CHILDREN_PER_PAGE);
        assert_eq!(pages.len(), 3);
        assert!(pages
            .values()
            .all(|bytes| bytes.len() <= HISTORY_SEGMENT_MAX_BYTES));
        let loaded = load(root, &pages).unwrap();
        assert_eq!(loaded.segments, source.segments);
        assert_eq!(loaded.checkpoint_sequence, source.checkpoint_sequence);
        assert_eq!(loaded.checkpoint_hash, source.checkpoint_hash);
        assert_eq!(loaded.manifest_page_ids.len(), 2);
    }

    #[test]
    fn unindexed_history_keeps_the_larger_leaf_capacity_then_pages() {
        let mut source = manifest(MAX_HISTORY_SEGMENTS + 1);
        source.payload_index_root_id = None;
        for segment in &mut source.segments {
            segment.payload_index_block_id = None;
        }
        let pages = source.encode_pages().unwrap();
        assert_eq!(pages.len(), 3);
        let root = BlockId::for_payload(pages.last().unwrap());
        let pages: BTreeMap<_, _> = pages
            .into_iter()
            .map(|page| (BlockId::for_payload(&page), page))
            .collect();
        assert_eq!(load(root, &pages).unwrap().segments, source.segments);
    }

    #[test]
    fn recursive_manifest_pages_retain_every_descendant() {
        let mut source = manifest(13);
        let (root, pages) = encoded(&mut source, 2, 2);
        let loaded = load(root, &pages).unwrap();
        assert_eq!(loaded.segments, source.segments);
        let dependencies: BTreeSet<_> = loaded.manifest_page_ids.iter().copied().collect();
        assert_eq!(
            dependencies,
            pages.keys().copied().filter(|id| *id != root).collect()
        );
    }

    #[test]
    fn incomplete_or_corrupted_child_never_becomes_partial_history() {
        let mut source = manifest(7);
        let (root, pages) = encoded(&mut source, 2, 2);
        for id in pages.keys().copied().filter(|id| *id != root) {
            let mut missing = pages.clone();
            missing.remove(&id);
            assert!(load(root, &missing).is_err());
            let mut corrupt = pages.clone();
            corrupt.get_mut(&id).unwrap()[0] ^= 1;
            assert!(load(root, &corrupt).is_err());
        }
    }

    #[test]
    fn reordered_duplicated_or_false_witness_children_fail_closed() {
        let mut source = manifest(5);
        let (root, pages) = encoded(&mut source, 2, 3);
        let original = &pages[&root];
        let mut variants = Vec::new();
        let mut reordered = original.clone();
        reordered[PAGED_HEADER_BYTES..PAGED_HEADER_BYTES + 32]
            .copy_from_slice(&original[PAGED_HEADER_BYTES + 32..PAGED_HEADER_BYTES + 64]);
        reordered[PAGED_HEADER_BYTES + 32..PAGED_HEADER_BYTES + 64]
            .copy_from_slice(&original[PAGED_HEADER_BYTES..PAGED_HEADER_BYTES + 32]);
        variants.push(reordered);
        let mut duplicate = original.clone();
        duplicate[PAGED_HEADER_BYTES + 32..PAGED_HEADER_BYTES + 64]
            .copy_from_slice(&original[PAGED_HEADER_BYTES..PAGED_HEADER_BYTES + 32]);
        variants.push(duplicate);
        for offset in [8, 12, 20, 52, 60, 93] {
            let mut corrupted = original.clone();
            corrupted[offset] ^= 1;
            variants.push(corrupted);
        }
        for bytes in variants {
            let changed = BlockId::for_payload(&bytes);
            let mut candidate = pages.clone();
            candidate.insert(changed, bytes);
            assert!(load(changed, &candidate).is_err());
        }
    }

    #[test]
    fn history_page_publication_survives_each_io_error_torn_write_and_lost_sync() {
        use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
        use std::path::Path;
        use std::sync::Arc;

        let seed = Arc::new(DeterministicVfs::new(None));
        seed.create_dir(Path::new("/data")).unwrap();
        seed.sync_directory(Path::new("/")).unwrap();
        let store = BlockStore::initialize_with_vfs(Path::new("/data"), seed.clone()).unwrap();
        drop(store);
        let mut source = manifest(7);
        let pages = source.encode_test_pages().unwrap();
        let root = BlockId::for_payload(pages.last().unwrap());
        let probe = Arc::new(seed.fork_durable().unwrap());
        let store = BlockStore::open_with_vfs(Path::new("/data"), probe.clone()).unwrap();
        probe.arm_fault(None).unwrap();
        HistoryManifest::publish_pages(&pages, &store).unwrap();
        let trace = probe.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write {
                actions.push(FaultAction::ShortWrite(3));
            }
            if matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = Arc::new(seed.fork_durable().unwrap());
                let store = BlockStore::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let publication = HistoryManifest::publish_pages(&pages, &store);
                drop(store);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let reopened = BlockStore::open_with_vfs(Path::new("/data"), vfs).unwrap();
                let recovered = HistoryManifest::load(root, |id| reopened.get(id));
                if publication.is_ok() {
                    assert_eq!(
                        recovered.unwrap().segments,
                        source.segments,
                        "successful publication lost pages at {index} {operation:?}"
                    );
                } else if reopened.get(root).is_ok() {
                    assert_eq!(
                        recovered.unwrap().segments,
                        source.segments,
                        "root became durable before children at {index} {operation:?}"
                    );
                }
            }
        }
    }
}
