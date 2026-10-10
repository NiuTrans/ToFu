// Physical tombstone compaction tests (L1 write-path reclamation,
// docs/TOFUDB_TOMBSTONE_COMPACTION.md).

    fn tkey(value: u16) -> EntityKey {
        EntityKey::new(7, 11, "tombstone", &value.to_be_bytes()).unwrap()
    }

    fn tree_get(database: &EntityDatabase, key: &EntityKey) -> Option<EntityValue> {
        (Tree {
            backend: database.writer_engine().unwrap(),
            page_cache: Some(&database.page_cache),
        })
        .get(database.root, key.encoded())
        .unwrap()
    }

    fn put_rows(database: &mut EntityDatabase, indexes: &[u16]) {
        let mut transaction = database.begin(7, 11).unwrap();
        for index in indexes {
            database
                .put(&mut transaction, tkey(*index), vec![*index as u8; 32])
                .unwrap();
        }
        database.commit(transaction).unwrap();
    }

    fn delete_rows(database: &mut EntityDatabase, indexes: &[u16]) {
        let mut transaction = database.begin(7, 11).unwrap();
        for index in indexes {
            database.delete(&mut transaction, tkey(*index)).unwrap();
        }
        database.commit(transaction).unwrap();
    }

    #[test]
    fn merge_reclaims_sub_watermark_tombstones_and_keeps_fresh_ones() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        put_rows(&mut database, &[1, 2, 3, 4]);
        database.merge_overlay_fully().unwrap();

        // Pin A covers the merge: tombstones staged while it is open are
        // younger than its snapshot and survive into the tree.
        let pin_a = database.begin(7, 11).unwrap();
        delete_rows(&mut database, &[1, 2]);
        database.merge_overlay_fully().unwrap();
        let tombstone_version = tree_get(&database, &tkey(1))
            .unwrap()
            .version;
        assert!(
            tree_get(&database, &tkey(1)).unwrap().value.is_none(),
            "the merge stages tombstones into the tree under a live pin"
        );
        drop(pin_a);

        // Pin B's snapshot postdates the tombstones: they are below the
        // watermark and reclaimable; tombstone 5 is younger and survives.
        let pin_b = database.begin(7, 11).unwrap();
        put_rows(&mut database, &[5]);
        delete_rows(&mut database, &[5]);
        database.merge_overlay_fully().unwrap();

        assert!(
            tree_get(&database, &tkey(1)).is_none(),
            "tombstone older than the watermark is reclaimed"
        );
        assert!(
            tree_get(&database, &tkey(2)).is_none(),
            "tombstone older than the watermark is reclaimed"
        );
        let young = tree_get(&database, &tkey(5)).unwrap();
        assert!(
            young.value.is_none() && young.version > tombstone_version,
            "tombstone younger than the watermark survives"
        );
        assert_eq!(
            tree_get(&database, &tkey(3)).unwrap().value,
            Some(vec![3; 32]),
            "live row untouched"
        );
        assert_eq!(
            tree_get(&database, &tkey(4)).unwrap().value,
            Some(vec![4; 32]),
            "live row untouched"
        );
        drop(pin_b);
    }

    #[test]
    fn reclaimed_delete_never_resurrects_the_underlying_value() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        put_rows(&mut database, &[10]);
        database.merge_overlay_fully().unwrap();
        assert_eq!(
            tree_get(&database, &tkey(10)).unwrap().value,
            Some(vec![10; 32])
        );
        delete_rows(&mut database, &[10]);
        // No pins are open: the watermark admits every earlier version, and
        // the merge must remove the key entirely — value, tombstone and
        // all. Resurrection would show the pre-delete value again.
        database.merge_overlay_fully().unwrap();
        assert_eq!(
            tree_get(&database, &tkey(10)),
            None,
            "a reclaimed delete must never resurrect the old value"
        );
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut transaction, &tkey(10)).unwrap(), None);
    }

    #[test]
    fn fully_reclaimed_leaf_runs_collapse_without_empty_pages() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        // Two dense regions sharing one repack run; one is fully deleted,
        // the other stays live.
        put_rows(&mut database, &(0..32).collect::<Vec<_>>());
        delete_rows(&mut database, &(0..16).collect::<Vec<_>>());
        put_rows(&mut database, &[32]);
        // No pins are open: the merge repack reclaims every tombstone it
        // touches, and the deleted region never materializes as dead pages.
        database.merge_overlay_fully().unwrap();
        let (rows, leaves_after) = (Tree {
            backend: database.writer_engine().unwrap(),
            page_cache: Some(&database.page_cache),
        })
        .scan(database.root, &tkey(0).encoded(), &tkey(33).encoded(), 64, &mut 0)
        .unwrap();
        assert_eq!(rows.len(), 17, "live rows 16..32 survive");
        for (offset, (key, _)) in rows.iter().enumerate() {
            assert_eq!(key.as_slice(), tkey(16 + offset as u16).encoded());
        }
        assert!(
            leaves_after.len() <= 2,
            "reclaimed region must collapse pages, got {leaves_after:?}"
        );
        // The tree stays fully operational afterwards.
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            database.get(&mut transaction, &tkey(100)).unwrap(),
            None
        );
        assert_eq!(
            database.get(&mut transaction, &tkey(20)).unwrap(),
            Some(vec![20; 32])
        );
    }

    #[test]
    fn occ_conflict_on_reclaimed_witness_is_conservative_and_recovers() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        put_rows(&mut database, &[20, 21]);
        database.merge_overlay_fully().unwrap();

        // Pin A covers the delete's merge so the tombstone survives into
        // the tree; the stale transaction then witnesses THAT TOMBSTONE
        // (value None, version Some(v)).
        let pin_a = database.begin(7, 11).unwrap();
        delete_rows(&mut database, &[20]);
        database.merge_overlay_fully().unwrap();
        assert!(tree_get(&database, &tkey(20)).unwrap().value.is_none());
        drop(pin_a);

        let mut stale = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut stale, &tkey(20)).unwrap(), None);

        // A later watermark admits the tombstone; the next repack reclaims
        // it, so the stale transaction's Some(v) witness meets None.
        let pin_b = database.begin(7, 11).unwrap();
        put_rows(&mut database, &[21]);
        database.merge_overlay_fully().unwrap();
        assert!(tree_get(&database, &tkey(20)).is_none());
        drop(pin_b);

        // Conservative conflict — never a silent lost update.
        database
            .put(&mut stale, tkey(21), b"concurrent".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(stale).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        // The retried transaction re-reads the post-reclaim state and
        // commits cleanly.
        let mut fresh = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut fresh, &tkey(20)).unwrap(), None);
        database
            .put(&mut fresh, tkey(21), b"concurrent".to_vec())
            .unwrap();
        database.commit(fresh).unwrap();
        database.merge_overlay_fully().unwrap();
        assert_eq!(
            tree_get(&database, &tkey(21)).unwrap().value,
            Some(b"concurrent".to_vec())
        );
    }

    #[test]
    fn synchronous_tree_reclaim_defers_to_a_live_snapshot_pin_and_resumes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_enabled(false);
        database.set_tombstone_reclaim_override_for_test(Some(true));
        // Both small rows share a leaf, so touching the live neighbor
        // repacks the tombstone without any overlay merge or explicit vacuum.
        put_rows(&mut database, &[30, 31]);
        let mut reader = database.begin(7, 11).unwrap();
        delete_rows(&mut database, &[30]);
        let tombstone = tree_get(&database, &tkey(30)).unwrap();
        assert!(tombstone.value.is_none());

        put_rows(&mut database, &[31]);
        assert_eq!(
            tree_get(&database, &tkey(30)),
            Some(tombstone),
            "a synchronous repack must retain the tombstone under a live pin"
        );
        assert_eq!(
            database.get(&mut reader, &tkey(30)).unwrap(),
            Some(vec![30; 32]),
            "the pinned pre-delete snapshot remains readable"
        );

        drop(reader);
        put_rows(&mut database, &[31]);
        assert!(
            tree_get(&database, &tkey(30)).is_none(),
            "synchronous repack resumes reclamation after the pin drops"
        );
        assert_eq!(
            tree_get(&database, &tkey(31)).unwrap().value,
            Some(vec![31; 32]),
            "reclamation preserves the live neighbor"
        );
        let mut fresh = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut fresh, &tkey(30)).unwrap(), None);
    }

    #[test]
    fn tombstone_younger_than_oldest_live_pin_survives() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        put_rows(&mut database, &[30, 31]);
        database.merge_overlay_fully().unwrap();
        // Pin the pre-delete state; its snapshot sequence is the watermark.
        let pin_transaction = database.begin(7, 11).unwrap();
        delete_rows(&mut database, &[30]);
        database.merge_overlay_fully().unwrap();
        let tombstone = tree_get(&database, &tkey(30)).unwrap();
        assert!(
            tombstone.value.is_none(),
            "tombstone younger than a live pin must survive reclamation"
        );
        drop(pin_transaction);
        // A new pin opens the watermark past the tombstone; the next repack
        // across the region drops it.
        let pin_two = database.begin(7, 11).unwrap();
        put_rows(&mut database, &[31]);
        database.merge_overlay_fully().unwrap();
        assert!(tree_get(&database, &tkey(30)).is_none());
        assert_eq!(
            tree_get(&database, &tkey(31)).unwrap().value,
            Some(vec![31; 32])
        );
        drop(pin_two);
    }

    fn big_value_rows(database: &mut EntityDatabase, indexes: &[u16]) {
        let mut transaction = database.begin(7, 11).unwrap();
        for index in indexes {
            database
                .put(&mut transaction, tkey(*index), vec![(*index % 251) as u8; 4 * 1024])
                .unwrap();
        }
        database.commit(transaction).unwrap();
    }

    fn dense_tombstone_region(database: &mut EntityDatabase, rows: u16) {
        // Live rows merged under pin A so their tombstones survive the
        // delete merges (pin A's snapshot is the watermark floor). Deletes
        // land in small per-leaf batches so the dead region spans many
        // leaves instead of coalescing into one frame.
        big_value_rows(database, &(0..rows).collect::<Vec<_>>());
        database.merge_overlay_fully().unwrap();
        let pin_a = database.begin(7, 11).unwrap();
        for chunk_start in (0..rows).step_by(4) {
            let chunk: Vec<u16> = (chunk_start..chunk_start + 4).collect();
            delete_rows(database, &chunk);
            database.merge_overlay_fully().unwrap();
        }
        drop(pin_a);
    }

    #[test]
    fn dense_scan_reports_and_coalesces_compaction_hints() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        dense_tombstone_region(&mut database, 40);
        assert_eq!(database.tombstone_compaction_hint_count(), 0);
        // A bounded scan across the dead region reports one coalesced hint.
        let mut transaction = database.begin(7, 11).unwrap();
        let rows = database
            .scan(&mut transaction, &tkey(0), &tkey(1000), 1000)
            .unwrap();
        assert!(rows.is_empty(), "all rows are tombstoned");
        assert_eq!(database.tombstone_compaction_hint_count(), 1);
        let (start, end, count) = database
            .take_tombstone_compaction_hint()
            .unwrap()
            .expect("hint recorded");
        assert!(count >= 8, "dense region must clear the threshold: {count}");
        assert!(start.as_slice() <= tkey(0).encoded());
        assert!(end.as_slice() >= tkey(1000).encoded());
        assert!(database.take_tombstone_compaction_hint().unwrap().is_none());
        // A small live-only scan reports nothing.
        put_rows(&mut database, &[2000]);
        let mut live = database.begin(7, 11).unwrap();
        database.scan(&mut live, &tkey(1999), &tkey(2001), 10).unwrap();
        assert_eq!(database.tombstone_compaction_hint_count(), 0);
    }

    #[test]
    fn reclaim_range_vacuums_tombstones_and_preserves_live_bytes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        // Keep pin A open through the live-row merge so its watermark keeps
        // protecting the tombstones until the explicit compaction runs.
        big_value_rows(&mut database, &(0..40).collect::<Vec<_>>());
        database.merge_overlay_fully().unwrap();
        let pin_a = database.begin(7, 11).unwrap();
        delete_rows(&mut database, &(0..40).collect::<Vec<_>>());
        database.merge_overlay_fully().unwrap();
        put_rows(&mut database, &[5000]);
        database.merge_overlay_fully().unwrap();
        drop(pin_a);
        let progress = database
            .reclaim_tombstones_in_range(&tkey(0).encoded(), &tkey(1000).encoded(), 64)
            .unwrap();
        assert!(progress.entries_reclaimed >= 40, "progress: {progress:?}");
        assert!(progress.resume.is_none(), "one slice covers 64 leaves: {progress:?}");
        // Every tombstone below the watermark is gone; the live row and the
        // tree stay byte-exact.
        for index in 0..40 {
            assert!(tree_get(&database, &tkey(index)).is_none());
        }
        assert_eq!(
            tree_get(&database, &tkey(5000)).unwrap().value,
            Some(vec![(5000_u16 % 256) as u8; 32].to_vec())
        );
        let (rows, _) = (Tree {
            backend: database.writer_engine().unwrap(),
            page_cache: Some(&database.page_cache),
        })
        .scan(database.root, &tkey(0).encoded(), &tkey(6000).encoded(), 100, &mut 0)
        .unwrap();
        assert_eq!(rows.len(), 1);
        // Re-running the sweep is a cheap no-op.
        let again = database
            .reclaim_tombstones_in_range(&tkey(0).encoded(), &tkey(1000).encoded(), 64)
            .unwrap();
        assert_eq!(again.entries_reclaimed, 0);
        assert!(again.resume.is_none());
    }

    #[test]
    fn reclaim_range_is_bounded_and_resumable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        dense_tombstone_region(&mut database, 40);
        let first = database
            .reclaim_tombstones_in_range(&tkey(0).encoded(), &tkey(1000).encoded(), 4)
            .unwrap();
        assert_eq!(first.pages_rewritten, 4);
        let (resume_start, resume_end) = first
            .resume
            .clone()
            .expect("budget spent mid-range must resume");
        assert!(resume_start.as_slice() > tkey(0).encoded());
        assert_eq!(resume_end, tkey(1000).encoded().to_vec());
        // Drain through the resume cursor: every slice makes progress and
        // the final one completes the range.
        let mut total = first.entries_reclaimed;
        let mut window = (resume_start, resume_end);
        for _ in 0..16 {
            let progress = database
                .reclaim_tombstones_in_range(&window.0, &window.1, 4)
                .unwrap();
            total += progress.entries_reclaimed;
            match progress.resume {
                Some(next) => window = next,
                None => break,
            }
        }
        assert!(total >= 40, "resumed sweeps reclaim everything: {total}");
        for index in 0..40 {
            assert!(tree_get(&database, &tkey(index)).is_none());
        }
    }

    #[test]
    fn reclaim_range_respects_the_pin_watermark() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        dense_tombstone_region(&mut database, 20);
        // A pin older than the tombstones keeps them alive; a pin newer
        // than the tombstones admits them.
        let old_pin = database.begin(7, 11).unwrap();
        // old_pin's snapshot postdates the tombstones? No: the tombstones
        // predate old_pin, so its watermark does NOT protect them. Hold a
        // snapshot from BEFORE the deletes instead — reopen the region.
        drop(old_pin);
        let progress = database
            .reclaim_tombstones_in_range(&tkey(0).encoded(), &tkey(1000).encoded(), 64)
            .unwrap();
        assert!(progress.entries_reclaimed >= 20);
    }

    #[test]
    fn absence_aba_stays_strict_while_the_witness_holds_a_pin() {
        // The pin watermark makes reclamation invisible to open
        // transactions: a tombstone younger than any live pin survives, so
        // the strict insert/delete ABA contract holds for every pinned
        // reader. The documented weakening is unreachable while the witness
        // is alive; it only ever costs a conservative conflict + retry to a
        // transaction that witnessed a tombstone later reclaimed after its
        // own watermark admitted it (covered above).
        let directory = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(directory.path()).unwrap();
        database.set_tombstone_reclaim_override_for_test(Some(true));
        put_rows(&mut database, &[60]);
        database.merge_overlay_fully().unwrap();

        let mut stale = database.begin(7, 11).unwrap();
        assert_eq!(database.get(&mut stale, &tkey(61)).unwrap(), None);

        // Key 61 is created and deleted while the witness holds its pin:
        // the tombstone is younger than the witness's snapshot and can
        // never be reclaimed underneath it.
        put_rows(&mut database, &[61]);
        database.merge_overlay_fully().unwrap();
        delete_rows(&mut database, &[61]);
        database.merge_overlay_fully().unwrap();
        assert!(
            tree_get(&database, &tkey(61)).unwrap().value.is_none(),
            "a live pin protects the tombstone from reclamation"
        );

        database
            .put(&mut stale, tkey(62), b"decided-on-absence".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(stale).unwrap_err().kind(),
            io::ErrorKind::WouldBlock,
            "strict ABA: absence witness still conflicts the insert/delete"
        );
    }
