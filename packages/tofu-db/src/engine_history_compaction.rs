//! History pruning with immutable payload promotion outside the authority mutex.
//!
//! Engine reserves its existing physical-publication lane; the maintenance
//! worker encodes bounded source-sized payload segments and publishes CONTROL
//! outside authority; Engine then reconciles the current logical state.
use super::*;

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
    #[cfg(test)]
    payload_segment_test_ids: Vec<Uuid>,
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
        if maximum == 0 || maximum > crate::history::MAX_HISTORY_SEGMENTS {
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
            #[cfg(test)]
            payload_segment_test_ids: Vec::new(),
        };
        self.async_publication_in_flight = true;
        Ok(Some(prepared))
    }

    pub(crate) fn finish_history_compaction(
        &mut self,
        mut prepared: PreparedHistoryCompaction,
        outcome: io::Result<HistoryCompactionOutcome>,
    ) -> io::Result<HistoryCompactionMetrics> {
        self.async_publication_in_flight = false;
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

/// No Engine/authority borrow: at most one <=4 MiB history source and one
/// bounded payload-segment encoder are resident at once. Existing payload
/// catalog/GC handles the promoted bytes; no second lifetime registry exists.
pub(crate) fn encode_history_compaction(
    prepared: &PreparedHistoryCompaction,
) -> io::Result<HistoryCompactionOutcome> {
    let blocks = &prepared.blocks;
    let mut references = prepared.payload_references.clone();
    let mut changed = false;
    #[cfg(test)]
    let mut promotion_ordinal = 0;
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
            for entries in index.entries.chunks(MAX_SEGMENT_BLOCKS) {
                let read = |id: BlockId| -> io::Result<Vec<u8>> {
                    let position =
                        entries
                            .binary_search_by_key(&id, |entry| entry.0)
                            .map_err(|_| {
                                io::Error::new(
                                    io::ErrorKind::InvalidData,
                                    "history promotion block missing",
                                )
                            })?;
                    let (_, offset, length) = entries[position];
                    Ok(source[offset as usize..(offset + length) as usize].to_vec())
                };
                if blocks.payload_segment_store().is_ok()
                {
                    let ids: Vec<_> = entries.iter().map(|entry| entry.0).collect();
                    #[cfg(test)]
                    let segment_id = {
                        let id = prepared.payload_segment_test_ids.get(promotion_ordinal)
                            .copied().unwrap_or_else(Uuid::new_v4);
                        promotion_ordinal += 1;
                        id
                    };
                    #[cfg(not(test))]
                    let segment_id = Uuid::new_v4();
                    // A full catalog is a packing problem, not a reason to
                    // synchronously spill every carried block to a loose file.
                    if promote_history_batch(blocks, &mut references, entries,
                        source.as_slice(), segment_id)? {
                        changed = true;
                    } else {
                        for &(id, _, _) in entries {
                            crate::fsync_stats::note_history_promotion_loose_block();
                            blocks.put_loose(&read(id)?)?;
                        }
                    }
                } else {
                    // Existing catalog capacity is a protocol bound. Durable
                    // loose fallback stays outside the foreground mutex.
                    for &(id, _, _) in entries {
                        crate::fsync_stats::note_history_promotion_loose_block();
                        blocks.put_loose(&read(id)?)?;
                    }
                }
            }
        }
    }
    let segments = prepared.current.segments[prepared.first_retained..].to_vec();
    let routing = prepared
        .current
        .payload_index_root_id
        .map(|id| {
            let root =
                crate::history_payload::RoutingRoot::decode(&blocks.get_materialized_shared(id)?)?;
            root.retain_sources(
                blocks,
                &segments
                    .iter()
                    .map(|reference| reference.block_id)
                    .collect(),
            )
        })
        .transpose()?;
    let routing_id = routing
        .as_ref()
        .map(|root| blocks.put_loose(&root.encode()?))
        .transpose()?;
    let history = HistoryManifest {
        checkpoint_sequence: prepared.current.checkpoint_sequence,
        checkpoint_hash: prepared.current.checkpoint_hash,
        payload_index_root_id: routing_id,
        segments,
    };
    let history_id = blocks.put_loose(&history.encode()?)?;
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
    drop(control);
    // Route/filter preparation may open promoted segments. Like pack adoption,
    // install it outside authority after CONTROL owns the complete catalog.
    if let Some(payload) = &payload {
        blocks.install_payload_manifest(payload.clone())?;
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

/// Pack a carried history batch using its authenticated source offsets. At the
/// catalog ceiling, replace the smallest fitting segment through its exact
/// verified handle. Old sources remain until ordinary CONTROL-aware GC; readers
/// and fallback CONTROL slots must still be able to resolve them.
fn promote_history_batch(
    blocks: &BlockStore,
    references: &mut Vec<PayloadSegmentReference>,
    entries: &[(BlockId, u32, u32)],
    source: &[u8],
    segment_id: Uuid,
) -> io::Result<bool> {
    let batch_bytes = entries.iter().try_fold(0_u64, |total, (_, _, length)|
        total.checked_add(u64::from(*length)).ok_or_else(||
            io::Error::new(io::ErrorKind::InvalidData, "history batch size overflow")))?;
    let replacement = if references.len() >= MAX_PAYLOAD_SEGMENTS {
        let candidate = references.iter().enumerate().filter(|(_, old)|
            old.block_count as usize + entries.len() <= MAX_SEGMENT_BLOCKS
                && old.payload_bytes + batch_bytes <= MAX_SEGMENT_PAYLOAD_BYTES)
            .min_by_key(|(_, old)| (old.payload_bytes, old.block_count));
        let Some((slot, reference)) = candidate else { return Ok(false); };
        let old = blocks.payload_segment_store()?.open_segment(reference.segment_id)?;
        if PayloadSegmentReference::new(old.metadata(), old.first_block_id(), old.last_block_id())? != *reference {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "history promotion catalog source changed"));
        }
        Some((slot, old))
    } else { None };
    let mut ids: Vec<_> = entries.iter().map(|entry| entry.0).collect();
    if let Some((_, segment)) = &replacement { ids.extend(segment.block_ids()); }
    ids.sort_unstable(); ids.dedup();
    let store = blocks.payload_segment_store()?;
    let segment = store.create(segment_id, ids, |id| {
        if let Ok(position) = entries.binary_search_by_key(&id, |entry| entry.0) {
            let (_, offset, length) = entries[position];
            let start = usize::try_from(offset).map_err(|_| invalid_promotion_source())?;
            let end = start.checked_add(length as usize).ok_or_else(invalid_promotion_source)?;
            return source.get(start..end).map(<[u8]>::to_vec).ok_or_else(invalid_promotion_source);
        }
        let (_, old) = replacement.as_ref().ok_or_else(invalid_promotion_source)?;
        store.get_with_handle(old, id)?.ok_or_else(invalid_promotion_source)
    })?;
    let promoted = PayloadSegmentReference::new(segment.metadata(),
        segment.first_block_id(), segment.last_block_id())?;
    blocks.verify_payload_segment(&promoted)?;
    blocks.note_packed(&entries.iter().map(|entry| entry.0).collect::<Vec<_>>())?;
    crate::fsync_stats::note_history_promotion_pack(batch_bytes, replacement.is_some());
    if let Some((slot, _)) = replacement { references[slot] = promoted; }
    else { references.push(promoted); }
    Ok(true)
}

fn invalid_promotion_source() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "history promotion source block is missing")
}

#[cfg(test)]
mod promotion_tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Operation, Vfs};
    use std::path::Path;

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
        let mut payloads: Vec<_> = (0..128).map(|i| {
            let bytes = format!("promoted-{i:03}").repeat(64).into_bytes();
            (BlockId::for_payload(&bytes), bytes)
        }).collect();
        payloads.sort_by_key(|entry| entry.0);
        let mut source = Vec::new(); let mut entries = Vec::new();
        for (id, bytes) in &payloads {
            entries.push((*id, source.len() as u32, bytes.len() as u32));
            source.extend(bytes);
        }
        vfs.arm_fault(None).unwrap();
        assert!(promote_history_batch(&blocks, &mut references, &entries, &source, Uuid::new_v4()).unwrap());
        let trace = vfs.trace().unwrap();
        let opens = trace.iter().filter(|op| **op == Operation::Open).count();
        assert!(opens < 12, "known-source promotion reopened {opens} files");
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
        let entries = [(new_id,0,new_bytes.len() as u32)];
        std::mem::forget(engine); seed.crash().unwrap();
        let baseline = Arc::new(seed.fork_durable().unwrap());
        let engine=Engine::open_with_vfs(Path::new("/data"),baseline.clone()).unwrap();
        baseline.arm_fault(None).unwrap();
        assert!(promote_history_batch(&engine.block_store_shared(),&mut references.clone(),
            &entries,new_bytes,Uuid::new_v4()).unwrap());
        let trace=baseline.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions=vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation==Operation::Write { actions.push(FaultAction::ShortWrite(3)); }
            if matches!(operation,Operation::SyncData|Operation::SyncAll|Operation::SyncDirectory) { actions.push(FaultAction::DropSync); }
            for action in actions {
                let vfs=Arc::new(seed.fork_durable().unwrap());
                let engine=Engine::open_with_vfs(Path::new("/data"),vfs.clone()).unwrap();
                vfs.arm_fault(Some(FaultRule{operation_number:index as u64+1,action})).unwrap();
                let _=promote_history_batch(&engine.block_store_shared(),&mut references.clone(),
                    &entries,new_bytes,Uuid::new_v4());
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
                let mut payloads: Vec<_> = (0..128).map(|i| {
                    let mut bytes = vec![i as u8;8192]; bytes[..8].copy_from_slice(&(i as u64).to_le_bytes());
                    (BlockId::for_payload(&bytes),bytes)
                }).collect(); payloads.sort_by_key(|entry| entry.0);
                let mut source = Vec::new(); let mut entries = Vec::new();
                for (id, bytes) in &payloads {
                    entries.push((*id, source.len() as u32, bytes.len() as u32)); source.extend(bytes);
                }
                let start = std::time::Instant::now();
                if packed {
                    assert!(promote_history_batch(&blocks, &mut references, &entries, &source, Uuid::new_v4()).unwrap());
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
        let reference = PayloadSegmentReference { segment_id: Uuid::new_v4(), shard: 0,
            block_count: MAX_SEGMENT_BLOCKS as u32, payload_bytes: MAX_SEGMENT_PAYLOAD_BYTES,
            file_bytes: MAX_SEGMENT_PAYLOAD_BYTES, first_block_id: BlockId([0;32]), last_block_id: BlockId([255;32]) };
        let mut references = vec![reference; MAX_PAYLOAD_SEGMENTS];
        vfs.arm_fault(None).unwrap();
        assert!(!promote_history_batch(&engine.block_store_shared(), &mut references,
            &[(BlockId::for_payload(b"x"),0,1)], b"x", Uuid::new_v4()).unwrap());
        assert!(vfs.trace().unwrap().is_empty());
        assert!(references.iter().all(|entry| *entry == reference));
    }
}
