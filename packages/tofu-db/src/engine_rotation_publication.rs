//! Predecessor checkpoint publication without the foreground authority mutex.
//! The rotation driver reserves the existing CONTROL lane, publishes through
//! owned handles, then reconciles current successor state under authority.
//! No extra worker, queue, WAL generation or acknowledgement policy is added.
use super::*;

pub(crate) enum RotationPublication {
    Complete(u64),
    Prepared(Box<PreparedRotationPublication>),
}

pub(crate) struct PreparedRotationPublication {
    manifest: HistoryManifest,
    manifest_bytes: Vec<u8>,
    manifest_id: BlockId,
    catchup: Option<ControlState>,
    next: ControlState,
    outcome: RotationSegmentOutcome,
    blocks: Arc<BlockStore>,
    control: Arc<Mutex<ControlFile>>,
    wal_cover: Option<(PathBuf, Arc<dyn Vfs>)>,
    restart_required: Arc<std::sync::atomic::AtomicBool>,
    completed: bool,
}

impl Drop for PreparedRotationPublication {
    fn drop(&mut self) {
        if !self.completed {
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
        }
    }
}

impl Engine {
    /// Reserve metadata only; all filesystem work belongs to publish. Legacy
    /// callers and failed encodings keep the original settlement/error path.
    pub(crate) fn prepare_rotation_publication(
        &mut self,
        snapshot: Box<PreparedRotationCheckpoint>,
        outcome: io::Result<RotationSegmentOutcome>,
    ) -> io::Result<RotationPublication> {
        if snapshot.sealed_log.is_none() || outcome.is_err() {
            return self.finish_rotation_checkpoint(snapshot, outcome).map(RotationPublication::Complete);
        }
        if self.async_publication_in_flight {
            self.deferred_rotation = Some((snapshot, outcome?));
            return Err(io::Error::new(io::ErrorKind::WouldBlock,
                "background publication owns materialization; rotation deferred"));
        }
        let result = (|| {
            self.require_usable_authority()?;
            let sealed = snapshot.sealed_log.as_ref().unwrap();
            if self.state.sealed_log.as_ref() != Some(sealed) {
                return Err(io::Error::new(io::ErrorKind::InvalidData,
                    "checkpoint lost its sealed WAL ownership"));
            }
            let mut outcome = outcome?;
            let mut segments = snapshot.history_manifest_segments;
            segments.append(&mut outcome.new_references);
            let manifest = HistoryManifest {
                checkpoint_sequence: sealed.terminal_sequence,
                checkpoint_hash: sealed.terminal_hash,
                payload_index_root_id: outcome.payload_index_root_id,
                segments,
            };
            let manifest_bytes = manifest.encode()?;
            let manifest_id = BlockId::for_payload(&manifest_bytes);
            let mut generation = self.state.generation;
            let catchup = if self.control_dirty {
                generation = generation.checked_add(1)
                    .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?;
                Some(ControlState { generation, ..self.state.clone() })
            } else { None };
            let next = ControlState {
                generation: generation.checked_add(1)
                    .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow"))?,
                checkpoint_sequence: sealed.terminal_sequence,
                checkpoint_hash: sealed.terminal_hash,
                history_manifest_block_id: Some(manifest_id),
                sealed_log: None,
                ..self.state.clone()
            };
            // Cover the captured logical state before publishing CONTROL.
            // This includes Strict's in-flight group-barrier acknowledgements.
            let wal_cover = (self.wal.unsynced_bytes() > 0)
                .then(|| (self.wal.path().to_path_buf(), Arc::clone(&self.vfs)));
            let prepared = PreparedRotationPublication {
                manifest, manifest_bytes, manifest_id, catchup, next, outcome,
                blocks: Arc::clone(&self.blocks), control: Arc::clone(&self.control),
                wal_cover, restart_required: Arc::clone(&self.restart_required), completed: false,
            };
            self.async_publication_in_flight = true;
            Ok(RotationPublication::Prepared(Box::new(prepared)))
        })();
        if result.is_err() {
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
        }
        result
    }

    /// No filesystem I/O. Publication owns physical fields; successor commits
    /// own sequence/hash/entity root, including writes during the fsync window.
    pub(crate) fn finish_rotation_publication(
        &mut self,
        mut prepared: Box<PreparedRotationPublication>,
        outcome: io::Result<()>,
    ) -> io::Result<u64> {
        self.async_publication_in_flight = false;
        let result = (|| {
            outcome?;
            let advanced = self.state.durable_sequence != prepared.next.durable_sequence
                || self.state.root_hash != prepared.next.root_hash
                || self.state.authority_state_root != prepared.next.authority_state_root;
            self.state.generation = prepared.next.generation;
            self.state.checkpoint_sequence = prepared.next.checkpoint_sequence;
            self.state.checkpoint_hash = prepared.next.checkpoint_hash;
            self.state.history_manifest_block_id = Some(prepared.manifest_id);
            self.state.sealed_log = None;
            self.control_dirty = advanced;
            self.blocks.install_history_payload_catalog(&prepared.manifest, prepared.outcome.payload_routing.take())?;
            self.blocks.release_history_carried_payloads(&prepared.outcome.indexed_payload_ids)?;
            self.blocks.restore_rotation_pending()?;
            self.history_manifest = Some(prepared.manifest.clone());
            self.wal_retirement.retire(prepared.next.active_log_generation - 1)?;
            self.blocks.wake_background_packer()?;
            self.blocks.finish_background_drain();
            self.rotation_read_batches.clear();
            self.rotation_in_flight = false;
            self.rotation_install_fenced = false;
            crate::fsync_stats::note_checkpoint();
            crate::fsync_stats::note_checkpoint_history_tail(0);
            Ok(self.state.checkpoint_sequence)
        })();
        if result.is_err() {
            self.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
            self.rotation_read_batches.clear();
        }
        prepared.completed = true;
        result
    }
}

/// WAL cover, immutable manifest and checksummed CONTROL publication retain
/// their ordering and lost-sync barriers; only their lock placement changes.
pub(crate) fn publish_rotation_publication(prepared: &PreparedRotationPublication) -> io::Result<()> {
    if let Some((path, vfs)) = &prepared.wal_cover {
        let mut file = vfs.open(path, OpenRequest { write: true, ..Default::default() })?;
        crate::vfs::sync_data_barrier(file.as_mut())?;
    }
    let mut control = prepared.control.lock()
        .map_err(|_| io::Error::other("tofu-db CONTROL lock is poisoned"))?;
    if let Some(catchup) = &prepared.catchup { control.publish(catchup)?; }
    {
        let _timer = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_MANIFEST);
        prepared.blocks.put_loose(&prepared.manifest_bytes)?;
    }
    let _timer = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_ROTATION_INSTALL_CONTROL);
    control.publish(&prepared.next)
}
