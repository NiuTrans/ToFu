//! Deferred-application write overlay tests (TOFU_DB_WRITE_OVERLAY path).
//!
//! Every test compares the overlay authority against the legacy synchronous
//! authority on the same operation sequence, or pins down one of the
//! correctness rules in `entity_overlay.rs`: read-your-writes across
//! commits, pinned-replica isolation across a merge, WAL replay with and
//! without a merge, structural flush-first, and OCC conflict preservation.

use std::io;
use std::path::Path;
use std::sync::Arc;

use crate::authority::AuthorityDatabase;
use crate::entity::EntityKey;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

const TENANT: u64 = 1;
const OWNER: u64 = 1;

fn entity_key(namespace: &str, key: &[u8]) -> EntityKey {
    EntityKey::new(TENANT, OWNER, namespace, key).unwrap()
}

fn put(database: &mut AuthorityDatabase, namespace: &str, key: &[u8], value: &[u8]) {
    let mut transaction = database.begin(TENANT, OWNER).unwrap();
    database
        .entity_put(
            &mut transaction,
            entity_key(namespace, key),
            value.to_vec(),
        )
        .unwrap();
    database.commit(transaction).unwrap();
}

fn delete(database: &mut AuthorityDatabase, namespace: &str, key: &[u8]) {
    let mut transaction = database.begin(TENANT, OWNER).unwrap();
    database
        .entity_delete(&mut transaction, entity_key(namespace, key))
        .unwrap();
    database.commit(transaction).unwrap();
}

fn get(database: &AuthorityDatabase, namespace: &str, key: &[u8]) -> Option<Vec<u8>> {
    let mut read = database.begin(TENANT, OWNER).unwrap();
    database
        .entity_get(&mut read, &entity_key(namespace, key))
        .unwrap()
}

fn scan_all(database: &AuthorityDatabase, namespace: &str) -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut read = database.begin(TENANT, OWNER).unwrap();
    let (start, end) =
        EntityKey::prefix_range(TENANT, OWNER, namespace, b"").unwrap();
    database
        .entity_scan(&mut read, &start, &end, 1_000)
        .unwrap()
        .into_iter()
        .map(|(key, value)| (key.key_bytes().to_vec(), value))
        .collect()
}

fn overlay_authority() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    // Make the /data entry durable so vfs.crash() keeps the authority's home
    // (the power-loss crash matrix below; matches engine_tests' crash idiom).
    vfs.sync_directory(Path::new("/")).unwrap();
    let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), dyn_vfs).unwrap();
    database.set_write_overlay_for_test(true);
    (vfs, database)
}

fn reopen(vfs: &Arc<DeterministicVfs>) -> AuthorityDatabase {
    let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), dyn_vfs).unwrap();
    database.set_write_overlay_for_test(true);
    database
}

#[test]
fn commit_path_backstop_merges_one_bounded_chunk_per_commit() {
    // 2026-09-21 FUSE brownout regression: the hard commit-path trigger
    // used to merge the FULL overlay inside the foreground commit —
    // seconds-to-minutes inside the authority hold on a degraded mount.
    // It now merges exactly one bounded chunk per tripped commit: the
    // oldest chain positions, including a safe prefix within an atomic
    // span. Cold start begins at 64 and observed latency adapts subsequent
    // slices up to 2,048; it never drains the whole backlog in one hold.
    let (_vfs, mut database) = overlay_authority();
    // Seed a two-sequence backlog under the generous default budget so no
    // seed commit trips the trigger.
    for (prefix, count) in [("a", 2_500_u32), ("b", 1_500_u32)] {
        let mut transaction = database.begin(TENANT, OWNER).unwrap();
        for index in 0..count {
            database
                .entity_put(
                    &mut transaction,
                    entity_key("record", format!("{prefix}{index}").as_bytes()),
                    b"v".to_vec(),
                )
                .unwrap();
        }
        database.commit(transaction).unwrap();
    }
    assert_eq!(database.entity_overlay_entries_for_test(), 4_000);
    // Trip the trigger deterministically: any backlog beats a 1-byte budget.
    database.set_overlay_budget_for_test(1);
    // The tripped commit merges only the OLDEST chunk — commit A's atomic
    // prefix — never sequence B's newer entries. A full merge (the old
    // backstop) would leave exactly this commit's own key behind.
    put(&mut database, "record", b"trigger-1", b"x");
    assert_eq!(database.entity_overlay_entries_for_test(), 3_937);
    // A fast first slice grows at most 1.5x; a slow slice shrinks. Neither
    // path can turn the second foreground commit into a full drain.
    put(&mut database, "record", b"trigger-2", b"x");
    assert!((3_842..=3_937).contains(&database.entity_overlay_entries_for_test()));
    database.merge_entity_overlay_fully().unwrap();
    // Content stays correct through chunked merging.
    assert_eq!(get(&database, "record", b"a42"), Some(b"v".to_vec()));
    assert_eq!(get(&database, "record", b"b1499"), Some(b"v".to_vec()));
    assert_eq!(get(&database, "record", b"trigger-1"), Some(b"x".to_vec()));
    assert_eq!(get(&database, "record", b"trigger-2"), Some(b"x".to_vec()));
}

#[test]
fn payload_commit_between_overlay_delta_and_reopen_preserves_replay() {
    // 2026-09-20 stranding regression: a generic engine record
    // (`authority_state_update == None`, e.g. an Engine payload-family
    // commit) interleaved between a pending overlay delta and a reopen must
    // not bound delta replay — it leaves the authority state unchanged by
    // contract, so it can never be the root advance that covers the delta.
    let (vfs, mut database) = overlay_authority();
    put(&mut database, "record", b"reopen-live", b"reopen-value");
    database
        .commit_engine_payload_for_test(b"payload-only")
        .unwrap();
    drop(database);
    vfs.crash().unwrap();

    let mut reopened = reopen(&vfs);
    assert_eq!(
        get(&reopened, "record", b"reopen-live"),
        Some(b"reopen-value".to_vec()),
        "committed delta stranded behind a payload-only engine record"
    );
    // Continued writes plus a merge keep exactly-once application.
    put(&mut reopened, "record", b"after-reopen", b"v2");
    reopened.merge_entity_overlay_for_test().unwrap();
    assert_eq!(
        get(&reopened, "record", b"reopen-live"),
        Some(b"reopen-value".to_vec())
    );
    assert_eq!(
        get(&reopened, "record", b"after-reopen"),
        Some(b"v2".to_vec())
    );
    drop(reopened);
    vfs.crash().unwrap();
    let reopened_again = reopen(&vfs);
    assert_eq!(
        get(&reopened_again, "record", b"reopen-live"),
        Some(b"reopen-value".to_vec())
    );
    assert_eq!(
        scan_all(&reopened_again, "record"),
        vec![
            (b"after-reopen".to_vec(), b"v2".to_vec()),
            (b"reopen-live".to_vec(), b"reopen-value".to_vec()),
        ]
    );
}

#[test]
fn overlay_commit_matches_synchronous_tree_state() {
    let (_vfs_a, mut overlay_db) = overlay_authority();
    let vfs_b = Arc::new(DeterministicVfs::new(None));
    vfs_b.create_dir(Path::new("/data")).unwrap();
    let dyn_vfs: Arc<dyn Vfs> = vfs_b.clone();
    let mut legacy_db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), dyn_vfs).unwrap();

    let namespaces = ["turn.doc", "lane.index", "receipt", "outbox"];
    for round in 0..30_u8 {
        for (index, namespace) in namespaces.iter().enumerate() {
            let key = format!("ns{index}/key-{:03}", round % 7).into_bytes();
            let value = format!("value-r{round}-n{index}").into_bytes();
            put(&mut overlay_db, namespace, &key, &value);
            put(&mut legacy_db, namespace, &key, &value);
        }
        if round % 5 == 4 {
            let key = format!("ns0/key-{:03}", (round + 1) % 7).into_bytes();
            delete(&mut overlay_db, "turn.doc", &key);
            delete(&mut legacy_db, "turn.doc", &key);
        }
    }
    assert!(overlay_db.entity_overlay_entries_for_test() > 0);
    for namespace in namespaces {
        assert_eq!(
            scan_all(&overlay_db, namespace),
            scan_all(&legacy_db, namespace),
            "namespace {namespace} diverged before merge"
        );
    }
    assert!(overlay_db.merge_entity_overlay_for_test().unwrap());
    assert_eq!(overlay_db.entity_overlay_entries_for_test(), 0);
    for namespace in namespaces {
        assert_eq!(
            scan_all(&overlay_db, namespace),
            scan_all(&legacy_db, namespace),
            "namespace {namespace} diverged after merge"
        );
    }
}

#[test]
fn overlay_read_your_writes_across_commits() {
    let (_vfs, mut database) = overlay_authority();
    put(&mut database, "records", b"k", b"v1");
    assert_eq!(get(&database, "records", b"k"), Some(b"v1".to_vec()));
    put(&mut database, "records", b"k", b"v2");
    assert_eq!(get(&database, "records", b"k"), Some(b"v2".to_vec()));
    delete(&mut database, "records", b"k");
    assert_eq!(get(&database, "records", b"k"), None);
    put(&mut database, "records", b"k", b"v3");
    assert_eq!(get(&database, "records", b"k"), Some(b"v3".to_vec()));
}

#[test]
fn overlay_replica_reads_its_pinned_snapshot_across_a_merge() {
    let (_vfs, mut database) = overlay_authority();
    put(&mut database, "records", b"a", b"one");
    let replica = database.build_read_replica().unwrap();
    put(&mut database, "records", b"a", b"two");
    let mut replica_read = replica.begin(TENANT, OWNER).unwrap();
    assert_eq!(
        replica
            .entity_get(&mut replica_read, &entity_key("records", b"a"))
            .unwrap(),
        Some(b"one".to_vec())
    );
    assert!(database.merge_entity_overlay_for_test().unwrap());
    let mut replica_read = replica.begin(TENANT, OWNER).unwrap();
    assert_eq!(
        replica
            .entity_get(&mut replica_read, &entity_key("records", b"a"))
            .unwrap(),
        Some(b"one".to_vec()),
        "the pinned replica must keep its pre-merge view across the merge"
    );
    assert_eq!(get(&database, "records", b"a"), Some(b"two".to_vec()));
}

#[test]
fn overlay_recovery_replays_unmerged_deltas() {
    let (vfs, mut database) = overlay_authority();
    for index in 0..10_u8 {
        put(&mut database, "records", &[b'k', index], &[index]);
    }
    assert!(database.entity_overlay_entries_for_test() > 0);
    drop(database);
    let reopened = reopen(&vfs);
    assert!(
        reopened.entity_overlay_entries_for_test() > 0,
        "reopen must replay un-merged deltas into the overlay"
    );
    for index in 0..10_u8 {
        assert_eq!(
            get(&reopened, "records", &[b'k', index]),
            Some(vec![index]),
            "replayed value for key {index} differs"
        );
    }
}

#[test]
fn overlay_recovery_across_a_merge_is_exact() {
    let (vfs, mut database) = overlay_authority();
    for index in 0..10_u8 {
        put(&mut database, "records", &[b'k', index], &[index]);
    }
    assert!(database.merge_entity_overlay_for_test().unwrap());
    for index in 10..15_u8 {
        put(&mut database, "records", &[b'k', index], &[index]);
    }
    delete(&mut database, "records", &[b'k', 0]);
    drop(database);
    let reopened = reopen(&vfs);
    for index in 1..15_u8 {
        assert_eq!(
            get(&reopened, "records", &[b'k', index]),
            Some(vec![index]),
            "post-merge replayed value for key {index} differs"
        );
    }
    assert_eq!(get(&reopened, "records", &[b'k', 0]), None);
}

#[test]
fn overlay_structural_transaction_flushes_first() {
    let (_vfs, mut database) = overlay_authority();
    for index in 0..6_u8 {
        put(&mut database, "records", &[b'k', index], &[index]);
    }
    assert!(database.entity_overlay_entries_for_test() > 0);
    // A retired-range transaction is structural: it must flush the overlay
    // and take the exact legacy path.
    let mut transaction = database.begin(TENANT, OWNER).unwrap();
    database
        .entity_retire_range(
            &mut transaction,
            &entity_key("records", &[b'k', 2]),
            &entity_key("records", &[b'k', 4]),
        )
        .unwrap();
    database.commit(transaction).unwrap();
    for index in 0..6_u8 {
        let expected = if (2..4).contains(&index) {
            None
        } else {
            Some(vec![index])
        };
        assert_eq!(get(&database, "records", &[b'k', index]), expected);
    }
}

#[test]
fn overlay_budget_trigger_drains_eagerly_and_stays_exact() {
    let (_vfs, mut database) = overlay_authority();
    database.set_overlay_budget_for_test(1);
    // With a 1-byte budget every commit merges the PREVIOUS deltas before
    // installing its own: the pending set stays bounded at one commit's keys.
    for index in 0..10_u8 {
        put(&mut database, "records", &[b'k', index], &[index]);
        assert!(
            database.entity_overlay_entries_for_test() <= 1,
            "budget trigger left {} entries pending after commit {index}",
            database.entity_overlay_entries_for_test()
        );
        for readback in 0..=index {
            assert_eq!(
                get(&database, "records", &[b'k', readback]),
                Some(vec![readback]),
                "read diverged at commit {index} for key {readback}"
            );
        }
    }
    assert!(database.merge_entity_overlay_for_test().unwrap());
    for index in 0..10_u8 {
        assert_eq!(get(&database, "records", &[b'k', index]), Some(vec![index]));
    }
}

#[test]
fn overlay_knob_parsing_clamps_and_defaults() {
    use crate::entity::{
        parse_overlay_budget_bytes, parse_overlay_wal_watermark_bytes,
    };
    assert_eq!(parse_overlay_budget_bytes(None), 8 * 1024 * 1024);
    assert_eq!(parse_overlay_budget_bytes(Some("junk".to_owned())), 8 * 1024 * 1024);
    assert_eq!(parse_overlay_budget_bytes(Some("1".to_owned())), 1024 * 1024);
    assert_eq!(
        parse_overlay_budget_bytes(Some((1024_u64 * 1024 * 1024 * 1024).to_string())),
        256 * 1024 * 1024
    );
    assert_eq!(parse_overlay_wal_watermark_bytes(None), 48 * 1024 * 1024);
    assert_eq!(parse_overlay_wal_watermark_bytes(Some("1".to_owned())), 8 * 1024 * 1024);
    assert_eq!(
        parse_overlay_wal_watermark_bytes(Some(u64::MAX.to_string())),
        60 * 1024 * 1024,
        "watermark clamp keeps 4 MiB of headroom under the 64 MiB active-WAL ceiling"
    );
}

#[test]
fn write_overlay_defaults_on_with_emergency_opt_out() {
    use crate::entity::parse_write_overlay_enabled;
    assert!(
        parse_write_overlay_enabled(None),
        "unset defaults to the overlay write path since 2026-09-21"
    );
    assert!(parse_write_overlay_enabled(Some("1".to_owned())));
    assert!(
        !parse_write_overlay_enabled(Some("0".to_owned())),
        "TOFU_DB_WRITE_OVERLAY=0 remains the emergency escape hatch"
    );
}

#[test]
fn overlay_occ_conflict_survives() {
    let (_vfs, mut database) = overlay_authority();
    put(&mut database, "records", b"k", b"base");
    // T1 begins (snapshot S), T2 commits over k, T1 commits a write of k
    // derived from its stale snapshot: the OCC conflict must survive the
    // overlay's flush-first fallback.
    let mut stale = database.begin(TENANT, OWNER).unwrap();
    database
        .entity_get(&mut stale, &entity_key("records", b"k"))
        .unwrap();
    put(&mut database, "records", b"k", b"moved");
    database
        .entity_put(&mut stale, entity_key("records", b"k"), b"stale".to_vec())
        .unwrap();
    let error = database.commit(stale).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "stale-snapshot write must conflict, got {error:?}");
    assert_eq!(get(&database, "records", b"k"), Some(b"moved".to_vec()));
}

// ---- Deterministic crash matrix (the P2 phase gate of
// docs/TOFUDB_WRITE_PATH_OVERLAY.md): kill-before-merge, kill-during-merge,
// merge-then-crash-before-CONTROL. Each scenario asserts the exact
// exactly-once invariant: after recovery (and one drain), every acknowledged
// write is visible exactly once, identical to the legacy synchronous
// authority that ran the same operation sequence.

/// The crash seed: plain puts, one overwrite, one tombstone — every delta
/// kind the merge must apply. Small enough for a one-chunk merge.
fn seed_overlay_authority() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let (vfs, mut database) = overlay_authority();
    for index in 0..6_u8 {
        put(&mut database, "records", &[b'k', index], &[index]);
    }
    put(&mut database, "records", b"k\x00", b"replaced");
    delete(&mut database, "records", &[b'k', 5]);
    (vfs, database)
}

/// The same operation sequence on a legacy (overlay-off) authority is the
/// exactness reference for every crash scenario.
fn legacy_records_reference() -> Vec<(Vec<u8>, Vec<u8>)> {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
    let mut database =
        AuthorityDatabase::initialize_with_vfs(Path::new("/data"), dyn_vfs).unwrap();
    for index in 0..6_u8 {
        put(&mut database, "records", &[b'k', index], &[index]);
    }
    put(&mut database, "records", b"k\x00", b"replaced");
    delete(&mut database, "records", &[b'k', 5]);
    scan_all(&database, "records")
}

fn reopen_overlay(vfs: &Arc<DeterministicVfs>) -> AuthorityDatabase {
    let dyn_vfs: Arc<dyn Vfs> = vfs.clone();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), dyn_vfs).unwrap();
    database.set_write_overlay_for_test(true);
    database
}

#[test]
fn overlay_crash_before_merge_replays_every_acknowledged_delta() {
    let golden = legacy_records_reference();
    let (vfs, database) = seed_overlay_authority();
    // Power failure with committed-but-unmerged deltas: only the durable
    // (barriered) WAL tail survives.
    drop(database);
    vfs.crash().unwrap();
    let mut reopened = reopen_overlay(&vfs);
    assert!(
        reopened.entity_overlay_entries_for_test() > 0,
        "reopen must replay the un-merged deltas before any read"
    );
    assert_eq!(scan_all(&reopened, "records"), golden);
    reopened.merge_entity_overlay_fully().unwrap();
    assert_eq!(reopened.entity_overlay_entries_for_test(), 0);
    assert_eq!(scan_all(&reopened, "records"), golden);
}

#[test]
fn overlay_merge_then_crash_before_control_publication_is_exact() {
    let golden = legacy_records_reference();
    let (vfs, mut database) = seed_overlay_authority();
    assert!(database.merge_entity_overlay_for_test().unwrap());
    // No checkpoint ran: CONTROL still names the pre-merge boundary and the
    // merge survives only as a barriered WAL tail transaction. Power fails.
    drop(database);
    vfs.crash().unwrap();
    let mut reopened = reopen_overlay(&vfs);
    assert_eq!(
        reopened.entity_overlay_entries_for_test(),
        0,
        "the merge advanced the tree root; deltas at or below it must not replay"
    );
    assert_eq!(scan_all(&reopened, "records"), golden);
    // The authority keeps accepting writes on the merged OCC chain.
    put(&mut reopened, "records", b"post", b"alive");
    assert_eq!(get(&reopened, "records", b"post"), Some(b"alive".to_vec()));
    assert_eq!(scan_all(&reopened, "records").len(), golden.len() + 1);
}

#[test]
fn overlay_merge_fault_at_any_operation_recovers_exact_state() {
    let golden = legacy_records_reference();
    // Baseline: measure the merge's VFS operation window on one clean run.
    let (baseline_vfs, mut baseline) = seed_overlay_authority();
    baseline_vfs.arm_fault(None).unwrap(); // reset the counter and trace
    assert!(baseline.merge_entity_overlay_for_test().unwrap());
    let trace = baseline_vfs.trace().unwrap();
    let operation_count = trace.len() as u64;
    assert!(operation_count > 0);
    let write_numbers: Vec<u64> = trace
        .iter()
        .enumerate()
        .filter(|(_, operation)| **operation == Operation::Write)
        .map(|(index, _)| index as u64 + 1)
        .collect();
    let sync_numbers: Vec<u64> = trace
        .iter()
        .enumerate()
        .filter(|(_, operation)| {
            matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            )
        })
        .map(|(index, _)| index as u64 + 1)
        .collect();
    assert!(!write_numbers.is_empty(), "a merge must write at least one block");

    // Campaign A: an I/O error at every operation of the merge.
    let mut campaigns: Vec<(u64, FaultAction)> = (1..=operation_count)
        .map(|number| (number, FaultAction::ErrorBefore(io::ErrorKind::Interrupted)))
        .collect();
    // Campaign B: a torn write at every block/WAL write of the merge.
    campaigns.extend(
        write_numbers
            .iter()
            .map(|number| (*number, FaultAction::ShortWrite(7))),
    );
    // Campaign C: a lost barrier at every sync of the merge (power ate the
    // flush), immediately followed by the crash.
    campaigns.extend(sync_numbers.iter().map(|number| (*number, FaultAction::DropSync)));

    for (operation_number, action) in campaigns {
        let (vfs, mut database) = seed_overlay_authority();
        vfs.arm_fault(Some(FaultRule {
            operation_number,
            action,
        }))
        .unwrap();
        // The merge may fail at the injected point or succeed behind a lost
        // barrier; either way the process then dies.
        let _ = database.merge_entity_overlay_fully();
        drop(database);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered = reopen_overlay(&vfs);
        // Pre-merge or post-merge state are both legal; one drain settles
        // whichever the crash left, and the result must be exactly-once.
        recovered.merge_entity_overlay_fully().unwrap();
        assert_eq!(
            scan_all(&recovered, "records"),
            golden,
            "fault {action:?} at operation {operation_number} diverged after recovery"
        );
        assert_eq!(recovered.entity_overlay_entries_for_test(), 0);
    }
}


#[test]
fn overlay_merge_adapts_chunk_size_to_scattered_key_fanout() {
    // The 2026-09-21 soak wedge: a full 2,048-entry merge chunk over a deep,
    // scattered tree produced more written pages than the transaction
    // block-reference budget allows, so every merge attempt failed closed
    // and the authority wedged permanently. The merge must halve its chunk
    // until the produced page set fits — and still finish draining.
    let (_vfs, mut database) = overlay_authority();
    // Deep, wide tree: every merge entry writes its whole path.
    for index in 0..6_000_u32 {
        put(
            &mut database,
            "wide",
            format!("key-{index:06}").as_bytes(),
            b"base",
        );
    }
    database.merge_overlay_fully_for_operators().unwrap();
    // Scattered updates: 2,048 deltas whose paths share almost nothing.
    for index in (0..6_000_u32).step_by(3) {
        put(
            &mut database,
            "wide",
            format!("key-{index:06}").as_bytes(),
            b"updated",
        );
    }
    // This wedged before the adaptive halving: drain fully and read back.
    database.merge_overlay_fully_for_operators().unwrap();
    for index in (0..6_000_u32).step_by(3) {
        assert_eq!(
            get(&database, "wide", format!("key-{index:06}").as_bytes()).unwrap(),
            b"updated".to_vec(),
            "merged value lost for key-{index:06}"
        );
    }
    for index in (1..6_000_u32).step_by(3) {
        assert_eq!(
            get(&database, "wide", format!("key-{index:06}").as_bytes()).unwrap(),
            b"base".to_vec()
        );
    }
}

#[test]
fn coalesced_merge_and_checkpoint_faults_preserve_latest_versions_and_unmerged_suffix() {
    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let (vfs, mut database) = overlay_authority();
        for version in 1..=4_u8 {
            let mut transaction = database.begin(TENANT, OWNER).unwrap();
            for index in 0..64_u32 {
                database.entity_put(&mut transaction,
                    entity_key("coalesced", &index.to_be_bytes()), vec![version]).unwrap();
            }
            database.commit(transaction).unwrap();
        }
        put(&mut database, "coalesced", &64_u32.to_be_bytes(), b"suffix");
        (vfs, database)
    }
    fn merge_and_checkpoint(database: &mut AuthorityDatabase) -> io::Result<()> {
        assert!(database.merge_entity_overlay()?);
        assert_eq!(database.entity_overlay_entries_for_test(), 1,
            "one full distinct-key slice must coalesce repeated versions");
        database.checkpoint_wal_window()?;
        Ok(())
    }
    let (vfs, mut baseline) = prepared();
    vfs.arm_fault(None).unwrap();
    merge_and_checkpoint(&mut baseline).unwrap();
    let trace = vfs.trace().unwrap();
    assert!(trace.contains(&Operation::Write));
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        for action in actions {
            let (vfs, mut database) = prepared();
            vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
            let _ = merge_and_checkpoint(&mut database);
            // Capture power loss before Drop can publish a catch-up boundary.
            let crash = Arc::new(vfs.fork_durable().unwrap());
            let recovered = reopen(&crash);
            for key in 0..64_u32 {
                assert_eq!(get(&recovered, "coalesced", &key.to_be_bytes()), Some(vec![4]));
            }
            assert_eq!(get(&recovered, "coalesced", &64_u32.to_be_bytes()), Some(b"suffix".to_vec()));
        }
    }
}

#[test]
fn superseded_only_prefix_survives_every_merge_checkpoint_fault_with_old_readers() {
    const KEYS: u32 = 2_100;
    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase, AuthorityDatabase) {
        let (vfs, mut database) = overlay_authority();
        let mut first = database.begin(TENANT, OWNER).unwrap();
        for key in 0..KEYS {
            database.entity_put(&mut first, entity_key("obsolete", &key.to_be_bytes()), b"old".to_vec()).unwrap();
        }
        database.commit(first).unwrap();
        let pinned = database.build_read_replica().unwrap();
        let mut second = database.begin(TENANT, OWNER).unwrap();
        for key in 0..KEYS {
            let entity = entity_key("obsolete", &key.to_be_bytes());
            if key % 3 == 0 { database.entity_delete(&mut second, entity).unwrap(); }
            else { database.entity_put(&mut second, entity, b"new".to_vec()).unwrap(); }
        }
        database.commit(second).unwrap();
        (vfs, database, pinned)
    }
    fn merge_checkpoint(database: &mut AuthorityDatabase) -> io::Result<()> {
        assert!(database.merge_entity_overlay()?);
        assert_eq!(database.entity_overlay_entries_for_test(), 2 * KEYS as usize - 2_048);
        database.checkpoint_wal_window()?;
        Ok(())
    }
    fn verify(database: &AuthorityDatabase, pinned: Option<&AuthorityDatabase>) {
        for key in 0..KEYS {
            assert_eq!(get(database, "obsolete", &key.to_be_bytes()),
                if key % 3 == 0 { None } else { Some(b"new".to_vec()) });
            if let Some(pinned) = pinned {
                assert_eq!(get(pinned, "obsolete", &key.to_be_bytes()), Some(b"old".to_vec()));
            }
        }
    }
    let (vfs, mut baseline, pinned) = prepared();
    vfs.arm_fault(None).unwrap();
    merge_checkpoint(&mut baseline).unwrap();
    let operations = vfs.trace().unwrap();
    assert!(operations.contains(&Operation::Write));
    verify(&baseline, Some(&pinned));
    let recovered = reopen(&Arc::new(vfs.fork_durable().unwrap()));
    verify(&recovered, Some(&pinned));
    drop(recovered);
    baseline.merge_entity_overlay_fully().unwrap();
    verify(&baseline, Some(&pinned));
    let recovered = reopen(&Arc::new(vfs.fork_durable().unwrap()));
    verify(&recovered, Some(&pinned));
    for (index, operation) in operations.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        for action in actions {
            let (vfs, mut database, pinned) = prepared();
            vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
            let _ = merge_checkpoint(&mut database);
            let recovered = reopen(&Arc::new(vfs.fork_durable().unwrap()));
            verify(&recovered, None);
            drop(pinned);
        }
    }
}

#[test]
fn overlay_partial_merge_crash_replays_prepublication_tail_and_newer_mutations() {
    let (vfs, mut database) = overlay_authority();
    for index in 0..2_500_u32 {
        put(&mut database, "partial", &index.to_be_bytes(), &index.to_be_bytes());
    }
    assert!(database.merge_entity_overlay().unwrap());
    assert!(database.entity_overlay_entries_for_test() > 0);
    assert!(database.entity_overlay_entries_for_test() < 2_500);
    // Cover a newer overwrite of an already merged key and a tombstone of
    // a key whose original delta precedes the partial root publication.
    put(&mut database, "partial", &0u32.to_be_bytes(), b"new");
    delete(&mut database, "partial", &2_499u32.to_be_bytes());
    database.commit_engine_payload_for_test(b"interleaved-payload").unwrap();
    // Force the pending tail into checkpointed history: CONTROL now points
    // directly at the partial root, so recovery needs its persistent fence.
    database.checkpoint_wal_window().unwrap();
    drop(database);
    vfs.crash().unwrap();
    let mut recovered = reopen(&vfs);
    for index in 0..2_500_u32 {
        let expected = match index {
            0 => Some(b"new".to_vec()),
            2_499 => None,
            _ => Some(index.to_be_bytes().to_vec()),
        };
        assert_eq!(get(&recovered, "partial", &index.to_be_bytes()), expected,
            "partial merge lost key {index}");
    }
    recovered.merge_entity_overlay_fully().unwrap();
    drop(recovered);
    vfs.crash().unwrap();
    let recovered = reopen(&vfs);
    assert_eq!(recovered.entity_overlay_entries_for_test(), 0);
    assert_eq!(get(&recovered, "partial", &0u32.to_be_bytes()), Some(b"new".to_vec()));
    assert_eq!(get(&recovered, "partial", &2_400u32.to_be_bytes()), Some(2_400u32.to_be_bytes().to_vec()));
    assert_eq!(get(&recovered, "partial", &2_499u32.to_be_bytes()), None);
}

#[test]
fn overlay_partial_checkpoint_rejects_corrupt_required_history() {
    let (vfs, mut database) = overlay_authority();
    for index in 0..2_500_u32 {
        put(&mut database, "required", &index.to_be_bytes(), b"value");
    }
    assert!(database.merge_entity_overlay().unwrap());
    assert!(database.entity_overlay_entries_for_test() > 0);
    database.checkpoint_wal_window().unwrap();
    let history = *database.history_blocks_for_test().last().unwrap();
    drop(database);
    let hex = history.to_hex();
    let path = Path::new("/data").join("blocks").join(&hex[..2]).join(format!("{hex}.blk"));
    let mut file = vfs.open(&path, crate::vfs::OpenRequest {
        write: true, ..crate::vfs::OpenRequest::default()
    }).unwrap();
    file.write_all_at(16, b"X").unwrap();
    file.sync_all().unwrap();
    drop(file);
    vfs.crash().unwrap();
    match AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs) {
        Ok(_) => panic!("partial root cannot silently lose its required overlay tail"),
        Err(error) => assert_eq!(error.kind(), io::ErrorKind::InvalidData),
    }
}


#[test]
fn overlay_recovery_decodes_each_required_history_segment_once() {
    let (vfs, mut database) = overlay_authority();
    for round in 0..3_u32 {
        for index in 0..64_u32 {
            put(&mut database, "linear-replay", &(round * 64 + index).to_be_bytes(), &index.to_be_bytes());
        }
        database.checkpoint_wal_window().unwrap();
    }
    // The same-key suffix and deletion remain in the active WAL.
    put(&mut database, "linear-replay", &0_u32.to_be_bytes(), b"new");
    delete(&mut database, "linear-replay", &191_u32.to_be_bytes());
    std::mem::forget(database);
    vfs.crash().unwrap();
    let before = crate::history::decode_work();
    let started = std::time::Instant::now();
    let recovered = reopen(&vfs);
    let work = crate::history::decode_work().since(before);
    println!("overlay_replay: elapsed_ns={} work={work:?}", started.elapsed().as_nanos());
    for index in 0..192_u32 {
        let expected = match index { 0 => Some(b"new".to_vec()), 191 => None,
            _ => Some((index % 64).to_be_bytes().to_vec()) };
        assert_eq!(get(&recovered, "linear-replay", &index.to_be_bytes()), expected);
    }
    assert_eq!(work.attempts, 3, "recovery must decode each required segment once, regardless of transaction count");
    assert_eq!(work.transactions, 192);
}


fn private_merge_fixture() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let (vfs, mut database) = overlay_authority();
    let mut transaction = database.begin(TENANT, OWNER).unwrap();
    for key in 0..20_u32 {
        database.entity_put(&mut transaction, entity_key("private-merge", &key.to_be_bytes()), b"base".to_vec()).unwrap();
    }
    database.commit(transaction).unwrap();
    database.merge_entity_overlay_fully().unwrap();
    database.checkpoint_wal_window().unwrap();
    let mut transaction = database.begin(TENANT, OWNER).unwrap();
    for key in 0..20_u32 {
        database.entity_put(&mut transaction, entity_key("private-merge", &key.to_be_bytes()), b"captured".to_vec()).unwrap();
    }
    database.commit(transaction).unwrap();
    database.configure_hot_data_caches(0, 0).unwrap();
    (vfs, database)
}

fn write_private_merge_suffix(database: &mut AuthorityDatabase) {
    put(database, "private-merge", &0_u32.to_be_bytes(), b"latest");
    delete(database, "private-merge", &1_u32.to_be_bytes());
    put(database, "private-merge", &20_u32.to_be_bytes(), b"new suffix");
}

fn assert_private_merge_latest(database: &AuthorityDatabase) {
    for key in 0..=20_u32 {
        let expected = match key { 0 => Some(b"latest".to_vec()), 1 => None,
            20 => Some(b"new suffix".to_vec()), _ => Some(b"captured".to_vec()) };
        assert_eq!(get(database, "private-merge", &key.to_be_bytes()), expected);
    }
}

#[test]
fn private_overlay_merge_preserves_concurrent_suffix_and_old_reader_across_rotation() {
    let (vfs, mut database) = private_merge_fixture();
    let old = database.build_read_replica().unwrap();
    let capture = database.prepare_entity_overlay_merge().unwrap().unwrap();
    vfs.arm_fault(None).unwrap();
    let encoded = capture.encode().unwrap();
    let trace = vfs.trace().unwrap();
    assert!(trace.contains(&Operation::Read), "cold preparation must exercise the real page-source read path");
    assert!(!trace.iter().any(|operation| matches!(operation, Operation::Write | Operation::SetLen | Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory | Operation::Rename | Operation::RemoveFile | Operation::CreateDir)),
        "private encoding must not publish or sync shared pages");
    let retained_private_bytes = database.snapshot_pin_metrics().unwrap().retained_transaction_bytes;
    assert!(retained_private_bytes > 0);
    let preparation_reads = trace.iter().filter(|operation| **operation == Operation::Read).count();
    write_private_merge_suffix(&mut database);
    database.checkpoint_wal_window().unwrap();
    vfs.arm_fault(None).unwrap();
    assert!(database.finish_entity_overlay_merge(encoded).unwrap());
    assert!(!vfs.trace().unwrap().contains(&Operation::Read),
        "publication must reuse prepared pages, not repeat the old-tree walk under authority");
    println!("private_merge_work: preparation_reads={preparation_reads} publication_reads=0 retained_private_bytes={retained_private_bytes}");
    assert_private_merge_latest(&database);
    for key in 0..20_u32 { assert_eq!(get(&old, "private-merge", &key.to_be_bytes()), Some(b"captured".to_vec())); }
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    let crash = Arc::new(vfs.fork_durable().unwrap());
    let recovered = reopen(&crash);
    assert_private_merge_latest(&recovered);
}

#[test]
fn private_overlay_merge_rejects_changed_root_before_any_shared_publication() {
    let (vfs, mut database) = private_merge_fixture();
    let encoded = database.prepare_entity_overlay_merge().unwrap().unwrap().encode().unwrap();
    database.merge_entity_overlay_fully().unwrap();
    write_private_merge_suffix(&mut database);
    vfs.arm_fault(None).unwrap();
    assert_eq!(database.finish_entity_overlay_merge(encoded).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    assert!(vfs.trace().unwrap().is_empty(), "stale publication must not stage or write any shared pages");
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
    assert_private_merge_latest(&database);
}

#[test]
fn private_overlay_merge_fault_matrix_preserves_acknowledged_suffix_before_drop_catchup() {
    fn run(database: &mut AuthorityDatabase, capture: crate::entity::OverlayMergePreparation) -> io::Result<()> {
        let encoded = capture.encode()?;
        database.finish_entity_overlay_merge(encoded)?;
        database.checkpoint_wal_window()?;
        Ok(())
    }
    let (vfs, mut baseline) = private_merge_fixture();
    let capture = baseline.prepare_entity_overlay_merge().unwrap().unwrap();
    write_private_merge_suffix(&mut baseline);
    vfs.arm_fault(None).unwrap();
    run(&mut baseline, capture).unwrap();
    let trace = vfs.trace().unwrap();
    assert!(trace.contains(&Operation::Read));
    assert!(trace.contains(&Operation::Write));
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        if matches!(operation, Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory) { actions.push(FaultAction::DropSync); }
        for action in actions {
            let (vfs, mut database) = private_merge_fixture();
            let capture = database.prepare_entity_overlay_merge().unwrap().unwrap();
            write_private_merge_suffix(&mut database);
            vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
            let _ = run(&mut database, capture);
            // Fork durable bytes before Drop can merge or checkpoint again.
            let crash = Arc::new(vfs.fork_durable().unwrap());
            let recovered = reopen(&crash);
            assert_private_merge_latest(&recovered);
            assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0,
                "fault {action:?} at operation {} leaked preparation memory", index + 1);
        }
    }
}

#[test]
fn private_overlay_merge_retains_partial_key_cursor_and_empty_tail_case() {
    let (vfs, mut database) = overlay_authority();
    let mut transaction = database.begin(TENANT, OWNER).unwrap();
    for key in 0..80_u32 {
        database.entity_put(&mut transaction, entity_key("partial-private", &key.to_be_bytes()), vec![1]).unwrap();
    }
    database.commit(transaction).unwrap();
    let encoded = database.prepare_entity_overlay_merge().unwrap().unwrap().encode().unwrap();
    assert!(database.finish_entity_overlay_merge(encoded).unwrap());
    assert_eq!(database.entity_overlay_entries_for_test(), 16);
    let crash = Arc::new(vfs.fork_durable().unwrap());
    let mut recovered = reopen(&crash);
    assert_eq!(recovered.entity_overlay_entries_for_test(), 16);
    let encoded = recovered.prepare_entity_overlay_merge().unwrap().unwrap().encode().unwrap();
    assert!(recovered.finish_entity_overlay_merge(encoded).unwrap());
    assert_eq!(recovered.entity_overlay_entries_for_test(), 0);
    let second = Arc::new(crash.fork_durable().unwrap());
    let recovered = reopen(&second);
    for key in 0..80_u32 { assert_eq!(get(&recovered, "partial-private", &key.to_be_bytes()), Some(vec![1])); }
}
