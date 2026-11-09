//! History pruning with immutable payload promotion outside the authority mutex.
//!
//! Engine reserves its existing physical-publication lane; the maintenance
//! worker packs each slice's carried payloads into payload segments and
//! publishes CONTROL outside authority; Engine then reconciles the current
//! logical state.
use super::*;
use std::collections::btree_map;

pub(crate) struct PreparedHistoryCompaction {
    history_id: BlockId,
    payload_id: Option<BlockId>,
    current: HistoryManifest,
    first_retained: usize,
    payload_references: Vec<PayloadSegmentReference>,
    blocks: Arc<BlockStore>,
    captured_state: ControlState,
    control_dirty: bool,
    control: Arc<Mutex<ControlFile>>,
    wal_cover: Option<crate::wal::PublicationWalCover>,
    restart_required: Arc<std::sync::atomic::AtomicBool>,
    completed: bool,
    /// Automatic slices publish the unchanged routing root: retired sources
    /// leave the manifest, and their locations leave routing when a rotation's
    /// merge rewrites their run (`RoutingRoot::append`). Hash-ordered pages
    /// interleave every source, so dropping them here rewrote nearly every page.
    lazy_routing_retirement: bool,
    /// The launch materialization budget that already sizes an automatic
    /// slice; it bounds the source bytes one promotion group keeps resident.
    promotion_group_bytes: u64,
    #[cfg(test)]
    payload_segment_test_ids: Vec<Uuid>,
}
impl PreparedHistoryCompaction {
    /// Operator compaction may retire the entire history. Preserve its
    /// streaming memory bound instead of retaining every source's locations.
    pub(super) fn use_streaming_routing_retirement(&mut self) {
        self.lazy_routing_retirement = false;
    }
}
#[cfg(test)]
impl PreparedHistoryCompaction {
    pub(crate) fn set_payload_segment_test_ids(&mut self, ids: Vec<Uuid>) {
        self.payload_segment_test_ids = ids;
    }
}
impl Drop for PreparedHistoryCompaction {
    fn drop(&mut self) {
        if !self.completed {
            self.restart_required
                .store(true, std::sync::atomic::Ordering::Relaxed);
        }
    }
}

pub(crate) struct HistoryCompactionOutcome {
    history: HistoryManifest,
    history_id: BlockId,
    routing: Option<crate::history_payload::RoutingRoot>,
    payload: Option<PayloadManifest>,
    payload_id: Option<BlockId>,
    published_state: ControlState,
}

impl Engine {
    pub(crate) fn try_prepare_history_compaction(
        &mut self,
        maximum: usize,
    ) -> io::Result<Option<PreparedHistoryCompaction>> {
        self.require_usable_authority()?;
        self.require_physical_maintenance_available()?;
        if maximum == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history retained-segment bound is invalid",
            ));
        }
        if self.state.authority_state_root.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history compaction requires a current authority state root",
            ));
        }
        let current = self
            .history_manifest
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "history is empty"))?;
        let first_retained = current.segments.len().saturating_sub(maximum);
        if first_retained == 0 {
            return Ok(None);
        }
        self.prepare_history_publication(first_retained).map(Some)
    }

    /// Reserves the physical publication lane for a new history manifest that
    /// retires `segments[..first_retained]` (none for the routing rewrite).
    fn prepare_history_publication(&mut self, first_retained: usize) -> io::Result<PreparedHistoryCompaction> {
        let current = self
            .history_manifest
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "history is empty"))?;
        if self.rotation_in_flight || self.deferred_rotation.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "rotation owns the history publication lane",
            ));
        }
        let prepared = PreparedHistoryCompaction {
            history_id: self.state.history_manifest_block_id.ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "history manifest witness missing",
                )
            })?,
            payload_id: self.state.payload_manifest_block_id,
            current: current.clone(),
            first_retained,
            payload_references: self
                .payload_manifest
                .as_ref()
                .map_or_else(Vec::new, |manifest| manifest.segments.clone()),
            blocks: Arc::clone(&self.blocks),
            captured_state: self.state.clone(),
            control_dirty: self.control_dirty,
            control: Arc::clone(&self.control),
            wal_cover: self.wal.publication_cover(Arc::clone(&self.vfs))?,
            restart_required: Arc::clone(&self.restart_required),
            completed: false,
            lazy_routing_retirement: true,
            promotion_group_bytes: self.checkpoint_materialize_batch_bytes,
            #[cfg(test)]
            payload_segment_test_ids: Vec::new(),
        };
        self.async_publication_in_flight = true;
        self.open_publication_gc_window();
        Ok(prepared)
    }

    /// The published routing root's shape; `None` without indexed history.
    pub fn history_routing(&self) -> io::Result<Option<HistoryRouting>> {
        let Some(manifest) = self.history_manifest.as_ref() else { return Ok(None) };
        let Some(id) = manifest.payload_index_root_id else { return Ok(None) };
        let root = crate::history_payload::RoutingRoot::decode(&self.blocks.get_materialized_shared(id)?)?;
        Ok(Some(HistoryRouting {
            version: root.version(),
            entries: root.entries(),
            pages: root.page_ids().count(),
            retained_sources: manifest.segments.len(),
        }))
    }

    /// The downgrade converter (`tofu-db rewrite-history-routing`). A lazily
    /// retired root (version 2) is rewritten to hold only the locations of the
    /// sources the manifest retains and published as version 1, which engines
    /// before 2026-11-08 open; nothing is retired and every read answers as
    /// before. The next automatic slice of a current engine marks it again.
    /// Returns whether a root was rewritten.
    pub fn rewrite_history_routing(&mut self) -> io::Result<bool> {
        self.require_usable_authority()?;
        self.require_physical_maintenance_available()?;
        if self.history_routing()?.is_none_or(|routing| routing.version == 1) {
            return Ok(false);
        }
        if self.state.authority_state_root.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history compaction requires a current authority state root",
            ));
        }
        let mut prepared = self.prepare_history_publication(0)?;
        prepared.use_streaming_routing_retirement();
        let outcome = encode_history_compaction(&prepared);
        self.finish_history_compaction(prepared, outcome)?;
        Ok(true)
    }

    pub(crate) fn finish_history_compaction(
        &mut self,
        mut prepared: PreparedHistoryCompaction,
        outcome: io::Result<HistoryCompactionOutcome>,
    ) -> io::Result<HistoryCompactionMetrics> {
        self.async_publication_in_flight = false;
        self.close_publication_gc_window();
        prepared.completed = true;
        if let Err(error) = self.require_usable_authority() {
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
            return Err(error);
        }
        // Logical appends may advance while encoding. Physical roots may not:
        // rebasing them would erase a competing catalog or checkpoint.
        if self.state.history_manifest_block_id != Some(prepared.history_id)
            || self.state.payload_manifest_block_id != prepared.payload_id
        {
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "history compaction source roots changed",
            ));
        }
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(error) => {
                // The unlocked CONTROL publication may have partially landed.
                self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
                return Err(error);
            }
        };
        let advanced = self.state.durable_sequence != outcome.published_state.durable_sequence
            || self.state.root_hash != outcome.published_state.root_hash
            || self.state.authority_state_root != outcome.published_state.authority_state_root;
        self.state.generation = outcome.published_state.generation;
        self.state.history_manifest_block_id = Some(outcome.history_id);
        self.state.payload_manifest_block_id = outcome.payload_id;
        self.control_dirty = advanced;
        let installed = (|| -> io::Result<()> {
            self.blocks
                .install_history_payload_catalog(&outcome.history, outcome.routing)?;
            Ok(())
        })();
        if let Err(error) = installed {
            self.restart_required
                .store(true, std::sync::atomic::Ordering::Relaxed);
            return Err(error);
        }
        if let Some(payload) = outcome.payload {
            self.payload_manifest = Some(payload);
        }
        let metrics = HistoryCompactionMetrics {
            retained_first_sequence: outcome.history.segments[0].first_sequence,
            retained_segments: outcome.history.segments.len() as u32,
            retired_segments: prepared.first_retained as u32,
        };
        self.history_manifest = Some(outcome.history);
        Ok(metrics)
    }
}

/// No Engine/authority borrow. Keeps one promotion group resident: the
/// retired sources of one automatic slice, whose total the launch
/// materialization budget bounds (a single larger source forms its own
/// group), plus one payload-segment encoder. Automatic slices write no
/// routing page; bulk operator compaction streams one rewrite of the retained
/// routing. Existing payload catalog/GC owns promoted bytes; no new registry
/// exists.
pub(crate) fn encode_history_compaction(
    prepared: &PreparedHistoryCompaction,
) -> io::Result<HistoryCompactionOutcome> {
    let blocks = &prepared.blocks;
    let mut promotion = HistoryPromotion {
        blocks: blocks.as_ref(),
        references: prepared.payload_references.clone(),
        promoted_segment_ids: Vec::new(),
        #[cfg(test)]
        test_segment_ids: prepared.payload_segment_test_ids.iter(),
    };
    let mut group = PromotionGroup::default();
    for reference in &prepared.current.segments[..prepared.first_retained] {
        if let Some(index_id) = reference.payload_index_block_id {
            let source = blocks.get_materialized_uncached(reference.block_id)?;
            let index = crate::history_payload::SegmentIndex::build(&source, reference)?;
            if index.encode()? != *blocks.get_materialized_shared(index_id)? {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "history promotion offset witness mismatch",
                ));
            }
            if !index.entries.is_empty() {
                if !group.units.is_empty()
                    && group.source_bytes + source.len() as u64 > prepared.promotion_group_bytes
                {
                    promote_history_group(&mut promotion, &std::mem::take(&mut group))?;
                }
                group.add(source, &index.entries)?;
            }
        }
    }
    if !group.units.is_empty() {
        promote_history_group(&mut promotion, &group)?;
    }
    drop(group);
    let changed = !promotion.promoted_segment_ids.is_empty();
    let HistoryPromotion { references, promoted_segment_ids, .. } = promotion;
    let segments = prepared.current.segments[prepared.first_retained..].to_vec();
    let routing = prepared
        .current
        .payload_index_root_id
        .map(|id| {
            let root =
                crate::history_payload::RoutingRoot::decode(&blocks.get_materialized_shared(id)?)?;
            if prepared.lazy_routing_retirement {
                if root.holds_retired() {
                    return Ok((root, id));
                }
                // Same pages; only the root's version tells older engines,
                // which read a retired location as corruption, to refuse it.
                let root = root.with_retired_sources();
                let id = blocks.put_loose(&root.encode()?)?;
                return Ok((root, id));
            }
            let root = root.retain_sources(blocks,
                &segments.iter().map(|reference| reference.block_id).collect())?;
            let id = blocks.put_loose(&root.encode()?)?;
            Ok::<_, io::Error>((root, id))
        })
        .transpose()?;
    let (routing, routing_id) = routing.unzip();
    let mut history = HistoryManifest {
        manifest_page_ids: Vec::new(),
        checkpoint_sequence: prepared.current.checkpoint_sequence,
        checkpoint_hash: prepared.current.checkpoint_hash,
        payload_index_root_id: routing_id,
        segments,
    };
    let history_id = history.store(&blocks)?;
    let payload = changed
        .then(|| PayloadManifest::new(references))
        .transpose()?;
    let payload_id = match &payload {
        Some(manifest) => Some(blocks.put_loose(&manifest.encode()?)?),
        None => prepared.payload_id,
    };
    // The reserved physical lane prevents competing CONTROL publications.
    // Logical commits continue on this same WAL and are reconciled at finish.
    if let Some(cover) = &prepared.wal_cover { cover.sync()?; }
    let mut control = prepared.control.lock()
        .map_err(|_| io::Error::other("tofu-db CONTROL lock is poisoned"))?;
    let mut captured = prepared.captured_state.clone();
    if prepared.control_dirty {
        captured.generation = captured.generation.checked_add(1)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?;
        control.publish(&captured)?;
    }
    let published_state = ControlState {
        generation: captured.generation.checked_add(1)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?,
        history_manifest_block_id: Some(history_id),
        payload_manifest_block_id: payload_id,
        ..captured
    };
    control.publish(&published_state)?;
    // A full catalog replaces its smallest fitting segments. Republish the
    // new catalog into the fallback slot too, so no durable CONTROL
    // generation names a replaced source once it is parked below.
    let published_state = if payload.is_some() {
        let stabilized = ControlState {
            generation: published_state.generation.checked_add(1)
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?,
            ..published_state
        };
        control.publish(&stabilized)?;
        stabilized
    } else {
        published_state
    };
    drop(control);
    // Route/filter preparation may open promoted segments. Like pack adoption,
    // install it outside authority after CONTROL owns the complete catalog.
    if let Some(payload) = &payload {
        blocks.install_payload_manifest(payload.clone())?;
        // Replaced sources (including promotions superseded within this
        // slice) take the same parked retirement as checkpoint merges and
        // pack adoption. A failed park leaves an unreferenced file that the
        // online collector's orphan lane reclaims.
        let retained = payload.segments.iter().map(|reference| reference.segment_id)
            .collect::<BTreeSet<_>>();
        let store = blocks.payload_segment_store()?;
        for segment_id in prepared.payload_references.iter().map(|reference| reference.segment_id)
            .chain(promoted_segment_ids)
            .filter(|segment_id| !retained.contains(segment_id))
        {
            let _ = store.defer_segment_retirement(segment_id);
        }
    }
    Ok(HistoryCompactionOutcome {
        history,
        history_id,
        routing,
        payload,
        payload_id,
        published_state,
    })
}

/// Catalog state shared by every promotion group of one compaction slice.
struct HistoryPromotion<'a> {
    blocks: &'a BlockStore,
    references: Vec<PayloadSegmentReference>,
    /// Every segment this slice sealed, in order. A later write of the same
    /// slice may replace one of them; parking then retires it too.
    promoted_segment_ids: Vec<Uuid>,
    #[cfg(test)]
    test_segment_ids: std::slice::Iter<'a, Uuid>,
}

/// Consecutive retired sources whose carried payloads share segment writes.
/// The sources stay resident until the group is placed: a sealed segment
/// stores its IDs in sorted order, which interleaves every source.
#[derive(Default)]
struct PromotionGroup {
    sources: Vec<Arc<Vec<u8>>>,
    source_bytes: u64,
    /// First authenticated location of each carried ID: source, offset, length.
    locations: BTreeMap<BlockId, (usize, u32, u32)>,
    /// Retirement-ordered placement units: the IDs one source adds to the
    /// group, split at `MAX_SEGMENT_BLOCKS` like the former per-source batches.
    units: Vec<PromotionUnit>,
}

struct PromotionUnit {
    ids: Vec<BlockId>,
    bytes: u64,
}

impl PromotionGroup {
    fn add(&mut self, source: Arc<Vec<u8>>, entries: &[(BlockId, u32, u32)]) -> io::Result<()> {
        let index = self.sources.len();
        self.source_bytes = self.source_bytes.checked_add(source.len() as u64)
            .ok_or_else(promotion_size_overflow)?;
        self.sources.push(source);
        for chunk in entries.chunks(MAX_SEGMENT_BLOCKS) {
            let mut unit = PromotionUnit { ids: Vec::new(), bytes: 0 };
            for &(id, offset, length) in chunk {
                if let btree_map::Entry::Vacant(slot) = self.locations.entry(id) {
                    slot.insert((index, offset, length));
                    unit.ids.push(id);
                    unit.bytes = unit.bytes.checked_add(u64::from(length))
                        .ok_or_else(promotion_size_overflow)?;
                }
            }
            if !unit.ids.is_empty() { self.units.push(unit); }
        }
        Ok(())
    }

    fn payload(&self, id: BlockId) -> io::Result<Option<Vec<u8>>> {
        let Some(&(source, offset, length)) = self.locations.get(&id) else { return Ok(None) };
        let start = offset as usize;
        start.checked_add(length as usize)
            .and_then(|end| self.sources[source].get(start..end))
            .map(|payload| Some(payload.to_vec()))
            .ok_or_else(invalid_promotion_source)
    }
}

/// Plans the next segment write for the leading `units`: how many it absorbs
/// and which catalog slot it replaces (`None` appends). Metadata only.
/// Below the ceiling a fresh segment takes the longest prefix the segment
/// format admits. At the ceiling the smallest segment that can absorb every
/// remaining unit takes all of them, so one slice rewrites one existing
/// segment. Only when no segment can do that does the smallest segment that
/// fits the first unit take the longest prefix it can hold. `None`: no
/// segment can absorb even the first unit.
fn plan_history_promotion(references: &[PayloadSegmentReference], units: &[PromotionUnit])
    -> Option<(usize, Option<usize>)> {
    let fitting = |blocks: u32, bytes: u64| {
        let (mut blocks, mut bytes) = (blocks as usize, bytes);
        units.iter().take_while(|unit| {
            blocks += unit.ids.len();
            bytes += unit.bytes;
            blocks <= MAX_SEGMENT_BLOCKS && bytes <= MAX_SEGMENT_PAYLOAD_BYTES
        }).count()
    };
    if references.len() < MAX_PAYLOAD_SEGMENTS {
        let take = fitting(0, 0);
        return (take > 0).then_some((take, None));
    }
    let smallest = |minimum: usize| references.iter().enumerate()
        .filter(|(_, reference)| fitting(reference.block_count, reference.payload_bytes) >= minimum)
        .min_by_key(|(_, reference)| (reference.payload_bytes, reference.block_count))
        .map(|(slot, _)| slot);
    if let Some(slot) = smallest(units.len()) {
        return Some((units.len(), Some(slot)));
    }
    let slot = smallest(1)?;
    Some((fitting(references[slot].block_count, references[slot].payload_bytes), Some(slot)))
}

/// Places one group with as few segment writes as the plan admits. A full
/// catalog is a packing problem, not a reason to spill carried blocks to
/// loose files; only a unit that no segment can absorb takes the durable
/// loose fallback, outside the foreground mutex.
fn promote_history_group(promotion: &mut HistoryPromotion, group: &PromotionGroup) -> io::Result<()> {
    let mut units = group.units.as_slice();
    while let Some(first) = units.first() {
        // Without a segment directory, catalog capacity is a protocol bound.
        let plan = match promotion.blocks.payload_segment_store() {
            Ok(_) => plan_history_promotion(&promotion.references, units),
            Err(_) => None,
        };
        let Some((take, slot)) = plan else {
            for &id in &first.ids {
                crate::fsync_stats::note_history_promotion_loose_block();
                promotion.blocks.put_loose(&group.payload(id)?.ok_or_else(invalid_promotion_source)?)?;
            }
            units = &units[1..];
            continue;
        };
        promotion.write_segment(group, &units[..take], slot)?;
        units = &units[take..];
    }
    Ok(())
}

impl HistoryPromotion<'_> {
    fn next_segment_id(&mut self) -> Uuid {
        #[cfg(test)]
        if let Some(id) = self.test_segment_ids.next() {
            return *id;
        }
        Uuid::new_v4()
    }

    /// Seals `units` into one new segment using their authenticated source
    /// offsets. A replacement also copies every block of the catalog segment
    /// in `slot` through its exact verified handle. The replaced file stays
    /// readable until the caller parks it after both CONTROL slots move.
    fn write_segment(&mut self, group: &PromotionGroup, units: &[PromotionUnit], slot: Option<usize>)
        -> io::Result<()> {
        let blocks = self.blocks;
        let store = blocks.payload_segment_store()?;
        let replaced = match slot {
            Some(slot) => {
                let reference = self.references[slot];
                let old = store.open_segment(reference.segment_id)?;
                if PayloadSegmentReference::new(old.metadata(), old.first_block_id(), old.last_block_id())? != reference {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "history promotion catalog source changed"));
                }
                Some(old)
            }
            None => None,
        };
        let promoted_ids: Vec<BlockId> = units.iter().flat_map(|unit| unit.ids.iter().copied()).collect();
        let promoted_bytes = units.iter().map(|unit| unit.bytes).sum::<u64>();
        let mut ids = promoted_ids.clone();
        if let Some(old) = &replaced { ids.extend(old.block_ids()); }
        ids.sort_unstable();
        ids.dedup();
        let segment_id = self.next_segment_id();
        let segment = store.create(segment_id, ids, |id| {
            // Incoming authenticated history bytes win duplicate IDs.
            if let Some(payload) = group.payload(id)? { return Ok(payload); }
            let old = replaced.as_ref().ok_or_else(invalid_promotion_source)?;
            store.get_with_handle(old, id)?.ok_or_else(invalid_promotion_source)
        })?;
        let promoted = PayloadSegmentReference::new(segment.metadata(),
            segment.first_block_id(), segment.last_block_id())?;
        blocks.verify_payload_segment(&promoted)?;
        blocks.note_packed(&promoted_ids)?;
        crate::fsync_stats::note_history_promotion_pack(promoted_bytes, slot.is_some());
        match slot {
            Some(slot) => self.references[slot] = promoted,
            None => self.references.push(promoted),
        }
        self.promoted_segment_ids.push(segment_id);
        Ok(())
    }
}

fn invalid_promotion_source() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "history promotion source block is missing")
}

fn promotion_size_overflow() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "history promotion size overflow")
}

#[cfg(test)]
mod promotion_tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Operation, Vfs};
    use std::path::Path;

    const NO_TEST_IDS: &[Uuid] = &[];

    fn promotion(blocks: &BlockStore, references: Vec<PayloadSegmentReference>) -> HistoryPromotion<'_> {
        HistoryPromotion { blocks, references, promoted_segment_ids: Vec::new(),
            test_segment_ids: NO_TEST_IDS.iter() }
    }

    /// Retired sources in order, each laid out with authenticated offsets.
    fn carried_group(sources: &[Vec<(BlockId, Vec<u8>)>]) -> PromotionGroup {
        let mut group = PromotionGroup::default();
        for payloads in sources {
            let mut payloads = payloads.clone();
            payloads.sort_by_key(|entry| entry.0);
            let mut source = Vec::new(); let mut entries = Vec::new();
            for (id, bytes) in &payloads {
                entries.push((*id, source.len() as u32, bytes.len() as u32));
                source.extend(bytes);
            }
            group.add(Arc::new(source), &entries).unwrap();
        }
        group
    }

    fn unit(blocks: usize, bytes: u64) -> PromotionUnit {
        PromotionUnit { ids: vec![BlockId([0; 32]); blocks], bytes }
    }

    fn catalog_entry(segment: u128, block_count: u32, payload_bytes: u64) -> PayloadSegmentReference {
        PayloadSegmentReference { segment_id: Uuid::from_u128(segment), shard: 0, block_count, payload_bytes,
            file_bytes: payload_bytes, first_block_id: BlockId([0; 32]), last_block_id: BlockId([255; 32]) }
    }

    #[test]
    fn full_catalog_history_promotion_uses_one_exact_source_and_keeps_old_reader() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let blocks = engine.block_store_shared();
        let store = blocks.payload_segment_store().unwrap();
        let old_bytes = b"old reader must survive";
        let old_id = BlockId::for_payload(old_bytes);
        let old = store.create(Uuid::new_v4(), vec![old_id], |_| Ok(old_bytes.to_vec())).unwrap();
        let old_ref = PayloadSegmentReference::new(old.metadata(), old.first_block_id(), old.last_block_id()).unwrap();
        // Unusable metadata entries model a full catalog. Only the chosen
        // authenticated source may be opened; no other reference is consulted.
        let mut unavailable = old_ref;
        unavailable.block_count = MAX_SEGMENT_BLOCKS as u32;
        let mut references = vec![unavailable; MAX_PAYLOAD_SEGMENTS];
        references[123] = old_ref;
        let payloads: Vec<_> = (0..128).map(|i| {
            let bytes = format!("promoted-{i:03}").repeat(64).into_bytes();
            (BlockId::for_payload(&bytes), bytes)
        }).collect();
        let group = carried_group(&[payloads.clone()]);
        let mut promotion = promotion(&blocks, references);
        vfs.arm_fault(None).unwrap();
        promote_history_group(&mut promotion, &group).unwrap();
        let trace = vfs.trace().unwrap();
        let opens = trace.iter().filter(|op| **op == Operation::Open).count();
        assert!(opens < 12, "known-source promotion reopened {opens} files");
        let references = promotion.references;
        assert_eq!(references.len(), MAX_PAYLOAD_SEGMENTS);
        assert_ne!(references[123].segment_id, old_ref.segment_id);
        let merged = store.open_segment(references[123].segment_id).unwrap();
        for (id, bytes) in payloads { assert_eq!(merged.get(id).unwrap(), Some(bytes)); }
        assert_eq!(merged.get(old_id).unwrap(), Some(old_bytes.to_vec()));
        assert_eq!(old.get(old_id).unwrap(), Some(old_bytes.to_vec()));
        // An unpublished replacement is an orphan. Crash cannot make the
        // acknowledged old source depend on its uncommitted replacement.
        std::mem::forget(engine); vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.block_store_shared().payload_segment_store().unwrap()
            .open_segment(old_ref.segment_id).unwrap().get(old_id).unwrap(), Some(old_bytes.to_vec()));
    }

    #[test]
    fn full_catalog_history_promotion_faults_keep_the_acknowledged_source() {
        use crate::vfs::{FaultAction, FaultRule};
        let seed = Arc::new(DeterministicVfs::new(None));
        seed.create_dir(Path::new("/data")).unwrap(); seed.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), seed.clone()).unwrap();
        let blocks = engine.block_store_shared(); let store = blocks.payload_segment_store().unwrap();
        let old_bytes = b"acknowledged old source";
        let old_id = BlockId::for_payload(old_bytes);
        let old = store.create(Uuid::new_v4(), vec![old_id], |_| Ok(old_bytes.to_vec())).unwrap();
        let reference = PayloadSegmentReference::new(old.metadata(),old.first_block_id(),old.last_block_id()).unwrap();
        let mut full = reference; full.block_count = MAX_SEGMENT_BLOCKS as u32;
        let references = { let mut r=vec![full;MAX_PAYLOAD_SEGMENTS];r[123]=reference;r };
        let new_bytes = b"carried new source"; let new_id=BlockId::for_payload(new_bytes);
        // Three retired sources share the one replacement under every fault.
        let group = carried_group(&[vec![(new_id, new_bytes.to_vec())],
            vec![(BlockId::for_payload(b"second retired source"), b"second retired source".to_vec())],
            vec![(BlockId::for_payload(b"third retired source"), b"third retired source".to_vec())]]);
        std::mem::forget(engine); seed.crash().unwrap();
        let baseline = Arc::new(seed.fork_durable().unwrap());
        let engine=Engine::open_with_vfs(Path::new("/data"),baseline.clone()).unwrap();
        baseline.arm_fault(None).unwrap();
        let blocks = engine.block_store_shared();
        let mut packed = promotion(&blocks, references.clone());
        promote_history_group(&mut packed, &group).unwrap();
        assert_eq!(packed.promoted_segment_ids.len(), 1);
        let trace=baseline.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions=vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation==Operation::Write { actions.push(FaultAction::ShortWrite(3)); }
            if matches!(operation,Operation::SyncData|Operation::SyncAll|Operation::SyncDirectory) { actions.push(FaultAction::DropSync); }
            for action in actions {
                let vfs=Arc::new(seed.fork_durable().unwrap());
                let engine=Engine::open_with_vfs(Path::new("/data"),vfs.clone()).unwrap();
                vfs.arm_fault(Some(FaultRule{operation_number:index as u64+1,action})).unwrap();
                let blocks = engine.block_store_shared();
                let _=promote_history_group(&mut promotion(&blocks, references.clone()), &group);
                assert!(vfs.trace().unwrap().len()>index,"fault {index} {operation:?} was not reached");
                std::mem::forget(engine);vfs.crash().unwrap();vfs.arm_fault(None).unwrap();
                let recovered=Engine::open_with_vfs(Path::new("/data"),vfs).unwrap();
                assert_eq!(recovered.block_store_shared().payload_segment_store().unwrap()
                    .open_segment(reference.segment_id).unwrap().get(old_id).unwrap(),Some(old_bytes.to_vec()));
            }
        }
    }

    #[test]
    #[ignore = "paired real-filesystem performance measurement"]
    fn history_promotion_full_catalog_paired_measurement() {
        let parent = std::env::var_os("TOFUDB_PROMOTION_BENCH_PARENT")
            .map(std::path::PathBuf::from).unwrap_or_else(std::env::temp_dir);
        let source_blocks = std::env::var("TOFUDB_PROMOTION_BENCH_SOURCE_BLOCKS")
            .ok().map(|value| value.parse::<usize>().unwrap()).unwrap_or(0);
        assert!(source_blocks <= 4096);
        let mut samples = Vec::new();
        for pair in 0..4 {
            for packed in if pair % 2 == 0 { [false, true] } else { [true, false] } {
                let directory = tempfile::tempdir_in(&parent).unwrap();
                let engine = Engine::initialize(directory.path()).unwrap();
                let blocks = engine.block_store_shared();
                let store = blocks.payload_segment_store().unwrap();
                let old_bytes = b"retained previous reader";
                let old_id = BlockId::for_payload(old_bytes);
                let mut old_payloads = BTreeMap::from([(old_id, old_bytes.to_vec())]);
                for i in 0..source_blocks {
                    let mut bytes=vec![251_u8;8192]; bytes[..8].copy_from_slice(&(i as u64).to_le_bytes());
                    old_payloads.insert(BlockId::for_payload(&bytes),bytes);
                }
                let old = store.create(Uuid::new_v4(), old_payloads.keys().copied().collect(),
                    |id| Ok(old_payloads[&id].clone())).unwrap();
                let old_ref = PayloadSegmentReference::new(old.metadata(), old.first_block_id(), old.last_block_id()).unwrap();
                let mut unavailable = old_ref; unavailable.block_count = MAX_SEGMENT_BLOCKS as u32;
                let mut references = vec![unavailable; MAX_PAYLOAD_SEGMENTS]; references[123] = old_ref;
                let payloads: Vec<_> = (0..128).map(|i| {
                    let mut bytes = vec![i as u8;8192]; bytes[..8].copy_from_slice(&(i as u64).to_le_bytes());
                    (BlockId::for_payload(&bytes),bytes)
                }).collect();
                let group = carried_group(&[payloads.clone()]);
                let start = std::time::Instant::now();
                if packed {
                    let mut promotion = promotion(&blocks, references);
                    promote_history_group(&mut promotion, &group).unwrap();
                    references = promotion.references;
                } else {
                    for (_, bytes) in &payloads { blocks.put_loose(bytes).unwrap(); }
                }
                let elapsed_ms = start.elapsed().as_secs_f64()*1000.0;
                // Reopen the actual sealed file, then read every exact byte.
                if packed {
                    let merged = store.open_segment(references[123].segment_id).unwrap();
                    for (id, bytes) in &payloads { assert_eq!(merged.get(*id).unwrap().as_ref(), Some(bytes)); }
                    for (id, bytes) in &old_payloads { assert_eq!(merged.get(*id).unwrap().as_ref(),Some(bytes)); }
                    assert_eq!(old.get(old_id).unwrap(),Some(old_bytes.to_vec()));
                } else {
                    blocks.set_cache_capacity_bytes(0).unwrap();
                    for (id, bytes) in &payloads { assert_eq!(blocks.get(*id).unwrap(),*bytes); }
                }
                samples.push(serde_json::json!({"pair":pair,"packed":packed,"elapsed_ms":elapsed_ms}));
            }
        }
        println!("HISTORY_PROMOTION_MEASURE={}", serde_json::json!({"source_sha256":env!("TOFUDB_SOURCE_SHA256"),
            "parent":parent,"blocks":128,"source_bytes":1048576,
            "replacement_source_bytes":24+source_blocks*8192,"replacement_source_blocks":source_blocks+1,"samples":samples,
            "scope":"full-catalog representation preparation and exact sealed-file readback; excludes CONTROL publication, provider and browser"}));
    }

    #[test]
    fn history_promotion_refuses_a_union_above_existing_format_bounds() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap(); vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let reference = catalog_entry(1, MAX_SEGMENT_BLOCKS as u32, MAX_SEGMENT_PAYLOAD_BYTES);
        let references = vec![reference; MAX_PAYLOAD_SEGMENTS];
        let id = BlockId::for_payload(b"x");
        let group = carried_group(&[vec![(id, b"x".to_vec())]]);
        // The refusal is a metadata decision: no candidate file is opened.
        assert_eq!(plan_history_promotion(&references, &group.units), None);
        let blocks = engine.block_store_shared();
        let mut promotion = promotion(&blocks, references);
        promote_history_group(&mut promotion, &group).unwrap();
        assert!(promotion.promoted_segment_ids.is_empty());
        assert!(promotion.references.iter().all(|entry| *entry == reference));
        assert_eq!(blocks.get(id).unwrap().as_slice(), b"x", "the unit takes the durable loose fallback");
    }

    /// A whole automatic slice shares one replacement: eight retired sources
    /// at the catalog ceiling seal one file and rewrite exactly the smallest
    /// fitting segment, instead of one rewrite per source.
    #[test]
    fn full_catalog_slice_rewrites_one_segment_for_every_retired_source() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap(); vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let blocks = engine.block_store_shared();
        let store = blocks.payload_segment_store().unwrap();
        let small_bytes = b"smallest fitting catalog segment".to_vec();
        let small_id = BlockId::for_payload(&small_bytes);
        let small = store.create(Uuid::from_u128(1), vec![small_id], |_| Ok(small_bytes.clone())).unwrap();
        let small_ref = PayloadSegmentReference::new(small.metadata(), small.first_block_id(), small.last_block_id()).unwrap();
        let larger_bytes = b"a larger catalog segment that also fits".repeat(4);
        let larger = store.create(Uuid::from_u128(2), vec![BlockId::for_payload(&larger_bytes)],
            |_| Ok(larger_bytes.clone())).unwrap();
        let larger_ref = PayloadSegmentReference::new(larger.metadata(), larger.first_block_id(), larger.last_block_id()).unwrap();
        let mut unavailable = small_ref;
        unavailable.block_count = MAX_SEGMENT_BLOCKS as u32;
        let mut references = vec![unavailable; MAX_PAYLOAD_SEGMENTS];
        references[7] = larger_ref;
        references[123] = small_ref;
        // Sources 0 and 5 both carry one shared payload; the slice packs it once.
        let shared = b"payload carried by two retired sources".to_vec();
        let sources: Vec<Vec<(BlockId, Vec<u8>)>> = (0..8).map(|source| {
            let mut payloads: Vec<_> = (0..16).map(|block| {
                let bytes = format!("source-{source}-block-{block:02}").repeat(32).into_bytes();
                (BlockId::for_payload(&bytes), bytes)
            }).collect();
            if source == 0 || source == 5 { payloads.push((BlockId::for_payload(&shared), shared.clone())); }
            payloads
        }).collect();
        let unique: BTreeMap<_, _> = sources.iter().flatten().cloned().collect();
        let group = carried_group(&sources);
        assert_eq!(group.units.len(), 8);
        let mut promotion = promotion(&blocks, references.clone());
        vfs.arm_fault(None).unwrap();
        promote_history_group(&mut promotion, &group).unwrap();
        let trace = vfs.trace().unwrap();
        assert_eq!(trace.iter().filter(|op| **op == Operation::Rename).count(), 1,
            "the whole slice seals exactly one segment");
        assert_eq!(promotion.promoted_segment_ids.len(), 1);
        let replaced: Vec<_> = (0..MAX_PAYLOAD_SEGMENTS)
            .filter(|&slot| promotion.references[slot] != references[slot]).collect();
        assert_eq!(replaced, vec![123], "only the smallest fitting segment is rewritten");
        let merged_ref = promotion.references[123];
        assert_eq!(merged_ref.block_count as usize, 1 + unique.len());
        assert_eq!(merged_ref.payload_bytes,
            small_ref.payload_bytes + unique.values().map(|bytes| bytes.len() as u64).sum::<u64>());
        let merged = store.open_segment(merged_ref.segment_id).unwrap();
        for (id, bytes) in &unique { assert_eq!(merged.get(*id).unwrap().as_ref(), Some(bytes)); }
        assert_eq!(merged.get(small_id).unwrap(), Some(small_bytes.clone()));
        assert_eq!(small.get(small_id).unwrap(), Some(small_bytes), "the replaced file keeps its readers");
    }

    /// Operator compaction streams several groups through one catalog: a later
    /// group may replace the segment an earlier group sealed. Both IDs stay
    /// tracked, so publication parks the superseded one.
    #[test]
    fn later_group_may_replace_a_segment_sealed_earlier_in_the_slice() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap(); vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let blocks = engine.block_store_shared();
        let store = blocks.payload_segment_store().unwrap();
        let old_bytes = b"only fitting catalog segment".to_vec();
        let old_id = BlockId::for_payload(&old_bytes);
        let old = store.create(Uuid::from_u128(1), vec![old_id], |_| Ok(old_bytes.clone())).unwrap();
        let old_ref = PayloadSegmentReference::new(old.metadata(), old.first_block_id(), old.last_block_id()).unwrap();
        let mut unavailable = old_ref;
        unavailable.block_count = MAX_SEGMENT_BLOCKS as u32;
        let mut references = vec![unavailable; MAX_PAYLOAD_SEGMENTS];
        references[123] = old_ref;
        let first = (BlockId::for_payload(b"first group"), b"first group".to_vec());
        let second = (BlockId::for_payload(b"second group"), b"second group".to_vec());
        let mut promotion = promotion(&blocks, references);
        promote_history_group(&mut promotion, &carried_group(&[vec![first.clone()]])).unwrap();
        promote_history_group(&mut promotion, &carried_group(&[vec![second.clone()]])).unwrap();
        assert_eq!(promotion.promoted_segment_ids.len(), 2);
        assert_eq!(promotion.references[123].segment_id, promotion.promoted_segment_ids[1]);
        let merged = store.open_segment(promotion.promoted_segment_ids[1]).unwrap();
        for (id, bytes) in [(old_id, old_bytes), first, second] {
            assert_eq!(merged.get(id).unwrap(), Some(bytes));
        }
    }

    #[test]
    fn promotion_plan_rewrites_one_segment_while_any_segment_fits_the_slice() {
        const MIB: u64 = 1024 * 1024;
        let slice = [unit(10, 4 * MIB), unit(10, 4 * MIB), unit(10, 4 * MIB)];
        // Headroom: one fresh segment holds the whole slice; format bounds
        // alone split it.
        assert_eq!(plan_history_promotion(&[catalog_entry(1, 1, 1)], &slice), Some((3, None)));
        let wide = [unit(MAX_SEGMENT_BLOCKS - 5, 1), unit(10, 1), unit(1, 1)];
        assert_eq!(plan_history_promotion(&[], &wide), Some((1, None)));
        assert_eq!(plan_history_promotion(&[], &wide[1..]), Some((2, None)));
        // Ceiling: the smallest segment that fits every unit takes them all.
        // Slot 13 is smaller but lacks block room for the whole slice.
        let mut catalog = vec![catalog_entry(9, 1, MAX_SEGMENT_PAYLOAD_BYTES); MAX_PAYLOAD_SEGMENTS];
        catalog[11] = catalog_entry(11, 100, 200 * MIB);
        catalog[12] = catalog_entry(12, 100, 100 * MIB);
        catalog[13] = catalog_entry(13, MAX_SEGMENT_BLOCKS as u32 - 15, MIB);
        assert_eq!(plan_history_promotion(&catalog, &slice), Some((3, Some(12))));
        // No segment fits the slice: the smallest segment that fits the
        // first unit takes the longest prefix it can hold, then the next.
        catalog[12] = catalog_entry(12, 100, MAX_SEGMENT_PAYLOAD_BYTES - 9 * MIB);
        catalog[11] = catalog_entry(11, 100, MAX_SEGMENT_PAYLOAD_BYTES - 5 * MIB);
        assert_eq!(plan_history_promotion(&catalog, &slice), Some((1, Some(13))));
        catalog[13] = catalog_entry(13, MAX_SEGMENT_BLOCKS as u32, MIB);
        assert_eq!(plan_history_promotion(&catalog, &slice), Some((2, Some(12))));
        catalog[12] = catalog_entry(12, 120, MAX_SEGMENT_PAYLOAD_BYTES - MIB);
        assert_eq!(plan_history_promotion(&catalog, &slice[2..]), Some((1, Some(11))));
        // Nothing can absorb even one unit.
        catalog[11] = catalog_entry(11, 100, MAX_SEGMENT_PAYLOAD_BYTES - MIB);
        assert_eq!(plan_history_promotion(&catalog, &slice[2..]), None);
    }
}
