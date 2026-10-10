//! Long range walks, cursor progress, OCC and owned-reservation rollback.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Vfs};
use std::path::Path;

fn key(index: u16) -> EntityKey {
    EntityKey::new(7, 11, "range-demand", &index.to_be_bytes()).unwrap()
}

fn database_with_vfs() -> (Arc<DeterministicVfs>, EntityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database = EntityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, database)
}

fn database() -> EntityDatabase { database_with_vfs().1 }

#[test]
fn both_directions_walk_beyond_retired_leaf_quota_and_find_tail() {
    let database = database();
    let tree = Tree { backend: database.writer_engine().unwrap(), page_cache: None };
    let mut written = Vec::new();
    let mut leaves = Vec::new();
    for index in 0..1100_u16 {
        let value = match index { 0 => Some(b"reverse-tail".to_vec()), 1099 => Some(b"forward-tail".to_vec()), _ => None };
        leaves.push(tree.store(&Page::Leaf(vec![(key(index).0, EntityValue { version: 1, value })]), &mut written).unwrap());
    }
    let children: Vec<_> = leaves.chunks(64).map(|part|
        tree.store(&Page::Internal { level: 1, children: part.to_vec() }, &mut written).unwrap()).collect();
    let root = tree.store(&Page::Internal { level: 2, children }, &mut written).unwrap().block_id;
    let started = std::time::Instant::now();
    let (forward, forward_leaves) = tree.scan(Some(root), key(1).encoded(), key(1100).encoded(), 1, &mut 0).unwrap();
    assert_eq!(forward[0].1.value.as_deref(), Some(&b"forward-tail"[..]));
    assert!(forward_leaves.len() > 1024);
    let (reverse, reverse_leaves) = tree.scan_reverse(Some(root), key(0).encoded(), key(1099).encoded(), 1, &mut 0).unwrap();
    assert_eq!(reverse[0].1.value.as_deref(), Some(&b"reverse-tail"[..]));
    assert!(reverse_leaves.len() > 1024);
    eprintln!("range demand: {} forward / {} reverse leaf references, {:?}", forward_leaves.len(), reverse_leaves.len(), started.elapsed());
}

fn seeded_masked_database_with_vfs() -> (Arc<DeterministicVfs>, EntityDatabase) {
    let (vfs, mut database) = database_with_vfs();
    database.set_write_overlay_enabled(false);
    for batch in 0..16_u16 {
        let mut seed = database.begin(7, 11).unwrap();
        for index in batch*1000..(batch+1)*1000 {
            database.put(&mut seed, key(index), vec![19; 8]).unwrap();
        }
        database.commit(seed).unwrap();
    }
    (vfs, database)
}

fn seeded_masked_database() -> EntityDatabase { seeded_masked_database_with_vfs().1 }

#[test]
fn masked_prefix_pages_progress_without_row_ceiling_and_keep_late_occ() {
    let mut database = seeded_masked_database();
    for reverse in [false, true] {
        let mut transaction = database.begin(7, 11).unwrap();
        let masked = if reverse { 2000..16000_u16 } else { 0..14000_u16 };
        for index in masked { database.delete(&mut transaction, key(index)).unwrap(); }
        let started = std::time::Instant::now();
        let rows = if reverse {
            database.scan_reverse(&mut transaction, &key(0), &key(16000), 1000).unwrap()
        } else {
            database.scan(&mut transaction, &key(0), &key(16000), 1000).unwrap()
        };
        assert_eq!(rows.len(), 1000);
        for (position, (actual, _)) in rows.iter().enumerate() {
            let expected = if reverse { 1999-position as u16 } else { 14000+position as u16 };
            assert_eq!(*actual, key(expected));
        }
        assert!(transaction.range_witnesses.len() >= 15);
        assert!(transaction.range_witnesses.iter().all(|witness| witness.scan_limit <= MAX_ENTITY_RANGE_ROWS));
        eprintln!("masked range demand: reverse={reverse}, 14000 hidden rows, {} windows, {:?}", transaction.range_witnesses.len(), started.elapsed());
        let changed = if reverse { 1000 } else { 14999 };
        let mut competing = database.begin(7, 11).unwrap();
        database.put(&mut competing, key(changed), b"late change".to_vec()).unwrap();
        database.commit(competing).unwrap();
        assert_eq!(database.commit(transaction).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    }
}

#[test]
fn exact_masked_interval_ends_without_invalid_empty_cursor_and_accounts_actual_leaves() {
    let mut database = database();
    database.set_write_overlay_enabled(false);
    let mut seed = database.begin(7, 11).unwrap();
    database.put(&mut seed, key(1), b"value".to_vec()).unwrap();
    database.commit(seed).unwrap();
    for reverse in [false, true] {
        let mut transaction = database.begin(7, 11).unwrap();
        database.delete(&mut transaction, key(1)).unwrap();
        let (start, end) = key(1).exact_range().unwrap();
        let before = transaction.snapshot_pin.retained_transaction_bytes;
        let rows = if reverse { database.scan_reverse(&mut transaction, &start, &end, 1).unwrap() }
            else { database.scan(&mut transaction, &start, &end, 1).unwrap() };
        assert!(rows.is_empty());
        assert_eq!(transaction.range_witnesses.len(), 1);
        let witness = &transaction.range_witnesses[0];
        let expected = range_witness_reservation_bytes(&witness.start, &witness.end, witness.leaf_ids.capacity()).unwrap();
        assert_eq!(transaction.snapshot_pin.retained_transaction_bytes-before, expected);
        assert!(expected < 4096, "small reads must not pre-charge 1024 hypothetical leaves");
    }
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
}

#[test]
fn failed_later_window_repays_new_ownership_and_keeps_prior_witnesses() {
    let database = seeded_masked_database();
    let mut transaction = database.begin(7, 11).unwrap();
    database.scan(&mut transaction, &key(30000), &key(30001), 1).unwrap();
    let previous_count = transaction.range_witnesses.len();
    for index in 0..14000_u16 { database.delete(&mut transaction, key(index)).unwrap(); }
    let tree = Tree { backend: database.backend(), page_cache: Some(database.page_cache.as_ref()) };
    let (_, leaves) = tree.scan(transaction.snapshot.root, key(0).encoded(), key(16000).encoded(), 1, &mut 0).unwrap();
    let first_bytes = range_witness_reservation_bytes(key(0).encoded(), key(16000).encoded(), leaves.capacity()).unwrap();
    let original = transaction.snapshot_pin.retained_transaction_bytes;
    transaction.snapshot_pin.reserve_transaction_bytes(usize::MAX-original-first_bytes).unwrap();
    let before = transaction.snapshot_pin.retained_transaction_bytes;
    assert_eq!(database.scan(&mut transaction, &key(0), &key(16000), 1).unwrap_err().kind(), io::ErrorKind::InvalidData);
    assert_eq!(transaction.range_witnesses.len(), previous_count);
    assert_eq!(transaction.snapshot_pin.retained_transaction_bytes, before);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, before);
    drop(transaction);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    assert!(range_witness_reservation_bytes(&[], &[], usize::MAX).is_err());
}


#[test]
fn source_io_failure_repays_window_ownership_without_changing_prior_reads() {
    let (vfs, database) = seeded_masked_database_with_vfs();
    database.writer_engine().unwrap().block_store().flush_pending().unwrap();
    database.set_page_cache_capacity_bytes(0).unwrap();
    database.writer_engine().unwrap().block_store().set_cache_capacity_bytes(0).unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database.scan(&mut transaction, &key(30000), &key(30001), 1).unwrap();
    for index in 0..14000_u16 { database.delete(&mut transaction, key(index)).unwrap(); }
    let before = transaction.snapshot_pin.retained_transaction_bytes;
    let previous_count = transaction.range_witnesses.len();
    vfs.arm_fault(None).unwrap();
    let tree = Tree { backend: database.backend(), page_cache: None };
    tree.scan(transaction.snapshot.root, key(0).encoded(), key(16000).encoded(), 1, &mut 0).unwrap();
    let first_window_io = vfs.trace().unwrap().len();
    assert!(first_window_io > 0);
    vfs.arm_fault(Some(FaultRule {
        operation_number: (first_window_io + 1) as u64,
        action: FaultAction::ErrorBefore(io::ErrorKind::Other),
    })).unwrap();
    assert_eq!(database.scan(&mut transaction, &key(0), &key(16000), 1).unwrap_err().kind(), io::ErrorKind::Other);
    assert_eq!(transaction.range_witnesses.len(), previous_count);
    assert_eq!(transaction.snapshot_pin.retained_transaction_bytes, before);
    vfs.arm_fault(None).unwrap();
    drop(transaction);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
}
