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
    wal_cover: Option<(PathBuf, Arc<dyn Vfs>)>,
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
            wal_cover: (self.wal.unsynced_bytes() > 0)
                .then(|| (self.wal.path().to_path_buf(), Arc::clone(&self.vfs))),
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
        self.require_usable_authority()?;
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
                if references.len() < MAX_PAYLOAD_SEGMENTS && blocks.payload_segment_store().is_ok()
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
                    let segment = blocks.payload_segment_store()?.create(
                        segment_id,
                        ids.clone(),
                        read,
                    )?;
                    let promoted = PayloadSegmentReference::new(
                        segment.metadata(),
                        segment.first_block_id(),
                        segment.last_block_id(),
                    )?;
                    blocks.verify_payload_segment(&promoted)?;
                    blocks.note_packed(&ids)?;
                    references.push(promoted);
                    changed = true;
                } else {
                    // Existing catalog capacity is a protocol bound. Durable
                    // loose fallback stays outside the foreground mutex.
                    for &(id, _, _) in entries {
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
    if let Some((path, vfs)) = &prepared.wal_cover {
        let mut file = vfs.open(path, OpenRequest { write: true, ..Default::default() })?;
        crate::vfs::sync_data_barrier(file.as_mut())?;
    }
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
