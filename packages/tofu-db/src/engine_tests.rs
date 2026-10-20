    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    fn initialized_simulated_engine() -> (Arc<DeterministicVfs>, Engine) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    #[test]
    fn spill_duplicate_corruption_never_receives_identifier_only_ack() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let payload = vec![31; MAX_CARRIED_PAYLOAD_BYTES + 128];
        let id = engine.blocks.put_loose(&payload).unwrap();
        let path = indexed_block_path(id);
        let mut file = vfs.open(&path, crate::vfs::OpenRequest { write: true, ..crate::vfs::OpenRequest::default() }).unwrap();
        file.write_all_at(0, b"X").unwrap();
        file.sync_all().unwrap();
        engine.blocks.set_cache_capacity_bytes(0).unwrap();
        // The existing blob path legitimately carries complete supplied bytes
        // without an optional loose probe. No stream optimization is involved.
        assert_eq!(engine.write_blob_block(&payload).unwrap(), id);
        assert_eq!(engine.commit_references(b"oversized supplied payload", &[id]).unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert_eq!(engine.state.durable_sequence, 0);
        assert_eq!(engine.blocks.pending_payload(id).unwrap().unwrap().as_slice(), payload);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 0);
        assert_eq!(reopened.read_block(id).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn rotation_captured_prefix_remains_visible_to_exact_reads() {
        let (_vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        engine.commit(b"prefix one").unwrap();
        engine.commit(b"prefix two").unwrap();
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected prepared rotation: {other:?}"),
        };
        engine.commit(b"window three").unwrap();
        assert_eq!(engine.transaction_at(1).unwrap().unwrap().envelope.inline_payload, b"prefix one");
        assert_eq!(engine.transaction_at(2).unwrap().unwrap().envelope.inline_payload, b"prefix two");
        assert_eq!(engine.transaction_at(3).unwrap().unwrap().envelope.inline_payload, b"window three");
        assert_eq!(engine.transaction_snapshot().unwrap().iter().map(|tx| tx.sequence).collect::<Vec<_>>(), vec![1, 2, 3]);
        let mut suffix = Vec::new();
        engine.visit_transaction_metadata_from(1, |tx| { suffix.push(tx.sequence); Ok(()) }).unwrap();
        assert_eq!(suffix, vec![1, 2, 3]);
        let encoded = encode_rotation_segments(&snapshot);
        engine.finish_rotation_checkpoint(snapshot, encoded).unwrap();
    }

    #[test]
    fn online_reclaim_can_change_first_block_shard_of_a_mixed_segment() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let payloads: BTreeMap<_, _> = (0..256).map(|index| {
            let payload = format!("mixed shard GC block {index}").into_bytes();
            (BlockId::for_payload(&payload), payload)
        }).collect();
        let dead = *payloads.first_key_value().unwrap().0;
        let live = *payloads.last_key_value().unwrap().0;
        assert_ne!(dead.0[0], live.0[0]);
        let store = engine.blocks.payload_segment_store().unwrap();
        let source = store.create(Uuid::new_v4(), vec![dead, live], |id| Ok(payloads[&id].clone())).unwrap();
        let source_ref = PayloadSegmentReference::new(source.metadata(), source.first_block_id(), source.last_block_id()).unwrap();
        engine.publish_payload_manifest(PayloadManifest::new(vec![source_ref]).unwrap(), &[]).unwrap();
        engine.blocks.begin_relink_observation();
        let replacement = engine.blocks.payload_segment_store().unwrap()
            .create(Uuid::new_v4(), vec![live], |id| Ok(payloads[&id].clone())).unwrap();
        let next_ref = PayloadSegmentReference::new(replacement.metadata(), live, live).unwrap();
        assert_ne!(source_ref.shard, next_ref.shard);
        engine.blocks.verify_payload_segment(&next_ref).unwrap();
        engine.finish_segment_reclaim(source_ref.segment_id, &source_ref, &[live], Some(next_ref)).unwrap();
        assert_eq!(engine.read_block(live).unwrap(), payloads[&live]);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.read_block(live).unwrap(), payloads[&live]);
        assert_eq!(reopened.read_block(dead).unwrap_err().kind(), io::ErrorKind::NotFound);
    }

    fn prepared_simulated_vfs() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn checkpointed_authority_history(vfs: Arc<DeterministicVfs>) -> Engine {
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let root = engine.write_block(b"authority-root").unwrap();
        engine
            .commit_references_with_authority_state(b"one", &[], Some(root))
            .unwrap();
        engine.checkpoint().unwrap();
        for payload in [b"two".as_slice(), b"three"] {
            engine.commit(payload).unwrap();
            engine.checkpoint().unwrap();
        }
        vfs.arm_fault(None).unwrap();
        engine
    }

    fn checkpointed_history_with_active_wal(vfs: Arc<DeterministicVfs>) -> Engine {
        let mut engine = checkpointed_authority_history(vfs.clone());
        let latest_root = engine.write_block(b"active-authority-root").unwrap();
        engine
            .commit_references_with_authority_state(b"four", &[latest_root], Some(latest_root))
            .unwrap();
        assert!(engine.control_dirty);
        vfs.arm_fault(None).unwrap();
        engine
    }

    fn is_sync(operation: &Operation) -> bool {
        matches!(
            operation,
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        )
    }

    fn batch(count: usize) -> Vec<BatchTransaction> {
        (0..count)
            .map(|index| BatchTransaction {
                inline_payload: format!("turn-{index:02}").into_bytes(),
                block_payloads: Vec::new(),
            })
            .collect()
    }

    #[test]
    fn checkpoint_pack_merge_tolerates_restaged_published_block() {
        let (_vfs, mut engine) = initialized_simulated_engine();
        let overlap_payload = b"overlap-restaged".to_vec();
        let overlap_id = BlockId::for_payload(&overlap_payload);
        engine.blocks.restore_loose(&overlap_payload).unwrap();
        let segment = engine
            .blocks
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![overlap_id], |id| {
                engine.blocks.get(id)
            })
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        engine
            .blocks
            .retire_staged_payloads(&BTreeSet::from([overlap_id]))
            .unwrap();
        // Recovery restaged the WAL-carried copy even though the block is
        // already packed: the chunk overlaps the oldest segment, and the
        // merge must deduplicate instead of failing the seal.
        engine.blocks.restore_loose(&overlap_payload).unwrap();
        let fresh_payload = b"fresh-merge-member".to_vec();
        let fresh_id = engine.blocks.restore_loose(&fresh_payload).unwrap();
        let mut plan = CheckpointPackPlan {
            references: vec![reference; MAX_PAYLOAD_SEGMENTS],
            packed: BTreeSet::new(),
            fallback: BTreeSet::new(),
            merged_away: Vec::new(),
            changed: false,
        };
        let chunk_ids = vec![overlap_id, fresh_id];
        let chunk_bytes = (overlap_payload.len() + fresh_payload.len()) as u64;
        engine
            .materialize_checkpoint_chunk(&chunk_ids, chunk_bytes, &mut plan)
            .unwrap();
        assert!(plan.changed);
        assert_eq!(plan.merged_away, vec![reference.segment_id]);
        assert_eq!(plan.references[0].block_count as usize, 2);
        assert_eq!(engine.blocks.get(overlap_id).unwrap(), overlap_payload);
        assert_eq!(engine.blocks.get(fresh_id).unwrap(), fresh_payload);
    }

    #[test]
    fn full_catalog_merge_uses_smallest_source_without_global_block_lookup() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let store = engine.blocks.payload_segment_store().unwrap();
        let large_bytes = vec![7; 64 * 1024];
        let large_id = BlockId::for_payload(&large_bytes);
        let large = store.create(Uuid::new_v4(), vec![large_id],
            |_| Ok(large_bytes.clone())).unwrap();
        let small_payloads: std::collections::BTreeMap<_, _> = (0..32)
            .map(|i| {
                let bytes = format!("cold-merge-source-{i}").into_bytes();
                (BlockId::for_payload(&bytes), bytes)
            }).collect();
        let small = store.create(Uuid::new_v4(), small_payloads.keys().copied().collect(),
            |id| Ok(small_payloads[&id].clone())).unwrap();
        let reference = |segment: &crate::payload_segment::PayloadSegment| {
            PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(),
                segment.last_block_id()).unwrap()
        };
        let mut plan = CheckpointPackPlan {
            references: vec![reference(&large); MAX_PAYLOAD_SEGMENTS],
            packed: BTreeSet::new(), fallback: BTreeSet::new(),
            merged_away: Vec::new(), changed: false,
        };
        plan.references[123] = reference(&small);
        let fresh = b"new checkpoint payload";
        let fresh_id = engine.blocks.restore_loose(fresh).unwrap();
        // The new payload is acknowledged in the WAL before materialization.
        engine.commit_references(b"merge-wal-owner", &[fresh_id]).unwrap();
        // The source is deliberately cold and not installed in BlockStore's
        // global catalog: its verified plan reference must be sufficient.
        vfs.arm_fault(None).unwrap();
        engine.materialize_checkpoint_chunk(&[fresh_id], fresh.len() as u64, &mut plan).unwrap();
        let opens = vfs.trace().unwrap().iter()
            .filter(|op| **op == Operation::Open).count();
        assert!(opens < 12, "merge reopened {opens} files for 32 known-source blocks");
        assert_eq!(plan.merged_away, vec![small.metadata().segment_id]);
        assert_eq!(plan.references[0], reference(&large));
        let merged = engine.blocks.payload_segment_store().unwrap()
            .open_segment(plan.references[123].segment_id).unwrap();
        assert_eq!(merged.metadata().block_count, 33);
        for (id, bytes) in small_payloads {
            assert_eq!(merged.get(id).unwrap(), Some(bytes));
        }
        assert_eq!(merged.get(fresh_id).unwrap(), Some(fresh.to_vec()));
        // Crash after sealing the merge but before publishing its catalog.
        // The new segment is only an orphan; the acknowledged WAL still owns
        // the fresh payload and recovery must not depend on that orphan.
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 1);
        assert_eq!(reopened.read_block(fresh_id).unwrap(), fresh);
    }

    #[test]
    fn checkpointed_open_does_not_walk_cold_payload_catalog() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let mut references = Vec::new();
        for index in 0..64 {
            let payload = format!("cold catalog entry {index}").into_bytes();
            let id = BlockId::for_payload(&payload);
            let segment = engine.blocks.payload_segment_store().unwrap()
                .create(Uuid::new_v4(), vec![id], |_| Ok(payload.clone())).unwrap();
            references.push(PayloadSegmentReference::new(segment.metadata(),
                segment.first_block_id(), segment.last_block_id()).unwrap());
        }
        let manifest = PayloadManifest::new(references).unwrap();
        let manifest_id = engine.blocks.put(&manifest.encode().unwrap()).unwrap();
        let root = engine.blocks.put(b"verified loose authority root").unwrap();
        engine.commit_references_with_authority_state(b"root", &[], Some(root)).unwrap();
        engine.checkpoint().unwrap();
        engine.state.payload_manifest_block_id = Some(manifest_id);
        engine.state.generation += 1;
        engine.control.lock().unwrap().publish(&engine.state).unwrap();
        drop(engine);
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.read_block(root).unwrap(), b"verified loose authority root");
        let opens = vfs.trace().unwrap().iter().filter(|op| **op == Operation::Open).count();
        assert!(opens < 12, "clean open traversed cold catalog: {opens} opens");
        assert_eq!(reopened.state().durable_sequence, 1);
    }

    #[test]
    fn inline_only_wal_does_not_walk_unrelated_cold_payload_catalog() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let mut references = Vec::new();
        for index in 0..64 {
            let payload = format!("cold catalog entry {index}").into_bytes();
            let id = BlockId::for_payload(&payload);
            let segment = engine.blocks.payload_segment_store().unwrap()
                .create(Uuid::new_v4(), vec![id], |_| Ok(payload.clone())).unwrap();
            references.push(PayloadSegmentReference::new(segment.metadata(),
                segment.first_block_id(), segment.last_block_id()).unwrap());
        }
        let manifest = PayloadManifest::new(references).unwrap();
        let manifest_id = engine.blocks.put(&manifest.encode().unwrap()).unwrap();
        let root = engine.blocks.put(b"verified loose authority root").unwrap();
        engine.commit_references_with_authority_state(b"root", &[], Some(root)).unwrap();
        engine.checkpoint().unwrap();
        engine.state.payload_manifest_block_id = Some(manifest_id);
        engine.state.generation += 1;
        engine.control.lock().unwrap().publish(&engine.state).unwrap();
        engine.commit(b"inline WAL has no physical payload references").unwrap();
        drop(engine);
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.read_block(root).unwrap(), b"verified loose authority root");
        let opens = vfs.trace().unwrap().iter().filter(|op| **op == Operation::Open).count();
        assert!(opens < 12, "clean open traversed cold catalog: {opens} opens");
        assert_eq!(reopened.state().durable_sequence, 2);
        eprintln!("inline_only_wal_reopen cold_segments=64 opens={opens}");
    }

    #[test]
    fn cancelled_recovery_at_every_boundary_preserves_acknowledged_history_and_root() {
        use std::cell::Cell;
        let seed = prepared_simulated_vfs();
        drop(checkpointed_history_with_active_wal(seed.clone()));
        let baseline = Arc::new(seed.fork_durable().unwrap());
        let checks = Cell::new(0);
        drop(Engine::open_with_vfs_until(Path::new("/data"), baseline, &|| {
            checks.set(checks.get() + 1);
            false
        }).unwrap());
        assert!(checks.get() > 12);
        for at in 1..=checks.get() {
            let vfs = Arc::new(seed.fork_durable().unwrap());
            let seen = Cell::new(0);
            let result = Engine::open_with_vfs_until(Path::new("/data"), vfs.clone(), &|| {
                seen.set(seen.get() + 1);
                seen.get() >= at
            });
            assert_eq!(result.err().unwrap().kind(), io::ErrorKind::Interrupted, "boundary {at}");
            vfs.crash().unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 4);
            assert_eq!(reopened.read_block(reopened.state().authority_state_root.flatten().unwrap()).unwrap(),
                b"active-authority-root");
            assert_eq!(reopened.transaction_snapshot().unwrap().iter().map(|tx| tx.sequence).collect::<Vec<_>>(),
                vec![1, 2, 3, 4]);
        }
    }

    #[test]
    fn committed_sequence_survives_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let authority;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            authority = engine.state().authority_uuid;
            assert_eq!(engine.commit(b"transaction-ir-placeholder").unwrap(), 1);
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.state().authority_uuid, authority);
    }

    #[test]
    fn checkpoint_materialize_batch_setter_validates_bounds() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        assert_eq!(
            engine.set_checkpoint_materialize_batch_bytes(0).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            engine
                .set_checkpoint_materialize_batch_bytes(
                    crate::payload_segment::MAX_SEGMENT_PAYLOAD_BYTES / 2 + 1
                )
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        engine
            .set_checkpoint_materialize_batch_bytes(32 * 1024 * 1024)
            .unwrap();
        // The accepted value drives the per-step budget directly.
        assert_eq!(engine.checkpoint_materialize_batch_bytes, 32 * 1024 * 1024);
    }

    fn history_staging_seed(vfs: Arc<DeterministicVfs>) -> Engine {
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        for index in 0..2_u8 {
            let payload = vec![index; 2 * 1024 * 1024];
            engine.commit_transaction(&[index], &[&payload]).unwrap();
        }
        vfs.arm_fault(None).unwrap();
        engine
    }

    #[test]
    fn incremental_history_preparation_has_one_buffer_and_crashes_back_to_the_wal() {
        let vfs = prepared_simulated_vfs();
        let mut engine = incremental_history_preparation_seed(vfs.clone());
        let expected = engine.transaction_snapshot().unwrap();
        let registration = engine.blocks.register_background_packer().unwrap();
        let before = engine.block_write_metrics();
        for _ in 0..2 {
            engine.stage_full_history_segment().unwrap();
        }
        assert!(engine.history_preparation.is_some());
        assert!(engine.staged_history.is_empty());
        assert_eq!(
            engine.block_write_metrics().bytes_staged,
            before.bytes_staged
        );
        assert!(vfs.trace().unwrap().is_empty());
        drop(registration);
        drop(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert!(reopened.history_preparation.is_none());
        assert_eq!(reopened.transaction_snapshot().unwrap(), expected);

        let registration = reopened.blocks.register_background_packer().unwrap();
        for _ in 0..32 {
            reopened.stage_full_history_segment().unwrap();
            if !reopened.staged_history.is_empty() {
                break;
            }
        }
        assert_eq!(reopened.staged_history.len(), 1);
        assert!(reopened.history_preparation.is_none());
        let before = reopened.block_write_metrics().bytes_staged;
        for _ in 0..32 {
            reopened.stage_full_history_segment().unwrap();
            assert!(reopened.history_preparation.is_none());
        }
        assert_eq!(reopened.block_write_metrics().bytes_staged, before);
        assert!(before <= crate::generated_tofudb_ir::MAX_HISTORY_PREPARATION_BYTES as u64);
        drop(registration);
        reopened.checkpoint().unwrap();
        drop(reopened);
        vfs.crash().unwrap();
        assert_eq!(
            Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap()
                .transaction_snapshot()
                .unwrap(),
            expected
        );
    }

    fn incremental_history_preparation_seed(vfs: Arc<DeterministicVfs>) -> Engine {
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        for index in 0..68_u8 {
            engine.commit(&vec![index; 64 * 1024]).unwrap();
        }
        let registration = engine.blocks.register_background_packer().unwrap();
        engine.stage_full_history_segment().unwrap();
        assert!(engine.history_preparation.is_some());
        drop(registration);
        vfs.arm_fault(None).unwrap();
        engine
    }

    #[test]
    fn incremental_history_soft_rotation_defers_encoding_and_worker_handoff() {
        for grouped in [false, true] {
            let vfs = prepared_simulated_vfs();
            let mut engine = incremental_history_preparation_seed(vfs.clone());
            let registration = engine.blocks.register_background_packer().unwrap();
            engine.rotation_threshold_bytes = 500;
            let generation = engine.state.active_log_generation;
            for _ in 0..32 {
                if grouped {
                    engine.commit_batch(&batch(2)).unwrap();
                } else {
                    engine.commit(b"while encoding").unwrap();
                }
                assert_eq!(engine.state.active_log_generation, generation);
                assert_eq!(engine.state.checkpoint_sequence, 0);
                if !engine.staged_history.is_empty() {
                    break;
                }
            }
            assert_eq!(engine.staged_history.len(), 1);
            assert!(engine.history_preparation.is_none());
            let pending_history = engine.staged_history[0].block_id;
            engine.commit(b"while awaiting worker").unwrap();
            assert_eq!(engine.state.active_log_generation, generation);
            assert!(engine
                .blocks
                .pending_payload(pending_history)
                .unwrap()
                .is_some());
            drop(registration);
            engine.commit(b"synchronous worker fallback").unwrap();
            assert_eq!(engine.state.active_log_generation, generation + 1);
            assert!(engine.history_preparation.is_none());
            let expected = engine.transaction_snapshot().unwrap();
            drop(engine);
            vfs.crash().unwrap();
            assert_eq!(
                Engine::open_with_vfs(Path::new("/data"), vfs)
                    .unwrap()
                    .transaction_snapshot()
                    .unwrap(),
                expected
            );
        }
        // Exit before encoding finishes also resumes on an ordinary commit,
        // even far below the no-worker preparation window.
        let vfs = prepared_simulated_vfs();
        let mut engine = incremental_history_preparation_seed(vfs);
        assert!(engine.history_preparation.is_some());
        engine.commit(b"worker left during encoding").unwrap();
        assert!(engine.history_preparation.is_none());
        assert_eq!(engine.staged_history.len(), 1);
        assert!(engine
            .blocks
            .pending_payload(engine.staged_history[0].block_id)
            .unwrap()
            .is_none());
    }

    #[test]
    fn incremental_history_checkpoint_survives_each_io_error_short_write_and_lost_sync() {
        let vfs = prepared_simulated_vfs();
        let mut engine = incremental_history_preparation_seed(vfs.clone());
        let expected = engine.transaction_snapshot().unwrap();
        engine.checkpoint().unwrap();
        assert!(engine.history_preparation.is_none());
        let trace = vfs.trace().unwrap();
        drop(engine);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied)];
            if matches!(operation, Operation::Write) {
                actions.push(FaultAction::ShortWrite(7));
            }
            if is_sync(operation) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = prepared_simulated_vfs();
                let mut engine = incremental_history_preparation_seed(vfs.clone());
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let _ = engine.checkpoint();
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                assert_eq!(
                    reopened.transaction_snapshot().unwrap(),
                    expected,
                    "checkpoint operation {} {operation:?}",
                    index + 1
                );
                assert_eq!(
                    reopened
                        .commit(b"after partial-history checkpoint recovery")
                        .unwrap(),
                    69
                );
                reopened.checkpoint().unwrap();
                drop(reopened);
                vfs.crash().unwrap();
                assert_eq!(
                    Engine::open_with_vfs(Path::new("/data"), vfs)
                        .unwrap()
                        .transaction_at(69)
                        .unwrap()
                        .unwrap()
                        .envelope
                        .inline_payload,
                    b"after partial-history checkpoint recovery"
                );
            }
        }
    }

    #[test]
    fn background_history_staging_bounds_one_pending_segment_and_recovers_from_wal() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        let registration = engine.blocks.register_background_packer().unwrap();
        let before = engine.block_write_metrics();
        engine.prepare_history_for_rotation(1024).unwrap();
        let after = engine.block_write_metrics();
        assert_eq!(after.blocks_written, before.blocks_written);
        assert_eq!(after.blocks_staged - before.blocks_staged, 1);
        assert!(after.bytes_staged - before.bytes_staged <= HISTORY_SEGMENT_MAX_BYTES as u64 + 128);
        assert!(!vfs
            .trace()
            .unwrap()
            .iter()
            .any(|operation| matches!(operation, Operation::Write) || is_sync(operation)));
        let staged = engine.staged_history[0].block_id;
        assert!(engine.blocks.pending_payload(staged).unwrap().is_some());
        let source_offset = engine.staged_history_transactions;
        engine
            .commit_transaction(b"third", &[&vec![3; 2 * 1024 * 1024]])
            .unwrap();
        // A stopped worker also leaves the pending segment bounded until
        // checkpoint's synchronous fallback can publish it durably.
        drop(registration);
        engine.stage_full_history_segment().unwrap();
        assert_eq!(engine.staged_history.len(), 1);
        assert_eq!(engine.staged_history_transactions, source_offset);
        assert_eq!(engine.state().checkpoint_sequence, 0);
        let expected = engine.transaction_snapshot().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap(), expected);
        assert!(reopened.staged_history.is_empty());
        reopened.checkpoint().unwrap();
        drop(reopened);
        vfs.crash().unwrap();
        assert_eq!(
            Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap()
                .transaction_snapshot()
                .unwrap(),
            expected
        );
    }

    #[test]
    fn background_history_preparation_resumes_only_after_durable_publication() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        let _registration = engine.blocks.register_background_packer().unwrap();
        engine.prepare_history_for_rotation(1024).unwrap();
        let first = engine.staged_history[0].block_id;
        engine
            .commit_transaction(b"third", &[&vec![3; 2 * 1024 * 1024]])
            .unwrap();
        assert_eq!(engine.staged_history.len(), 1);
        engine.materialize_deferred_blocks().unwrap();
        assert!(engine.blocks.pending_payload(first).unwrap().is_none());
        engine.prepare_history_for_rotation(1024).unwrap();
        assert_eq!(engine.staged_history.len(), 2);
        assert_eq!(engine.staged_history_transactions, 2);
        assert_eq!(
            engine.staged_history[1].parent_hash,
            engine.staged_history[0].terminal_hash
        );
        let expected = engine.transaction_snapshot().unwrap();
        engine.checkpoint().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        assert_eq!(
            Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap()
                .transaction_snapshot()
                .unwrap(),
            expected
        );
    }

    #[test]
    fn background_history_checkpoint_materializes_after_worker_exit() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        let registration = engine.blocks.register_background_packer().unwrap();
        engine.stage_full_history_segment().unwrap();
        let staged = engine.staged_history[0].block_id;
        let expected = engine.transaction_snapshot().unwrap();
        drop(registration);
        engine.checkpoint().unwrap();
        assert!(engine.blocks.pending_payload(staged).unwrap().is_none());
        assert!(engine.history_segment_block_ids().contains(&staged));
        drop(engine);
        vfs.crash().unwrap();
        assert_eq!(
            Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap()
                .transaction_snapshot()
                .unwrap(),
            expected
        );
    }

    #[test]
    fn history_staging_is_bounded_and_keeps_wal_authority_until_checkpoint() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        let before = engine.block_write_metrics();
        let control = engine.state().clone();
        let expected = engine.transaction_snapshot().unwrap();
        engine.stage_full_history_segment().unwrap();
        let after = engine.block_write_metrics();
        assert_eq!(after.blocks_written - before.blocks_written, 1);
        assert!(
            after.bytes_written - before.bytes_written <= HISTORY_SEGMENT_MAX_BYTES as u64 + 128
        );
        assert_eq!(engine.state(), &control);
        assert_eq!(engine.transaction_snapshot().unwrap(), expected);
        let staged = engine.staged_history[0].block_id;
        let mut retained = BTreeSet::new();
        engine
            .visit_retained_transaction_blocks(|block| {
                retained.insert(block);
                Ok(())
            })
            .unwrap();
        assert!(
            retained.contains(&staged),
            "GC must retain unpublished segments"
        );
        engine.checkpoint().unwrap();
        assert!(engine.history_segment_block_ids().contains(&staged));
        assert!(engine.staged_history.is_empty());
        assert_eq!(engine.unstaged_history_bytes, 0);
        drop(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap(), expected);
    }

    #[test]
    fn history_staging_restarts_from_wal_after_an_unpublished_crash() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        let expected = engine.transaction_snapshot().unwrap();
        engine.stage_full_history_segment().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap(), expected);
        assert!(reopened.staged_history.is_empty());
        reopened.stage_full_history_segment().unwrap();
        reopened.checkpoint().unwrap();
        drop(reopened);
        vfs.crash().unwrap();
        assert_eq!(
            Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap()
                .transaction_snapshot()
                .unwrap(),
            expected
        );
    }

    #[test]
    fn history_staging_runs_before_single_and_group_commits() {
        for grouped in [false, true] {
            let vfs = prepared_simulated_vfs();
            let mut engine = history_staging_seed(vfs.clone());
            // Short generations do not eagerly duplicate their WAL to history.
            engine.prepare_history_for_rotation(1024).unwrap();
            assert!(engine.staged_history.is_empty());
            engine.rotation_threshold_bytes = 8 * 1024 * 1024;
            if grouped {
                engine.commit_batch(&batch(2)).unwrap();
            } else {
                engine.commit(b"third").unwrap();
            }
            assert_eq!(engine.staged_history.len(), 1);
            assert_eq!(engine.state().checkpoint_sequence, 0);
            let expected = engine.transaction_snapshot().unwrap();
            engine.checkpoint().unwrap();
            drop(engine);
            vfs.crash().unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.transaction_snapshot().unwrap(), expected);
        }
    }

    #[test]
    fn history_staging_starts_earlier_for_large_incoming_commits() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        engine.rotation_threshold_bytes = 16 * 1024 * 1024;
        let before = engine.block_write_metrics();
        // The same 4 MiB prefix should stay solely in WAL for small writes,
        // while 3 MiB arrivals need a head start to drain one segment per call.
        engine.prepare_history_for_rotation(32 * 1024).unwrap();
        assert!(engine.staged_history.is_empty());
        assert_eq!(
            engine.block_write_metrics().blocks_written,
            before.blocks_written
        );
        engine
            .prepare_history_for_rotation(3 * 1024 * 1024)
            .unwrap();
        assert_eq!(engine.staged_history.len(), 1);
        assert_eq!(
            engine.block_write_metrics().blocks_written - before.blocks_written,
            1
        );
        assert_eq!(engine.state().checkpoint_sequence, 0);
        drop(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 2);
    }

    #[test]
    fn history_staging_and_publication_faults_preserve_acknowledged_transactions() {
        fn stage_and_checkpoint(engine: &mut Engine, background: bool) -> io::Result<()> {
            // Foreground catalog bytes also contain segment UUIDs; keep all
            // checkpoint IDs identical across trace probes and fault replays.
            engine.set_checkpoint_segment_ids_for_test((0..64).map(|index|
                uuid::Uuid::from_u128(0x52c0806c_f346_442c_889f_724ab2d4c581 + index)).collect());
            engine.stage_full_history_segment()?;
            if background {
                let store = engine.block_store_shared();
                let (segment_id, reserved) = store
                    .begin_background_pack_with_test_id(1, checkpoint_materialize_batch_bytes(),
                        uuid::Uuid::from_u128(0x42c0806c_f346_442c_889f_724ab2d4c581))?
                    .expect("history and source blocks are staged");
                let segment =
                    store
                        .payload_segment_store()?
                        .create(segment_id, reserved.clone(), |id| store.get(id))?;
                let reference = PayloadSegmentReference::new(
                    segment.metadata(),
                    segment.first_block_id(),
                    segment.last_block_id(),
                )?;
                store.verify_payload_segment(&reference)?;
                store.note_packed(&reserved)?;
                store.complete_background_pack(segment_id, reference)?;
                engine.adopt_background_pack()?;
            }
            engine.checkpoint().map(|_| ())
        }
        let seed_vfs = prepared_simulated_vfs();
        drop(history_staging_seed(seed_vfs.clone()));
        let prepared = || {
            let vfs = Arc::new(seed_vfs.fork_durable().unwrap());
            let engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(None).unwrap();
            (vfs, engine)
        };
        for background in [false, true] {
            let (baseline_vfs, mut baseline) = prepared();
            let registration =
                background.then(|| baseline.blocks.register_background_packer().unwrap());
            stage_and_checkpoint(&mut baseline, background).unwrap();
            let trace = baseline_vfs.trace().unwrap();
            drop(registration);
            drop(baseline);
            for _ in 0..8 {
                let (vfs, mut engine) = prepared();
                let registration = background.then(|| engine.blocks.register_background_packer().unwrap());
                stage_and_checkpoint(&mut engine, background).unwrap();
                assert_eq!(vfs.trace().unwrap(), trace, "fixed-ID checkpoint trace changed");
                drop(registration);
                drop(engine);
            }
            for (index, operation) in trace.iter().enumerate() {
                let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
                if matches!(operation, Operation::Write) {
                    actions.push(FaultAction::ShortWrite(3));
                }
                if is_sync(operation) {
                    actions.push(FaultAction::DropSync);
                }
                for action in actions {
                    let occurrence = trace[..=index].iter().filter(|op| *op == operation).count();
                    // Probe and replay use the same segment ID, hence the same
                    // catalog hash/shard layout. Keep the fired-fault assertion
                    // and all recovery checks; never skip an unexercised target.
                    let (vfs, mut engine) = prepared();
                    let registration = background.then(|| engine.blocks.register_background_packer().unwrap());
                    vfs.arm_operation_fault(*operation, occurrence, action).unwrap();
                    let _ = stage_and_checkpoint(&mut engine, background);
                    assert!(vfs.operation_fault_fired().unwrap(),
                        "unexercised background={background} {operation:?} occurrence={occurrence} action={action:?}");
                    // Recovery is a separate fault-free phase; lost sync effects
                    // remain in this crash image after the fault is disarmed.
                    vfs.arm_fault(None).unwrap();
                    drop(registration);
                    drop(engine);
                    vfs.crash().unwrap();
                    let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs)
                        .unwrap_or_else(|error| {
                            panic!("background={background} fault {index} {operation:?}: {error}")
                        });
                    let snapshot = reopened.transaction_snapshot().unwrap();
                    assert_eq!(snapshot.len(), 2, "fault {index} {operation:?}");
                    for (index, transaction) in snapshot.iter().enumerate() {
                        assert_eq!(transaction.envelope.inline_payload, vec![index as u8]);
                        assert_eq!(
                            reopened
                                .read_block(transaction.envelope.block_ids[0])
                                .unwrap(),
                            vec![index as u8; 2 * 1024 * 1024]
                        );
                    }
                    reopened.commit(b"after-recovery").unwrap();
                    reopened.checkpoint().unwrap();
                }
            }
        }
    }

    #[test]
    fn every_single_lost_initialization_sync_leaves_a_reopenable_store() {
        let baseline_vfs = prepared_simulated_vfs();
        let baseline =
            Engine::initialize_with_vfs(Path::new("/data"), baseline_vfs.clone()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let vfs = prepared_simulated_vfs();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(
                reopened.state().durable_sequence,
                0,
                "lost sync at {operation:?}"
            );
        }
    }

    #[test]
    fn repeated_checkpoints_rotate_wal_and_preserve_lazy_history() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            engine.commit(b"one").unwrap();
            assert_eq!(engine.checkpoint().unwrap(), 1);
            assert_eq!(engine.state().active_log_generation, 2);
            engine.commit(b"two").unwrap();
            assert_eq!(engine.checkpoint().unwrap(), 2);
            assert_eq!(engine.state().active_log_generation, 3);
            engine.commit(b"three").unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 3);
        assert_eq!(engine.state().checkpoint_sequence, 2);
        assert_eq!(engine.committed_transactions().len(), 1);
        let snapshot = engine.transaction_snapshot().unwrap();
        assert_eq!(snapshot.len(), 3);
        assert_eq!(snapshot[0].envelope.inline_payload, b"one");
        assert_eq!(snapshot[1].envelope.inline_payload, b"two");
        assert_eq!(snapshot[2].envelope.inline_payload, b"three");
    }

    #[test]
    fn explicit_history_compaction_publishes_a_bounded_suffix() {
        let vfs = prepared_simulated_vfs();
        let mut engine = checkpointed_authority_history(vfs.clone());
        let metrics = engine.compact_history(1).unwrap();
        assert_eq!(metrics.retained_first_sequence, 3);
        assert_eq!(metrics.retained_segments, 1);
        assert_eq!(metrics.retired_segments, 2);
        assert!(engine.transaction_at(1).unwrap().is_none());
        assert!(engine.transaction_at(2).unwrap().is_none());
        assert_eq!(
            engine
                .transaction_at(3)
                .unwrap()
                .unwrap()
                .envelope
                .inline_payload,
            b"three"
        );
        drop(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.retained_history_first_sequence(), Some(3));
        reopened.commit(b"four").unwrap();
        reopened.checkpoint().unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        assert_eq!(reopened.transaction_at(4).unwrap().unwrap().sequence, 4);
    }

    #[test]
    fn history_compaction_preserves_active_wal_and_latest_authority_root() {
        let vfs = prepared_simulated_vfs();
        let mut engine = checkpointed_authority_history(vfs.clone());
        let latest_root = engine.write_block(b"active-authority-root").unwrap();
        engine
            .commit_references_with_authority_state(b"four", &[latest_root], Some(latest_root))
            .unwrap();
        let wal_path = engine.wal.path().to_owned();
        let wal_bytes = vfs.durable_file(&wal_path).unwrap().unwrap();
        let active_generation = engine.state.active_log_generation;
        let checkpoint_sequence = engine.state.checkpoint_sequence;
        let metrics = engine.compact_history(1).unwrap();
        assert_eq!(metrics.retired_segments, 2);
        assert_eq!(metrics.retained_first_sequence, 3);
        assert_eq!(engine.state.active_log_generation, active_generation);
        assert_eq!(engine.state.checkpoint_sequence, checkpoint_sequence);
        assert_eq!(engine.state.authority_state_root, Some(Some(latest_root)));
        assert_eq!(engine.committed_transactions().len(), 1);
        assert_eq!(vfs.durable_file(&wal_path).unwrap().unwrap(), wal_bytes);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            reopened.state().authority_state_root,
            Some(Some(latest_root))
        );
        assert_eq!(reopened.state().durable_sequence, 4);
        assert_eq!(reopened.retained_history_first_sequence(), Some(3));
        assert_eq!(
            reopened
                .transaction_at(4)
                .unwrap()
                .unwrap()
                .envelope
                .inline_payload,
            b"four"
        );
        reopened.commit(b"five").unwrap();
        reopened.checkpoint().unwrap();
        assert_eq!(reopened.transaction_at(5).unwrap().unwrap().sequence, 5);
    }

    #[test]
    fn every_history_compaction_fault_recovers_old_or_new_manifest() {
        let baseline_vfs = prepared_simulated_vfs();
        let mut baseline = checkpointed_authority_history(baseline_vfs.clone());
        baseline.compact_history(1).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.iter().any(is_sync));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if operation == &Operation::Write {
                cases.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
            if is_sync(operation) {
                cases.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in cases {
            let vfs = prepared_simulated_vfs();
            let mut engine = checkpointed_authority_history(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.compact_history(1);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 3);
            let first = recovered.retained_history_first_sequence().unwrap();
            assert!(first == 1 || first == 3);
            assert_eq!(recovered.transaction_at(3).unwrap().unwrap().sequence, 3);
        }
    }

    #[test]
    fn history_compaction_preserves_staged_active_history_prefix() {
        let vfs = prepared_simulated_vfs();
        let mut engine = checkpointed_history_with_active_wal(vfs.clone());
        for value in 0..80_u8 {
            engine.commit(&vec![value; 64 * 1024]).unwrap();
        }
        engine.stage_full_history_segment().unwrap();
        assert_eq!(engine.staged_history.len(), 1);
        let staged = engine.staged_history.clone();
        let staged_transactions = engine.staged_history_transactions;
        let unstaged_bytes = engine.unstaged_history_bytes;
        let wal_path = engine.wal.path().to_owned();
        let wal_bytes = vfs.durable_file(&wal_path).unwrap().unwrap();
        engine.compact_history(1).unwrap();
        assert_eq!(engine.staged_history, staged);
        assert_eq!(engine.staged_history_transactions, staged_transactions);
        assert_eq!(engine.unstaged_history_bytes, unstaged_bytes);
        assert_eq!(vfs.durable_file(&wal_path).unwrap().unwrap(), wal_bytes);
        engine.checkpoint().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 84);
        assert_eq!(reopened.retained_history_first_sequence(), Some(3));
        for value in 0..80_u8 {
            assert_eq!(
                reopened
                    .transaction_at(5 + u64::from(value))
                    .unwrap()
                    .unwrap()
                    .envelope
                    .inline_payload,
                vec![value; 64 * 1024]
            );
        }
    }

    #[test]
    fn every_active_wal_history_compaction_fault_preserves_durable_tail() {
        let baseline_vfs = prepared_simulated_vfs();
        let mut baseline = checkpointed_history_with_active_wal(baseline_vfs.clone());
        baseline.compact_history(1).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.iter().any(is_sync));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if operation == &Operation::Write {
                cases.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
            if is_sync(operation) {
                cases.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in cases {
            let vfs = prepared_simulated_vfs();
            let mut engine = checkpointed_history_with_active_wal(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.compact_history(1);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 4);
            assert_eq!(
                recovered.state().authority_state_root,
                Some(Some(BlockId::for_payload(b"active-authority-root")))
            );
            assert_eq!(recovered.committed_transactions().len(), 1);
            assert_eq!(
                recovered
                    .transaction_at(4)
                    .unwrap()
                    .unwrap()
                    .envelope
                    .inline_payload,
                b"four"
            );
            let first = recovered.retained_history_first_sequence().unwrap();
            assert!(first == 1 || first == 3);
            assert_eq!(recovered.transaction_at(3).unwrap().unwrap().sequence, 3);
        }
    }

    #[test]
    fn exact_transaction_lookup_reads_active_and_checkpointed_history() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        for payload in [b"one".as_slice(), b"two"] {
            engine.commit(payload).unwrap();
        }
        engine.checkpoint().unwrap();
        engine.commit(b"three").unwrap();
        for (sequence, payload) in [(1, b"one".as_slice()), (2, b"two"), (3, b"three")] {
            let transaction = engine.transaction_at(sequence).unwrap().unwrap();
            assert_eq!(transaction.sequence, sequence);
            assert_eq!(transaction.envelope.inline_payload, payload);
        }
        assert!(engine.transaction_at(0).unwrap().is_none());
        assert!(engine.transaction_at(4).unwrap().is_none());
    }

    #[test]
    fn commit_rotates_before_the_active_wal_soft_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        engine.rotation_threshold_bytes = 500;
        engine.commit(&[1; 200]).unwrap();
        assert_eq!(engine.state().active_log_generation, 1);
        engine.commit(&[2; 200]).unwrap();
        assert_eq!(engine.state().checkpoint_sequence, 1);
        assert_eq!(engine.state().active_log_generation, 2);
        assert_eq!(engine.transaction_snapshot().unwrap().len(), 2);
    }

    #[test]
    fn failed_control_publication_requires_reopen_before_more_authority_work() {
        let initialized = || {
            let (vfs, engine) = initialized_simulated_engine();
            // Hashes containing per-authority metadata can land in an existing
            // shard in one fixture and a fresh shard in another. Fix directory
            // prerequisites so the targeted CONTROL-sync ordinal is stable.
            for shard in 0..=u8::MAX {
                let path = Path::new("/data/blocks").join(format!("{shard:02x}"));
                match vfs.metadata(&path) {
                    Ok(crate::vfs::FileKind::Directory) => {}
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {
                        vfs.create_dir(&path).unwrap()
                    }
                    result => panic!("unexpected CONTROL fixture shard: {result:?}"),
                }
            }
            vfs.sync_directory(Path::new("/data/blocks")).unwrap();
            vfs.arm_fault(None).unwrap();
            (vfs, engine)
        };
        let (baseline_vfs, mut baseline) = initialized();
        // Mirror the real run's pre-fault state exactly (a pending loose
        // block changes the checkpoint's operation sequence).
        baseline.write_block(b"existing").unwrap();
        baseline.commit(b"first").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let control_sync_operation = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .rposition(|operation| operation == &Operation::SyncAll)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized();
        let existing_block = engine.write_block(b"existing").unwrap();
        engine.commit(b"first").unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: control_sync_operation,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.checkpoint().unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert_eq!(
            vfs.trace().unwrap()[control_sync_operation as usize - 1],
            Operation::SyncAll
        );
        assert!(engine.is_restart_required());

        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(engine.checkpoint().is_err());
        assert!(engine.transaction_snapshot().is_err());
        assert!(engine.read_block(existing_block).is_err());
        assert!(vfs.trace().unwrap().is_empty());

        drop(engine);
        vfs.crash().unwrap();
        let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert!(!recovered.is_restart_required());
        assert!(recovered.state().durable_sequence <= 1);
        recovered.commit(b"after recovery").unwrap();
    }

    #[test]
    fn every_checkpoint_io_error_recovers_the_same_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit(b"checkpoint me").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncAll));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = engine.checkpoint();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            let snapshot = recovered.transaction_snapshot().unwrap();
            assert_eq!(snapshot.len(), 1);
            assert_eq!(snapshot[0].envelope.inline_payload, b"checkpoint me");
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = engine.checkpoint();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            assert_eq!(recovered.transaction_snapshot().unwrap().len(), 1);
        }
    }

    #[test]
    fn every_single_lost_commit_sync_preserves_the_acknowledged_transaction() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let committed = engine
                .commit_transaction(b"turn.append", &[b"large result"])
                .unwrap();
            assert_eq!(committed.sequence, 1, "lost sync at {operation:?}");
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            let transaction = &recovered.committed_transactions()[0];
            assert_eq!(transaction.envelope.inline_payload, b"turn.append");
            assert_eq!(
                recovered
                    .read_block(transaction.envelope.block_ids[0])
                    .unwrap(),
                b"large result"
            );
        }
    }

    /// Correlated loss: a single power failure can lose more than one sync
    /// call. Every pair of sync operations in one commit is dropped in the
    /// same crash window — including pairs that defeat both calls of one
    /// double barrier. The certified outcome is never a torn state: reopen
    /// either recovers a complete prefix (the full commit or the empty
    /// pre-commit state) and keeps accepting new work, or fails closed.
    #[test]
    fn every_correlated_pair_of_lost_commit_syncs_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        let syncs: Vec<u64> = trace
            .iter()
            .enumerate()
            .filter(|(_, operation)| is_sync(operation))
            .map(|(index, _)| index as u64 + 1)
            .collect();
        assert!(syncs.len() >= 4, "trace: {trace:?}");

        let mut recovered_full = 0_usize;
        let mut recovered_empty = 0_usize;
        let mut failed_closed = 0_usize;
        for first in 0..syncs.len() {
            for second in first + 1..syncs.len() {
                let (vfs, mut engine) = initialized_simulated_engine();
                vfs.arm_faults(&[
                    FaultRule {
                        operation_number: syncs[first],
                        action: FaultAction::DropSync,
                    },
                    FaultRule {
                        operation_number: syncs[second],
                        action: FaultAction::DropSync,
                    },
                ])
                .unwrap();
                // DropSync reports success, so the commit itself acknowledges;
                // durability is only decided by what survives the crash.
                let _ = engine.commit_transaction(b"turn.append", &[b"large result"]);
                let _ = engine.checkpoint();
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut recovered = match Engine::open_with_vfs(Path::new("/data"), vfs) {
                    Ok(engine) => engine,
                    Err(_) => {
                        failed_closed += 1;
                        continue;
                    }
                };
                let sequence = recovered.state().durable_sequence;
                let checkpoint_sequence = recovered.state().checkpoint_sequence;
                assert!(
                    sequence <= 1,
                    "pair ({}, {}) recovered an impossible sequence {sequence}",
                    syncs[first],
                    syncs[second]
                );
                assert!(checkpoint_sequence <= sequence);
                // A manifest/block reference that did not survive the crash is
                // refused at read time rather than projected as torn history:
                // fail closed. A length mismatch would be a torn projection
                // and stays a hard assertion.
                let serve_result = (|| -> io::Result<()> {
                    let snapshot = recovered.transaction_snapshot()?;
                    assert_eq!(snapshot.len() as u64, sequence);
                    if sequence == 1 {
                        let transaction = &snapshot[0];
                        assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                        assert_eq!(
                            recovered.read_block(transaction.envelope.block_ids[0])?,
                            b"large result"
                        );
                    }
                    Ok(())
                })();
                if serve_result.is_err() {
                    failed_closed += 1;
                    continue;
                }
                if sequence == 1 {
                    recovered_full += 1;
                } else {
                    recovered_empty += 1;
                    assert_eq!(checkpoint_sequence, 0);
                }
                let next = recovered.commit(b"after recovery").unwrap();
                assert_eq!(next, sequence + 1);
                recovered.checkpoint().unwrap();
            }
        }
        assert!(recovered_full > 0);
        assert!(
            recovered_empty > 0 || failed_closed > 0,
            "double barriers must make some same-barrier pair observable"
        );
    }

    /// Extended rotation: commit+checkpoint cycles chain WAL generations and
    /// history segments. A long unfaulted chain certifies rotation mechanics;
    /// then, for every rotation window, each single lost sync inside that
    /// window must leave a reopenable store at a complete prefix, with the
    /// rotation chain still able to advance afterwards.
    #[test]
    fn extended_rotation_chain_and_window_faults_recover_complete_prefixes() {
        const ROTATIONS: u64 = 12;
        // Unfaulted extended chain: every rotation publishes a new generation
        // and the full history survives reopen.
        let (vfs, mut engine) = initialized_simulated_engine();
        for round in 1..=ROTATIONS {
            engine.commit(format!("round-{round}").as_bytes()).unwrap();
            assert_eq!(engine.checkpoint().unwrap(), round);
            assert_eq!(engine.state().active_log_generation, round + 1);
        }
        drop(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, ROTATIONS);
        assert_eq!(reopened.state().checkpoint_sequence, ROTATIONS);
        assert_eq!(
            reopened.transaction_snapshot().unwrap().len() as u64,
            ROTATIONS
        );

        // Faulted windows: for each rotation, replay the earlier rotations
        // unfaulted, then drop each sync of the final commit+checkpoint in
        // turn. Recovery must land on a complete prefix: the window's commit
        // and checkpoint either both survived or neither partially advanced
        // past the durable sequence.
        for window in 1..=ROTATIONS {
            let (baseline_vfs, mut baseline) = initialized_simulated_engine();
            for round in 1..window {
                baseline
                    .commit(format!("round-{round}").as_bytes())
                    .unwrap();
                baseline.checkpoint().unwrap();
            }
            baseline_vfs.arm_fault(None).unwrap();
            baseline
                .commit(format!("round-{window}").as_bytes())
                .unwrap();
            baseline.checkpoint().unwrap();
            let trace = baseline_vfs.trace().unwrap();

            for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
                let (vfs, mut engine) = initialized_simulated_engine();
                for round in 1..window {
                    engine.commit(format!("round-{round}").as_bytes()).unwrap();
                    engine.checkpoint().unwrap();
                }
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action: FaultAction::DropSync,
                }))
                .unwrap();
                engine.commit(format!("round-{window}").as_bytes()).unwrap();
                let _ = engine.checkpoint();
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                let durable_sequence = recovered.state().durable_sequence;
                let checkpoint_sequence = recovered.state().checkpoint_sequence;
                assert_eq!(
                    durable_sequence, window,
                    "lost sync at {operation:?} must preserve every acknowledged rotation"
                );
                assert!(checkpoint_sequence <= durable_sequence);
                let snapshot = recovered.transaction_snapshot().unwrap();
                assert_eq!(
                    snapshot.len() as u64,
                    window,
                    "snapshot must be a complete prefix (window {window}, {operation:?})"
                );
                for (index, transaction) in snapshot.iter().enumerate() {
                    let sequence = index as u64 + 1;
                    assert_eq!(transaction.sequence, sequence);
                    assert_eq!(
                        transaction.envelope.inline_payload,
                        format!("round-{sequence}").as_bytes()
                    );
                    assert!(transaction.envelope.block_ids.is_empty());
                }
                let next = recovered.commit(b"still rotating").unwrap();
                assert_eq!(next, durable_sequence + 1);
            }
        }
    }

    #[test]
    fn every_single_lost_checkpoint_sync_preserves_the_published_generation() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit(b"checkpoint me").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            assert_eq!(
                engine.checkpoint().unwrap(),
                1,
                "lost sync at {operation:?}"
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().checkpoint_sequence, 1);
            assert_eq!(recovered.state().active_log_generation, 2);
            assert_eq!(
                recovered.transaction_snapshot().unwrap()[0]
                    .envelope
                    .inline_payload,
                b"checkpoint me"
            );
        }
    }

    #[test]
    fn second_process_cannot_share_authority() {
        let directory = tempfile::tempdir().unwrap();
        let _engine = Engine::initialize(directory.path()).unwrap();
        let error = Engine::open(directory.path()).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn deterministic_vfs_enforces_the_engine_authority_lease() {
        let (vfs, _engine) = initialized_simulated_engine();
        let error = Engine::open_with_vfs(Path::new("/data"), vfs)
            .err()
            .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn complete_lost_ack_record_promotes_sequence_and_authority_root() {
        let directory = tempfile::tempdir().unwrap();
        let authority_root = {
            let engine = Engine::initialize(directory.path()).unwrap();
            // The block must survive the drop: write_block only stages into
            // the pending set, so use the durable loose path for this
            // hand-crafted WAL fixture.
            engine.blocks.put_loose(b"authority-root").unwrap()
        };
        {
            let mut wal = ActiveLog::open(directory.path()).unwrap();
            let payload = TransactionEnvelope {
                block_ids: vec![authority_root],
                inline_payload: b"possibly-committed".to_vec(),
                authority_state_update: Some(Some(authority_root)),
            }
            .encode()
            .unwrap();
            wal.append(1, [0; 32], &payload).unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.state().generation, 2);
        assert_eq!(
            engine.state().authority_state_root,
            Some(Some(authority_root))
        );
    }

    #[test]
    fn blocks_are_durable_before_their_commit_is_acknowledged() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            let result = engine
                .commit_transaction(b"turn.append", &[b"tool result"])
                .unwrap();
            block_id = result.block_ids[0];
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.read_block(block_id).unwrap(), b"tool result");
    }

    #[test]
    fn crashes_at_durability_boundaries_never_expose_partial_transaction() {
        for crash_stage in [
            CommitStage::BlockPublished(BlockId([0; 32])),
            CommitStage::WalDurable(1),
            CommitStage::ControlDurable(1),
        ] {
            let directory = tempfile::tempdir().unwrap();
            {
                let mut engine = Engine::initialize(directory.path()).unwrap();
                let error = engine
                    .commit_transaction_with_checkpoint(
                        b"turn.append",
                        &[b"large result"],
                        |stage| {
                            let should_crash = match (crash_stage, stage) {
                                (
                                    CommitStage::BlockPublished(_),
                                    CommitStage::BlockPublished(_),
                                ) => true,
                                (expected, actual) => expected == actual,
                            };
                            if should_crash {
                                Err(io::Error::new(
                                    io::ErrorKind::Interrupted,
                                    "simulated crash",
                                ))
                            } else {
                                Ok(())
                            }
                        },
                    )
                    .unwrap_err();
                assert_eq!(error.kind(), io::ErrorKind::Interrupted);
            }
            let engine = Engine::open(directory.path()).unwrap();
            let expected_sequence = match crash_stage {
                CommitStage::BlockPublished(_) => 0,
                CommitStage::WalDurable(_) | CommitStage::ControlDurable(_) => 1,
            };
            assert_eq!(engine.state().durable_sequence, expected_sequence);
        }
    }

    #[test]
    fn oversized_spill_does_not_flush_unrelated_carried_backlog() {
        for (references, recovered) in [(false, false), (true, false), (false, true), (true, true)] {
            let (vfs, mut engine) = initialized_simulated_engine();
            let mut earlier = Vec::new();
            for index in 0_u32..256 {
                let mut payload = vec![0x51; 4096];
                payload[..4].copy_from_slice(&index.to_le_bytes());
                let id = engine.commit_transaction(b"small", &[&payload]).unwrap().block_ids[0];
                earlier.push((id, payload));
            }
            if recovered {
                drop(engine);
                vfs.crash().unwrap();
                engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            }
            vfs.arm_fault(None).unwrap();
            let large = vec![0xAB; MAX_CARRIED_PAYLOAD_BYTES + 1];
            let root_payload = b"separately referenced authority root";
            let (large_id, root) = if references {
                let id = engine.write_block(&large).unwrap();
                let root = engine.write_block(root_payload).unwrap();
                engine.commit_references_with_authority_state(b"large", &[id], Some(root)).unwrap();
                (id, Some(root))
            } else {
                (engine.commit_transaction(b"large", &[&large]).unwrap().block_ids[0], None)
            };
            let barriers = vfs.trace().unwrap().iter().filter(|operation|
                matches!(operation, Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory)
            ).count();
            // Includes initial shard publication and the independent root's
            // CONTROL update, in addition to the reference/WAL barriers.
            assert!(barriers <= 16, "spill barriers must depend on current references: {barriers}");
            for (id, _) in &earlier {
                assert!(engine.blocks.pending_payload(*id).unwrap().is_some(),
                        "large commit must not materialize unrelated WAL-carried blocks");
            }
            assert!(engine.blocks.pending_payload(large_id).unwrap().is_none());
            drop(engine);
            vfs.crash().unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 257);
            for (id, payload) in earlier { assert_eq!(reopened.read_block(id).unwrap(), payload); }
            assert_eq!(reopened.read_block(large_id).unwrap(), large);
            if let Some(root) = root { assert_eq!(reopened.read_block(root).unwrap(), root_payload); }
        }
    }

    #[test]
    fn durable_transaction_with_missing_block_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            // Deferred blocks below the carried ceiling are rebuilt from the
            // WAL payload on replay, so a missing loose file only fails
            // closed for identifier-only records. Exceed the carried ceiling
            // to force the spill path: blocks fsync up front, the WAL logs a
            // bare reference, and replay has no copy to rebuild from.
            let oversized = vec![0xAB; MAX_CARRIED_PAYLOAD_BYTES + 1];
            block_id = engine
                .commit_transaction(b"turn.append", &[oversized.as_slice()])
                .unwrap()
                .block_ids[0];
        }
        let hexadecimal = block_id.to_hex();
        fs::remove_file(
            directory
                .path()
                .join("blocks")
                .join(&hexadecimal[..2])
                .join(format!("{hexadecimal}.blk")),
        )
        .unwrap();
        let error = Engine::open(directory.path()).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("references a missing block"));
    }

    #[test]
    fn durable_transaction_with_carried_payload_rebuilds_missing_block() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            block_id = engine
                .commit_transaction(b"turn.append", &[b"carried block"])
                .unwrap()
                .block_ids[0];
        }
        // The block was staged but never materialized (no checkpoint ran),
        // so no loose file exists. The WAL envelope carries the payload for
        // deferred blocks, so replay rebuilds it instead of failing closed.
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.read_block(block_id).unwrap(), b"carried block");
    }

    #[test]
    fn unconfirmed_transaction_with_missing_block_is_truncated() {
        let directory = tempfile::tempdir().unwrap();
        {
            let _engine = Engine::initialize(directory.path()).unwrap();
        }
        let missing = BlockId::for_payload(b"never persisted");
        let payload = TransactionEnvelope {
            block_ids: vec![missing],
            inline_payload: b"turn.append".to_vec(),
            authority_state_update: None,
        }
        .encode()
        .unwrap();
        {
            let mut wal = ActiveLog::open(directory.path()).unwrap();
            wal.append(1, [0; 32], &payload).unwrap();
        }
        {
            let engine = Engine::open(directory.path()).unwrap();
            assert_eq!(engine.state().durable_sequence, 0);
        }
        assert_eq!(
            fs::metadata(directory.path().join("active.wal"))
                .unwrap()
                .len(),
            0
        );
    }

    #[test]
    fn every_commit_io_error_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncData));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let commit_result = engine.commit_transaction(b"turn.append", &[b"large result"]);
            assert!(
                commit_result.is_err(),
                "operation_number={operation_number} of {}: commit unexpectedly succeeded",
                trace.len()
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert!(recovered.state().durable_sequence <= 1);
            if recovered.state().durable_sequence == 1 {
                let transaction = &recovered.committed_transactions()[0];
                assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                assert_eq!(transaction.envelope.block_ids.len(), 1);
                assert_eq!(
                    recovered
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"large result"
                );
            }
        }
    }

    #[test]
    fn every_commit_short_write_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            assert!(engine
                .commit_transaction(b"turn.append", &[b"large result"])
                .is_err());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert!(recovered.state().durable_sequence <= 1);
            if recovered.state().durable_sequence == 1 {
                let transaction = &recovered.committed_transactions()[0];
                assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                assert_eq!(transaction.envelope.block_ids.len(), 1);
                assert_eq!(
                    recovered
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"large result"
                );
            }
        }
    }

    #[test]
    fn sixty_four_transactions_share_one_wal_barrier_without_control() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let initial_generation = engine.state().generation;
        let results = engine.commit_batch(&batch(64)).unwrap();
        assert_eq!(results.len(), 64);
        assert_eq!(results[0].sequence, 1);
        assert_eq!(results[63].sequence, 64);
        assert_eq!(engine.state().durable_sequence, 64);
        // CONTROL publication is deferred: the batch is acknowledged by one
        // group WAL fsync, and the on-disk generation only advances when a
        // structural transition (or crash recovery) catches it up.
        assert_eq!(engine.state().generation, initial_generation);
        let trace = vfs.trace().unwrap();
        let count = |kind: Operation| trace.iter().filter(|operation| operation == &&kind).count();
        assert_eq!(count(Operation::SyncAll), 0);
        assert_eq!(count(Operation::SyncData), 2);

        // Clean shutdown performs the deferred catch-up publication: one
        // double-barrier CONTROL sync and no extra WAL barrier.
        drop(engine);
        let trace = vfs.trace().unwrap();
        let count = |kind: Operation| trace.iter().filter(|operation| operation == &&kind).count();
        assert_eq!(count(Operation::SyncAll), 2);
        assert_eq!(count(Operation::SyncData), 2);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state().durable_sequence, 64);
        let snapshot = recovered.transaction_snapshot().unwrap();
        assert_eq!(snapshot.len(), 64);
        assert_eq!(snapshot[0].envelope.inline_payload, b"turn-00");
        assert_eq!(snapshot[63].envelope.inline_payload, b"turn-63");
    }

    #[test]
    fn batch_block_roots_are_durable_and_returned_per_transaction() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let transactions = vec![
            BatchTransaction {
                inline_payload: b"conversation.turn.append".to_vec(),
                block_payloads: vec![b"large-turn".to_vec()],
            },
            BatchTransaction {
                inline_payload: b"task.event.append".to_vec(),
                block_payloads: vec![b"tool-result".to_vec(), b"large-turn".to_vec()],
            },
        ];
        let results = engine.commit_batch(&transactions).unwrap();
        assert_eq!(results[0].block_ids.len(), 1);
        assert_eq!(results[1].block_ids.len(), 2);
        assert_eq!(results[0].block_ids[0], results[1].block_ids[1]);

        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state().durable_sequence, 2);
        assert_eq!(
            recovered.read_block(results[0].block_ids[0]).unwrap(),
            b"large-turn"
        );
        assert_eq!(
            recovered.read_block(results[1].block_ids[0]).unwrap(),
            b"tool-result"
        );
    }

    #[test]
    fn batch_rotates_before_crossing_the_active_wal_soft_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        engine.rotation_threshold_bytes = 500;
        engine.commit(&[1; 200]).unwrap();
        let transactions = vec![
            BatchTransaction {
                inline_payload: vec![2; 200],
                block_payloads: Vec::new(),
            },
            BatchTransaction {
                inline_payload: vec![3; 200],
                block_payloads: Vec::new(),
            },
        ];
        let results = engine.commit_batch(&transactions).unwrap();
        assert_eq!(results[0].sequence, 2);
        assert_eq!(results[1].sequence, 3);
        assert_eq!(engine.state().checkpoint_sequence, 1);
        assert_eq!(engine.state().active_log_generation, 2);
        assert_eq!(engine.transaction_snapshot().unwrap().len(), 3);
    }

    #[test]
    fn batch_admission_rejects_unbounded_work_before_writes() {
        let (vfs, mut engine) = initialized_simulated_engine();
        assert_eq!(
            engine.commit_batch(&[]).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            engine.commit_batch(&batch(65)).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let oversized = (0..64)
            .map(|_| BatchTransaction {
                inline_payload: vec![7; 131_072],
                block_payloads: Vec::new(),
            })
            .collect::<Vec<_>>();
        assert_eq!(
            engine.commit_batch(&oversized).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let oversized_blocks = vec![BatchTransaction {
            inline_payload: Vec::new(),
            block_payloads: vec![
                vec![1; MAX_BLOCK_BYTES],
                vec![2; MAX_BLOCK_BYTES],
                vec![3; 1],
            ],
        }];
        assert_eq!(
            engine.commit_batch(&oversized_blocks).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(!vfs
            .trace()
            .unwrap()
            .iter()
            .any(|operation| matches!(operation, Operation::Write)));
        assert_eq!(engine.state().durable_sequence, 0);
    }

    #[test]
    fn every_single_lost_batch_sync_preserves_the_acknowledged_group() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let results = engine.commit_batch(&batch(64)).unwrap();
            assert_eq!(results.len(), 64, "lost sync at {operation:?}");
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(
                recovered.state().durable_sequence,
                64,
                "lost sync at {operation:?}"
            );
        }
    }

    #[test]
    fn every_batch_write_fault_recovers_only_an_unacknowledged_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(127),
            }))
            .unwrap();
            assert!(engine.commit_batch(&batch(64)).is_err());
            assert!(engine.is_restart_required());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let durable = recovered.state().durable_sequence as usize;
            assert!(durable <= 64);
            for (index, transaction) in recovered.committed_transactions().iter().enumerate() {
                assert_eq!(
                    transaction.envelope.inline_payload,
                    format!("turn-{index:02}").as_bytes()
                );
            }
        }
    }

    #[test]
    fn every_batch_io_error_recovers_only_an_unacknowledged_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let operation_count = baseline_vfs.trace().unwrap().len() as u64;

        for operation_number in 1..=operation_count {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            assert!(engine.commit_batch(&batch(64)).is_err());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let durable = recovered.state().durable_sequence as usize;
            assert!(durable <= 64);
            assert_eq!(recovered.committed_transactions().len(), durable);
            for (index, transaction) in recovered.committed_transactions().iter().enumerate() {
                assert_eq!(
                    transaction.envelope.inline_payload,
                    format!("turn-{index:02}").as_bytes()
                );
            }
        }
    }

    #[test]
    fn failed_wal_append_requires_reopen_before_more_authority_work() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(2)).unwrap();
        let first_write = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (single_baseline_vfs, mut single_baseline) = initialized_simulated_engine();
        single_baseline.commit(b"single").unwrap();
        let single_first_write = single_baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        vfs.arm_fault(Some(FaultRule {
            operation_number: first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.commit_batch(&batch(2)).unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(vfs.trace().unwrap().is_empty());

        let (vfs, mut engine) = initialized_simulated_engine();
        vfs.arm_fault(Some(FaultRule {
            operation_number: single_first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.commit(b"single").unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit_batch(&batch(2)).is_err());
        assert!(vfs.trace().unwrap().is_empty());

        let (reference_baseline_vfs, mut reference_baseline) = initialized_simulated_engine();
        let reference_block = reference_baseline.write_block(b"reference").unwrap();
        reference_baseline_vfs.arm_fault(None).unwrap();
        reference_baseline
            .commit_references(b"reference-commit", &[reference_block])
            .unwrap();
        let reference_first_write = reference_baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        let reference_block = engine.write_block(b"reference").unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: reference_first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine
                .commit_references(b"reference-commit", &[reference_block])
                .unwrap_err()
                .kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(vfs.trace().unwrap().is_empty());
    }

    #[test]
    fn shared_reference_payloads_preserve_wal_bytes_and_release_handles() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let page = vec![7; 8 * 1024];
        let root = vec![9; 8 * 1024];
        let page_id = engine.write_block(&page).unwrap();
        let root_id = engine.write_block(&root).unwrap();
        let page_handle = engine.blocks.pending_payload(page_id).unwrap().unwrap();
        let root_handle = engine.blocks.pending_payload(root_id).unwrap().unwrap();
        let page_owners = Arc::strong_count(&page_handle);
        let root_owners = Arc::strong_count(&root_handle);
        engine
            .commit_references_with_authority_state(b"turn.append", &[page_id], Some(root_id))
            .unwrap();
        assert_eq!(Arc::strong_count(&page_handle), page_owners);
        assert_eq!(Arc::strong_count(&root_handle), root_owners);
        let expected = TransactionEnvelope {
            block_ids: vec![page_id],
            inline_payload: b"turn.append".to_vec(),
            authority_state_update: Some(Some(root_id)),
        }
        .encode_with_carried(&[Some(page.as_slice())], Some(root.as_slice()))
        .unwrap();
        assert_eq!(engine.committed[0].encoded_envelope, expected);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().authority_state_root, Some(Some(root_id)));
        assert_eq!(reopened.read_block(page_id).unwrap(), page);
        assert_eq!(reopened.read_block(root_id).unwrap(), root);
    }

    #[test]
    fn authority_root_reference_is_carried_once_across_create_append_and_reopen() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let mut expected_roots = Vec::new();
        for (operation, byte) in [(b"conversation.create".as_slice(), 7), (b"turn.append", 9)] {
            let root = vec![byte; 8 * 1024];
            let id = engine.write_block(&root).unwrap();
            engine
                .commit_references_with_authority_state(operation, &[id], Some(id))
                .unwrap();
            let committed = engine.committed.last().unwrap();
            let duplicate_encoding = committed
                .envelope
                .encode_with_carried(&[Some(root.as_slice())], Some(root.as_slice()))
                .unwrap();
            assert_eq!(
                duplicate_encoding.len() - committed.encoded_envelope.len(),
                8 * 1024
            );
            let (envelope, carried, authority) =
                TransactionEnvelope::decode_with_carried(&committed.encoded_envelope).unwrap();
            assert_eq!(envelope.block_ids, vec![id]);
            assert_eq!(carried.iter().filter(|slot| slot.is_some()).count(), 1);
            assert_eq!(carried[0].as_ref(), Some(&root));
            assert!(authority.is_none());
            expected_roots.push((id, root));
        }
        // No loose-file durability is assumed: both pages must recover from
        // their sole WAL copies after an abrupt power event.
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.state().durable_sequence, 2);
        assert_eq!(
            reopened.state().authority_state_root,
            Some(Some(expected_roots[1].0))
        );
        for (id, payload) in &expected_roots {
            assert_eq!(&reopened.read_block(*id).unwrap(), payload);
        }
        reopened.checkpoint().unwrap();
        drop(reopened);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 2);
        assert_eq!(
            reopened.state().authority_state_root,
            Some(Some(expected_roots[1].0))
        );
        for (id, payload) in expected_roots {
            assert_eq!(reopened.read_block(id).unwrap(), payload);
        }
    }

    #[test]
    fn owned_reference_payload_preserves_allocation_wal_bytes_and_recovery() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let payload = vec![31; 4096];
        let allocation = payload.as_ptr();
        let id = engine.write_block_fresh_owned(payload).unwrap();
        let handle = engine.blocks.pending_payload(id).unwrap().unwrap();
        assert_eq!(handle.as_ptr(), allocation);
        assert_eq!(handle.capacity(), handle.len());
        engine
            .commit_references_with_authority_state(b"owned-page", &[id], Some(id))
            .unwrap();
        let expected = TransactionEnvelope {
            block_ids: vec![id],
            inline_payload: b"owned-page".to_vec(),
            authority_state_update: Some(Some(id)),
        }
        .encode_with_carried(&[Some(handle.as_slice())], None)
        .unwrap();
        assert_eq!(engine.committed[0].encoded_envelope, expected);
        drop(handle);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().authority_state_root, Some(Some(id)));
        assert_eq!(reopened.read_block(id).unwrap(), vec![31; 4096]);
    }

    #[test]
    fn shared_reference_payloads_survive_every_commit_fault() {
        reference_payloads_survive_every_commit_fault(false);
    }

    #[test]
    fn owned_reference_payloads_survive_every_commit_fault() {
        reference_payloads_survive_every_commit_fault(true);
    }

    fn reference_payloads_survive_every_commit_fault(owned: bool) {
        let stage = |engine: &Engine| {
            let payload = vec![7; 8 * 1024];
            if owned {
                engine.write_block_fresh_owned(payload)
            } else {
                engine.write_block(&payload)
            }
        };
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        let id = stage(&baseline).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline
            .commit_references_with_authority_state(b"create", &[id], Some(id))
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncData));
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            match operation {
                Operation::Write => actions.push(FaultAction::ShortWrite(5)),
                Operation::SyncData => actions.push(FaultAction::DropSync),
                _ => {}
            }
            for action in actions {
                let (vfs, mut engine) = initialized_simulated_engine();
                let id = stage(&engine).unwrap();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = engine
                    .commit_references_with_authority_state(b"create", &[id], Some(id))
                    .is_ok();
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                let sequence = reopened.state().durable_sequence;
                assert!(sequence <= 1);
                if acknowledged {
                    assert_eq!(sequence, 1);
                }
                if sequence == 1 {
                    assert_eq!(reopened.state().authority_state_root, Some(Some(id)));
                    assert_eq!(reopened.read_block(id).unwrap(), vec![7; 8 * 1024]);
                } else {
                    assert_eq!(reopened.state().authority_state_root, Some(None));
                }
            }
        }
    }

    fn engine_with_compaction_victim() -> (Arc<DeterministicVfs>, Engine, BlockId) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let result = engine
            .commit_transaction(b"payload-reference", &[b"large-agent-tool-payload"])
            .unwrap();
        let block_id = result.block_ids[0];
        vfs.arm_fault(None).unwrap();
        (vfs, engine, block_id)
    }

    fn two_payloads_in_one_shard() -> (Vec<u8>, Vec<u8>) {
        let mut first_by_shard = std::collections::BTreeMap::<u8, Vec<u8>>::new();
        for value in 0..10_000_u32 {
            let payload = format!("segment-gc-payload-{value}").into_bytes();
            let shard = BlockId::for_payload(&payload).0[0];
            if let Some(first) = first_by_shard.remove(&shard) {
                return (first, payload);
            }
            first_by_shard.insert(shard, payload);
        }
        panic!("failed to find two fixture payloads in one hash shard");
    }

    fn engine_with_partially_live_segment() -> (
        Arc<DeterministicVfs>,
        Engine,
        Uuid,
        BlockId,
        Vec<u8>,
        BlockId,
    ) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let (live_payload, dead_payload) = two_payloads_in_one_shard();
        let committed = engine
            .commit_transaction(b"live-segment-reference", &[live_payload.as_slice()])
            .unwrap();
        let live_block_id = committed.block_ids[0];
        let dead_block_id = engine.write_block(&dead_payload).unwrap();
        let compacted = engine
            .compact_payload_blocks(&[live_block_id, dead_block_id])
            .unwrap();
        vfs.arm_fault(None).unwrap();
        (
            vfs,
            engine,
            compacted.segment_id,
            live_block_id,
            live_payload,
            dead_block_id,
        )
    }

    #[test]
    fn payload_compaction_stabilizes_catalog_before_reclaim_and_reopens() {
        let (vfs, mut engine, block_id) = engine_with_compaction_victim();
        let metrics = engine.compact_payload_blocks(&[block_id]).unwrap();
        assert_eq!(metrics.blocks_packed, 1);
        assert_eq!(engine.payload_segment_count(), 1);
        assert!(!engine
            .blocks
            .list_block_ids(100)
            .unwrap()
            .contains(&block_id));
        assert_eq!(
            engine.read_block(block_id).unwrap(),
            b"large-agent-tool-payload"
        );
        assert_eq!(
            engine.write_block(b"large-agent-tool-payload").unwrap(),
            block_id
        );
        assert!(!engine
            .blocks
            .list_block_ids(100)
            .unwrap()
            .contains(&block_id));

        let mut control_file = vfs
            .open(
                Path::new("/data/CONTROL"),
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        control_file.write_all_at(4096 + 300, &[0xff]).unwrap();
        control_file.sync_all().unwrap();
        control_file.sync_all().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 1);
        assert_eq!(reopened.state().generation, 3);
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(
            reopened.read_block(block_id).unwrap(),
            b"large-agent-tool-payload"
        );
    }

    /// Compaction of a pending (never materialized) block reclaims only
    /// memory; the fault-injection test below must exercise the loose-file
    /// reclaim path, so this fixture spills an oversized commit to a loose
    /// file and checkpoints to retire the WAL before arming the trace.
    fn engine_with_loose_compaction_victim() -> (Arc<DeterministicVfs>, Engine, BlockId) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let oversized = vec![0xCD; MAX_CARRIED_PAYLOAD_BYTES + 1];
        let result = engine
            .commit_transaction(b"payload-reference", &[oversized.as_slice()])
            .unwrap();
        let block_id = result.block_ids[0];
        engine.checkpoint().unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine, block_id)
    }

    #[test]
    fn every_payload_compaction_fault_preserves_the_referenced_block() {
        let (baseline_vfs, mut baseline, baseline_block) = engine_with_loose_compaction_victim();
        baseline.compact_payload_blocks(&[baseline_block]).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(
            trace
                .iter()
                .filter(|operation| **operation == Operation::Write)
                .count()
                >= 3
        );

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(5))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut engine, block_id) = engine_with_loose_compaction_victim();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.compact_payload_blocks(&[block_id]);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 1);
            assert_eq!(
                reopened.read_block(block_id).unwrap(),
                vec![0xCD; MAX_CARRIED_PAYLOAD_BYTES + 1]
            );
        }
    }

    #[test]
    fn payload_segment_gc_rewrites_partial_and_retires_fully_dead_segments() {
        let (vfs, mut engine, segment_id, live_id, live_payload, dead_id) =
            engine_with_partially_live_segment();
        let rewritten = engine
            .collect_payload_segment(segment_id, &[live_id])
            .unwrap();
        assert_eq!(rewritten.blocks_retained, 1);
        assert_eq!(rewritten.blocks_retired, 1);
        assert!(rewritten.replacement_segment_id.is_some());
        assert_eq!(engine.read_block(live_id).unwrap(), live_payload);
        assert_eq!(
            engine.read_block(dead_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(reopened.read_block(live_id).unwrap(), live_payload);

        let (vfs, mut engine) = initialized_simulated_engine();
        let orphan_id = engine.write_block(b"fully-dead-segment").unwrap();
        let compacted = engine.compact_payload_blocks(&[orphan_id]).unwrap();
        let retired = engine
            .collect_payload_segment(compacted.segment_id, &[])
            .unwrap();
        assert_eq!(retired.blocks_retired, 1);
        assert_eq!(retired.replacement_segment_id, None);
        assert_eq!(engine.payload_segment_count(), 0);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 0);
        assert_eq!(
            reopened.read_block(orphan_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn deferred_retirement_crash_before_sweep_recovers_as_orphan() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let dead_id = engine.write_block(b"parked-crash-segment").unwrap();
        let compacted = engine.compact_payload_blocks(&[dead_id]).unwrap();
        let retired = engine
            .collect_payload_segment(compacted.segment_id, &[])
            .unwrap();
        assert_eq!(retired.replacement_segment_id, None);
        assert_eq!(engine.payload_segment_count(), 0);
        // The retired source is parked, not unlinked: the file is durable.
        let parked_path = Path::new("/data/payload-segments")
            .join(format!("{}.pseg", compacted.segment_id.simple()));
        assert_eq!(vfs.metadata(&parked_path).unwrap(), FileKind::File);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        // The in-memory park does not survive the crash; the unreferenced
        // file becomes an ordinary orphan candidate and the authority still
        // opens and serves (no stale-snapshot reader survives a restart).
        assert_eq!(reopened.payload_segment_count(), 0);
        let plan = reopened.plan_payload_segment_orphan().unwrap();
        let candidate = plan.candidate.expect("parked file is orphan-reclaimable");
        assert_eq!(candidate.segment_id, Some(compacted.segment_id));
        reopened.remove_payload_segment_orphan(&candidate).unwrap();
        assert_eq!(
            reopened.read_block(dead_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn every_payload_segment_gc_fault_preserves_all_live_blocks() {
        let (baseline_vfs, mut baseline, segment_id, live_id, _, _) =
            engine_with_partially_live_segment();
        baseline
            .collect_payload_segment(segment_id, &[live_id])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        // The retired source is parked, not unlinked in the reclaim path;
        // the deferred-retirement sweep performs the removal separately.
        assert!(!trace.contains(&Operation::RemoveFile));
        baseline
            .blocks
            .sweep_deferred_segment_retirements(std::time::Duration::ZERO)
            .unwrap();
        assert!(baseline_vfs.trace().unwrap().contains(&Operation::RemoveFile));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(5))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut engine, segment_id, live_id, live_payload, _) =
                engine_with_partially_live_segment();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.collect_payload_segment(segment_id, &[live_id]);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 1);
            assert_eq!(reopened.read_block(live_id).unwrap(), live_payload);
        }
    }

    /// Stages 1 MiB of distinct payloads across eight carried commits and
    /// returns the (block id, payload) pairs in commit order.
    fn stage_carried_blocks(engine: &mut Engine) -> Vec<(BlockId, Vec<u8>)> {
        let mut expected = Vec::new();
        for commit_index in 0..8_u8 {
            let owned: Vec<Vec<u8>> = (0..4_u8)
                .map(|slot| vec![commit_index * 4 + slot; 32 * 1024])
                .collect();
            let refs: Vec<&[u8]> = owned.iter().map(Vec::as_slice).collect();
            let committed = engine.commit_transaction(b"turn.append", &refs).unwrap();
            expected.extend(committed.block_ids.iter().copied().zip(owned));
        }
        expected
    }

    fn assert_manifest_segments_verify(engine: &Engine) {
        let published = engine.state().payload_manifest_block_id.is_some();
        let manifest = engine.payload_manifest.as_ref();
        assert_eq!(published, manifest.is_some());
        if let Some(manifest) = manifest {
            for reference in &manifest.segments {
                engine.blocks.verify_payload_segment(reference).unwrap();
            }
        }
    }

    #[test]
    fn incremental_materialize_steps_bound_work_and_preserve_blocks() {
        // An unbounded step drains the whole staged set in one call, exactly
        // like the checkpoint path.
        let (check_vfs, mut check) = initialized_simulated_engine();
        stage_carried_blocks(&mut check);
        assert_eq!(check.materialize_staged_blocks_step(u64::MAX).unwrap(), 0);
        drop(check);
        drop(check_vfs);

        // The same backlog drains in bounded steps.
        let (vfs, mut engine) = initialized_simulated_engine();
        let expected = stage_carried_blocks(&mut engine);
        let mut steps = 0_usize;
        let mut remaining = engine.materialize_staged_blocks_step(256 * 1024).unwrap();
        assert_eq!(remaining, (1024 - 256) * 1024);
        while remaining > 0 {
            let next = engine.materialize_staged_blocks_step(256 * 1024).unwrap();
            assert!(next < remaining, "a bounded step made no progress");
            remaining = next;
            steps += 1;
            assert!(steps <= 8, "1 MiB at 256 KiB steps needs four steps");
        }
        assert_eq!(steps, 3, "the budget must split the pack into four steps");
        assert!(engine.payload_segment_count() >= 4);
        assert_manifest_segments_verify(&engine);
        for (id, payload) in &expected {
            assert_eq!(&engine.read_block(*id).unwrap(), payload);
        }
        engine.checkpoint().unwrap();
        assert_eq!(engine.state().durable_sequence, 8);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 8);
        assert_manifest_segments_verify(&reopened);
        for (id, payload) in &expected {
            assert_eq!(&reopened.read_block(*id).unwrap(), payload);
        }
    }

    #[test]
    fn incremental_materialize_step_survives_crash_at_every_operation() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        stage_carried_blocks(&mut baseline);
        baseline_vfs.arm_fault(None).unwrap();
        baseline.materialize_staged_blocks_step(256 * 1024).unwrap();
        let operation_count = baseline_vfs.trace().unwrap().len() as u64;

        for operation_number in 1..=operation_count {
            let (vfs, mut engine) = initialized_simulated_engine();
            let expected = stage_carried_blocks(&mut engine);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = engine.materialize_staged_blocks_step(256 * 1024);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            // Whether the faulted step published or not, every block stays
            // readable and the catalog matches the durable CONTROL root.
            assert_manifest_segments_verify(&recovered);
            for (id, payload) in &expected {
                assert_eq!(
                    &recovered.read_block(*id).unwrap(),
                    payload,
                    "lost block at operation {operation_number}"
                );
            }
            // Draining the remainder and checkpointing after the crash must
            // converge to the same durable state as an uninterrupted run.
            while recovered
                .materialize_staged_blocks_step(256 * 1024)
                .unwrap()
                > 0
            {}
            recovered.checkpoint().unwrap();
            assert_eq!(recovered.state().durable_sequence, 8);
            assert_manifest_segments_verify(&recovered);
            for (id, payload) in &expected {
                let read = recovered
                    .read_block(*id)
                    .unwrap_or_else(|error| panic!("op {operation_number}: {error:?}"));
                assert_eq!(&read, payload, "lost block after recovery");
            }
        }
    }
    #[test]
    fn incremental_materialize_step_makes_progress_with_zero_budget() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let expected = stage_carried_blocks(&mut engine);
        // A zero budget still routes one chunk per call, so a caller that
        // always passes a stale budget cannot starve the backlog.
        let mut remaining = engine.materialize_staged_blocks_step(0).unwrap();
        assert!(remaining < 1024 * 1024);
        let mut calls = 0_usize;
        while remaining > 0 {
            remaining = engine.materialize_staged_blocks_step(0).unwrap();
            calls += 1;
            assert!(calls <= 32);
        }
        engine.checkpoint().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        for (id, payload) in &expected {
            assert_eq!(&reopened.read_block(*id).unwrap(), payload);
        }
    }

    /// Reserves the staged payloads and finishes the pack exactly the way
    /// the background pack worker does, without spawning a thread: engine
    /// tests stay deterministic and keep their fault-injection control.
    fn build_and_complete_background_pack(engine: &Engine, payloads: &[&[u8]]) -> Vec<BlockId> {
        let ids: Vec<BlockId> = payloads
            .iter()
            .map(|payload| engine.write_block(payload).unwrap())
            .collect();
        let store = engine.block_store_shared();
        let (segment_id, reserved) = store
            .begin_background_pack(1, checkpoint_materialize_batch_bytes())
            .unwrap()
            .expect("staged payloads must be reservable");
        assert_eq!(
            reserved.iter().copied().collect::<BTreeSet<_>>(),
            ids.iter().copied().collect::<BTreeSet<_>>()
        );
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(segment_id, reserved.clone(), |id| store.get(id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store.verify_payload_segment(&reference).unwrap();
        store.note_packed(&reserved).unwrap();
        store
            .complete_background_pack(segment_id, reference)
            .unwrap();
        ids
    }

    #[test]
    fn maintenance_publishes_completed_background_pack() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let ids =
            build_and_complete_background_pack(&engine, &[b"background-alpha", b"background-beta"]);
        // Foreground commits deliberately leave the completed pack waiting;
        // the idle-maintenance publication adopts it.
        engine.commit(b"no-adopt").unwrap();
        assert_eq!(engine.payload_segment_count(), 0);
        assert!(engine.publish_completed_pack_if_capacity().unwrap());
        assert_eq!(engine.payload_segment_count(), 1);
        // The staged copies were retired at publication, so the checkpoint
        // drain has nothing left to pack into a duplicate segment.
        engine.checkpoint().unwrap();
        assert_eq!(engine.payload_segment_count(), 1);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(reopened.read_block(ids[0]).unwrap(), b"background-alpha");
        assert_eq!(reopened.read_block(ids[1]).unwrap(), b"background-beta");
    }

    #[test]
    fn foreground_commit_leaves_completed_pack_for_maintenance_adoption() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine
            .commit_transaction(b"acknowledged", &[b"packed-payload"])
            .unwrap();
        build_and_complete_background_pack(&engine, &[b"packed-payload"]);
        assert_eq!(engine.payload_segment_count(), 0);
        // The completed pack is ready, but foreground commits must not spend
        // a catalog publication on it: adoption belongs to idle maintenance,
        // soft-rotation drain, and checkpoint.
        engine.commit(b"foreground-one").unwrap();
        engine.commit(b"foreground-two").unwrap();
        assert_eq!(engine.payload_segment_count(), 0);
        // Idle maintenance adopts and publishes the waiting pack exactly once.
        assert!(engine.publish_completed_pack_if_capacity().unwrap());
        assert_eq!(engine.payload_segment_count(), 1);
        assert!(!engine.publish_completed_pack_if_capacity().unwrap());
        engine.commit(b"after-adoption").unwrap();
        engine.checkpoint().unwrap();
    }

    #[test]
    fn crash_with_unadopted_completed_pack_recovers_staged_payloads() {
        let vfs = prepared_simulated_vfs();
        {
            let mut engine =
                Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            engine
                .commit_transaction(b"acknowledged", &[b"packed-payload"])
                .unwrap();
            build_and_complete_background_pack(&engine, &[b"packed-payload"]);
            engine.commit(b"foreground").unwrap();
            // Foreground commits left the completed pack unadopted; its
            // payloads are still staged and WAL-carried at the crash.
            assert_eq!(engine.payload_segment_count(), 0);
            drop(engine);
            vfs.crash().unwrap();
        }
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let acknowledged = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(
            reopened
                .read_block(acknowledged.envelope.block_ids[0])
                .unwrap(),
            b"packed-payload"
        );
        let foreground = reopened.transaction_at(2).unwrap().unwrap();
        assert_eq!(foreground.envelope.inline_payload, b"foreground");
        reopened.commit(b"after-crash").unwrap();
        reopened.checkpoint().unwrap();
        assert_eq!(
            reopened
                .read_block(acknowledged.envelope.block_ids[0])
                .unwrap(),
            b"packed-payload"
        );
    }

    #[test]
    fn replay_stages_carried_blocks_without_probing_loose_files() {
        let vfs = prepared_simulated_vfs();
        let payloads: Vec<&[u8]> = (0..8)
            .map(|index| match index {
                0 => b"carried-0".as_slice(),
                1 => b"carried-1".as_slice(),
                2 => b"carried-2".as_slice(),
                3 => b"carried-3".as_slice(),
                4 => b"carried-4".as_slice(),
                5 => b"carried-5".as_slice(),
                6 => b"carried-6".as_slice(),
                _ => b"carried-7".as_slice(),
            })
            .collect();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let block_ids = engine
            .commit_transaction(b"acknowledged", &payloads)
            .unwrap()
            .block_ids;
        engine.commit(b"tail").unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let trace_before = vfs.trace().unwrap().len();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let reopen_ops = &vfs.trace().unwrap()[trace_before..];
        let opens = reopen_ops
            .iter()
            .filter(|operation| matches!(operation, Operation::Open))
            .count();
        let reads = reopen_ops
            .iter()
            .filter(|operation| matches!(operation, Operation::Read))
            .count();
        // Carried-first replay touches only LOCK/CONTROL/active.wal: the file
        // operation count of a reopen must never scale with the number of
        // WAL-carried block references (one open+read each on the old path).
        assert!(
            opens <= 4 && reads <= 4,
            "reopen probed loose files: opens={opens} reads={reads}"
        );
        for (payload, block_id) in payloads.iter().zip(block_ids.iter()) {
            assert_eq!(&reopened.read_block(*block_id).unwrap(), payload);
        }
    }

    #[test]
    fn normal_mode_unflushed_tail_is_lost_cleanly_and_synced_prefix_survives() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        // Strict stays the implicit default; Normal is always explicit.
        assert_eq!(engine.wal_durability(), WalDurability::Strict);
        engine.set_wal_durability(WalDurability::Normal).unwrap();
        engine.commit(b"synced-one").unwrap();
        assert!(engine.wal_unsynced_bytes() > 0);
        engine.sync_wal_pending().unwrap();
        assert_eq!(engine.wal_unsynced_bytes(), 0);
        engine.commit(b"unsynced-two").unwrap();
        assert!(engine.wal_unsynced_bytes() > 0);
        // A real power event skips Drop: only the synced prefix survives.
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let first = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(first.envelope.inline_payload, b"synced-one");
        assert!(reopened.transaction_at(2).unwrap().is_none());
        reopened.commit(b"after-loss").unwrap();
        reopened.checkpoint().unwrap();
    }

    #[test]
    fn normal_mode_checkpoint_forces_the_deferred_barrier() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.set_wal_durability(WalDurability::Normal).unwrap();
        engine.commit(b"checkpoint-me").unwrap();
        assert!(engine.wal_unsynced_bytes() > 0);
        engine.checkpoint().unwrap();
        assert_eq!(engine.wal_unsynced_bytes(), 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let record = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(record.envelope.inline_payload, b"checkpoint-me");
        reopened.commit(b"after-crash").unwrap();
        reopened.checkpoint().unwrap();
    }

    #[test]
    fn normal_mode_catalog_publication_syncs_the_wal_first() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.set_wal_durability(WalDurability::Normal).unwrap();
        engine
            .commit_transaction(b"acknowledged", &[b"packed-payload"])
            .unwrap();
        build_and_complete_background_pack(&engine, &[b"packed-payload"]);
        engine.commit(b"tail").unwrap();
        assert!(engine.wal_unsynced_bytes() > 0);
        // The maintenance adoption publishes the catalog through CONTROL, so
        // it must perform the deferred WAL barrier first.
        assert!(engine.publish_completed_pack_if_capacity().unwrap());
        assert_eq!(engine.wal_unsynced_bytes(), 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        let record = reopened.transaction_at(2).unwrap().unwrap();
        assert_eq!(record.envelope.inline_payload, b"tail");
    }

    #[test]
    fn strict_mode_attached_barrier_pipelines_and_stays_durable() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(engine.wal_durability(), WalDurability::Strict);
        let (barrier, driver) = WalBarrierDriver::start(
            vfs.clone(),
            engine.wal.path().to_path_buf(),
            std::time::Duration::from_millis(1),
        )
        .unwrap();
        engine.attach_wal_barrier(&barrier).unwrap();
        engine.commit(b"pipelined-strict").unwrap();
        // The append posted its witness instead of fsyncing inline.
        assert!(engine.wal_unsynced_bytes() > 0);
        barrier
            .wait_synced(
                engine.wal_len(),
                std::time::Instant::now() + std::time::Duration::from_secs(30),
            )
            .unwrap();
        assert_eq!(engine.wal_unsynced_bytes(), 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let record = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(record.envelope.inline_payload, b"pipelined-strict");
        reopened.checkpoint().unwrap();
        driver.stop_and_join().unwrap();
    }

    #[test]
    fn attached_barrier_covers_pre_attachment_witness() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.commit(b"pre-attachment").unwrap();
        let witness = engine.wal_len();
        assert!(witness > 0);
        let (barrier, driver) = WalBarrierDriver::start(
            vfs.clone(),
            engine.wal.path().to_path_buf(),
            std::time::Duration::from_millis(1),
        )
        .unwrap();
        engine.attach_wal_barrier(&barrier).unwrap();
        // No new append after the attachment: a barrier wait on a witness
        // that predates the worker must still complete instead of parking
        // until its deadline (the boot-time health preflight regression).
        barrier
            .wait_synced(
                witness,
                std::time::Instant::now() + std::time::Duration::from_secs(30),
            )
            .unwrap();
        assert_eq!(engine.wal_unsynced_bytes(), 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let record = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(record.envelope.inline_payload, b"pre-attachment");
        driver.stop_and_join().unwrap();
    }

    #[test]
    fn strict_commit_without_a_covering_barrier_is_lost_cleanly() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        // A barrier handle with no driver: witnesses are posted but nothing
        // ever syncs, so the tail is exactly the lost-window case.
        let barrier = WalBarrier::default();
        engine.attach_wal_barrier(&barrier).unwrap();
        engine.commit(b"uncovered-strict").unwrap();
        assert!(engine.wal_unsynced_bytes() > 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert!(reopened.transaction_at(1).unwrap().is_none());
        assert!(!reopened.is_restart_required());
    }

    #[test]
    fn strict_checkpoint_with_attached_barrier_covers_everything() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let (barrier, driver) = WalBarrierDriver::start(
            vfs.clone(),
            engine.wal.path().to_path_buf(),
            std::time::Duration::from_millis(1),
        )
        .unwrap();
        engine.attach_wal_barrier(&barrier).unwrap();
        engine.commit(b"checkpoint-covered").unwrap();
        engine.checkpoint().unwrap();
        assert_eq!(engine.wal_unsynced_bytes(), 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let record = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(record.envelope.inline_payload, b"checkpoint-covered");
        driver.stop_and_join().unwrap();
    }

    #[test]
    fn wal_flusher_syncs_pending_in_the_background() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.set_wal_durability(WalDurability::Normal).unwrap();
        engine
            .start_wal_flusher(std::time::Duration::from_millis(1))
            .unwrap();
        engine.commit(b"flushed-by-worker").unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        while engine.wal_unsynced_bytes() > 0 {
            assert!(
                std::time::Instant::now() < deadline,
                "background flusher did not drain the pending barrier"
            );
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        engine.stop_wal_flusher().unwrap();
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let record = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(record.envelope.inline_payload, b"flushed-by-worker");
    }

    #[test]
    fn prepared_adoption_publishes_unlocked_and_preserves_window_commits() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine
            .commit_transaction(b"acknowledged", &[b"packed-payload"])
            .unwrap();
        build_and_complete_background_pack(&engine, &[b"packed-payload"]);
        let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
            PackAdoptionPreparation::Ready(prepared) => prepared,
            _ => panic!("a completed pack must prepare"),
        };
        assert_eq!(engine.payload_segment_count(), 0);
        // Foreground commits proceed through the whole unlocked window: the
        // prepared state owns Arc clones and never borrows the engine.
        engine.commit(b"window-one").unwrap();
        publish_prepared_pack_adoption(&mut prepared).unwrap();
        engine.commit(b"window-two").unwrap();
        assert!(engine.finish_pack_adoption(prepared, Ok(())).unwrap());
        assert_eq!(engine.payload_segment_count(), 1);
        // Window commits are never rolled back; CONTROL is simply dirty
        // again until the next publication catches up.
        let tail = engine.transaction_at(3).unwrap().unwrap();
        assert_eq!(tail.envelope.inline_payload, b"window-two");
        assert!(engine.control_dirty);
        engine.checkpoint().unwrap();
        assert!(!engine.control_dirty);
        let first = engine.transaction_at(1).unwrap().unwrap();
        assert_eq!(
            engine.read_block(first.envelope.block_ids[0]).unwrap(),
            b"packed-payload"
        );
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        let tail = reopened.transaction_at(3).unwrap().unwrap();
        assert_eq!(tail.envelope.inline_payload, b"window-two");
    }

    #[test]
    fn reopen_reconciles_stale_authority_root_with_verified_active_wal() {
        for clear_root in [false, true] {
            let vfs = prepared_simulated_vfs();
            let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let initial = engine.write_block(b"previous-authority-root").unwrap();
            engine.commit_references_with_authority_state(
                b"initial", &[initial], Some(initial),
            ).unwrap();
            let latest = engine.write_block(b"latest-authority-root").unwrap();
            let expected = if clear_root { None } else { Some(latest) };
            engine.commit_references_with_authority_state(
                b"acknowledged", &[latest], expected,
            ).unwrap();
            // Reproduce metadata left by the old async catalog merge: WAL
            // sequence/hash are current, but CONTROL carries an older root.
            engine.state.authority_state_root = Some(Some(initial));
            engine.flush_dirty_control().unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            assert_eq!(reopened.state().durable_sequence, 2);
            assert_eq!(reopened.state().authority_state_root, Some(expected));
            drop(reopened);
            vfs.crash().unwrap();
            let second = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(second.state().authority_state_root, Some(expected));
        }
    }

    #[test]
    fn prepared_adoption_preserves_latest_authority_root_across_restart() {
        for clear_root in [false, true] {
            for shutdown in ["clean", "checkpoint", "crash"] {
                let vfs = prepared_simulated_vfs();
                let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                let initial = engine.write_block(b"initial-authority-root").unwrap();
                engine.commit_references_with_authority_state(
                    b"initial", &[initial], Some(initial),
                ).unwrap();
                build_and_complete_background_pack(&engine, &[b"initial-authority-root"]);
                let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
                    PackAdoptionPreparation::Ready(prepared) => prepared,
                    _ => panic!("completed pack must prepare"),
                };
                let next = engine.write_block(b"new-authority-root").unwrap();
                engine.commit_references_with_authority_state(
                    b"during-publication", &[next], Some(next),
                ).unwrap();
                publish_prepared_pack_adoption(&mut prepared).unwrap();
                let expected = if clear_root { None } else { Some(next) };
                engine.commit_references_with_authority_state(
                    b"after-publication", &[], expected,
                ).unwrap();
                engine.finish_pack_adoption(prepared, Ok(())).unwrap();
                assert_eq!(engine.state().authority_state_root, Some(expected),
                    "pack adoption rolled back an acknowledged authority root");
                if shutdown == "checkpoint" { engine.checkpoint().unwrap(); }
                if shutdown == "crash" { std::mem::forget(engine); } else { drop(engine); }
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                assert_eq!(reopened.state().durable_sequence, 3);
                assert_eq!(reopened.state().authority_state_root, Some(expected),
                    "authority root differs after {shutdown} (clear={clear_root})");
            }
        }
    }

    #[test]
    fn synchronous_publish_defers_while_prepared_adoption_is_in_flight() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine
            .commit_transaction(b"acknowledged", &[b"packed-payload"])
            .unwrap();
        build_and_complete_background_pack(&engine, &[b"packed-payload"]);
        let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
            PackAdoptionPreparation::Ready(prepared) => prepared,
            _ => panic!("a completed pack must prepare"),
        };
        // A synchronous CONTROL publisher defers with WouldBlock and never
        // latches restart-required; a second prepare reports Busy.
        let error = engine.checkpoint().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert!(!engine.is_restart_required());
        assert!(matches!(
            engine.try_prepare_pack_adoption().unwrap(),
            PackAdoptionPreparation::Busy
        ));
        publish_prepared_pack_adoption(&mut prepared).unwrap();
        assert!(engine.finish_pack_adoption(prepared, Ok(())).is_ok());
        engine.checkpoint().unwrap();
    }

    #[test]
    fn failed_prepared_publication_latches_restart_required_and_recovers() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine
            .commit_transaction(b"acknowledged", &[b"packed-payload"])
            .unwrap();
        build_and_complete_background_pack(&engine, &[b"packed-payload"]);
        let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
            PackAdoptionPreparation::Ready(prepared) => prepared,
            _ => panic!("a completed pack must prepare"),
        };
        // Arming resets the simulator's operation counter: fail the very
        // first file operation of the unlocked publish phase.
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::Other),
        }))
        .unwrap();
        let outcome = publish_prepared_pack_adoption(&mut prepared);
        assert!(outcome.is_err());
        assert!(engine.finish_pack_adoption(prepared, outcome).is_err());
        assert!(engine.is_restart_required());
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        // The manifest publication never completed, so the staged payload
        // was never retired: it remains authoritative and readable.
        assert_eq!(reopened.payload_segment_count(), 0);
        let first = reopened.transaction_at(1).unwrap().unwrap();
        assert_eq!(
            reopened.read_block(first.envelope.block_ids[0]).unwrap(),
            b"packed-payload"
        );
    }

    #[test]
    fn async_rotation_covers_prefix_and_window_and_recovers_everything() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        for index in 0..5 {
            engine
                .commit(format!("covered-{index}").as_bytes())
                .unwrap();
        }
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected ready rotation, got {other:?}"),
        };
        // Window commits flow into the fresh buffer while phase 2 encodes
        // the covered prefix without borrowing the engine.
        for index in 0..3 {
            engine
                .commit(format!("window-{index}").as_bytes())
                .unwrap();
        }
        let outcome = encode_rotation_segments(&snapshot).unwrap();
        engine
            .finish_rotation_checkpoint(snapshot, Ok(outcome))
            .unwrap();
        for sequence in 1..=8 {
            assert!(engine.transaction_at(sequence).unwrap().is_some());
        }
        assert_eq!(engine.state().durable_sequence, 8);
        assert_eq!(engine.state().checkpoint_sequence, 5);
        assert!(!engine.control_dirty);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().checkpoint_sequence, 5);
        for sequence in 1..=8 {
            assert!(reopened.transaction_at(sequence).unwrap().is_some());
        }
        let tail = reopened.transaction_at(8).unwrap().unwrap();
        assert_eq!(tail.envelope.inline_payload, b"window-2");
    }

    #[test]
    fn crossing_half_the_rotation_window_requests_the_early_drain() {
        // The early drain must fire strictly between the halves: below the
        // midpoint the packer keeps its quiet-gate; past it the drain is
        // requested so rotation never meets a full quiet-gate backlog.
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        engine.attach_rotation_driver();
        engine.set_rotation_threshold_bytes_for_test(64 * 1024);
        assert!(!engine.blocks.background_drain_requested());
        let payload = vec![0x5Au8; 8 * 1024];
        for _ in 0..3 {
            engine.commit(&payload).unwrap();
        }
        assert!(!engine.blocks.background_drain_requested());
        for _ in 0..3 {
            engine.commit(&payload).unwrap();
        }
        assert!(engine.blocks.background_drain_requested());
    }

    #[test]
    fn prestaged_next_wal_is_reused_by_the_install_and_survives_reopen() {
        // The rotation driver pre-creates the next-generation WAL file off
        // the authority mutex; the install must reuse that exact empty file
        // through its AlreadyExists arm, idempotently.
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        for index in 0..3 {
            engine
                .commit(format!("covered-{index}").as_bytes())
                .unwrap();
        }
        let generation_before = engine.state().active_log_generation;
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected ready rotation, got {other:?}"),
        };
        prestage_rotation_wal(&snapshot).unwrap();
        prestage_rotation_wal(&snapshot).unwrap();
        let outcome = encode_rotation_segments(&snapshot).unwrap();
        engine
            .finish_rotation_checkpoint(snapshot, Ok(outcome))
            .unwrap();
        assert_eq!(
            engine.state().active_log_generation,
            generation_before + 1
        );
        assert_eq!(engine.state().checkpoint_sequence, 3);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().checkpoint_sequence, 3);
        assert_eq!(
            reopened.state().active_log_generation,
            generation_before + 1
        );
        assert!(reopened.transaction_at(3).unwrap().is_some());
    }

    #[test]
    fn rotation_with_staged_backlog_and_window_staging_completes_without_defer() {
        // 2026-09-20 swarm-p99 root fix, the generational-pending half:
        // prepare rotates the staged set into the covered generation (the
        // fence and install defer key off it alone); window-commit staging
        // lands in the live generation and must not extend the install.
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        // Reserve the real pack lane before rotation, but do not race its
        // empty WAL-retirement mutex acquisition with this Ready assertion.
        // The production try-lock Busy response remains correct under a worker.
        let registration = engine.block_store_shared().register_background_packer().unwrap();
        let covered = engine
            .commit_transaction(b"covered", &[b"backlog-payload"])
            .unwrap();
        // A staged backlog at prepare is rotated, never a Busy gate.
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected ready rotation with staged backlog, got {other:?}"),
        };
        assert!(engine.has_rotation_drain_pending_for_test());
        drop(registration);
        let packer = engine.start_background_packer().unwrap();
        // The background packer drains the covered generation; publication
        // is driven through the engine's explicit adoption sequence (the
        // deterministic counterpart of the daemon's adoption driver).
        // Bound the wait so a stuck drain fails loudly.
        let started = std::time::Instant::now();
        while engine.has_rotation_drain_pending_for_test() {
            if let PackAdoptionPreparation::Ready(mut adoption) =
                engine.try_prepare_pack_adoption().unwrap()
            {
                let adoption_outcome =
                    crate::engine::publish_prepared_pack_adoption(&mut adoption);
                engine
                    .finish_pack_adoption(adoption, adoption_outcome)
                    .unwrap();
            }
            assert!(
                started.elapsed() < std::time::Duration::from_secs(30),
                "covered generation did not drain"
            );
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        // Stop the packer before the window commit so the window block can
        // only live in its WAL-carried copy and the live staged set — this
        // isolates the window-coverage invariant from packer timing.
        packer.stop_and_join().unwrap();
        // Window commits stage fresh blocks during the encode; admitted and
        // never counted against the covered drain.
        let window = engine
            .commit_transaction(b"window", &[b"window-payload"])
            .unwrap();
        let outcome = encode_rotation_segments(&snapshot).unwrap();
        engine
            .finish_rotation_checkpoint(snapshot, Ok(outcome))
            .unwrap();
        assert_eq!(
            engine.read_block(covered.block_ids[0]).unwrap(),
            b"backlog-payload",
            "covered block must be readable from the packed store pre-crash"
        );
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            reopened.read_block(covered.block_ids[0]).unwrap(),
            b"backlog-payload"
        );
        assert_eq!(
            reopened.read_block(window.block_ids[0]).unwrap(),
            b"window-payload"
        );
    }

    #[test]
    fn window_commits_admitted_with_packer_running_during_rotation_encode() {
        // 2026-09-20 swarm-p99 root fix: the daemon admission gate
        // (`prepare_history_for_rotation`) rejected every commit with
        // classified backpressure while a rotation encoded, so a rotation
        // landing mid-burst parked all writers for pickup+encode+install.
        // Window commits are absorbed by the install's final segment (see
        // async_rotation_covers_prefix_and_window_and_recovers_everything),
        // so with the packer running and a rotation in flight, commits must
        // be ADMITTED until the hard WAL ceiling — never parked.
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        let packer = engine.start_background_packer().unwrap();
        for index in 0..5 {
            engine
                .commit(format!("covered-{index}").as_bytes())
                .unwrap();
        }
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        let snapshot = loop {
            match engine.try_prepare_rotation_checkpoint().unwrap() {
                RotationCheckpointPreparation::Ready(snapshot) => break snapshot,
                RotationCheckpointPreparation::Busy if std::time::Instant::now() < deadline => {
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                other => panic!("expected ready rotation, got {other:?}"),
            }
        };
        assert!(engine.rotation_in_flight());
        for index in 0..3 {
            engine
                .commit(format!("window-{index}").as_bytes())
                .unwrap_or_else(|error| {
                    panic!("window commit {index} rejected during encode: {error}")
                });
        }
        let outcome = encode_rotation_segments(&snapshot).unwrap();
        engine
            .finish_rotation_checkpoint(snapshot, Ok(outcome))
            .unwrap();
        assert_eq!(engine.state().durable_sequence, 8);
        packer.stop_and_join().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        for sequence in 1..=8 {
            assert!(reopened.transaction_at(sequence).unwrap().is_some());
        }
        let tail = reopened.transaction_at(8).unwrap().unwrap();
        assert_eq!(tail.envelope.inline_payload, b"window-2");
    }

    #[test]
    fn failed_async_rotation_restores_the_covered_prefix() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        engine.commit(b"covered-one").unwrap();
        engine.commit(b"covered-two").unwrap();
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected ready rotation, got {other:?}"),
        };
        engine.commit(b"window-one").unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::Other),
        }))
        .unwrap();
        let outcome = encode_rotation_segments(&snapshot);
        assert!(outcome.is_err());
        assert!(engine.finish_rotation_checkpoint(snapshot, outcome).is_err());
        assert!(engine.is_restart_required());
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        // Nothing was swapped or retired: every committed transaction is
        // still replayed from the active WAL.
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        for sequence in 1..=3 {
            assert!(reopened.transaction_at(sequence).unwrap().is_some());
        }
        reopened.checkpoint().unwrap();
        let tail = reopened.transaction_at(3).unwrap().unwrap();
        assert_eq!(tail.envelope.inline_payload, b"window-one");
    }

    #[test]
    fn synchronous_checkpoint_defers_while_rotation_is_in_flight() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        engine.commit(b"covered").unwrap();
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected ready rotation, got {other:?}"),
        };
        assert!(matches!(
            engine.try_prepare_rotation_checkpoint().unwrap(),
            RotationCheckpointPreparation::Busy
        ));
        let error = engine.checkpoint().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert!(!engine.is_restart_required());
        let outcome = encode_rotation_segments(&snapshot).unwrap();
        engine
            .finish_rotation_checkpoint(snapshot, Ok(outcome))
            .unwrap();
        engine.commit(b"after-rotation").unwrap();
        engine.checkpoint().unwrap();
        let tail = engine.transaction_at(2).unwrap().unwrap();
        assert_eq!(tail.envelope.inline_payload, b"after-rotation");
    }

    #[test]
    fn hard_ceiling_rejects_with_retryable_backpressure_then_rotation_completes() {
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        // Test the actual hard boundary independently of soft-trigger refusal.
        engine.set_rotation_threshold_bytes_for_test(ACTIVE_LOG_MAX_BYTES);
        // Drive the active WAL toward the hard ceiling with unique inline
        // payloads; the first commit that would exceed
        // ACTIVE_LOG_MAX must reject with classified retryable backpressure,
        // carrying no mutation.
        let mut committed = 0_u64;
        let error = loop {
            committed += 1;
            assert!(committed <= 300, "ceiling must trip within 300 commits");
            let mut big = vec![7_u8; 250_000];
            big[..8].copy_from_slice(&committed.to_be_bytes());
            match engine.commit_transaction(&big, &[]) {
                Ok(_) => {}
                Err(error) => break error,
            }
        };
        assert!(crate::engine::is_rotation_backpressure(&error));
        let prepared = match engine
            .take_prepared_rotation()
            .unwrap()
            .expect("hard ceiling must prepare a rotation")
        {
            crate::engine::RotationWork::Fresh(prepared) => prepared,
            crate::engine::RotationWork::Deferred(..) => {
                panic!("a first rotation must arrive fresh")
            }
        };
        let encoded = encode_rotation_segments(&prepared).unwrap();
        engine
            .finish_rotation_checkpoint(prepared, Ok(encoded))
            .unwrap();
        engine.commit(b"over-ceiling").unwrap();
        let tail = engine
            .transaction_at(committed)
            .unwrap()
            .unwrap();
        assert_eq!(tail.envelope.inline_payload, b"over-ceiling");
    }

    #[test]
    fn every_background_pack_publication_fault_retains_wal_payloads() {
        fn seed(vfs: Arc<DeterministicVfs>) -> Engine {
            let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            engine
                .commit_transaction(b"acknowledged", &[b"packed-payload"])
                .unwrap();
            build_and_complete_background_pack(&engine, &[b"packed-payload"]);
            vfs.arm_fault(None).unwrap();
            engine
        }
        let baseline_vfs = prepared_simulated_vfs();
        let mut baseline = seed(baseline_vfs.clone());
        baseline.publish_completed_pack_if_capacity().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if matches!(operation, Operation::Write) {
                actions.push(FaultAction::ShortWrite(3));
            }
            if is_sync(operation) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = prepared_simulated_vfs();
                let mut engine = seed(vfs.clone());
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let _ = engine.publish_completed_pack_if_capacity();
                assert!(
                    engine
                        .blocks
                        .unpublished_background_segment_ids()
                        .unwrap()
                        .is_empty(),
                    "publication guard must release on every return path"
                );
                drop(engine);
                vfs.crash().unwrap();
                let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                assert_eq!(reopened.state().durable_sequence, 1);
                let transaction = reopened.transaction_at(1).unwrap().unwrap();
                assert_eq!(transaction.envelope.inline_payload, b"acknowledged");
                assert_eq!(
                    reopened
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"packed-payload"
                );
                reopened.commit(b"after-publication-fault").unwrap();
                reopened.checkpoint().unwrap();
            }
        }
    }

    #[test]
    fn checkpoint_drain_reclaims_stalled_background_pack() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let id = engine.write_block(b"stalled-background-pack").unwrap();
        let store = engine.block_store_shared();
        assert!(store
            .begin_background_pack(1, checkpoint_materialize_batch_bytes())
            .unwrap()
            .is_some());
        // A dead worker never finishes the create; once the wait budget
        // runs out the drain abandons the reservation and packs the same
        // staged payloads synchronously.
        engine.commit(b"stalled").unwrap();
        engine.set_background_pack_wait_rounds_for_test(0);
        engine.checkpoint().unwrap();
        assert_eq!(engine.payload_segment_count(), 1);
        assert!(!store.background_pack_creating().unwrap());
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(reopened.read_block(id).unwrap(), b"stalled-background-pack");
    }

    #[test]
    fn wal_retirement_is_one_slot_and_failure_blocks_the_next_checkpoint() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let registration = engine.wal_retirement.register_worker().unwrap();
        let id = engine
            .commit_transaction(b"first", &[b"retained-payload"])
            .unwrap()
            .block_ids[0];
        vfs.arm_fault(None).unwrap();
        engine.checkpoint().unwrap();
        assert!(!vfs.trace().unwrap().contains(&Operation::RemoveFile));
        assert_eq!(
            vfs.metadata(Path::new("/data/active.wal")).unwrap(),
            FileKind::File
        );
        engine.commit(b"second").unwrap();
        let generation = engine.state.active_log_generation;
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
        }))
        .unwrap();
        assert_eq!(
            engine.checkpoint().unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(vfs.trace().unwrap(), vec![Operation::RemoveFile]);
        assert_eq!(engine.state.active_log_generation, generation);
        // No new generation may accumulate behind an unsuccessful retirement.
        let wal_count = || {
            vfs.read_directory(Path::new("/data"))
                .unwrap()
                .iter()
                .filter(|path| path.extension().is_some_and(|value| value == "wal"))
                .count()
        };
        assert_eq!(wal_count(), 2);
        vfs.arm_fault(None).unwrap();
        engine.checkpoint().unwrap();
        assert_eq!(wal_count(), 2);
        assert_eq!(engine.state.active_log_generation, generation + 1);
        drop(engine);
        drop(registration);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert!(!vfs.trace().unwrap().contains(&Operation::ReadDirectory));
        assert_eq!(reopened.state.durable_sequence, 2);
        assert_eq!(reopened.read_block(id).unwrap(), b"retained-payload");
        // Reopen reconstructs exactly the last predecessor without scanning.
        assert!(reopened.wal_retirement.reclaim().unwrap());
        assert_eq!(wal_count(), 1);
        reopened.commit(b"third").unwrap();
        reopened.checkpoint().unwrap();
        assert_eq!(wal_count(), 1);
    }

    #[test]
    fn wal_retirement_publication_and_cleanup_survive_every_io_fault() {
        let setup = || {
            let (vfs, mut engine) = initialized_simulated_engine();
            let registration = engine.wal_retirement.register_worker().unwrap();
            let id = engine
                .commit_transaction(b"acknowledged", &[b"retired-wal-payload"])
                .unwrap()
                .block_ids[0];
            (vfs, engine, registration, id)
        };
        for cleanup in [false, true] {
            let (vfs, mut engine, registration, _) = setup();
            if cleanup {
                engine.checkpoint().unwrap();
            }
            vfs.arm_fault(None).unwrap();
            if cleanup {
                engine.wal_retirement.reclaim().unwrap();
            } else {
                engine.checkpoint().unwrap();
            }
            let trace = vfs.trace().unwrap();
            assert_eq!(trace.contains(&Operation::RemoveFile), cleanup);
            drop(engine);
            drop(registration);
            for (index, operation) in trace.iter().enumerate() {
                let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
                match operation {
                    Operation::Write => actions.push(FaultAction::ShortWrite(5)),
                    Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory => {
                        actions.push(FaultAction::DropSync)
                    }
                    _ => {}
                }
                for action in actions {
                    let (vfs, mut engine, registration, id) = setup();
                    if cleanup {
                        engine.checkpoint().unwrap();
                    }
                    vfs.arm_fault(Some(FaultRule {
                        operation_number: index as u64 + 1,
                        action,
                    }))
                    .unwrap();
                    let acknowledged = if cleanup {
                        engine.wal_retirement.reclaim().is_ok()
                    } else {
                        engine.checkpoint().is_ok()
                    };
                    drop(engine);
                    drop(registration);
                    vfs.crash().unwrap();
                    vfs.arm_fault(None).unwrap();
                    let mut reopened =
                        Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                    assert_eq!(reopened.state.durable_sequence, 1);
                    assert_eq!(reopened.read_block(id).unwrap(), b"retired-wal-payload");
                    if cleanup && acknowledged {
                        assert_eq!(
                            vfs.metadata(Path::new("/data/active.wal"))
                                .unwrap_err()
                                .kind(),
                            io::ErrorKind::NotFound
                        );
                    }
                    reopened.commit(b"after-crash").unwrap();
                    reopened.checkpoint().unwrap();
                    assert_eq!(reopened.state.durable_sequence, 2);
                }
            }
        }
    }

    #[test]
    fn soft_rotation_keeps_writing_while_a_background_pack_is_in_flight() {
        for grouped in [false, true] {
            let (vfs, mut engine) = initialized_simulated_engine();
            let payload = vec![0x68; 1024 * 1024];
            let id = engine
                .commit_transaction(b"seed", &[&payload])
                .unwrap()
                .block_ids[0];
            let store = engine.block_store_shared();
            let registration = store.register_background_packer().unwrap();
            let (segment_id, ids) = store
                .begin_background_pack(1, checkpoint_materialize_batch_bytes())
                .unwrap()
                .unwrap();
            engine.rotation_threshold_bytes = 500;
            engine.set_background_pack_wait_rounds_for_test(0);
            let generation = engine.state.active_log_generation;
            vfs.arm_fault(None).unwrap();
            if grouped {
                engine.commit_batch(&batch(2)).unwrap();
            } else {
                engine.commit(b"next").unwrap();
            }
            assert_eq!(engine.state.active_log_generation, generation);
            assert_eq!(engine.state.checkpoint_sequence, 0);
            assert!(store.background_pack_creating().unwrap());
            assert!(!vfs.trace().unwrap().contains(&Operation::RemoveFile));
            let acknowledged = engine.state.durable_sequence;

            let segment = store
                .payload_segment_store()
                .unwrap()
                .create(segment_id, ids.clone(), |id| store.get(id))
                .unwrap();
            let reference = PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )
            .unwrap();
            store.verify_payload_segment(&reference).unwrap();
            store.note_packed(&ids).unwrap();
            store
                .complete_background_pack(segment_id, reference)
                .unwrap();
            engine.commit(b"after-pack").unwrap();
            assert_eq!(engine.state.checkpoint_sequence, acknowledged);
            assert_eq!(engine.state.active_log_generation, generation + 1);
            drop(registration);
            drop(store);
            drop(engine);
            vfs.crash().unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state.durable_sequence, acknowledged + 1);
            assert_eq!(reopened.read_block(id).unwrap(), payload);
        }
    }

    #[test]
    fn soft_and_forced_rotation_with_reserved_payloads_survive_every_io_fault() {
        let setup = || {
            let (vfs, mut engine) = initialized_simulated_engine();
            let id = engine
                .commit_transaction(b"seed", &[b"reserved-payload"])
                .unwrap()
                .block_ids[0];
            let store = engine.block_store_shared();
            let registration = store.register_background_packer().unwrap();
            store
                .begin_background_pack(1, checkpoint_materialize_batch_bytes())
                .unwrap()
                .unwrap();
            engine.rotation_threshold_bytes = 1;
            engine.set_background_pack_wait_rounds_for_test(0);
            vfs.arm_fault(None).unwrap();
            (vfs, engine, store, registration, id)
        };
        for forced in [false, true] {
            for grouped in [false, true] {
                let commit = |engine: &mut Engine| -> io::Result<()> {
                    if forced {
                        // Exercise the hard-cap handoff at a small fault-injection
                        // scale; the real-file test covers actual 64 MiB admission.
                        engine.drain_staged_then_checkpoint(true)?;
                    }
                    if grouped {
                        engine.commit_batch(&batch(2))?;
                    } else {
                        engine.commit_transaction(b"next", &[b"next-payload"])?;
                    }
                    Ok(())
                };
                let (vfs, mut engine, store, registration, _) = setup();
                commit(&mut engine).unwrap();
                let trace = vfs.trace().unwrap();
                assert!(trace.contains(&Operation::Write));
                assert!(trace.contains(&Operation::SyncData));
                assert_eq!(
                    engine.state.active_log_generation,
                    if forced { 2 } else { 1 }
                );
                drop(registration);
                drop(store);
                drop(engine);
                for (index, operation) in trace.iter().enumerate() {
                    let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
                    match operation {
                        Operation::Write => actions.push(FaultAction::ShortWrite(5)),
                        Operation::SyncData | Operation::SyncDirectory => {
                            actions.push(FaultAction::DropSync)
                        }
                        _ => {}
                    }
                    for action in actions {
                        let (vfs, mut engine, store, registration, id) = setup();
                        vfs.arm_fault(Some(FaultRule {
                            operation_number: index as u64 + 1,
                            action,
                        }))
                        .unwrap();
                        let acknowledged = commit(&mut engine).is_ok();
                        drop(registration);
                        drop(store);
                        drop(engine);
                        vfs.crash().unwrap();
                        vfs.arm_fault(None).unwrap();
                        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                        let sequence = reopened.state.durable_sequence;
                        let maximum = if grouped { 3 } else { 2 };
                        assert!((1..=maximum).contains(&sequence));
                        if acknowledged {
                            assert_eq!(sequence, maximum);
                        }
                        assert_eq!(reopened.read_block(id).unwrap(), b"reserved-payload");
                        for number in 2..=sequence {
                            let transaction = reopened.transaction_at(number).unwrap().unwrap();
                            let expected = if grouped {
                                batch(2)[number as usize - 2].clone()
                            } else {
                                BatchTransaction {
                                    inline_payload: b"next".to_vec(),
                                    block_payloads: vec![b"next-payload".to_vec()],
                                }
                            };
                            assert_eq!(
                                transaction.envelope.inline_payload,
                                expected.inline_payload
                            );
                            assert_eq!(
                                transaction.envelope.block_ids.len(),
                                expected.block_payloads.len()
                            );
                            for (id, payload) in transaction
                                .envelope
                                .block_ids
                                .iter()
                                .zip(&expected.block_payloads)
                            {
                                assert_eq!(&reopened.read_block(*id).unwrap(), payload);
                            }
                        }
                        reopened.commit(b"after-fault").unwrap();
                        reopened.checkpoint().unwrap();
                    }
                }
            }
        }
    }

    #[test]
    fn hard_rotation_fully_drains_a_large_backlog_before_single_or_group_append() {
        for grouped in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let mut engine = Engine::initialize(directory.path()).unwrap();
            engine.rotation_threshold_bytes = ACTIVE_LOG_MAX_BYTES;
            // Hold the bounded handoff instead of running a worker, so the
            // real 64 MiB boundary also measures retained WAL disk capacity.
            let retirement_registration = engine.wal_retirement.register_worker().unwrap();
            let mut ids = Vec::new();
            for index in 0..31_u8 {
                let payload = vec![index; 2 * 1024 * 1024];
                ids.push(
                    engine
                        .commit_transaction(b"large-seed", &[&payload])
                        .unwrap()
                        .block_ids[0],
                );
            }
            assert_eq!(engine.state.checkpoint_sequence, 0);
            let before_bytes = fs::metadata(engine.wal.path()).unwrap().len();
            assert!(before_bytes > ACTIVE_LOG_MAX_BYTES - 2 * 1024 * 1024);
            let payload = vec![0x7f; 2 * 1024 * 1024];
            let id = if grouped {
                engine
                    .commit_batch(&[BatchTransaction {
                        inline_payload: b"after-cap".to_vec(),
                        block_payloads: vec![payload.clone()],
                    }])
                    .unwrap()[0]
                    .block_ids[0]
            } else {
                engine
                    .commit_transaction(b"after-cap", &[&payload])
                    .unwrap()
                    .block_ids[0]
            };
            assert_eq!(engine.state.checkpoint_sequence, 31);
            assert_eq!(engine.state.active_log_generation, 2);
            assert!(fs::metadata(engine.wal.path()).unwrap().len() <= ACTIVE_LOG_MAX_BYTES);
            let wal_sizes: Vec<u64> = fs::read_dir(directory.path())
                .unwrap()
                .map(|entry| entry.unwrap().path())
                .filter(|path| path.extension().is_some_and(|value| value == "wal"))
                .map(|path| fs::metadata(path).unwrap().len())
                .collect();
            assert_eq!(wal_sizes.len(), 2);
            assert!(wal_sizes.iter().all(|bytes| *bytes <= ACTIVE_LOG_MAX_BYTES));
            assert!(wal_sizes.iter().sum::<u64>() <= 2 * ACTIVE_LOG_MAX_BYTES);
            assert_eq!(
                fs::metadata(directory.path().join("active.wal"))
                    .unwrap()
                    .len(),
                before_bytes
            );
            drop(retirement_registration);
            drop(engine);
            let reopened = Engine::open(directory.path()).unwrap();
            assert_eq!(reopened.state.durable_sequence, 32);
            for (index, id) in ids.into_iter().enumerate() {
                assert_eq!(
                    reopened.read_block(id).unwrap(),
                    vec![index as u8; 2 * 1024 * 1024]
                );
            }
            assert_eq!(reopened.read_block(id).unwrap(), payload);
        }
    }

    #[test]
    fn idle_pack_publication_defers_full_catalog_without_io_or_consuming_the_pack() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine
            .commit_transaction(b"acknowledged", &[b"idle-capacity"])
            .unwrap();
        let ids = build_and_complete_background_pack(&engine, &[b"idle-capacity"]);
        let (reference, packed, publication) = engine
            .blocks
            .take_completed_background_pack()
            .unwrap()
            .unwrap();
        drop(publication);
        // Recreate a completed reservation after inspecting its canonical reference.
        let (segment_id, _) = engine
            .blocks
            .begin_background_pack(1, checkpoint_materialize_batch_bytes())
            .unwrap()
            .unwrap();
        let segment = engine
            .blocks
            .payload_segment_store()
            .unwrap()
            .create(segment_id, ids.clone(), |id| engine.read_block(id))
            .unwrap();
        let completed = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        engine
            .blocks
            .complete_background_pack(segment_id, completed)
            .unwrap();
        let references = (0..MAX_PAYLOAD_SEGMENTS)
            .map(|index| {
                let mut reference = reference;
                reference.segment_id = uuid::Uuid::from_u128(index as u128 + 1);
                reference
            })
            .collect();
        engine.payload_manifest = Some(PayloadManifest::new(references).unwrap());
        vfs.arm_fault(None).unwrap();
        assert!(!engine.publish_completed_pack_if_capacity().unwrap());
        assert!(vfs.trace().unwrap().is_empty());
        assert_eq!(
            engine
                .blocks
                .unpublished_background_segment_ids()
                .unwrap()
                .len(),
            1
        );
        assert!(packed.contains(&ids[0]));
        engine.payload_manifest = None;
        engine.restart_required.store(true, std::sync::atomic::Ordering::Relaxed);
        assert!(engine.publish_completed_pack_if_capacity().is_err());
        assert!(vfs.trace().unwrap().is_empty());
        engine.restart_required.store(false, std::sync::atomic::Ordering::Relaxed);
        let wal_before = vfs.durable_file(engine.wal.path()).unwrap();
        assert!(engine.publish_completed_pack_if_capacity().unwrap());
        assert_eq!(engine.payload_segment_count(), 1);
        assert_eq!(vfs.durable_file(engine.wal.path()).unwrap(), wal_before);
        assert_eq!(engine.read_block(ids[0]).unwrap(), b"idle-capacity");
    }

    #[test]
    fn every_unpublished_pack_cleanup_fault_preserves_wal_recovery() {
        fn seed(vfs: Arc<DeterministicVfs>) -> Engine {
            let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            engine
                .commit_transaction(b"acknowledged", &[b"cleanup-wal-source"])
                .unwrap();
            build_and_complete_background_pack(&engine, &[b"cleanup-wal-source"]);
            vfs.arm_fault(None).unwrap();
            engine
        }
        let baseline_vfs = prepared_simulated_vfs();
        let baseline = seed(baseline_vfs.clone());
        assert!(baseline
            .blocks
            .discard_completed_background_pack()
            .unwrap()
            .is_some());
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if is_sync(operation) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = prepared_simulated_vfs();
                let engine = seed(vfs.clone());
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let _ = engine.blocks.discard_completed_background_pack();
                assert_eq!(
                    engine
                        .read_block(BlockId::for_payload(b"cleanup-wal-source"))
                        .unwrap(),
                    b"cleanup-wal-source"
                );
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                let transaction = reopened.transaction_at(1).unwrap().unwrap();
                assert_eq!(transaction.envelope.inline_payload, b"acknowledged");
                assert_eq!(
                    reopened
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"cleanup-wal-source"
                );
                reopened.checkpoint().unwrap();
                drop(reopened);
                vfs.crash().unwrap();
                let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                assert_eq!(
                    reopened
                        .read_block(BlockId::for_payload(b"cleanup-wal-source"))
                        .unwrap(),
                    b"cleanup-wal-source"
                );
            }
        }
    }

    #[test]
    fn orphan_planning_preserves_unpublished_background_segment() {
        let (_vfs, engine) = initialized_simulated_engine();
        build_and_complete_background_pack(&engine, &[b"unpublished-background-segment"]);
        // The finished segment is durable but not yet manifest-referenced;
        // orphan planning must not offer it for removal.
        let plan = engine.plan_payload_segment_orphan().unwrap();
        assert_eq!(plan.scanned_files, 1);
        assert!(plan.candidate.is_none());
        assert!(!plan.more_candidates);
        let store = engine.block_store_shared();
        let segment_id = store.unpublished_background_segment_ids().unwrap()[0];
        let error = engine
            .remove_payload_segment_orphan(&PayloadSegmentOrphanCandidate {
                file_name: format!("{segment_id}.seg"),
                segment_id: Some(segment_id),
                file_bytes: 0,
            })
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(engine
            .plan_payload_segment_orphan()
            .unwrap()
            .candidate
            .is_none());
    }
    #[test]
    fn indexed_rotation_does_not_wait_for_a_private_pack_handoff() {
        let (_vfs, mut engine) = initialized_simulated_engine();
        engine.commit(b"covered").unwrap();
        let ids = build_and_complete_background_pack(&engine, &[b"pack-owned"]);
        let store = engine.block_store_shared();
        let publication = store.take_completed_background_pack().unwrap().unwrap();
        indexed_rotate(&mut engine);
        assert_eq!(store.get(ids[0]).unwrap(), b"pack-owned");
        drop(publication);
        engine.commit(b"after-install").unwrap();
    }

    #[test]
    fn rotation_window_payloads_are_durable_before_wal_retirement() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        // Register without scheduling a worker: every drain/publication step
        // is explicit, making the rotation window and power loss deterministic.
        let registration = engine.blocks.register_background_packer().unwrap();
        engine.commit(b"covered").unwrap();
        let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(prepared) => prepared,
            other => panic!("expected prepared rotation, got {other:?}"),
        };
        let outcome = encode_rotation_segments(&prepared).unwrap();
        let payload = b"committed after rotation snapshot";
        let id = BlockId::for_payload(payload);
        engine.commit_transaction(b"window", &[payload]).unwrap();
        engine.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap();
        assert!(!vfs.metadata(&indexed_block_path(id)).is_ok());
        assert_eq!(engine.read_block(id).unwrap(), payload);
        engine.commit(b"after install").unwrap();
        drop(registration);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.read_block(id).unwrap(), payload);
        assert_eq!(reopened.transaction_at(2).unwrap().unwrap().envelope.inline_payload, b"window");
    }

    #[test]
    fn deferred_rotation_parks_encoded_work_and_installs_without_reencode() {
        // 2026-09-20 swarm-p99 parked-defer contract on the daemon lane
        // (driver attached, packer running): a deferred install parks the
        // fully encoded rotation whole — fresh prepares answer Busy, the
        // successor WAL stays writable, and the retry installs the parked
        // snapshot+segments WITHOUT re-encoding the covered prefix.
        let vfs = prepared_simulated_vfs();
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        engine.attach_rotation_driver();
        let packer = engine.start_background_packer().unwrap();
        for index in 0..4_u8 {
            engine
                .commit(format!("covered-{index}").as_bytes())
                .unwrap();
        }
        let _ids = build_and_complete_background_pack(&engine, &[b"pack-owned"]);
        let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(prepared) => prepared,
            other => panic!("expected a ready rotation, got {other:?}"),
        };
        // Drive the publication through the engine and hold it unfinished:
        // `async_publication_in_flight` then owns the lane, which is what
        // the rotation install's defer keys on.
        let mut adoption = match engine.try_prepare_pack_adoption().unwrap() {
            PackAdoptionPreparation::Ready(prepared) => prepared,
            _other => panic!("completed pack must prepare an adoption"),
        };
        let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
        engine.commit(b"window-before-install").unwrap();
        let error = engine
            .finish_rotation_checkpoint(prepared, Ok(outcome))
            .err()
            .expect("publication-owned materialization must defer the rotation");
        assert!(is_publication_owns_materialization(&error));
        assert!(matches!(
            engine.try_prepare_rotation_checkpoint().unwrap(),
            RotationCheckpointPreparation::Busy
        ));
        assert!(!engine.online_rotation_blocks_writes());
        engine.commit(b"window-after-defer").unwrap();
        let adoption_outcome = crate::engine::publish_prepared_pack_adoption(&mut adoption);
        engine
            .finish_pack_adoption(adoption, adoption_outcome)
            .unwrap();
        let (prepared, outcome) = match engine.take_prepared_rotation().unwrap() {
            Some(crate::engine::RotationWork::Deferred(prepared, outcome)) => {
                (prepared, outcome)
            }
            _other => panic!("the parked rotation must come back deferred"),
        };
        engine
            .finish_rotation_checkpoint(prepared, Ok(outcome))
            .unwrap();
        assert!(engine.state.checkpoint_sequence > 0);
        engine.commit(b"after-install").unwrap();
        assert_eq!(
            engine
                .transaction_at(engine.state.durable_sequence - 1)
                .unwrap()
                .unwrap()
                .envelope
                .inline_payload,
            b"window-after-defer"
        );
        packer.stop_and_join().unwrap();
    }

    // ==== Integrity-fence regression tests (2026-09-22 hardening) ====
    //
    // The live authority logged `CONTROL payload manifest references a
    // missing segment` persisting across restarts. These tests pin the
    // fail-closed contract: a manifest-referenced segment that disappears
    // fences the affected reads with `InvalidData` and mutates nothing, and
    // no crash point of the publish/checkpoint paths can produce a manifest
    // that references a segment file which is not durable.

    fn manifest_segment_ids(engine: &Engine) -> Vec<Uuid> {
        engine
            .payload_manifest
            .as_ref()
            .map_or_else(Vec::new, |manifest| {
                manifest
                    .segments
                    .iter()
                    .map(|reference| reference.segment_id)
                    .collect()
            })
    }

    fn segment_file_present(vfs: &Arc<DeterministicVfs>, segment_id: Uuid) -> bool {
        let expected = format!("{}.pseg", segment_id.simple());
        vfs.read_directory(Path::new("/data/payload-segments"))
            .unwrap()
            .iter()
            .any(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name == expected)
            })
    }

    fn assert_manifest_references_only_present_segments(
        vfs: &Arc<DeterministicVfs>,
        engine: &Engine,
    ) {
        for segment_id in manifest_segment_ids(engine) {
            assert!(
                segment_file_present(vfs, segment_id),
                "manifest references missing segment {segment_id}"
            );
        }
    }

    #[test]
    fn deleted_manifest_segment_fences_reads_without_mutating_state() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let payload = b"fenced-segment-payload".to_vec();
        let id = BlockId::for_payload(&payload);
        engine.commit_transaction(b"payload-tx", &[&payload]).unwrap();
        engine.checkpoint().unwrap();
        let segment_ids = manifest_segment_ids(&engine);
        assert_eq!(segment_ids.len(), 1);
        let durable_sequence = engine.state.durable_sequence;
        let generation = engine.state.generation;
        drop(engine);
        // Out-of-band loss of a manifest-referenced segment (operator error,
        // volume damage): the integrity fence must fire on the affected read
        // and hold across a restart, never silently degrade.
        let path = Path::new("/data/payload-segments")
            .join(format!("{}.pseg", segment_ids[0].simple()));
        vfs.remove_file(&path).unwrap();
        let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let error = engine.read_block(id).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(
            error.to_string().contains("references a missing segment"),
            "{error}"
        );
        // Stable, not a transient retry artifact.
        let again = engine.read_block(id).unwrap_err();
        assert_eq!(again.kind(), io::ErrorKind::InvalidData);
        assert!(again.to_string().contains("references a missing segment"));
        // The fenced read mutated nothing.
        assert_eq!(engine.state.durable_sequence, durable_sequence);
        assert_eq!(engine.state.generation, generation);
        assert_eq!(manifest_segment_ids(&engine), segment_ids);
        // The engine stays alive for operations that do not touch the lost
        // segment: a fresh commit appends and acknowledges exactly like the
        // pre-loss state.
        engine.commit(b"after-fence").unwrap();
        assert_eq!(engine.state.durable_sequence, durable_sequence + 1);
        assert!(engine.read_block(id).is_err());
    }

    #[test]
    fn crash_anywhere_in_payload_publication_never_references_a_missing_segment() {
        // Seed: one acknowledged payload block awaiting materialization.
        let seed_vfs = prepared_simulated_vfs();
        {
            let mut seed = Engine::initialize_with_vfs(Path::new("/data"), seed_vfs.clone())
                .unwrap();
            seed.commit_transaction(b"publication-seed", &[b"published-payload"])
                .unwrap();
            drop(seed);
        }
        let payload_id = BlockId::for_payload(b"published-payload");
        let prepared = || {
            let vfs = Arc::new(seed_vfs.fork_durable().unwrap());
            let engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(None).unwrap();
            (vfs, engine)
        };
        // Baseline trace of one materialize+publish round.
        let (baseline_vfs, mut baseline) = prepared();
        baseline
            .materialize_staged_blocks_step(u64::MAX)
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert!(
            trace.len() > 8,
            "a publication trace must exercise real filesystem operations, got {trace:?}"
        );
        let mut cases = 0_u32;
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if matches!(operation, Operation::Write) {
                actions.push(FaultAction::ShortWrite(3));
            }
            if is_sync(operation) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let occurrence = trace[..=index].iter().filter(|op| *op == operation).count();
                let mut exercised = None;
                for _ in 0..16 {
                    let (vfs, mut engine) = prepared();
                    vfs.arm_operation_fault(*operation, occurrence, action)
                        .unwrap();
                    let _ = engine.materialize_staged_blocks_step(u64::MAX);
                    if vfs.operation_fault_fired().unwrap() {
                        exercised = Some((vfs, engine));
                        break;
                    }
                }
                let (vfs, engine) = exercised.unwrap_or_else(|| {
                    panic!("unexercised {operation:?} occurrence={occurrence} action={action:?}")
                });
                vfs.arm_fault(None).unwrap();
                drop(engine);
                vfs.crash().unwrap();
                let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone())
                    .unwrap_or_else(|error| {
                        panic!("fault {index} {operation:?}: reopen failed: {error}")
                    });
                // The invariant: no durable CONTROL generation may reference
                // a segment file that is not present.
                assert_manifest_references_only_present_segments(&vfs, &reopened);
                // The acknowledged payload resolves either way: through its
                // published segment or through the replayed WAL-carried copy.
                assert_eq!(
                    reopened.read_block(payload_id).unwrap(),
                    b"published-payload",
                    "fault {index} {operation:?}"
                );
                // Forward progress after recovery is unimpaired.
                reopened.checkpoint().unwrap();
                assert_manifest_references_only_present_segments(&vfs, &reopened);
                assert_eq!(reopened.read_block(payload_id).unwrap(), b"published-payload");
                cases += 1;
            }
        }
        assert!(cases > 8, "the crash matrix must exercise real cases, got {cases}");
    }

    #[test]
    fn reopen_after_a_failed_shutdown_checkpoint_never_references_a_missing_segment() {
        // The production shutdown logged `authority shutdown checkpoint
        // failed: WAL barrier wait exceeded its deadline`. That failure ran
        // on the LIVE engine while it still held an active committed window
        // (a reopened engine has already replayed its window into durable
        // CONTROL, so its checkpoint is the clean InvalidInput skip). Model
        // it exactly: every prepared engine is live with one
        // un-checkpointed window transaction, and each case injects one
        // fault into the shutdown-shaped checkpoint. Every torn image must
        // reopen fail-closed-consistent.
        let seed_vfs = prepared_simulated_vfs();
        let first_id = BlockId::for_payload(b"checkpointed-payload");
        let second_id = BlockId::for_payload(b"window-payload");
        {
            let mut seed = Engine::initialize_with_vfs(Path::new("/data"), seed_vfs.clone())
                .unwrap();
            seed.set_checkpoint_segment_ids_for_test((0..64).map(|index|
                uuid::Uuid::from_u128(0x62c0806c_f346_442c_889f_724ab2d4c581 + index)).collect());
            seed.commit_transaction(b"seed", &[b"checkpointed-payload"])
                .unwrap();
            seed.checkpoint().unwrap();
            drop(seed);
        }
        let prepared = || {
            let vfs = Arc::new(seed_vfs.fork_durable().unwrap());
            let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            // The pre-shutdown window the failed checkpoint was retiring.
            engine
                .commit_transaction(b"window", &[b"window-payload"])
                .unwrap();
            // Identical segment IDs make catalog bytes/hash-shard paths and
            // the entire checkpoint trace repeatable across every fault replay.
            engine.set_checkpoint_segment_ids_for_test((0..64).map(|index|
                uuid::Uuid::from_u128(0x72c0806c_f346_442c_889f_724ab2d4c581 + index)).collect());
            vfs.arm_fault(None).unwrap();
            (vfs, engine)
        };
        let (baseline_vfs, mut baseline) = prepared();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert!(
            trace.len() > 8,
            "a checkpoint trace must exercise real filesystem operations, got {trace:?}"
        );
        for _ in 0..8 {
            let (vfs, mut engine) = prepared();
            engine.checkpoint().unwrap();
            assert_eq!(vfs.trace().unwrap(), trace, "fixed-ID shutdown checkpoint trace changed");
        }
        let mut cases = 0_u32;
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if matches!(operation, Operation::Write) {
                actions.push(FaultAction::ShortWrite(3));
            }
            if is_sync(operation) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let occurrence = trace[..=index].iter().filter(|op| *op == operation).count();
                let (vfs, mut engine) = prepared();
                vfs.arm_operation_fault(*operation, occurrence, action).unwrap();
                // Keep the shutdown swallow-and-recover contract; every armed
                // target must fire, with the same pre-fault trace as the probe.
                let _ = engine.checkpoint();
                assert!(vfs.operation_fault_fired().unwrap(),
                    "unexercised {operation:?} occurrence={occurrence} action={action:?}");
                let actual_trace = vfs.trace().unwrap();
                assert_eq!(actual_trace.get(..=index), Some(&trace[..=index]),
                    "shutdown checkpoint trace diverged before fault {index}");
                vfs.arm_fault(None).unwrap();
                drop(engine);
                vfs.crash().unwrap();
                let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone())
                    .unwrap_or_else(|error| {
                        panic!("fault {index} {operation:?}: reopen failed: {error}")
                    });
                assert_manifest_references_only_present_segments(&vfs, &reopened);
                assert_eq!(
                    reopened.read_block(first_id).unwrap(),
                    b"checkpointed-payload",
                    "fault {index} {operation:?}"
                );
                assert_eq!(
                    reopened.read_block(second_id).unwrap(),
                    b"window-payload",
                    "fault {index} {operation:?}"
                );
                // Forward progress after recovery is unimpaired. The
                // reopened engine may hold no active window at all (a
                // late-firing fault let the failed checkpoint retire the
                // WAL first; the daemon treats that InvalidInput as a clean
                // skip), so prove progress with a fresh commit instead.
                reopened.commit(b"post-recovery").unwrap();
                reopened.checkpoint().unwrap();
                assert_manifest_references_only_present_segments(&vfs, &reopened);
                assert_eq!(reopened.read_block(first_id).unwrap(), b"checkpointed-payload");
                assert_eq!(reopened.read_block(second_id).unwrap(), b"window-payload");
                cases += 1;
            }
        }
        assert!(cases > 8, "the crash matrix must exercise real cases, got {cases}");
    }

    mod adoption_isolation {
        include!("engine_adoption_tests.rs");
    }

    fn indexed_block_path(id: BlockId) -> PathBuf {
        let hex = id.to_hex();
        PathBuf::from(format!("/data/blocks/{}/{hex}.blk", &hex[..2]))
    }

    fn indexed_rotate(engine: &mut Engine) {
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected ready indexed rotation: {other:?}"),
        };
        let outcome = encode_rotation_segments(&snapshot);
        engine.finish_rotation_checkpoint(snapshot, outcome).unwrap();
    }

    #[test]
    fn indexed_rotation_without_packer_recovers_covered_window_and_manual_checkpoint() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        let first = engine.commit_transaction(b"covered", &[b"covered bytes"]).unwrap().block_ids[0];
        let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
            RotationCheckpointPreparation::Ready(snapshot) => snapshot,
            other => panic!("expected ready rotation: {other:?}"),
        };
        let outcome = encode_rotation_segments(&snapshot).unwrap();
        let second = engine.commit_transaction(b"window", &[b"window bytes"]).unwrap().block_ids[0];
        engine.finish_rotation_checkpoint(snapshot, Ok(outcome)).unwrap();
        assert!(engine.history_manifest.as_ref().unwrap().payload_index_root_id.is_some());
        for (id, bytes) in [(first, b"covered bytes".as_slice()), (second, b"window bytes")] {
            assert!(!vfs.metadata(&indexed_block_path(id)).is_ok());
            assert_eq!(engine.blocks.pending_payload(id).unwrap().is_some(), id == second);
            assert_eq!(engine.read_block(id).unwrap(), bytes);
        }
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.state.checkpoint_sequence, 1);
        let root = reopened.history_manifest.as_ref().unwrap().payload_index_root_id;
        reopened.commit_transaction(b"tail", &[b"tail bytes"]).unwrap();
        reopened.checkpoint().unwrap();
        assert_eq!(reopened.history_manifest.as_ref().unwrap().payload_index_root_id, root);
        std::mem::forget(reopened);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.durable_sequence, 3);
        assert_eq!(reopened.read_block(first).unwrap(), b"covered bytes");
        assert_eq!(reopened.read_block(second).unwrap(), b"window bytes");
        assert_eq!(reopened.read_block(BlockId::for_payload(b"tail bytes")).unwrap(), b"tail bytes");
    }

    fn indexed_fault_seed(prior_rotation: bool) -> (Arc<DeterministicVfs>, Vec<BlockId>) {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        let first = engine.commit_transaction(b"one", &[b"first indexed payload"]).unwrap().block_ids[0];
        if prior_rotation { indexed_rotate(&mut engine); }
        let second = engine.commit_transaction(b"two", &[b"second indexed payload"]).unwrap().block_ids[0];
        std::mem::forget(engine);
        vfs.crash().unwrap();
        (vfs, vec![first, second])
    }

    #[test]
    fn indexed_rotation_survives_every_io_fault_with_and_without_existing_catalog() {
        for prior in [false, true] {
            let (seed, ids) = indexed_fault_seed(prior);
            let perform = |engine: &mut Engine| -> io::Result<()> {
                let snapshot = match engine.try_prepare_rotation_checkpoint()? {
                    RotationCheckpointPreparation::Ready(snapshot) => snapshot,
                    other => panic!("unexpected rotation: {other:?}"),
                };
                let outcome = encode_rotation_segments(&snapshot);
                engine.finish_rotation_checkpoint(snapshot, outcome)?;
                Ok(())
            };
            let sample = Arc::new(seed.fork_durable().unwrap());
            let mut engine = Engine::open_with_vfs(Path::new("/data"), sample.clone()).unwrap();
            engine.attach_rotation_driver();
            sample.arm_fault(None).unwrap();
            perform(&mut engine).unwrap();
            let trace = sample.trace().unwrap();
            std::mem::forget(engine);
            for (index, operation) in trace.iter().enumerate() {
                let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
                if matches!(operation, Operation::Write) { actions.push(FaultAction::ShortWrite(3)); }
                if is_sync(operation) { actions.push(FaultAction::DropSync); }
                for action in actions {
                    let vfs = Arc::new(seed.fork_durable().unwrap());
                    let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                    engine.attach_rotation_driver();
                    vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
                    let _result = perform(&mut engine);
                    // Abrupt death: never allow Drop to flush or repair a failed publication.
                    std::mem::forget(engine);
                    vfs.crash().unwrap();
                    vfs.arm_fault(None).unwrap();
                    let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs.clone())
                        .unwrap_or_else(|error| panic!("prior={prior} operation={index} {operation:?}: {error}"));
                    assert_eq!(recovered.state.durable_sequence, 2, "fault {index}");
                    assert_eq!(recovered.read_block(ids[0]).unwrap(), b"first indexed payload");
                    assert_eq!(recovered.read_block(ids[1]).unwrap(), b"second indexed payload");
                    recovered.commit(b"after recovery").unwrap();
                    recovered.checkpoint().unwrap();
                    std::mem::forget(recovered);
                    vfs.crash().unwrap();
                    assert_eq!(Engine::open_with_vfs(Path::new("/data"), vfs).unwrap().state.durable_sequence, 3);
                }
            }
        }
    }

    #[test]
    fn indexed_source_corruption_is_rejected_without_an_ordinary_copy() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let payload = b"only history owns these bytes";
        let id = engine.commit_transaction(b"carried", &[payload]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        let manifest = engine.history_manifest.as_ref().unwrap();
        let reference = &manifest.segments[0];
        let index = crate::history_payload::SegmentIndex::decode(
            &engine.blocks.get(reference.payload_index_block_id.unwrap()).unwrap(), reference.block_id).unwrap();
        let offset = index.entries.iter().find(|entry| entry.0 == id).unwrap().1;
        let mut file = vfs.open(&indexed_block_path(reference.block_id), OpenRequest { write: true, ..OpenRequest::default() }).unwrap();
        file.write_all_at(16 + u64::from(offset), b"!").unwrap();
        engine.blocks.set_cache_capacity_bytes(0).unwrap();
        assert_eq!(engine.read_block(id).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn indexed_history_metadata_is_retained_and_backup_restore_is_complete() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let id = engine.commit_transaction(b"backup", &[b"indexed backup data"]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        let metadata = engine.blocks.history_payload_metadata_ids(engine.history_manifest.as_ref().unwrap()).unwrap();
        assert!(metadata.len() >= 3);
        let mut retained = BTreeSet::new();
        engine.visit_retained_transaction_blocks(|id| { retained.insert(id); Ok(()) }).unwrap();
        assert!(metadata.iter().all(|id| retained.contains(id)));
        vfs.create_dir(Path::new("/backup")).unwrap();
        vfs.create_dir(Path::new("/restored")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        crate::backup::create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        crate::backup::restore_latest_backup_with_vfs(Path::new("/backup"), Path::new("/restored"), vfs.clone()).unwrap();
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let restored = Engine::open_with_vfs(Path::new("/restored"), vfs).unwrap();
        assert_eq!(restored.read_block(id).unwrap(), b"indexed backup data");
        assert_eq!(restored.transaction_at(1).unwrap().unwrap().envelope.inline_payload, b"backup");
    }

    #[test]
    fn indexed_compaction_preserves_pruned_payloads_for_old_readers() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let root = engine.write_block(b"opaque authority root for compaction").unwrap();
        engine.commit_references_with_authority_state(b"root", &[], Some(root)).unwrap();
        let old = engine.commit_transaction(b"old", &[b"old reader bytes"]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        let old_source = engine.history_manifest.as_ref().unwrap().segments[0].block_id;
        let reader = engine.block_store_shared();
        let recent = engine.commit_transaction(b"new", &[b"recent indexed bytes"]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        engine.compact_history(1).unwrap();
        assert!(!vfs.metadata(&indexed_block_path(old)).is_ok());
        assert!(engine.payload_segment_count() > 0);
        vfs.remove_file(&indexed_block_path(old_source)).unwrap();
        reader.set_cache_capacity_bytes(0).unwrap();
        assert_eq!(reader.get(old).unwrap(), b"old reader bytes");
        assert_eq!(reader.get(recent).unwrap(), b"recent indexed bytes");
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.read_block(old).unwrap(), b"old reader bytes");
        assert_eq!(reopened.read_block(recent).unwrap(), b"recent indexed bytes");
    }

    #[test]
    fn indexed_compaction_survives_every_io_fault() {
        let (seed, mut engine) = initialized_simulated_engine();
        let root = engine.write_block(b"authority root").unwrap();
        engine.commit_references_with_authority_state(b"root", &[], Some(root)).unwrap();
        let old = engine.commit_transaction(b"old", &[b"pruned payload"]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        let new = engine.commit_transaction(b"new", &[b"retained payload"]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        std::mem::forget(engine);
        seed.crash().unwrap();
        let sample = Arc::new(seed.fork_durable().unwrap());
        let mut engine = Engine::open_with_vfs(Path::new("/data"), sample.clone()).unwrap();
        sample.arm_fault(None).unwrap();
        engine.compact_history(1).unwrap();
        let trace = sample.trace().unwrap();
        std::mem::forget(engine);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if matches!(operation, Operation::Write) { actions.push(FaultAction::ShortWrite(3)); }
            if is_sync(operation) { actions.push(FaultAction::DropSync); }
            for action in actions {
                let vfs = Arc::new(seed.fork_durable().unwrap());
                let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
                let _ = engine.compact_history(1);
                std::mem::forget(engine);
                vfs.crash().unwrap(); vfs.arm_fault(None).unwrap();
                let reopened = Engine::open_with_vfs(Path::new("/data"), vfs)
                    .unwrap_or_else(|error| panic!("compaction fault {index} {operation:?}: {error}"));
                assert_eq!(reopened.state.durable_sequence, 3);
                assert_eq!(reopened.read_block(old).unwrap(), b"pruned payload");
                assert_eq!(reopened.read_block(new).unwrap(), b"retained payload");
            }
        }
    }

    #[test]
    fn indexed_compaction_prepares_without_io_and_preserves_window_authority_updates() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let first_root = engine.write_block(b"first authority root").unwrap();
        engine.commit_references_with_authority_state(b"first", &[], Some(first_root)).unwrap();
        let first = engine.commit_transaction(b"old", &[b"old promoted data"]).unwrap().block_ids[0];
        indexed_rotate(&mut engine);
        engine.commit_transaction(b"new", &[b"new indexed data"]).unwrap();
        indexed_rotate(&mut engine);
        vfs.arm_fault(None).unwrap();
        let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
        assert!(vfs.trace().unwrap().is_empty(), "prepare must perform metadata-only reservation");
        let root = engine.write_block(b"window authority root").unwrap();
        engine.commit_references_with_authority_state(b"window", &[], Some(root)).unwrap();
        let window = engine.commit_transaction(b"window data", &[b"live WAL bytes"]).unwrap().block_ids[0];
        let outcome = encode_history_compaction(&prepared);
        engine.finish_history_compaction(prepared, outcome).unwrap();
        assert_eq!(engine.state.authority_state_root, Some(Some(root)));
        assert_eq!(engine.state.durable_sequence, 5);
        assert_eq!(engine.state.checkpoint_sequence, 3);
        assert_eq!(engine.read_block(first).unwrap(), b"old promoted data");
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state.authority_state_root, Some(Some(root)));
        assert_eq!(reopened.state.durable_sequence, 5);
        assert_eq!(reopened.read_block(window).unwrap(), b"live WAL bytes");
        assert_eq!(reopened.read_block(first).unwrap(), b"old promoted data");
    }

    #[test]
    fn abandoning_history_publication_fails_closed_and_reopen_recovers() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let root = engine.write_block(b"authority root").unwrap();
        engine.commit_references_with_authority_state(b"one", &[], Some(root)).unwrap();
        indexed_rotate(&mut engine);
        engine.commit_transaction(b"two", &[b"two payload"]).unwrap();
        indexed_rotate(&mut engine);
        let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
        drop(prepared);
        assert!(engine.commit(b"must restart").is_err());
        std::mem::forget(engine); vfs.crash().unwrap();
        assert_eq!(Engine::open_with_vfs(Path::new("/data"), vfs).unwrap().state.durable_sequence, 2);
    }

    #[test]
    fn indexed_history_source_handles_reuse_evict_and_disable_with_cache_budget() {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.blocks.set_cache_capacity_bytes(crate::block::MAX_BLOCK_BYTES).unwrap();
        let first = engine.commit_transaction(b"first source", &[b"first-A", b"first-B", b"first-C"]).unwrap().block_ids;
        indexed_rotate(&mut engine);
        let second = engine.commit_transaction(b"second source", &[b"second-A", b"second-B"]).unwrap().block_ids;
        indexed_rotate(&mut engine);
        engine.read_block(first[0]).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(engine.read_block(first[1]).unwrap(), b"first-B");
        assert!(!vfs.trace().unwrap().contains(&Operation::Open), "same immutable source must reuse its verified handle");
        engine.read_block(second[0]).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(engine.read_block(first[2]).unwrap(), b"first-C");
        assert_eq!(vfs.trace().unwrap().iter().filter(|op| matches!(op, Operation::Open)).count(), 1,
            "one source slot must evict the previous source");
        engine.blocks.set_cache_capacity_bytes(0).unwrap();
        for _ in 0..2 {
            vfs.arm_fault(None).unwrap();
            assert_eq!(engine.read_block(second[1]).unwrap(), b"second-B");
            assert_eq!(vfs.trace().unwrap().iter().filter(|op| matches!(op, Operation::Open)).count(), 2,
                "zero budget disables both payload and source handle caching");
        }
    }

    #[test]
    fn cached_history_source_truncation_still_fails_closed() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let ids = engine.commit_transaction(b"source", &[b"cached first", b"uncached second"]).unwrap().block_ids;
        indexed_rotate(&mut engine);
        engine.read_block(ids[0]).unwrap();
        let source = engine.history_manifest.as_ref().unwrap().segments[0].block_id;
        let mut file = vfs.open(&indexed_block_path(source), OpenRequest { write: true, ..OpenRequest::default() }).unwrap();
        file.set_len(1).unwrap();
        assert_eq!(engine.read_block(ids[1]).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }


    #[test]
    fn transaction_suffix_visits_history_and_wal_once_with_exact_boundaries() {
        let (vfs, mut engine) = initialized_simulated_engine();
        for round in 0..3_u64 {
            for index in 1..=64_u64 { engine.commit(&(round * 64 + index).to_le_bytes()).unwrap(); }
            engine.checkpoint().unwrap();
        }
        for sequence in 193..=194_u64 { engine.commit(&sequence.to_le_bytes()).unwrap(); }
        for (first, expected_decodes) in [(40, 3), (128, 2), (193, 0), (194, 0), (195, 0)] {
            let before = crate::history::decode_work();
            let mut seen = Vec::new();
            engine.visit_transaction_metadata_from(first, |transaction| {
                assert_eq!(transaction.envelope.inline_payload, transaction.sequence.to_le_bytes());
                seen.push(transaction.sequence);
                Ok(())
            }).unwrap();
            assert_eq!(seen, (first..=194).collect::<Vec<_>>());
            assert_eq!(crate::history::decode_work().since(before).attempts, expected_decodes);
        }
        assert_eq!(engine.visit_transaction_metadata_from(0, |_| Ok(())).unwrap_err().kind(), io::ErrorKind::InvalidInput);
        let before = crate::history::decode_work();
        let mut seen = Vec::new();
        let error = engine.visit_transaction_metadata_from(1, |transaction| {
            seen.push(transaction.sequence);
            if transaction.sequence == 4 { Err(io::Error::new(io::ErrorKind::Interrupted, "caller stop")) } else { Ok(()) }
        }).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Interrupted);
        assert_eq!(seen, vec![1, 2, 3, 4]);
        assert_eq!(crate::history::decode_work().since(before).attempts, 1);
        // Required pruned prefix fails before returning any incomplete suffix.
        engine.compact_history(1).unwrap();
        let mut visited = 0;
        assert_eq!(engine.visit_transaction_metadata_from(1, |_| { visited += 1; Ok(()) }).unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert_eq!(visited, 0);
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let mut seen = Vec::new();
        reopened.visit_transaction_metadata_from(129, |transaction| { seen.push(transaction.sequence); Ok(()) }).unwrap();
        assert_eq!(seen, (129..=194).collect::<Vec<_>>());
    }

    #[test]
    fn transaction_suffix_authenticates_whole_segment_before_visiting_selected_tail() {
        let (_, mut engine) = initialized_simulated_engine();
        for sequence in 1..=64_u64 { engine.commit(&sequence.to_le_bytes()).unwrap(); }
        engine.checkpoint().unwrap();
        let source_id = engine.history_manifest.as_ref().unwrap().segments[0].block_id;
        let mut source = engine.blocks.get(source_id).unwrap();
        // An outer-valid block containing a damaged transaction hash must fail
        // even if the selected suffix excludes that transaction.
        source[crate::history::SEGMENT_HEADER_BYTES + 8] ^= 1;
        let corrupt_id = engine.write_block(&source).unwrap();
        engine.history_manifest.as_mut().unwrap().segments[0].block_id = corrupt_id;
        let mut visited = 0;
        let before = crate::history::decode_work();
        assert_eq!(engine.visit_transaction_metadata_from(64, |_| { visited += 1; Ok(()) }).unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert_eq!(visited, 0);
        let work = crate::history::decode_work().since(before);
        assert_eq!(work.attempts, 1);
        assert_eq!(work.transactions, 0);
    }

    include!("engine_generation_tests.rs");

    include!("engine_rotation_publication_tests.rs");
