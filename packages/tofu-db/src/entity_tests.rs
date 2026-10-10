    use super::*;
    use crate::transaction::{encode_family_records, FamilyRecord, FamilyRecordKind};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, OpenRequest, Operation, Vfs};
    use std::collections::BTreeSet;

    fn key(value: u16) -> EntityKey {
        EntityKey::new(7, 11, "conversation", &value.to_be_bytes()).unwrap()
    }

    #[test]
    fn bulk_occ_versions_match_points_with_mount_masks_and_share_page_reads() {
        use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
        struct CountReads<'a> { inner: &'a dyn EntityBlockIo, reads: AtomicUsize }
        impl EntityBlockIo for CountReads<'_> {
            fn read_verified(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
                self.reads.fetch_add(1, Relaxed); self.inner.read_verified(id)
            }
            fn write_fresh(&self, bytes: &[u8]) -> io::Result<BlockId> { self.inner.write_fresh(bytes) }
            fn write_fresh_owned(&self, bytes: Vec<u8>) -> io::Result<BlockId> { self.inner.write_fresh_owned(bytes) }
        }
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let backend = CountReads { inner: database.backend(), reads: AtomicUsize::new(0) };
        let tree = Tree { backend: &backend, page_cache: None };
        let mounted = tree.apply_batch(None, (0..2_000u16).map(|index|
            (key(index).0, Some(vec![9; 200]), 10)).collect(), TombstoneReclaim { below: 0, mounts: &[] }).unwrap().0.unwrap();
        let base = tree.apply_batch(None, (0..2_000u16).step_by(2).map(|index|
            (key(index).0, (index % 6 != 0).then(|| vec![8; 200]), if index % 6 == 0 { 50 } else { 40 }))
            .collect(), TombstoneReclaim { below: 0, mounts: &[] }).unwrap().0;
        let root = tree.store_directory(&RootDirectory { base_root: base, materialized: None,
            mounts: vec![RangeMount { start: key(0).0, end: key(1_000).0, root: mounted },
                RangeMount { start: key(1_000).0, end: key(2_000).0, root: mounted }] }, &mut Vec::new()).unwrap();
        let owned_keys: Vec<_> = (0..2_100u16).map(|index| key(index).0).collect();
        let keys: Vec<_> = owned_keys.iter().map(Vec::as_slice).collect();
        backend.reads.store(0, Relaxed);
        let expected: Vec<_> = keys.iter().map(|key| tree.get(Some(root), key).unwrap().map(|value| value.version)).collect();
        let point_reads = backend.reads.swap(0, Relaxed);
        assert_eq!(tree.versions(Some(root), &keys).unwrap(), expected);
        let bulk_reads = backend.reads.load(Relaxed);
        assert!(bulk_reads * 4 < point_reads, "bulk={bulk_reads}, points={point_reads}");
        assert_eq!(expected[0], Some(50), "a base tombstone must mask its mounted value");
        assert_eq!(expected[1], Some(10));
        assert_eq!(expected[2], Some(40));
        assert_eq!(expected[2_000], None);
        assert_eq!(tree.versions(None, &keys).unwrap(), vec![None; keys.len()]);
        assert!(tree.versions(Some(root), &[]).unwrap().is_empty());
    }

    #[test]
    fn large_key_internal_splits_and_torn_commits_preserve_one_complete_tree() {
        let large_key = |index: u32| {
            let mut bytes = vec![b'k'; MAX_ENTITY_KEY_BYTES - 4];
            bytes.extend_from_slice(&index.to_be_bytes());
            EntityKey::new(7, 11, "large_keys", &bytes).unwrap()
        };
        let setup = |vfs: Arc<DeterministicVfs>| {
            vfs.create_dir(std::path::Path::new("/data")).unwrap();
            vfs.sync_directory(std::path::Path::new("/")).unwrap();
            let mut database =
                EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
            database.set_write_overlay_enabled(false);
            let mut transaction = database.begin(7, 11).unwrap();
            for index in 0..4 {
                database
                    .put(
                        &mut transaction,
                        large_key(index),
                        vec![index as u8; MAX_ENTITY_INLINE_VALUE_BYTES],
                    )
                    .unwrap();
            }
            database.commit(transaction).unwrap();
            database
        };
        let change = |database: &mut EntityDatabase| -> io::Result<()> {
            let mut transaction = database.begin(7, 11)?;
            for index in [0, 4, 5, 6] {
                database.put(
                    &mut transaction,
                    large_key(index),
                    vec![64 + index as u8; MAX_ENTITY_INLINE_VALUE_BYTES],
                )?;
            }
            database.commit(transaction)?;
            Ok(())
        };
        let verify = |vfs: Arc<DeterministicVfs>| {
            vfs.arm_fault(None).unwrap();
            vfs.crash().unwrap();
            let mut database =
                EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
            database.set_write_overlay_enabled(false);
            let mut oversized_internal = false;
            visit_entity_page_graph(
                database.snapshot().root,
                |id| database.writer_engine().unwrap().read_block(id),
                |_, bytes| {
                    assert!(bytes.len() <= PAGE_MAX_BYTES);
                    if let Page::Internal { children, .. } = Page::decode(bytes)? {
                        assert!(children.len() >= MIN_INTERNAL_SPLIT_CHILDREN);
                        oversized_internal |= bytes.len() > PAGE_PACK_TARGET_BYTES;
                    }
                    Ok(())
                },
            )
            .unwrap();
            assert!(
                oversized_internal,
                "large separators must be allowed above the packing target"
            );
            let mut transaction = database.begin(7, 11).unwrap();
            let committed = database
                .get(&mut transaction, &large_key(4))
                .unwrap()
                .is_some();
            for index in 0..7 {
                let expected = if index < 4 || committed {
                    let byte = if committed && (index == 0 || index >= 4) {
                        64 + index as u8
                    } else {
                        index as u8
                    };
                    Some(vec![byte; MAX_ENTITY_INLINE_VALUE_BYTES])
                } else {
                    None
                };
                assert_eq!(
                    database.get(&mut transaction, &large_key(index)).unwrap(),
                    expected
                );
            }
            let end = large_key(7);
            let rows = database
                .scan(&mut transaction, &large_key(0), &end, 8)
                .unwrap();
            assert_eq!(rows.len(), if committed { 7 } else { 4 });
            assert!(rows.windows(2).all(|pair| pair[0].0 < pair[1].0));
        };
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = setup(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        change(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        verify(baseline_vfs);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if operation == &Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            for action in actions {
                let vfs = Arc::new(DeterministicVfs::new(None));
                let mut database = setup(vfs.clone());
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let _ = change(&mut database);
                drop(database);
                verify(vfs);
            }
        }
    }

    #[test]
    fn coalesced_leaf_deletes_preserve_absence_witnesses_and_untouched_references() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..64 {
            database
                .put(&mut seed, key(index), vec![index as u8; 3_000])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let untouched_leaves = |database: &EntityDatabase| {
            let tree = Tree {
            backend: database.writer_engine().unwrap(),
                page_cache: Some(&database.page_cache),
            };
            tree.scan(
                database.snapshot().root,
                key(60).encoded(),
                key(61).encoded(),
                1,
                &mut 0,
            )
            .unwrap()
            .1
        };
        let before = untouched_leaves(&database);
        assert!(!before.is_empty());
        let mut absence = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut absence, &key(1000)).unwrap(), None);
        let mut empty_range = database.begin(7, 11).unwrap();
        assert!(database
            .scan(&mut empty_range, &key(1000), &key(1001), 1)
            .unwrap()
            .is_empty());
        let mut changed = database.begin(7, 11).unwrap();
        for index in 0..50 {
            database.delete(&mut changed, key(index)).unwrap();
        }
        database
            .put(&mut changed, key(1000), b"transient".to_vec())
            .unwrap();
        database.commit(changed).unwrap();
        let mut removed = database.begin(7, 11).unwrap();
        for index in 0..50 {
            database.delete(&mut removed, key(index)).unwrap();
        }
        database.delete(&mut removed, key(1000)).unwrap();
        database.commit(removed).unwrap();
        assert_eq!(untouched_leaves(&database), before);
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut current, &key(1000)).unwrap(), None);
        let rows = database
            .scan(&mut current, &key(0), &key(1001), 100)
            .unwrap();
        assert_eq!(rows.len(), 14);
        for (index, (observed, value)) in rows.iter().enumerate() {
            assert_eq!(*observed, key(index as u16 + 50));
            assert_eq!(*value, vec![index as u8 + 50; 3_000]);
        }
        drop(current);
        // Insert/delete ABA remains visible to both point and range OCC, even
        // when all deleted rows have been repacked into fewer physical pages.
        for mut stale in [absence, empty_range] {
            database
                .put(&mut stale, key(2000), b"must conflict".to_vec())
                .unwrap();
            assert_eq!(
                database.commit(stale).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
        }
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
    }

    #[test]
    fn apply_batch_matches_a_btree_oracle_across_splits_replaces_and_deletes() {
        // Regression coverage for the merge-join leaf overlay and sparse
        // internal grouping in `Tree::apply_page`: every committed batch is
        // compared against an in-memory oracle, including keys that replace
        // existing values and tombstoned deletes, across enough data to force
        // leaf splits and multi-level internal nodes.
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut oracle: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
        let mut state: u64 = 0x9e3779b97f4a7c15;
        let mut next_random = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        let keyspace: Vec<Vec<u8>> = (0..240u32)
            .map(|index| {
                let mut encoded = b"oracle/".to_vec();
                encoded.extend_from_slice(&index.to_be_bytes());
                encoded
            })
            .collect();
        for round in 0..60 {
            let mut transaction = database.begin(7, 11).unwrap();
            for _ in 0..24 {
                let key = keyspace[(next_random() as usize) % keyspace.len()].clone();
                match next_random() % 4 {
                    0 => {
                        if oracle.remove(&key).is_some() || next_random() % 2 == 0 {
                            database
                                .delete(
                                    &mut transaction,
                                    EntityKey::new(7, 11, "oracle", &key).unwrap(),
                                )
                                .unwrap();
                            oracle.remove(&key);
                        } else {
                            let value = next_random().to_le_bytes().to_vec();
                            database
                                .put(
                                    &mut transaction,
                                    EntityKey::new(7, 11, "oracle", &key).unwrap(),
                                    value.clone(),
                                )
                                .unwrap();
                            oracle.insert(key, value);
                        }
                    }
                    _ => {
                        let size = 16 + (next_random() as usize) % 400;
                        let mut value = next_random().to_le_bytes().to_vec();
                        value.resize(size, (next_random() % 251) as u8);
                        database
                            .put(
                                &mut transaction,
                                EntityKey::new(7, 11, "oracle", &key).unwrap(),
                                value.clone(),
                            )
                            .unwrap();
                        oracle.insert(key, value);
                    }
                }
            }
            database.commit(transaction).unwrap();
            let mut read = database.begin(7, 11).unwrap();
            let (start, end) = EntityKey::prefix_range(7, 11, "oracle", b"").unwrap();
            let mut scanned: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
            let mut cursor = start;
            loop {
                let page = database.scan(&mut read, &cursor, &end, 37).unwrap();
                if page.is_empty() {
                    break;
                }
                let last = page.last().unwrap().0.clone();
                for (key, value) in page {
                    scanned.insert(key.key_bytes().to_vec(), value);
                }
                let mut after = last.key_bytes().to_vec();
                after.push(0);
                cursor = EntityKey::new(7, 11, "oracle", &after).unwrap();
            }
            assert_eq!(scanned, oracle, "round {round} diverged from the oracle");
        }
    }

    #[test]
    fn snapshot_pins_have_no_count_quota_and_release_with_transaction_lifetimes() {
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transactions = Vec::with_capacity((LEGACY_SNAPSHOT_HANDLE_LIMIT * 8));
        for _ in 0..(LEGACY_SNAPSHOT_HANDLE_LIMIT * 8) {
            transactions.push(database.begin(7, 11).unwrap());
        }
        assert_eq!(
            database.snapshot_pin_metrics().unwrap(),
            EntitySnapshotPinMetrics {
                active_handles: (LEGACY_SNAPSHOT_HANDLE_LIMIT * 8) as u32,
                distinct_snapshots: 1,
                oldest_sequence: Some(0),
                retained_transaction_bytes: 0,
            }
        );
        transactions.push(database.begin(7, 11).unwrap());
        transactions.pop();
        transactions.push(database.begin(7, 11).unwrap());
        drop(transactions);
        assert_eq!(
            database.snapshot_pin_metrics().unwrap(),
            EntitySnapshotPinMetrics::default()
        );
    }

    #[test]
    fn point_witnesses_cross_retired_count_and_keep_late_conflict_detection() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..=6144_u16 {
            assert_eq!(database.get(&mut transaction, &key(index)).unwrap(), None);
        }
        assert_eq!(transaction.point_witnesses.len(), 6145);
        assert_eq!(database.get(&mut transaction, &key(6144)).unwrap(), None);
        assert_eq!(transaction.point_witnesses.len(), 6145);
        let mut competing = database.begin(7, 11).unwrap();
        database.put(&mut competing, key(6144), b"late change".to_vec()).unwrap();
        database.commit(competing).unwrap();
        database.put(&mut transaction, key(u16::MAX), b"dependent".to_vec()).unwrap();
        assert_eq!(database.commit(transaction).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    }

    #[test]
    fn aggregate_transaction_bytes_allow_reads_beyond_retired_quota_and_release_on_drop() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database
            .put(&mut seed, key(1), b"present".to_vec())
            .unwrap();
        database.commit(seed).unwrap();

        let read_key = key(2);
        let next_read_bytes = point_witness_reservation_bytes(read_key.encoded());
        let mut holder = database.begin(7, 11).unwrap();
        holder
            .snapshot_pin
            .reserve_transaction_bytes(LEGACY_AGGREGATE_TRANSACTION_BYTES - next_read_bytes + 1)
            .unwrap();
        let mut reader = database.begin(7, 11).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(database.get(&mut reader, &read_key).unwrap(), None);
        assert!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes > LEGACY_AGGREGATE_TRANSACTION_BYTES);

        drop(holder);
        assert_eq!(database.get(&mut reader, &read_key).unwrap(), None);
        assert_eq!(
            database
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            next_read_bytes
        );
        drop(reader);
        assert_eq!(
            database
                .snapshot_pin_metrics()
                .unwrap()
                .retained_transaction_bytes,
            0
        );
    }

    #[test]
    fn replacing_a_staged_write_releases_its_value_reservation() {
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let entity_key = key(1);
        database
            .put(
                &mut transaction,
                entity_key.clone(),
                vec![0; MAX_ENTITY_INLINE_VALUE_BYTES],
            )
            .unwrap();
        let with_value = database
            .snapshot_pin_metrics()
            .unwrap()
            .retained_transaction_bytes;
        database.delete(&mut transaction, entity_key).unwrap();
        assert_eq!(
            with_value
                - database
                    .snapshot_pin_metrics()
                    .unwrap()
                    .retained_transaction_bytes,
            MAX_ENTITY_INLINE_VALUE_BYTES
        );
    }

    #[test]
    fn range_witnesses_cross_retired_count_and_keep_late_phantom_detection() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..=1024_u16 {
            assert!(database.scan(&mut transaction, &key(index), &key(index+1), 1).unwrap().is_empty());
            assert!(database.scan_reverse(&mut transaction, &key(index), &key(index+1), 1).unwrap().is_empty());
        }
        assert_eq!(transaction.range_witnesses.len(), 2050);
        assert_eq!(transaction.range_witnesses.last().unwrap().start, key(1024).encoded());
        let mut competing = database.begin(7, 11).unwrap();
        database.put(&mut competing, key(1024), b"late phantom".to_vec()).unwrap();
        database.commit(competing).unwrap();
        database.put(&mut transaction, key(u16::MAX), b"dependent".to_vec()).unwrap();
        assert_eq!(database.commit(transaction).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    }

    #[test]
    fn reachability_visits_each_structurally_bounded_page_without_a_global_set() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..256_u16 {
            database
                .put(&mut seed, key(index), vec![index as u8; 8 * 1024])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut visited = BTreeSet::new();
        let metrics = visit_entity_page_graph(
            database.snapshot().root,
            |block_id| database.writer_engine().unwrap().read_block(block_id),
            |block_id, _| {
                assert!(visited.insert(block_id));
                Ok(())
            },
        )
        .unwrap();
        assert!(metrics.page_count > 256);
        assert_eq!(metrics.page_count as usize, visited.len());
        assert!(metrics.payload_bytes > 2 * 1024 * 1024);
        assert!(metrics.maximum_frontier as usize <= MAX_ENTITY_REACHABILITY_FRONTIER);
    }

    #[test]
    fn persistent_snapshot_pin_survives_reopen_and_extends_reachability() {
        let directory = tempfile::tempdir().unwrap();
        let pinned_root;
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            database.set_write_overlay_enabled(false);
            let mut seed = database.begin(7, 11).unwrap();
            database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
            database.commit(seed).unwrap();
            pinned_root = database.snapshot().root.unwrap();

            let mut replace = database.begin(7, 11).unwrap();
            assert_eq!(
                database
                    .stage_persistent_snapshot_pin(&mut replace, b"conversation:one")
                    .unwrap()
                    .root,
                Some(pinned_root)
            );
            database
                .put(&mut replace, key(1), b"current".to_vec())
                .unwrap();
            database.commit(replace).unwrap();

            let mut visited = BTreeSet::new();
            visit_entity_page_graph(
                database.snapshot().root,
                |block_id| database.writer_engine().unwrap().read_block(block_id),
                |block_id, _| {
                    visited.insert(block_id);
                    Ok(())
                },
            )
            .unwrap();
            assert!(visited.contains(&pinned_root));
        }

        let mut database = EntityDatabase::open(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &key(1)).unwrap(),
            Some(b"current".to_vec())
        );
        let mut pinned = database
            .begin_persistent_snapshot(7, 11, b"conversation:one")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut pinned, &key(1)).unwrap(),
            Some(b"old".to_vec())
        );
        assert_eq!(
            database
                .put(&mut pinned, key(2), b"forbidden".to_vec())
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        drop(pinned);

        let mut remove = database.begin(7, 11).unwrap();
        assert!(database
            .remove_persistent_snapshot_pin(&mut remove, b"conversation:one")
            .unwrap());
        database.commit(remove).unwrap();
        assert!(database
            .begin_persistent_snapshot(7, 11, b"conversation:one")
            .unwrap()
            .is_none());
    }

    #[test]
    fn persistent_snapshot_pin_catalog_scales_past_legacy_bound_and_remains_immutable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
        database.commit(seed).unwrap();

        let mut pins = database.begin(7, 11).unwrap();
        for index in 0..(MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS * 2) {
            database
                .stage_persistent_snapshot_pin(&mut pins, format!("pin-{index}").as_bytes())
                .unwrap();
        }
        database.commit(pins).unwrap();
        let reachability = visit_entity_page_graph(
            database.root,
            |block_id| database.writer_engine().unwrap().read_block(block_id),
            |_, _| Ok(()),
        )
        .unwrap();
        assert!(reachability.page_count > MAX_LEGACY_PERSISTENT_ENTITY_ROOT_PINS as u64);

        let mut replacement = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .stage_persistent_snapshot_pin(&mut replacement, b"pin-0")
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );

        let count_key = persistent_root_pin_count_key().unwrap();
        let mut force_capacity = database
            .begin_with_additional_scope_prefixes(7, 11, vec![count_key.encoded().to_vec()])
            .unwrap();
        database
            .put(
                &mut force_capacity,
                count_key,
                encode_persistent_root_pin_count(MAX_PERSISTENT_ENTITY_ROOT_PINS).unwrap(),
            )
            .unwrap();
        database.commit(force_capacity).unwrap();
        let mut overflow = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .stage_persistent_snapshot_pin(&mut overflow, b"overflow")
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn persistent_pin_catalog_upgrades_legacy_records_and_uses_bounded_point_reads() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
        database.commit(seed).unwrap();

        let legacy_pin_key = persistent_root_pin_key(7, 11, b"legacy").unwrap();
        let legacy_snapshot = database.snapshot();
        let mut legacy = database
            .begin_with_additional_scope_prefixes(7, 11, vec![legacy_pin_key.encoded().to_vec()])
            .unwrap();
        database
            .put(
                &mut legacy,
                legacy_pin_key,
                encode_persistent_root_pin(legacy_snapshot).unwrap(),
            )
            .unwrap();
        database.commit(legacy).unwrap();

        let mut upgrade = database.begin(7, 11).unwrap();
        database
            .stage_persistent_snapshot_pin(&mut upgrade, b"catalogued")
            .unwrap();
        database.commit(upgrade).unwrap();
        let count_key = persistent_root_pin_count_key().unwrap();
        let count_record = (Tree {
            backend: database.writer_engine().unwrap(),
            page_cache: Some(&database.page_cache),
        })
        .get(database.root, count_key.encoded())
        .unwrap()
        .unwrap()
        .value
        .unwrap();
        assert_eq!(decode_persistent_root_pin_count(&count_record).unwrap(), 2);

        vfs.arm_fault(None).unwrap();
        let mut next = database.begin(7, 11).unwrap();
        database
            .stage_persistent_snapshot_pin(&mut next, b"point-only")
            .unwrap();
        let reads = vfs
            .trace()
            .unwrap()
            .into_iter()
            .filter(|operation| operation == &Operation::Read)
            .count();
        assert!(
            reads <= 16,
            "catalogued pin admission performed {reads} reads"
        );
    }

    fn prepared_persistent_pin_database(vfs: Arc<DeterministicVfs>) -> EntityDatabase {
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"old".to_vec()).unwrap();
        database.commit(seed).unwrap();
        database
    }

    fn commit_persistent_pin(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        database.stage_persistent_snapshot_pin(&mut transaction, b"fault-pin")?;
        database.put(&mut transaction, key(1), b"new".to_vec())?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_persistent_pin_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        match database.snapshot().sequence {
            1 => {
                assert_eq!(
                    database.get(&mut current, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
                assert!(database
                    .begin_persistent_snapshot(7, 11, b"fault-pin")
                    .unwrap()
                    .is_none());
                assert!((Tree {
            backend: database.writer_engine().unwrap(),
                    page_cache: Some(&database.page_cache),
                })
                .get(
                    database.root,
                    persistent_root_pin_count_key().unwrap().encoded(),
                )
                .unwrap()
                .is_none());
            }
            2 => {
                assert_eq!(
                    database.get(&mut current, &key(1)).unwrap(),
                    Some(b"new".to_vec())
                );
                let mut pinned = database
                    .begin_persistent_snapshot(7, 11, b"fault-pin")
                    .unwrap()
                    .unwrap();
                assert_eq!(
                    database.get(&mut pinned, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
                let count = (Tree {
            backend: database.writer_engine().unwrap(),
                    page_cache: Some(&database.page_cache),
                })
                .get(
                    database.root,
                    persistent_root_pin_count_key().unwrap().encoded(),
                )
                .unwrap()
                .unwrap()
                .value
                .unwrap();
                assert_eq!(decode_persistent_root_pin_count(&count).unwrap(), 1);
            }
            sequence => panic!("persistent pin recovered a non-prefix sequence {sequence}"),
        }
    }

    #[test]
    fn every_persistent_snapshot_pin_commit_fault_recovers_an_atomic_prefix() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_persistent_pin_database(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        commit_persistent_pin(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_persistent_pin(&mut database);
            drop(database);
            assert_recovered_persistent_pin_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_persistent_pin(&mut database);
            drop(database);
            assert_recovered_persistent_pin_prefix(vfs);
        }
    }

    #[test]
    fn reachability_rejects_child_level_and_key_range_transplants() {
        let first_leaf = Page::Leaf(vec![(
            b"a".to_vec(),
            EntityValue {
                version: 1,
                value: Some(b"first".to_vec()),
            },
        )])
        .encode()
        .unwrap();
        let transplanted_leaf = Page::Leaf(vec![(
            b"c".to_vec(),
            EntityValue {
                version: 1,
                value: Some(b"second".to_vec()),
            },
        )])
        .encode()
        .unwrap();
        let first_id = BlockId::for_payload(&first_leaf);
        let transplanted_id = BlockId::for_payload(&transplanted_leaf);
        let root = Page::Internal {
            level: 1,
            children: vec![
                Child {
                    lower_bound: b"a".to_vec(),
                    block_id: first_id,
                },
                Child {
                    lower_bound: b"b".to_vec(),
                    block_id: transplanted_id,
                },
            ],
        }
        .encode()
        .unwrap();
        let root_id = BlockId::for_payload(&root);
        let blocks = BTreeMap::from([
            (root_id, root),
            (first_id, first_leaf),
            (transplanted_id, transplanted_leaf),
        ]);
        assert_eq!(
            visit_entity_page_graph(
                Some(root_id),
                |block_id| {
                    blocks
                        .get(&block_id)
                        .cloned()
                        .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))
                },
                |_, _| Ok(()),
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn old_and_current_snapshots_remain_distinct_and_readable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut old = database.begin(7, 11).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        database.put(&mut write, key(1), b"new".to_vec()).unwrap();
        database.commit(write).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.snapshot_pin_metrics().unwrap(),
            EntitySnapshotPinMetrics {
                active_handles: 2,
                distinct_snapshots: 2,
                oldest_sequence: Some(0),
                retained_transaction_bytes: 0,
            }
        );
        assert_eq!(database.get(&mut old, &key(1)).unwrap(), None);
        assert_eq!(
            database.get(&mut current, &key(1)).unwrap(),
            Some(b"new".to_vec())
        );
    }

    #[test]
    fn transaction_cannot_cross_a_database_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        drop(database);
        let reopened = EntityDatabase::open(directory.path()).unwrap();
        assert_eq!(
            reopened.get(&mut transaction, &key(1)).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn entity_page_encoding_preserves_v1_golden_bytes_with_exact_capacity() {
        let leaf = Page::Leaf(vec![
            (
                b"a".to_vec(),
                EntityValue {
                    version: 7,
                    value: Some(b"hi".to_vec()),
                },
            ),
            (
                b"b".to_vec(),
                EntityValue {
                    version: 9,
                    value: None,
                },
            ),
        ]);
        let expected_leaf = b"TDBENT01\x01\0\0\0\0\0\x02\0\x01\0\x02\0\x07\0\0\0\0\0\0\0\0ahi\x01\0\0\0\x09\0\0\0\0\0\0\0\x01b";
        let encoded = leaf.encode().unwrap();
        assert_eq!(encoded.as_slice(), expected_leaf);
        assert_eq!(encoded.capacity(), encoded.len());
        assert_eq!(
            Page::decode(expected_leaf).unwrap().encode().unwrap(),
            encoded
        );
        let internal = Page::Internal {
            level: 1,
            children: vec![Child {
                lower_bound: b"a".to_vec(),
                block_id: BlockId([42; 32]),
            }],
        };
        let mut expected_internal = b"TDBENT01\x01\0\0\0\x01\x01\x01\0\x01\0a".to_vec();
        expected_internal.extend_from_slice(&[42; 32]);
        let encoded = internal.encode().unwrap();
        assert_eq!(encoded, expected_internal);
        assert_eq!(encoded.capacity(), encoded.len());
        assert_eq!(
            Page::decode(&expected_internal).unwrap().encode().unwrap(),
            encoded
        );
    }

    #[test]
    fn entity_page_encoding_bounds_maximum_page_before_allocation() {
        let mut entries = vec![
            (
                b"a".to_vec(),
                EntityValue {
                    version: 1,
                    value: Some(vec![11; 8170]),
                },
            ),
            (
                b"b".to_vec(),
                EntityValue {
                    version: 2,
                    value: Some(vec![12; 8170]),
                },
            ),
        ];
        let encoded = Page::Leaf(entries.clone()).encode().unwrap();
        assert_eq!(encoded.len(), PAGE_MAX_BYTES);
        assert_eq!(encoded.capacity(), PAGE_MAX_BYTES);
        assert_eq!(Page::decode(&encoded).unwrap().encode().unwrap(), encoded);
        entries.push((
            b"c".to_vec(),
            EntityValue {
                version: 3,
                value: None,
            },
        ));
        assert_eq!(
            Page::Leaf(entries).encode().unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn cow_snapshot_survives_page_splits_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let old_snapshot;
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            database.set_write_overlay_enabled(false);
            let mut first = database.begin(7, 11).unwrap();
            for value in 0..200_u16 {
                database
                    .put(&mut first, key(value), vec![value as u8; 100])
                    .unwrap();
            }
            database.commit(first).unwrap();
            old_snapshot = database.snapshot();
            let mut second = database.begin(7, 11).unwrap();
            database.put(&mut second, key(50), b"new".to_vec()).unwrap();
            database.commit(second).unwrap();
            let old = Tree {
            backend: database.writer_engine().unwrap(),
                page_cache: Some(&database.page_cache),
            }
            .get(old_snapshot.root, key(50).encoded())
            .unwrap()
            .unwrap();
            assert_eq!(old.value.unwrap(), vec![50; 100]);
        }
        let mut database = EntityDatabase::open(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut read, &key(50)).unwrap().unwrap(), b"new");
    }

    #[test]
    fn batch_cow_writes_each_live_page_once() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let before = database.writer_engine().unwrap().block_write_metrics();
        let mut transaction = database.begin(7, 11).unwrap();
        for value in 0..200_u16 {
            database
                .put(&mut transaction, key(value), vec![value as u8; 100])
                .unwrap();
        }
        database.commit(transaction).unwrap();
        let after = database.writer_engine().unwrap().block_write_metrics();
        let mut live_pages = BTreeSet::new();
        visit_entity_page_graph(
            database.snapshot().root,
            |id| database.writer_engine().unwrap().read_block(id),
            |id, _| {
                assert!(live_pages.insert(id));
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(
            after.blocks_staged - before.blocks_staged,
            live_pages.len() as u64
        );
        assert!(after.bytes_staged - before.bytes_staged <= 32 * 1024);
        let first_commit = database.writer_engine().unwrap().committed_transactions().last().unwrap();
        assert_eq!(first_commit.envelope.block_ids.len(), live_pages.len());
        assert_eq!(
            first_commit
                .envelope
                .block_ids
                .iter()
                .copied()
                .collect::<BTreeSet<_>>(),
            live_pages
        );
        assert!(first_commit
            .envelope
            .block_ids
            .contains(&database.root.unwrap()));

        let before_update = database.writer_engine().unwrap().block_write_metrics();
        let mut update = database.begin(7, 11).unwrap();
        database
            .put(&mut update, key(100), b"updated".to_vec())
            .unwrap();
        database.commit(update).unwrap();
        let after_update = database.writer_engine().unwrap().block_write_metrics();
        assert_eq!(after_update.blocks_staged - before_update.blocks_staged, 2);
        let update_commit = database.writer_engine().unwrap().committed_transactions().last().unwrap();
        assert_eq!(update_commit.envelope.block_ids.len(), 2);
        assert!(update_commit
            .envelope
            .block_ids
            .contains(&database.root.unwrap()));
    }

    #[test]
    fn range_retirement_drops_thousands_of_keys_with_boundary_only_io() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        database.set_write_overlay_enabled(false);
        let range_key =
            |index: u32| EntityKey::new(7, 11, "range_retirement", &index.to_be_bytes()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..10_000_u32 {
            database
                .put(&mut seed, range_key(index), vec![index as u8; 32])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut old_snapshot = database.begin(7, 11).unwrap();

        vfs.arm_fault(None).unwrap();
        let mut retirement = database.begin(7, 11).unwrap();
        database
            .stage_persistent_snapshot_pin(&mut retirement, b"retired-range")
            .unwrap();
        database
            .retire_range(&mut retirement, &range_key(2_000), &range_key(8_000))
            .unwrap();
        database.commit(retirement).unwrap();
        let trace = vfs.trace().unwrap();
        let reads = trace
            .iter()
            .filter(|operation| operation == &&Operation::Read)
            .count();
        let writes = trace
            .iter()
            .filter(|operation| operation == &&Operation::Write)
            .count();
        assert!(reads <= 48, "range retirement performed {reads} reads");
        assert!(writes <= 32, "range retirement performed {writes} writes");

        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(1_999)).unwrap(),
            Some(vec![207; 32])
        );
        assert_eq!(database.get(&mut current, &range_key(2_000)).unwrap(), None);
        assert_eq!(database.get(&mut current, &range_key(7_999)).unwrap(), None);
        assert_eq!(
            database.get(&mut current, &range_key(8_000)).unwrap(),
            Some(vec![64; 32])
        );
        assert_eq!(
            database.get(&mut old_snapshot, &range_key(4_000)).unwrap(),
            Some(vec![160; 32])
        );
        let mut persistent = database
            .begin_persistent_snapshot(7, 11, b"retired-range")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut persistent, &range_key(4_000)).unwrap(),
            Some(vec![160; 32])
        );
    }

    #[test]
    fn range_retirement_is_root_witnessed_bounded_and_rejects_overlapping_writes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let range_key =
            |index: u16| EntityKey::new(7, 11, "range_retirement", &index.to_be_bytes()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..100_u16 {
            database
                .put(&mut seed, range_key(index), vec![index as u8])
                .unwrap();
        }
        database.commit(seed).unwrap();

        let mut stale = database.begin(7, 11).unwrap();
        database
            .retire_range(&mut stale, &range_key(10), &range_key(20))
            .unwrap();
        assert_eq!(
            database
                .put(&mut stale, range_key(15), b"forbidden".to_vec())
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            database
                .retire_range(&mut stale, &range_key(15), &range_key(25))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        for index in 0..MAX_ENTITY_RETIRED_RANGES - 1 {
            let start = 100 + index as u16 * 2;
            database
                .retire_range(&mut stale, &range_key(start), &range_key(start + 1))
                .unwrap();
        }
        assert_eq!(
            database
                .retire_range(&mut stale, &range_key(500), &range_key(501))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );

        let mut concurrent = database.begin(7, 11).unwrap();
        database
            .put(&mut concurrent, range_key(99), b"changed".to_vec())
            .unwrap();
        database.commit(concurrent).unwrap();
        assert_eq!(
            database.commit(stale).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn persistent_range_capsule_excludes_unrelated_database_pages_and_reopens() {
        let directory = tempfile::tempdir().unwrap();
        let range_key =
            |index: u32| EntityKey::new(7, 11, "range_capsule", &index.to_be_bytes()).unwrap();
        let capsule_root;
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            let mut seed = database.begin(7, 11).unwrap();
            for index in 0..10_000_u32 {
                database
                    .put(&mut seed, range_key(index), vec![index as u8; 32])
                    .unwrap();
            }
            database.commit(seed).unwrap();

            let ranges = [
                (range_key(100), range_key(200)),
                (range_key(9_000), range_key(9_100)),
            ];
            let mut snapshot = database.begin(7, 11).unwrap();
            capsule_root = database
                .stage_persistent_range_snapshot_pin(&mut snapshot, b"capsule", &ranges)
                .unwrap()
                .root
                .unwrap();
            database.commit(snapshot).unwrap();

            let metrics = visit_entity_page_graph(
                Some(capsule_root),
                |block_id| database.writer_engine().unwrap().read_block(block_id),
                |_, _| Ok(()),
            )
            .unwrap();
            assert!(
                metrics.page_count <= 16,
                "range capsule retained {} pages",
                metrics.page_count
            );
        }

        let database = EntityDatabase::open(directory.path()).unwrap();
        let mut capsule = database
            .begin_persistent_snapshot(7, 11, b"capsule")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut capsule, &range_key(150)).unwrap(),
            Some(vec![150; 32])
        );
        assert_eq!(
            database.get(&mut capsule, &range_key(9_050)).unwrap(),
            Some(vec![90; 32])
        );
        assert_eq!(database.get(&mut capsule, &range_key(99)).unwrap(), None);
        assert_eq!(database.get(&mut capsule, &range_key(5_000)).unwrap(), None);
        assert_eq!(database.get(&mut capsule, &range_key(9_100)).unwrap(), None);
    }

    #[test]
    fn range_capsule_restore_mounts_in_constant_writes_and_supports_repeated_retirement() {
        let directory = tempfile::tempdir().unwrap();
        let range_key =
            |index: u32| EntityKey::new(7, 11, "range_restore", &index.to_be_bytes()).unwrap();
        let ranges = [(range_key(2_000), range_key(8_000))];
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..10_000_u32 {
            database
                .put(&mut seed, range_key(index), vec![index as u8; 32])
                .unwrap();
        }
        database.commit(seed).unwrap();

        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"first", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();

        let before_restore = database.writer_engine().unwrap().block_write_metrics();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, b"first", &ranges)
            .unwrap();
        database.commit(restore).unwrap();
        let after_restore = database.writer_engine().unwrap().block_write_metrics();
        assert_eq!(
            after_restore.blocks_staged - before_restore.blocks_staged,
            1,
            "restoring a capsule must publish only its bounded root directory"
        );

        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(5_000)).unwrap(),
            Some(vec![136; 32])
        );
        assert_eq!(
            database.get(&mut current, &range_key(1_999)).unwrap(),
            Some(vec![207; 32])
        );
        let rows = database
            .scan(&mut current, &range_key(1_998), &range_key(2_003), 5)
            .unwrap();
        assert_eq!(rows.len(), 5);
        assert_eq!(rows[2].0, range_key(2_000));
        drop(current);

        let mut override_value = database.begin(7, 11).unwrap();
        database
            .put(&mut override_value, range_key(5_000), b"newer".to_vec())
            .unwrap();
        database.commit(override_value).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(5_000)).unwrap(),
            Some(b"newer".to_vec())
        );
        drop(current);

        let mut retire_again = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire_again, b"second", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire_again, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire_again).unwrap();
        drop(database);

        let mut database = EntityDatabase::open(directory.path()).unwrap();

        database.set_write_overlay_enabled(false);
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut current, &range_key(5_000)).unwrap(), None);
        let mut second = database
            .begin_persistent_snapshot(7, 11, b"second")
            .unwrap()
            .unwrap();
        assert_eq!(
            database.get(&mut second, &range_key(5_000)).unwrap(),
            Some(b"newer".to_vec())
        );
    }

    #[test]
    fn range_mount_consolidation_is_bounded_and_eventually_restores_a_direct_root() {
        let directory = tempfile::tempdir().unwrap();
        let range_key =
            |index: u32| EntityKey::new(7, 11, "mount_merge", &index.to_be_bytes()).unwrap();
        let ranges = [(range_key(500), range_key(4_500))];
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..5_000_u32 {
            database
                .put(&mut seed, range_key(index), vec![index as u8; 32])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"merge", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, b"merge", &ranges)
            .unwrap();
        database.commit(restore).unwrap();

        let mut rounds = 0;
        loop {
            let mut consolidation = database.begin(7, 11).unwrap();
            let Some(progress) = database
                .consolidate_one_range_mount(&mut consolidation)
                .unwrap()
            else {
                break;
            };
            assert!(progress.rows_materialized as usize <= MAX_ENTITY_MOUNT_CONSOLIDATION_ROWS);
            assert!(progress.materialized_bytes as usize <= MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES);
            database.commit(consolidation).unwrap();
            rounds += 1;
            assert!(rounds <= 8, "mount consolidation did not converge");
            if progress.mount_completed {
                break;
            }
        }
        assert_eq!(rounds, 5);
        assert!(matches!(
            (Tree {
            backend: database.writer_engine().unwrap(),
                page_cache: Some(&database.page_cache),
            })
            .load_root(database.root.unwrap())
            .unwrap(),
            RootNode::Page(_)
        ));
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(500)).unwrap(),
            Some(vec![244; 32])
        );
        assert_eq!(
            database.get(&mut current, &range_key(4_499)).unwrap(),
            Some(vec![147; 32])
        );
    }

    #[test]
    fn bounded_scan_over_mounted_range_stops_at_its_window() {
        // 2026-09-22 retention deadlock repro: a directory root (base +
        // mount) whose union spans far more than the retired 1,024-leaf quota
        // leaves must still answer a small bounded scan — the witness
        // covers the observed window, never the whole range. The page
        // level was already limit-aware; the directory merge was not, so
        // `event.prune` against a large streaming backlog failed closed
        // with database_resource_exhausted on every attempt.
        let directory = tempfile::tempdir().unwrap();
        let row_key =
            |index: u32| EntityKey::new(7, 11, "scan_window", &index.to_be_bytes()).unwrap();
        // ~3 rows per 16 KiB page: 12,000 rows span well over 1,024 leaves
        // across base and mount, so a limit-ignorant directory merge trips
        // its range witness before it can answer.
        let rows = 12_000_u32;
        let value = |index: u32| vec![(index % 251) as u8; 4 * 1024];
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        // Seed in bounded commits: one transaction's block-reference budget
        // (2,048 blocks) cannot carry every fresh page at once.
        let seed_commits = 24;
        for chunk in 0..seed_commits {
            let mut seed = database.begin(7, 11).unwrap();
            for index in (chunk * rows / seed_commits)..((chunk + 1) * rows / seed_commits) {
                database.put(&mut seed, row_key(index), value(index)).unwrap();
            }
            database.commit(seed).unwrap();
        }
        let ranges = [(row_key(3_000), row_key(9_000))];
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"scanwin", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, b"scanwin", &ranges)
            .unwrap();
        database.commit(restore).unwrap();
        assert!(matches!(
            (Tree {
            backend: database.writer_engine().unwrap(),
                page_cache: Some(&database.page_cache),
            })
            .load_root(database.root.unwrap())
            .unwrap(),
            RootNode::Directory(_)
        ));

        // Forward head window: answered from the base prefix alone; the
        // mount (starting at row 3,000) is beyond the cut and never
        // traversed.
        let mut head = database.begin(7, 11).unwrap();
        let page = database.scan(&mut head, &row_key(0), &row_key(rows), 25).unwrap();
        assert_eq!(page.len(), 25);
        for (offset, (key, _)) in page.iter().enumerate() {
            assert_eq!(key, &row_key(offset as u32));
        }
        let head_leaves = head.range_witnesses[0].leaf_ids.len();
        assert!(
            head_leaves <= 64,
            "head window witnessed {head_leaves} leaves — directory merge is not limit-aware"
        );

        // A window straddling the base/mount boundary still merges exactly.
        let mut mid = database.begin(7, 11).unwrap();
        let page = database
            .scan(&mut mid, &row_key(2_990), &row_key(3_020), 25)
            .unwrap();
        assert_eq!(page.len(), 25);
        for (offset, (key, _)) in page.iter().enumerate() {
            assert_eq!(key, &row_key(2_990 + offset as u32));
        }

        // Reverse tail window: the mount (ending at row 9,000) lies below
        // the cut and is never traversed.
        let mut tail = database.begin(7, 11).unwrap();
        let page = database
            .scan_reverse(&mut tail, &row_key(0), &row_key(rows), 25)
            .unwrap();
        assert_eq!(page.len(), 25);
        for (offset, (key, _)) in page.iter().enumerate() {
            assert_eq!(key, &row_key(rows - 1 - offset as u32));
        }
        let tail_leaves = tail.range_witnesses[0].leaf_ids.len();
        assert!(
            tail_leaves <= 64,
            "tail window witnessed {tail_leaves} leaves — directory merge is not limit-aware"
        );

        // Range-exhaustion answers keep their full witness: a window that
        // never fills never exits early, so the empty-tail proof is
        // unchanged.
        let mut rest = database.begin(7, 11).unwrap();
        let page = database
            .scan(&mut rest, &row_key(11_990), &row_key(rows), 25)
            .unwrap();
        assert_eq!(page.len(), 10);
        for (offset, (key, _)) in page.iter().enumerate() {
            assert_eq!(key, &row_key(11_990 + offset as u32));
        }
    }

    #[test]
    fn range_mount_consolidation_enforces_its_byte_budget_before_row_budget() {
        let directory = tempfile::tempdir().unwrap();
        let range_key = |index: u32| {
            let mut raw_key = vec![b'k'; 2_996];
            raw_key.extend_from_slice(&index.to_be_bytes());
            EntityKey::new(7, 11, "mount_bytes", &raw_key).unwrap()
        };
        let ranges = [(range_key(0), range_key(800))];
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..800_u32 {
            database
                .put(
                    &mut seed,
                    range_key(index),
                    vec![index as u8; MAX_ENTITY_INLINE_VALUE_BYTES],
                )
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"byte-budget", &ranges)
            .unwrap();
        database
            .retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, b"byte-budget", &ranges)
            .unwrap();
        database.commit(restore).unwrap();

        let mut consolidation = database.begin(7, 11).unwrap();
        let progress = database
            .consolidate_one_range_mount(&mut consolidation)
            .unwrap()
            .unwrap();
        assert!(!progress.mount_completed);
        assert!(progress.rows_materialized < 800);
        assert!(progress.rows_materialized > 0);
        assert!(progress.materialized_bytes as usize <= MAX_ENTITY_MOUNT_CONSOLIDATION_BYTES);
        database.commit(consolidation).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &range_key(0)).unwrap(),
            Some(vec![0; MAX_ENTITY_INLINE_VALUE_BYTES])
        );
        assert_eq!(
            database.get(&mut current, &range_key(799)).unwrap(),
            Some(vec![31; MAX_ENTITY_INLINE_VALUE_BYTES])
        );
    }

    fn prepared_range_restore_database(vfs: Arc<DeterministicVfs>) -> EntityDatabase {
        let mut database = prepared_persistent_pin_database(vfs);
        commit_persistent_range_retirement(&mut database).unwrap();
        database
    }

    fn commit_persistent_range_restore(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        database.stage_persistent_range_snapshot_restore(
            &mut transaction,
            b"retirement-fault",
            &[(key(1), key(2))],
        )?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_range_restore_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        match database.snapshot().sequence {
            2 => assert_eq!(database.get(&mut current, &key(1)).unwrap(), None),
            3 => assert_eq!(
                database.get(&mut current, &key(1)).unwrap(),
                Some(b"old".to_vec())
            ),
            sequence => panic!("range restore recovered a non-prefix sequence {sequence}"),
        }
    }

    #[test]
    fn every_range_restore_commit_fault_recovers_an_atomic_mounted_prefix() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_range_restore_database(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        commit_persistent_range_restore(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_persistent_range_restore(&mut database);
            drop(database);
            assert_recovered_range_restore_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_persistent_range_restore(&mut database);
            drop(database);
            assert_recovered_range_restore_prefix(vfs);
        }
    }

    fn commit_range_mount_consolidation(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        let progress = database
            .consolidate_one_range_mount(&mut transaction)?
            .ok_or_else(|| invalid_data("expected one mounted range"))?;
        if !progress.mount_completed || progress.rows_materialized != 1 {
            return Err(invalid_data("unexpected mount consolidation progress"));
        }
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_mount_consolidation_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        assert!(matches!(database.snapshot().sequence, 3 | 4));
        let mut current = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut current, &key(1)).unwrap(),
            Some(b"old".to_vec())
        );
        match database.snapshot().sequence {
            3 => assert!(matches!(
                (Tree {
            backend: database.writer_engine().unwrap(),
                    page_cache: Some(&database.page_cache),
                })
                .load_root(database.root.unwrap())
                .unwrap(),
                RootNode::Directory(_)
            )),
            4 => assert!(matches!(
                (Tree {
            backend: database.writer_engine().unwrap(),
                    page_cache: Some(&database.page_cache),
                })
                .load_root(database.root.unwrap())
                .unwrap(),
                RootNode::Page(_)
            )),
            _ => unreachable!(),
        }
    }

    #[test]
    fn every_mount_consolidation_commit_fault_recovers_one_equivalent_root() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_range_restore_database(baseline_vfs.clone());
        commit_persistent_range_restore(&mut baseline).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        commit_range_mount_consolidation(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            commit_persistent_range_restore(&mut database).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_range_mount_consolidation(&mut database);
            drop(database);
            assert_recovered_mount_consolidation_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_range_restore_database(vfs.clone());
            commit_persistent_range_restore(&mut database).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_range_mount_consolidation(&mut database);
            drop(database);
            assert_recovered_mount_consolidation_prefix(vfs);
        }
    }

    fn commit_persistent_range_retirement(database: &mut EntityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        database.stage_persistent_range_snapshot_pin(
            &mut transaction,
            b"retirement-fault",
            &[(key(1), key(2))],
        )?;
        database.retire_range(&mut transaction, &key(1), &key(2))?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_range_retirement_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        match database.snapshot().sequence {
            1 => {
                assert_eq!(
                    database.get(&mut current, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
                assert!(database
                    .begin_persistent_snapshot(7, 11, b"retirement-fault")
                    .unwrap()
                    .is_none());
            }
            2 => {
                assert_eq!(database.get(&mut current, &key(1)).unwrap(), None);
                let mut pinned = database
                    .begin_persistent_snapshot(7, 11, b"retirement-fault")
                    .unwrap()
                    .unwrap();
                assert_eq!(
                    database.get(&mut pinned, &key(1)).unwrap(),
                    Some(b"old".to_vec())
                );
            }
            sequence => panic!("range retirement recovered a non-prefix sequence {sequence}"),
        }
    }

    #[test]
    fn every_range_retirement_commit_fault_recovers_an_atomic_pinned_prefix() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_persistent_pin_database(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        commit_persistent_range_retirement(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_persistent_range_retirement(&mut database);
            drop(database);
            assert_recovered_range_retirement_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut database = prepared_persistent_pin_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_persistent_range_retirement(&mut database);
            drop(database);
            assert_recovered_range_retirement_prefix(vfs);
        }
    }

    #[test]
    fn transaction_write_budget_admits_secondary_indexes_and_rejects_the_next_key() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let indexed_key =
            |index: u32| EntityKey::new(7, 11, "event_indexes", &index.to_be_bytes()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..MAX_ENTITY_TRANSACTION_WRITES as u32 {
            database
                .put(&mut transaction, indexed_key(index), vec![1])
                .unwrap();
        }
        assert_eq!(
            database
                .put(
                    &mut transaction,
                    indexed_key(MAX_ENTITY_TRANSACTION_WRITES as u32),
                    vec![1],
                )
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        database.commit(transaction).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .get(
                    &mut read,
                    &indexed_key(MAX_ENTITY_TRANSACTION_WRITES as u32 - 1),
                )
                .unwrap(),
            Some(vec![1])
        );
    }

    #[test]
    fn point_witness_prevents_lost_update() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut left = database.begin(7, 11).unwrap();
        let mut right = database.begin(7, 11).unwrap();
        database.put(&mut left, key(1), b"left".to_vec()).unwrap();
        database.put(&mut right, key(1), b"right".to_vec()).unwrap();
        database.commit(left).unwrap();
        assert_eq!(
            database.commit(right).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn leaf_range_witness_prevents_phantom() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"one".to_vec()).unwrap();
        database.put(&mut seed, key(9), b"nine".to_vec()).unwrap();
        database.commit(seed).unwrap();

        let mut scanner = database.begin(7, 11).unwrap();
        assert_eq!(
            database.scan(&mut scanner, &key(2), &key(8), 10).unwrap(),
            vec![]
        );
        let mut writer = database.begin(7, 11).unwrap();
        database
            .put(&mut writer, key(5), b"phantom".to_vec())
            .unwrap();
        database.commit(writer).unwrap();
        database
            .put(&mut scanner, key(3), b"dependent".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(scanner).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn variable_length_keys_scan_in_raw_byte_order_and_witness_phantoms() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let variable_key = |raw: &[u8]| EntityKey::new(7, 11, "records", raw).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for raw in [b"a".as_slice(), b"ab", b"b", "鲸".as_bytes()] {
            database
                .put(&mut seed, variable_key(raw), raw.to_vec())
                .unwrap();
        }
        database.commit(seed).unwrap();

        let (prefix_start, prefix_end) = EntityKey::prefix_range(7, 11, "records", b"a").unwrap();
        let mut scan = database.begin(7, 11).unwrap();
        let rows = database
            .scan(&mut scan, &prefix_start, &prefix_end, 10)
            .unwrap();
        assert_eq!(
            rows.iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"a".as_slice(), b"ab"]
        );
        let mut reverse_scan = database.begin(7, 11).unwrap();
        let reverse_rows = database
            .scan_reverse(&mut reverse_scan, &prefix_start, &prefix_end, 10)
            .unwrap();
        assert_eq!(
            reverse_rows
                .iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"ab".as_slice(), b"a"]
        );

        let mut writer = database.begin(7, 11).unwrap();
        database
            .put(&mut writer, variable_key(b"aa"), b"phantom".to_vec())
            .unwrap();
        database.commit(writer).unwrap();
        database
            .put(&mut scan, variable_key(b"dependent"), b"value".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(scan).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        database
            .put(
                &mut reverse_scan,
                variable_key(b"reverse-dependent"),
                b"value".to_vec(),
            )
            .unwrap();
        assert_eq!(
            database.commit(reverse_scan).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );

        let (binary_start, binary_end) =
            EntityKey::prefix_range(7, 11, "records", &[0xff, 0xff]).unwrap();
        assert!(binary_start < binary_end);
        assert_eq!(&binary_start.key_bytes()[..2], &[0xff, 0xff]);
        assert!(EntityKey::new(7, 11, "records", &vec![0; MAX_ENTITY_KEY_BYTES]).is_ok());
        assert!(EntityKey::new(7, 11, "records", &vec![0; MAX_ENTITY_KEY_BYTES + 1]).is_err());
    }

    #[test]
    fn staged_reads_keep_the_visible_snapshot_witness_and_detect_conflicts() {
        for overlay in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            database.set_write_overlay_enabled(overlay);
            let mut seed = database.begin(7, 11).unwrap();
            database.put(&mut seed, key(1), b"original".to_vec()).unwrap();
            database.commit(seed).unwrap();
            let mut reader = database.begin(7, 11).unwrap();
            assert_eq!(database.get(&mut reader, &key(1)).unwrap(), Some(b"original".to_vec()));
            let witnessed = reader.point_witnesses.get(key(1).encoded()).unwrap().version;
            drop(reader);

            let mut staged = database.begin(7, 11).unwrap();
            database.put(&mut staged, key(1), b"staged".to_vec()).unwrap();
            for _ in 0..3 {
                assert_eq!(database.get(&mut staged, &key(1)).unwrap(), Some(b"staged".to_vec()));
                assert_eq!(staged.point_witnesses.get(key(1).encoded()).unwrap().version, witnessed);
            }
            database.delete(&mut staged, key(1)).unwrap();
            assert_eq!(database.get(&mut staged, &key(1)).unwrap(), None);
            assert_eq!(staged.point_witnesses.get(key(1).encoded()).unwrap().version, witnessed);

            let mut other = database.begin(7, 11).unwrap();
            database.put(&mut other, key(1), b"concurrent".to_vec()).unwrap();
            database.commit(other).unwrap();
            assert_eq!(database.commit(staged).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        }
    }

    #[test]
    fn transaction_reads_its_own_point_and_range_writes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let variable_key = |raw: &[u8]| EntityKey::new(7, 11, "records", raw).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for raw in [b"a".as_slice(), b"b", b"c"] {
            database
                .put(&mut seed, variable_key(raw), raw.to_vec())
                .unwrap();
        }
        database.commit(seed).unwrap();

        let mut transaction = database.begin(7, 11).unwrap();
        database
            .put(&mut transaction, variable_key(b"a"), b"new-a".to_vec())
            .unwrap();
        database
            .delete(&mut transaction, variable_key(b"b"))
            .unwrap();
        database
            .put(&mut transaction, variable_key(b"aa"), b"new-aa".to_vec())
            .unwrap();
        assert_eq!(
            database.get(&mut transaction, &variable_key(b"a")).unwrap(),
            Some(b"new-a".to_vec())
        );
        assert_eq!(
            database.get(&mut transaction, &variable_key(b"b")).unwrap(),
            None
        );
        let (start, end) = EntityKey::prefix_range(7, 11, "records", b"").unwrap();
        let rows = database.scan(&mut transaction, &start, &end, 2).unwrap();
        assert_eq!(
            rows.iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"a".as_slice(), b"aa"]
        );
        database.commit(transaction).unwrap();

        let mut delete_first = database.begin(7, 11).unwrap();
        database
            .delete(&mut delete_first, variable_key(b"a"))
            .unwrap();
        let rows = database.scan(&mut delete_first, &start, &end, 2).unwrap();
        assert_eq!(
            rows.iter()
                .map(|(key, _)| key.key_bytes())
                .collect::<Vec<_>>(),
            vec![b"aa".as_slice(), b"c"]
        );
    }

    #[test]
    fn transaction_owner_scope_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let owner_a = EntityKey::new(1, 10, "settings", b"theme").unwrap();
        let owner_b = EntityKey::new(1, 20, "settings", b"theme").unwrap();
        let mut write = database.begin(1, 10).unwrap();
        database
            .put(&mut write, owner_a.clone(), b"dark".to_vec())
            .unwrap();
        assert_eq!(
            database
                .put(&mut write, owner_b.clone(), b"light".to_vec())
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        database.commit(write).unwrap();
        let mut read = database.begin(1, 10).unwrap();
        assert_eq!(database.get(&mut read, &owner_a).unwrap().unwrap(), b"dark");
        assert_eq!(
            database.get(&mut read, &owner_b).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn additional_scope_allows_its_prefix_scan_without_authorizing_adjacent_namespace() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let allowed = EntityKey::new(7, 99, "global_a", b"one").unwrap();
        let adjacent = EntityKey::new(7, 99, "global_b", b"secret").unwrap();
        let mut seed = database.begin(7, 99).unwrap();
        database
            .put(&mut seed, allowed.clone(), b"visible".to_vec())
            .unwrap();
        database
            .put(&mut seed, adjacent.clone(), b"hidden".to_vec())
            .unwrap();
        database.commit(seed).unwrap();

        let prefix = EntityKey::new(7, 99, "global_a", b"").unwrap();
        let mut scoped = database
            .begin_with_additional_scope_prefixes(7, 11, vec![prefix.encoded().to_vec()])
            .unwrap();
        let (start, end) = EntityKey::prefix_range(7, 99, "global_a", b"").unwrap();
        let rows = database.scan(&mut scoped, &start, &end, 10).unwrap();
        assert_eq!(rows, vec![(allowed, b"visible".to_vec())]);
        assert_eq!(
            database.get(&mut scoped, &adjacent).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn multiplexed_transaction_recovers_entity_root_with_other_families() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut database = EntityDatabase::initialize(directory.path()).unwrap();
            database.set_write_overlay_enabled(false);
            let mut write = database.begin(7, 11).unwrap();
            database
                .put(&mut write, key(1), b"durable".to_vec())
                .unwrap();
            database.commit(write).unwrap();

            let sequence = database.writer_engine().unwrap().next_sequence().unwrap();
            let root = database.root.unwrap();
            let entity_record = encode_root(sequence, Some(root));
            let inline = encode_family_records(&[
                FamilyRecord {
                    kind: FamilyRecordKind::EntityRoot,
                    payload: &entity_record,
                },
                FamilyRecord {
                    kind: FamilyRecordKind::CommandReceipt,
                    payload: b"receipt",
                },
            ])
            .unwrap();
            database
                .writer_engine_mut()
                .unwrap()
                .commit_references_with_authority_state(&inline, &[root], Some(root))
                .unwrap();
        }
        let mut database = EntityDatabase::open(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &key(1)).unwrap().unwrap(),
            b"durable"
        );
        assert_eq!(database.snapshot().sequence, 2);
    }

    #[test]
    fn current_entity_root_reopens_without_reading_superseded_history_segments() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let first_history;
        {
            let mut database =
                EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                    .unwrap();
            database.set_write_overlay_enabled(false);
            let mut first = database.begin(7, 11).unwrap();
            database.put(&mut first, key(1), b"first".to_vec()).unwrap();
            database.commit(first).unwrap();
            database.writer_engine_mut().unwrap().checkpoint().unwrap();
            first_history = database.writer_engine().unwrap().history_segment_block_ids()[0];

            let mut second = database.begin(7, 11).unwrap();
            database
                .put(&mut second, key(2), b"second".to_vec())
                .unwrap();
            database.commit(second).unwrap();
            database.writer_engine_mut().unwrap().checkpoint().unwrap();
            assert_eq!(database.writer_engine().unwrap().history_segment_block_ids().len(), 2);
        }

        let hexadecimal = first_history.to_hex();
        let path = std::path::Path::new("/data")
            .join("blocks")
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"));
        let mut file = vfs
            .open(
                &path,
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(16, b"X").unwrap();
        file.sync_all().unwrap();

        let mut database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();

        database.set_write_overlay_enabled(false);

        database.set_write_overlay_enabled(false);
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &key(1)).unwrap(),
            Some(b"first".to_vec())
        );
        assert_eq!(
            database.get(&mut read, &key(2)).unwrap(),
            Some(b"second".to_vec())
        );
        assert_eq!(
            database.writer_engine().unwrap().transaction_at(1).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn open_reads_only_the_root_and_descendants_fail_lazily() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let child_id;
        let child_key;
        {
            let mut database =
                EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                    .unwrap();
            // The corruption target below is a loose block file, so the seed
            // commit must spill: carried bytes past the ceiling materialize
            // every staged page as a loose file up front, and the checkpoint
            // retires the WAL copy.
            let mut seed = database.begin(7, 11).unwrap();
            for index in 0..300_u16 {
                database
                    .put(&mut seed, key(index), vec![index as u8; 8 * 1024])
                    .unwrap();
            }
            database.commit(seed).unwrap();
            database.writer_engine_mut().unwrap().checkpoint().unwrap();
            let root = database.snapshot().root.unwrap();
            let Page::Internal { children, .. } =
                Page::decode(&database.writer_engine().unwrap().read_block(root).unwrap()).unwrap()
            else {
                panic!("seeded entity root is not internal");
            };
            child_id = children[0].block_id;
            child_key = EntityKey(children[0].lower_bound.clone());
        }

        let hexadecimal = child_id.to_hex();
        let path = std::path::Path::new("/data")
            .join("blocks")
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"));
        let mut file = vfs
            .open(
                &path,
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(16, b"X").unwrap();
        file.sync_all().unwrap();

        let mut database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();

        database.set_write_overlay_enabled(false);
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &child_key).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn single_key_commit_read_work_is_bounded_by_tree_depth() {
        const MAXIMUM_POINT_COMMIT_READS: usize = 32;
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..256_u16 {
            database
                .put(&mut seed, key(index), vec![index as u8; 8 * 1024])
                .unwrap();
        }
        database.commit(seed).unwrap();

        vfs.arm_fault(None).unwrap();
        let mut update = database.begin(7, 11).unwrap();
        database
            .put(&mut update, key(127), b"updated".to_vec())
            .unwrap();
        database.commit(update).unwrap();
        let reads = vfs
            .trace()
            .unwrap()
            .into_iter()
            .filter(|operation| operation == &crate::vfs::Operation::Read)
            .count();
        assert!(
            reads <= MAXIMUM_POINT_COMMIT_READS,
            "single-key commit performed {reads} page/file reads"
        );
    }

    #[test]
    fn decoded_page_cache_admits_verified_misses_within_byte_budget() {
        let mut cache = DecodedPageCache::new(1_000);
        let node = || Arc::new(RootNode::Page(Page::Leaf(Vec::new())));
        // charge = encoded_len * PAGE_CACHE_DECODED_EXPANSION + 256
        cache.insert(BlockId([1; 32]), node(), 100);
        cache.insert(BlockId([2; 32]), node(), 100);
        assert!(cache.get(BlockId([1; 32])).is_none());
        assert!(cache.get(BlockId([2; 32])).is_some());
        cache.insert(BlockId([3; 32]), node(), 50);
        assert!(cache.get(BlockId([2; 32])).is_none());
        assert!(cache.get(BlockId([3; 32])).is_some());
        cache.insert(BlockId([3; 32]), node(), 50);
        assert_eq!(cache.resident_bytes, 456);
        cache.insert(BlockId([9; 32]), node(), 4_096);
        assert!(cache.get(BlockId([9; 32])).is_none());
        cache.set_capacity_bytes(0);
        assert_eq!(cache.resident_bytes, 0);
        assert!(cache.get(BlockId([3; 32])).is_none());
    }

    #[test]
    fn decoded_page_cache_hot_reads_use_one_clock_slot_and_receive_second_chance() {
        let mut cache = DecodedPageCache::new(2 * 656);
        let node = || Arc::new(RootNode::Page(Page::Leaf(Vec::new())));
        cache.insert(BlockId([1; 32]), node(), 100);
        cache.insert(BlockId([2; 32]), node(), 100);
        for _ in 0..100_000 {
            assert!(cache.get(BlockId([1; 32])).is_some());
            assert!(cache.clock.len() == cache.entries.len());
        }
        assert_eq!(cache.resident_bytes, 1312);
        cache.insert(BlockId([3; 32]), node(), 100);
        assert!(cache.get(BlockId([2; 32])).is_none());
        assert!(cache.get(BlockId([1; 32])).is_some());
        assert!(cache.get(BlockId([3; 32])).is_some());
        cache.set_capacity_bytes(0);
        assert!(cache.entries.is_empty());
        assert!(cache.clock.is_empty());
        assert_eq!(cache.clock.capacity(), 0);
    }

    #[test]
    fn decoded_page_cache_hits_progress_with_an_existing_reader_and_survive_eviction() {
        let directory = tempfile::tempdir().unwrap();
        let database = EntityDatabase::initialize(directory.path()).unwrap();
        let id = BlockId([77; 32]);
        let node = Arc::new(RootNode::Page(Page::Leaf(Vec::new())));
        database.page_cache.write().unwrap().insert(id, Arc::clone(&node), 100);
        let cache = &database.page_cache;
        let replica_backend = ReplicaBlockIo {
            blocks: database.writer_engine().unwrap().block_store_shared(),
            restart_required: database.writer_engine().unwrap().restart_flag(),
        };
        let backend = &replica_backend;
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        std::thread::scope(|scope| {
            let existing_reader = cache.read().unwrap();
            for _ in 0..8 {
                let done_tx = done_tx.clone();
                scope.spawn(move || {
                    let tree = Tree { backend, page_cache: Some(cache) };
                    done_tx.send(tree.load_node(id)).unwrap();
                });
            }
            let mut observed = Vec::new();
            for _ in 0..8 {
                match done_rx.recv_timeout(std::time::Duration::from_secs(2)) {
                    Ok(result) => observed.push(result),
                    Err(_) => break,
                }
            }
            // Release even on failure, so a broken exclusive hit path can exit.
            drop(existing_reader);
            assert_eq!(observed.len(), 8, "cache hits serialized behind a reader");
            for result in observed { assert!(Arc::ptr_eq(&result.unwrap(), &node)); }
        });
        database.set_page_cache_capacity_bytes(0).unwrap();
        assert!(cache.read().unwrap().get(id).is_none());
        assert!(matches!(&*node, RootNode::Page(Page::Leaf(entries)) if entries.is_empty()));
    }

    #[test]
    fn operations_remain_correct_without_decoded_page_cache_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_page_cache_capacity_bytes(0).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..64_u16 {
            database
                .put(&mut seed, key(index), vec![index as u8; 128])
                .unwrap();
        }
        database.commit(seed).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &key(17)).unwrap(),
            Some(vec![17; 128])
        );
        let rows = database.scan(&mut read, &key(10), &key(20), 16).unwrap();
        assert_eq!(rows.len(), 10);
        let mut update = database.begin(7, 11).unwrap();
        database.put(&mut update, key(17), b"new".to_vec()).unwrap();
        database.commit(update).unwrap();
        let mut reread = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut reread, &key(17)).unwrap(),
            Some(b"new".to_vec())
        );
    }

    #[test]
    fn stale_snapshot_still_conflicts_after_current_root_fast_path() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database.put(&mut seed, key(1), b"one".to_vec()).unwrap();
        database.commit(seed).unwrap();
        let mut stale = database.begin(7, 11).unwrap();
        database.get(&mut stale, &key(1)).unwrap();
        let mut intervening = database.begin(7, 11).unwrap();
        database
            .put(&mut intervening, key(1), b"two".to_vec())
            .unwrap();
        database.commit(intervening).unwrap();
        let mut current = database.begin(7, 11).unwrap();
        database
            .put(&mut current, key(2), b"second".to_vec())
            .unwrap();
        database.commit(current).unwrap();
        database.put(&mut stale, key(1), b"stale".to_vec()).unwrap();
        let error = database.commit(stale).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert_eq!(error.to_string(), "entity point witness changed");
    }

    // ── Snapshot read replica (2026-09-18, docs/STORAGE_TOFUDB.md) ──

    fn seeded_database() -> (tempfile::TempDir, EntityDatabase) {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .put(&mut transaction, key(1), b"committed-one".to_vec())
            .unwrap();
        database.commit(transaction).unwrap();
        (directory, database)
    }

    #[test]
    fn read_replica_serves_the_pinned_snapshot_while_the_writer_advances() {
        let (_directory, mut database) = seeded_database();
        let replica = database.build_read_replica().unwrap();
        let pinned = replica.snapshot();

        // The writer commits a newer state after the replica was built.
        let mut advance = database.begin(7, 11).unwrap();
        database
            .put(&mut advance, key(1), b"committed-two".to_vec())
            .unwrap();
        database
            .put(&mut advance, key(2), b"committed-two-new".to_vec())
            .unwrap();
        database.commit(advance).unwrap();
        assert!(database.snapshot().sequence > pinned.sequence);

        // The replica still serves its pinned snapshot, byte-identical.
        let mut read = replica.begin(7, 11).unwrap();
        assert_eq!(
            replica.get(&mut read, &key(1)).unwrap(),
            Some(b"committed-one".to_vec())
        );
        assert_eq!(replica.get(&mut read, &key(2)).unwrap(), None);

        // A fresh replica observes the advanced head.
        let newer = database.build_read_replica().unwrap();
        let mut read = newer.begin(7, 11).unwrap();
        assert_eq!(
            newer.get(&mut read, &key(1)).unwrap(),
            Some(b"committed-two".to_vec())
        );
        assert_eq!(
            newer.get(&mut read, &key(2)).unwrap(),
            Some(b"committed-two-new".to_vec())
        );
    }

    #[test]
    fn read_replica_mutation_paths_fail_closed() {
        let (_directory, database) = seeded_database();
        let mut replica = database.build_read_replica().unwrap();
        assert!(replica.is_read_replica());

        // Staging into a replica transaction is harmless by itself; every
        // engine-touching mutation entry must fail closed.
        let mut transaction = replica.begin(7, 11).unwrap();
        replica
            .put(&mut transaction, key(9), b"replica-write".to_vec())
            .unwrap();
        let error = replica.commit(transaction).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);

        let mut read = replica.begin(7, 11).unwrap();
        assert_eq!(replica.get(&mut read, &key(9)).unwrap(), None);
    }

    #[test]
    fn readers_and_writers_register_without_shared_count_refusals() {
        let (_directory, database) = seeded_database();
        let replicas: Vec<_> = (0..512)
            .map(|_| database.build_read_replica().unwrap()).collect();
        let writer = database.begin(7, 11).unwrap();
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 513);
        drop(writer);
        drop(replicas);
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
    }

    #[test]
    fn replica_transactions_lease_one_pin_handle() {
        let (_directory, database) = seeded_database();
        let before = database.snapshot_pin_metrics().unwrap().active_handles;
        let replica = database.build_read_replica().unwrap();
        assert_eq!(
            database.snapshot_pin_metrics().unwrap().active_handles,
            before + 1
        );
        let first = replica.begin(7, 11).unwrap();
        let second = replica.begin(7, 11).unwrap();
        assert_eq!(
            database.snapshot_pin_metrics().unwrap().active_handles,
            before + 1,
            "leases of the replica root pin must not consume extra handles"
        );
        drop(first);
        drop(second);
        drop(replica);
        assert_eq!(
            database.snapshot_pin_metrics().unwrap().active_handles,
            before
        );
    }

    #[test]
    fn restart_verdict_stops_replica_reads_and_new_replica_builds() {
        let (_directory, database) = seeded_database();
        let replica = database.build_read_replica().unwrap();
        let mut read = replica.begin(7, 11).unwrap();
        database
            .writer_engine()
            .unwrap()
            .restart_flag()
            .store(true, std::sync::atomic::Ordering::Relaxed);
        assert!(replica.get(&mut read, &key(1)).is_err());
        assert!(replica.begin(7, 11).is_err());
        assert!(database.build_read_replica().is_err());
        database
            .writer_engine()
            .unwrap()
            .restart_flag()
            .store(false, std::sync::atomic::Ordering::Relaxed);
    }

    #[test]
    fn direct_entity_commit_on_the_overlay_path_persists_and_replays() {
        // Regression for the 2026-09-20 stream-cursor investigation: a direct
        // EntityDatabase::commit on the deferred-application path used to
        // silently discard plain writes (early return before the WAL envelope
        // and the in-memory install).
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
        let mut database =
            EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), dyn_vfs).unwrap();
        database.set_write_overlay_enabled(true);

        let mut write = database.begin(7, 11).unwrap();
        database
            .put(&mut write, key(7), b"direct-commit-value".to_vec())
            .unwrap();
        database.commit(write).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut read, &key(7)).unwrap(),
            Some(b"direct-commit-value".to_vec()),
            "direct EntityDatabase::commit lost the write"
        );
        drop(database);

        let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
        let mut reopened =
            EntityDatabase::open_with_vfs(std::path::Path::new("/data"), dyn_vfs).unwrap();
        reopened.set_write_overlay_enabled(true);
        let mut read = reopened.begin(7, 11).unwrap();
        assert_eq!(
            reopened.get(&mut read, &key(7)).unwrap(),
            Some(b"direct-commit-value".to_vec()),
            "reopen did not replay the direct-commit EntityOverlay record"
        );
        // A corruption write through the direct path must become visible to
        // readers (the fail-closed case that flagged the investigation).
        let mut corrupt = reopened.begin(7, 11).unwrap();
        reopened
            .put(&mut corrupt, key(7), b"corrupt".to_vec())
            .unwrap();
        reopened.commit(corrupt).unwrap();
        let mut read = reopened.begin(7, 11).unwrap();
        assert_eq!(reopened.get(&mut read, &key(7)).unwrap(), Some(b"corrupt".to_vec()));
    }

    #[test]
    fn indexed_rotation_replays_pending_overlay_without_active_wal() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database = EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
        database.set_write_overlay_enabled(true);
        let mut tx = database.begin(7, 11).unwrap();
        database.put(&mut tx, key(7), b"acknowledged overlay".to_vec()).unwrap();
        database.commit(tx).unwrap();
        assert!(database.overlay.read().unwrap().entries() > 0);
        let engine = database.writer_engine_mut().unwrap();
        engine.attach_rotation_driver();
        let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
            crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
            other => panic!("expected rotation, got {other:?}"),
        };
        let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
        engine.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap();
        assert_eq!(engine.state().durable_sequence, engine.state().checkpoint_sequence);
        std::mem::forget(database);
        vfs.crash().unwrap();
        let reopened = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut read = reopened.begin(7, 11).unwrap();
        assert_eq!(reopened.get(&mut read, &key(7)).unwrap(), Some(b"acknowledged overlay".to_vec()));
    }

    #[test]
    fn rotation_fence_rejects_overlay_merge_before_any_materialization_io() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database = EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
        database.set_write_overlay_enabled(true);
        let mut tx = database.begin(7, 11).unwrap();
        for index in 0..128 { database.put(&mut tx, key(index), vec![1; 32]).unwrap(); }
        database.commit(tx).unwrap();
        let engine = database.writer_engine_mut().unwrap();
        let store = engine.block_store_shared();
        let registration = store.register_background_packer().unwrap();
        let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
            crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
            other => panic!("expected rotation, got {other:?}"),
        };
        let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
        // Capture a legacy single-WAL snapshot before attaching the driver:
        // its install still retires window writes and requires this fence.
        engine.attach_rotation_driver();
        engine.commit_transaction(b"window", &[b"pending payload"]).unwrap();
        let _adoption = engine.prepare_pack_publication_for_test();
        assert_eq!(engine.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        vfs.arm_fault(None).unwrap();
        let before = database.overlay.read().unwrap().entries();
        let staged_before = store.pending_staged_bytes().unwrap();
        assert_eq!(database.merge_overlay().unwrap_err().kind(), io::ErrorKind::ResourceBusy);
        assert_eq!(store.pending_staged_bytes().unwrap(), staged_before,
            "a fenced merge must not stage unreachable pages before rejecting publication");
        assert!(vfs.trace().unwrap().is_empty(), "a fenced merge must not build pages before discovering it cannot publish");
        assert_eq!(database.overlay.read().unwrap().entries(), before);
        drop(registration);
    }

    #[test]
    fn overlay_merge_slices_large_values_before_the_loose_fsync_spill() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database = EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
        database.set_write_overlay_enabled(true);
        for start in (0..512).step_by(16) {
            let mut tx = database.begin(7, 11).unwrap();
            for index in start..start + 16 { database.put(&mut tx, key(index), vec![index as u8; MAX_ENTITY_INLINE_VALUE_BYTES]).unwrap(); }
            database.commit(tx).unwrap();
        }
        assert_eq!(database.overlay.read().unwrap().entries(), 512);
        let store = database.writer_engine().unwrap().block_store_shared();
        let registration = store.register_background_packer().unwrap();
        database.merge_chunk_entries_adaptive.store(512, std::sync::atomic::Ordering::Release);
        vfs.arm_fault(None).unwrap();
        assert!(database.merge_overlay().unwrap());
        let barriers = vfs.trace().unwrap().iter().filter(|operation|
            matches!(operation, Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory)).count();
        assert!(barriers <= 4, "one overlay slice spilled into {barriers} serialized durability barriers");
        assert!(database.overlay.read().unwrap().entries() > 0, "large values require several carried-WAL slices");
        database.merge_overlay_fully().unwrap();
        drop(registration);
        drop(database);
        vfs.crash().unwrap();
        let reopened = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let mut read = reopened.begin(7, 11).unwrap();
        for index in 0..512 { assert_eq!(reopened.get(&mut read, &key(index)).unwrap(), Some(vec![index as u8; MAX_ENTITY_INLINE_VALUE_BYTES])); }
    }

    #[test]
    fn underfilled_merge_cannot_inflate_unmeasured_capacity() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        let mut tx = database.begin(7, 11).unwrap();
        database.put(&mut tx, key(1), vec![1; 32]).unwrap();
        database.commit(tx).unwrap();
        let before = database.merge_chunk_entries_adaptive.load(std::sync::atomic::Ordering::Acquire);
        database.merge_overlay().unwrap();
        let after = database.merge_chunk_entries_adaptive.load(std::sync::atomic::Ordering::Acquire);
        assert!(after <= before, "a one-entry slice does not prove a larger batch fits");
    }

    #[test]
    fn cold_open_measures_small_merge_before_growing() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        let initial = EntityDatabase::MERGE_CHUNK_ENTRIES_INITIAL;
        assert_eq!(database.merge_chunk_entries_adaptive.load(std::sync::atomic::Ordering::Acquire), initial);
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..256 { database.put(&mut transaction, key(index), vec![1; 32]).unwrap(); }
        database.commit(transaction).unwrap();
        let before = database.overlay.read().unwrap().entries();
        assert!(before >= 256);
        assert!(database.merge_overlay().unwrap());
        assert_eq!(database.overlay.read().unwrap().entries(), before - initial);
        drop(database);
        let mut reopened = EntityDatabase::open(directory.path()).unwrap();
        assert_eq!(reopened.merge_chunk_entries_adaptive.load(std::sync::atomic::Ordering::Acquire), initial);
        reopened.merge_overlay_fully().unwrap();
        let mut read = reopened.begin(7, 11).unwrap();
        for index in 0..256 { assert_eq!(reopened.get(&mut read, &key(index)).unwrap(), Some(vec![1; 32])); }
    }

    #[test]
    fn merge_chunk_aimd_halves_past_target_and_grows_back() {
        use std::time::Duration;
        let max = EntityDatabase::MERGE_CHUNK_ENTRIES;
        let min = EntityDatabase::MERGE_CHUNK_ENTRIES_MIN;
        let target = EntityDatabase::MERGE_CHUNK_TIME_TARGET;
        // Slow merge window (a serialized FUSE chunk): halve, floored.
        assert_eq!(
            EntityDatabase::next_merge_chunk_entries(max, target + Duration::from_millis(1)),
            max / 2
        );
        assert_eq!(
            EntityDatabase::next_merge_chunk_entries(min, target * 10),
            min
        );
        // Comfortably fast: grow x1.5, capped at the full chunk.
        assert_eq!(
            EntityDatabase::next_merge_chunk_entries(64, target / 4),
            96
        );
        assert_eq!(
            EntityDatabase::next_merge_chunk_entries(max - 1, Duration::ZERO),
            max
        );
        assert_eq!(EntityDatabase::next_merge_chunk_entries(1, Duration::ZERO), 2,
            "a lean start must grow when the host can sustain it");
        // Inside the band: hold steady (no oscillation around the target).
        assert_eq!(
            EntityDatabase::next_merge_chunk_entries(512, target),
            512
        );
    }

    #[test]
    fn merge_chunk_adaptive_size_converges_and_preserves_content() {
        // A merge sequence driven with a tiny adaptive floor must produce
        // exactly the same tree content as one full-size merge: the entry
        // count is a batching decision, never a semantic one.
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..300_u16 {
            database
                .put(&mut transaction, key(index), format!("value-{index}").into_bytes())
                .unwrap();
        }
        database.commit(transaction).unwrap();
        database
            .merge_chunk_entries_adaptive
            .store(64, std::sync::atomic::Ordering::Release);
        database.merge_overlay_fully().unwrap();
        let merged_root = database.root;
        drop(database);
        let reopened = EntityDatabase::open(directory.path()).unwrap();
        assert_eq!(reopened.root, merged_root);
        let mut read = reopened.begin(7, 11).unwrap();
        for index in 0..300_u16 {
            let expected = format!("value-{index}").into_bytes();
            let got = reopened.get(&mut read, &key(index)).unwrap();
            assert_eq!(got.as_deref(), Some(expected.as_slice()));
        }
    }

    #[test]
    fn tombstone_reclaim_flag_defaults_off_and_parses_explicit_enable() {
        assert!(!parse_tombstone_reclaim_enabled(None));
        assert!(!parse_tombstone_reclaim_enabled(Some("0".to_string())));
        assert!(!parse_tombstone_reclaim_enabled(Some("true".to_string())));
        assert!(parse_tombstone_reclaim_enabled(Some("1".to_string())));
    }

    /// L1 (`docs/TOFUDB_TOMBSTONE_COMPACTION.md`): a leaf repack drops
    /// untouched tombstones below the watermark, and reclaims nothing when the
    /// watermark is 0 (the default, feature-off path).
    #[test]
    fn leaf_repack_reclaims_tombstones_below_the_watermark() {
        fn leaf_tombstones(tree: &Tree, root: Option<BlockId>) -> usize {
            let (_, leaves) = tree
                .scan(root, key(0).encoded(), key(300).encoded(), 1_000, &mut 0)
                .unwrap();
            let mut count = 0;
            for id in leaves {
                if let RootNode::Page(Page::Leaf(entries)) = &*tree.load_node(id).unwrap() {
                    count += entries
                        .iter()
                        .filter(|(_, value)| value.value.is_none())
                        .count();
                }
            }
            count
        }

        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        // Synchronous tree path: deletes materialize as physical tombstones.
        database.set_write_overlay_enabled(false);
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..8_u16 {
            database.put(&mut seed, key(index), vec![index as u8; 3]).unwrap();
        }
        database.commit(seed).unwrap();
        let mut remove = database.begin(7, 11).unwrap();
        for index in 0..4_u16 {
            database.delete(&mut remove, key(index)).unwrap();
        }
        database.commit(remove).unwrap();

        let root = database.snapshot().root;
        let tree = Tree {
            backend: database.writer_engine().unwrap(),
            page_cache: Some(&database.page_cache),
        };
        assert_eq!(leaf_tombstones(&tree, root), 4);

        // Watermark above the tombstone versions but below the touching
        // write's own commit version: untouched tombstones are reclaimed.
        let touch = |value: u8| {
            vec![(key(7).encoded().to_vec(), Some(vec![value; 3]), u64::MAX)]
        };
        let (reclaimed_root, _) = tree.apply_batch(root, touch(9), TombstoneReclaim { below: u64::MAX, mounts: &[] }).unwrap();
        assert_eq!(leaf_tombstones(&tree, reclaimed_root), 0);

        // Watermark 0 => reclaim nothing (the default feature-off behavior).
        let (untouched_root, _) = tree.apply_batch(root, touch(10), TombstoneReclaim { below: 0, mounts: &[] }).unwrap();
        assert_eq!(leaf_tombstones(&tree, untouched_root), 4);
    }

    fn audit_put(database: &mut EntityDatabase, index: u16, value: Option<&[u8]>) {
        let mut tx = database.begin(7, 11).unwrap();
        match value {
            Some(value) => database.put(&mut tx, key(index), value.to_vec()).unwrap(),
            None => database.delete(&mut tx, key(index)).unwrap(),
        }
        database.commit(tx).unwrap();
    }

    fn audit_value(database: &EntityDatabase, index: u16) -> Option<Vec<u8>> {
        let mut tx = database.begin(7, 11).unwrap();
        database.get(&mut tx, &key(index)).unwrap()
    }

    #[test]
    fn audit_partial_merge_reopen_keeps_acknowledged_tail() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        for index in 1..=3 {
            audit_put(&mut database, index, Some(b"acknowledged"));
        }
        database.merge_chunk_entries_adaptive.store(1, std::sync::atomic::Ordering::Release);
        assert!(database.merge_overlay().unwrap());
        drop(database);
        let database = EntityDatabase::open(directory.path()).unwrap();
        for index in 1..=3 {
            assert_eq!(audit_value(&database, index), Some(b"acknowledged".to_vec()));
        }
    }

    fn audit_snapshot_survives_merge(partial: bool) {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        audit_put(&mut database, 1, Some(b"old"));
        audit_put(&mut database, 2, Some(b"tail"));
        let replica = database.build_read_replica().unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        if partial {
            database.merge_chunk_entries_adaptive.store(1, std::sync::atomic::Ordering::Release);
            database.merge_overlay().unwrap();
        } else {
            database.merge_overlay_fully().unwrap();
        }
        assert_eq!(database.get(&mut transaction, &key(1)).unwrap(), Some(b"old".to_vec()));
        assert_eq!(audit_value(&replica, 1), Some(b"old".to_vec()));
        audit_put(&mut database, 1, Some(b"new"));
        database.merge_overlay_fully().unwrap();
        assert_eq!(audit_value(&replica, 1), Some(b"old".to_vec()));
        assert_eq!(audit_value(&database, 1), Some(b"new".to_vec()));
        assert_eq!(replica.scan(&mut replica.begin(7, 11).unwrap(), &key(0), &key(3), 2).unwrap().len(), 2);
    }

    #[test]
    fn audit_head_snapshot_survives_full_merge_without_new_write() {
        audit_snapshot_survives_merge(false);
    }

    #[test]
    fn audit_head_snapshot_survives_partial_merge_below_pin() {
        audit_snapshot_survives_merge(true);
    }

    fn audit_mounted_database(path: &std::path::Path) -> EntityDatabase {
        let mut database = EntityDatabase::initialize(path).unwrap();
        database.set_write_overlay_enabled(false);
        audit_put(&mut database, 10, Some(b"mounted"));
        let ranges = [(key(10), key(11))];
        let mut tx = database.begin(7, 11).unwrap();
        database.stage_persistent_range_snapshot_pin(&mut tx, b"audit", &ranges).unwrap();
        database.retire_range(&mut tx, &ranges[0].0, &ranges[0].1).unwrap();
        database.commit(tx).unwrap();
        let mut tx = database.begin(7, 11).unwrap();
        database.stage_persistent_range_snapshot_restore(&mut tx, b"audit", &ranges).unwrap();
        database.commit(tx).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        database
    }

    #[test]
    fn audit_vacuum_preserves_mounted_directory_after_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = audit_mounted_database(directory.path());
        audit_put(&mut database, 1, Some(b"vacuum"));
        audit_put(&mut database, 1, None);
        let result = database.reclaim_tombstones_in_range(key(0).encoded(), key(2).encoded(), 100).unwrap();
        assert!(result.entries_reclaimed > 0);
        assert_eq!(audit_value(&database, 10), Some(b"mounted".to_vec()));
        drop(database);
        let database = EntityDatabase::open(directory.path()).unwrap();
        assert_eq!(audit_value(&database, 10), Some(b"mounted".to_vec()));
    }

    #[test]
    fn audit_reclaim_keeps_mask_over_deleted_mount() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = audit_mounted_database(directory.path());
        audit_put(&mut database, 10, None);
        database.set_write_overlay_enabled(true);
        audit_put(&mut database, 11, Some(b"touch-same-leaf"));
        database.merge_overlay_fully().unwrap();
        assert_eq!(audit_value(&database, 10), None);
        database.reclaim_tombstones_in_range(key(0).encoded(), key(20).encoded(), 100).unwrap();
        assert_eq!(audit_value(&database, 10), None);
        drop(database);
        let database = EntityDatabase::open(directory.path()).unwrap();
        assert_eq!(audit_value(&database, 10), None);
    }

    #[test]
    fn audit_transaction_prefix_merges_are_bounded_and_survive_each_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        let mut tx = database.begin(7, 11).unwrap();
        for index in 1..=6 { database.put(&mut tx, key(index), vec![index as u8]).unwrap(); }
        database.commit(tx).unwrap();
        for remaining in (0..6).rev() {
            database.merge_chunk_entries_adaptive.store(1, std::sync::atomic::Ordering::Release);
            database.merge_overlay().unwrap();
            assert_eq!(database.overlay.read().unwrap().entries(), remaining);
            // Exercise history replay when a checkpoint lands on a partial
            // transaction as well as normal active-WAL replay.
            if remaining == 3 { database.writer_engine_mut().unwrap().checkpoint().unwrap(); }
            drop(database);
            database = EntityDatabase::open(directory.path()).unwrap();
            assert_eq!(database.overlay.read().unwrap().entries(), remaining);
            for index in 1..=6 { assert_eq!(audit_value(&database, index), Some(vec![index as u8])); }
        }
    }

    #[test]
    fn audit_partial_merge_faults_recover_the_whole_logical_transaction() {
        fn setup() -> (Arc<DeterministicVfs>, EntityDatabase) {
            let vfs = Arc::new(DeterministicVfs::new(None));
            vfs.create_dir(std::path::Path::new("/data")).unwrap();
            vfs.sync_directory(std::path::Path::new("/")).unwrap();
            let mut database = EntityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
            database.set_write_overlay_enabled(true);
            let mut tx = database.begin(7, 11).unwrap();
            for index in 1..=3 { database.put(&mut tx, key(index), vec![index as u8]).unwrap(); }
            database.commit(tx).unwrap();
            database.merge_chunk_entries_adaptive.store(1, std::sync::atomic::Ordering::Release);
            (vfs, database)
        }
        let (vfs, mut database) = setup();
        vfs.arm_fault(None).unwrap();
        database.merge_overlay().unwrap();
        let trace = vfs.trace().unwrap();
        assert!(!trace.is_empty());
        for (offset, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
            if matches!(operation, Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory) { actions.push(FaultAction::DropSync); }
            for action in actions {
                let (vfs, mut database) = setup();
                vfs.arm_fault(Some(FaultRule { operation_number: offset as u64 + 1, action })).unwrap();
                let _ = database.merge_overlay();
                drop(database);
                vfs.arm_fault(None).unwrap();
                vfs.crash().unwrap();
                let mut database = EntityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
                for index in 1..=3 { assert_eq!(audit_value(&database, index), Some(vec![index as u8])); }
                database.merge_overlay_fully().unwrap();
                for index in 1..=3 { assert_eq!(audit_value(&database, index), Some(vec![index as u8])); }
            }
        }
    }

    #[test]
    fn audit_vacuum_preserves_mount_when_base_becomes_empty() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = audit_mounted_database(directory.path());
        // Remove the capsule's catalog records; the live mounted directory
        // itself remains the authority and must outlive an empty base.
        let tree = Tree { backend: database.backend(), page_cache: Some(&database.page_cache) };
        let mut root = tree.split_root(database.root).unwrap();
        let mut blocks = Vec::new();
        let (base, base_blocks) = tree.apply_batch(None, vec![(key(1).0, None, 1)], TombstoneReclaim { below: 0, mounts: &[] }).unwrap();
        root.base_root = base;
        blocks.extend(base_blocks);
        let next = tree.store_directory(&root, &mut blocks).unwrap();
        let record = encode_root(database.writer_engine().unwrap().next_sequence().unwrap(), Some(next));
        database.writer_engine_mut().unwrap().commit_references_with_authority_state(&record, &blocks, Some(next)).unwrap();
        database.root = Some(next);
        database.reclaim_tombstones_in_range(key(0).encoded(), key(2).encoded(), 100).unwrap();
        assert_eq!(audit_value(&database, 10), Some(b"mounted".to_vec()));
        drop(database);
        assert_eq!(audit_value(&EntityDatabase::open(directory.path()).unwrap(), 10), Some(b"mounted".to_vec()));
    }

    #[test]
    fn audit_stale_capsule_materializes_its_captured_overlay() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        audit_put(&mut database, 1, Some(b"captured"));
        let mut stale = database.begin(7, 11).unwrap();
        audit_put(&mut database, 1, Some(b"latest"));
        database.merge_overlay_fully().unwrap();
        let snapshot = database.stage_persistent_snapshot_pin(&mut stale, b"captured").unwrap();
        let mut pinned = database.begin_at_snapshot(7, 11, Vec::new(), snapshot, false).unwrap();
        assert_eq!(database.get(&mut pinned, &key(1)).unwrap(), Some(b"captured".to_vec()));
        assert_eq!(audit_value(&database, 1), Some(b"latest".to_vec()));
    }

    #[test]
    #[ignore = "explicit mixed read/write/merge measurement"]
    fn audit_mixed_load_measurement() {
        use std::time::Instant;
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        let mut tx = database.begin(7, 11).unwrap();
        for index in 1..=2048 { database.put(&mut tx, key(index), vec![1; 32]).unwrap(); }
        database.commit(tx).unwrap();
        let readers = std::thread::available_parallelism().map_or(1, |n| n.get()).min(8);
        let database = Arc::new(Mutex::new(database));
        let mut write_micros = Vec::new();
        let started = Instant::now();
        let peak_resident = std::sync::atomic::AtomicUsize::new(0);
        let read_micros = std::thread::scope(|scope| {
            let threads: Vec<_> = (0..readers).map(|_| {
                let database = Arc::clone(&database);
                scope.spawn(move || {
                    let mut times = Vec::new();
                    for _ in 0..128 {
                        let start = Instant::now();
                        let replica = database.lock().unwrap().build_read_replica().unwrap();
                        let mut tx = replica.begin(7, 11).unwrap();
                        let page = replica.scan(&mut tx, &key(0), &key(2049), 8).unwrap();
                        assert_eq!(page.len(), 8);
                        assert_eq!(page[0].0, key(1));
                        times.push(start.elapsed().as_micros());
                    }
                    times
                })
            }).collect();
            for index in 0..128 {
                let start = Instant::now();
                let mut database = database.lock().unwrap();
                audit_put(&mut database, 1, Some(&[index as u8]));
                database.merge_chunk_entries_adaptive.store(64, std::sync::atomic::Ordering::Release);
                database.merge_overlay().unwrap();
                peak_resident.fetch_max(database.overlay_memory_bytes().unwrap().1, std::sync::atomic::Ordering::Relaxed);
                write_micros.push(start.elapsed().as_micros());
            }
            threads.into_iter().flat_map(|thread| thread.join().unwrap()).collect::<Vec<_>>()
        });
        let mut read_micros = read_micros;
        read_micros.sort_unstable(); write_micros.sort_unstable();
        let mut database = database.lock().unwrap();
        database.merge_overlay_fully().unwrap();
        assert_eq!(audit_value(&database, 1), Some(vec![127]));
        assert_eq!(database.overlay_memory_bytes().unwrap(), (0, 0));
        println!("audit_mixed readers={readers} reads={} writes={} elapsed_ms={} read_p50_us={} read_p99_us={} write_p50_us={} write_p99_us={} peak_overlay_resident_bytes={}",
            read_micros.len(), write_micros.len(), started.elapsed().as_millis(),
            read_micros[read_micros.len()/2], read_micros[read_micros.len()*99/100],
            write_micros[write_micros.len()/2], write_micros[write_micros.len()*99/100],
            peak_resident.load(std::sync::atomic::Ordering::Relaxed));
    }

    #[test]
    fn audit_vacuum_preserves_pending_overlay_after_a_complete_root() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        audit_put(&mut database, 1, Some(b"old"));
        audit_put(&mut database, 1, None);
        drop(database);
        let mut database = EntityDatabase::open(directory.path()).unwrap();
        database.set_write_overlay_enabled(true);
        database.set_tombstone_reclaim_override_for_test(Some(true));
        audit_put(&mut database, 2, Some(b"unmerged"));
        assert!(database.reclaim_tombstones_in_range(key(0).encoded(), key(3).encoded(), 100).unwrap().entries_reclaimed > 0);
        drop(database);
        let mut database = EntityDatabase::open(directory.path()).unwrap();
        assert_eq!(audit_value(&database, 1), None);
        assert_eq!(audit_value(&database, 2), Some(b"unmerged".to_vec()));
        database.merge_overlay_fully().unwrap();
        assert_eq!(audit_value(&database, 2), Some(b"unmerged".to_vec()));
    }

    #[test]
    fn replica_capture_registers_current_root_without_a_capacity_preclaim() {
        let (_directory, mut database) = seeded_database();
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
        let mut write = database.begin(7, 11).unwrap();
        database.put(&mut write, key(1), b"before-root-capture".to_vec()).unwrap();
        database.commit(write).unwrap();
        let replica = database.build_read_replica().unwrap();
        let mut read = replica.begin(7, 11).unwrap();
        assert_eq!(replica.get(&mut read, &key(1)).unwrap(), Some(b"before-root-capture".to_vec()));
        drop(read); drop(replica);
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
    }

#[test]
fn real_staged_payloads_cross_retired_aggregate_quota_and_release() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/data")).unwrap();
    let database = EntityDatabase::initialize_with_vfs(
        std::path::Path::new("/data"), vfs).unwrap();
    let started = std::time::Instant::now();
    let mut transactions = Vec::new();
    for owner in 0..32 {
        let mut transaction = database.begin(7, 11).unwrap();
        for index in 0..700 {
            let entity = EntityKey::new(
                7, 11, "demand", format!("owned-{owner}-{index}").as_bytes()).unwrap();
            database.put(&mut transaction, entity, vec![19; 8192]).unwrap();
        }
        transactions.push(transaction);
    }
    let retained = database.snapshot_pin_metrics().unwrap().retained_transaction_bytes;
    assert!(retained > LEGACY_AGGREGATE_TRANSACTION_BYTES);
    eprintln!("demand byte ownership: 32 transactions, {} payload bytes, {} accounted bytes, {:?}",
        32 * 700 * 8192, retained, started.elapsed());
    drop(transactions);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
}


#[test]
fn ninety_six_real_historical_roots_remain_readable_and_visible_to_gc() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = EntityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_enabled(false);
    let mut snapshots = Vec::new();
    for revision in 0..96 {
        let expected = vec![revision as u8; 256];
        let mut transaction = database.begin(7, 11).unwrap();
        database.put(&mut transaction, key(1), expected.clone()).unwrap();
        database.commit(transaction).unwrap();
        snapshots.push((database.build_read_replica().unwrap(), expected));
    }
    let mut current = database.begin(7, 11).unwrap();
    database.put(&mut current, key(1), b"current-tail".to_vec()).unwrap();
    database.commit(current).unwrap();
    let roots = database.pinned_snapshots().unwrap();
    assert_eq!(roots.len(), 96);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 96);
    for (replica, expected) in &snapshots {
        assert!(roots.contains(&replica.current_snapshot()));
        let mut read = replica.begin(7, 11).unwrap();
        assert_eq!(replica.get(&mut read, &key(1)).unwrap().as_ref(), Some(expected));
    }
    drop(snapshots);
    assert!(database.pinned_snapshots().unwrap().is_empty());
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
}
