//! Deterministic fault-injection matrix for the online two-mark epoch
//! collector (docs/TOFUDB_ONLINE_GC.md, test plan tier 2). Mirrors the
//! offline `every_*_fault` families in `authority_gc.rs`: enumerate every
//! simulated VFS operation of a collector phase, inject one fault at a time
//! (error before, short write, silently dropped sync), and prove that every
//! live block survives, that reopening always validates the alternating
//! CONTROL slots, and that a fault-free rerun converges.
//!
//! Campaigns run against `DeterministicVfs::fork_durable` crash images of one
//! seeded fixture, exactly like the engine publish-fault matrix: faults are
//! targeted by (operation kind, occurrence) with a retry-until-fired loop so
//! randomized segment/temp UUIDs can never silently skip a case.

use super::tests::{
    assert_live_value, authority, create_temporary_block_file, metrics, run_epoch, test_budget,
};
use super::*;
use crate::entity::EntityKey;
use crate::vfs::{sync_all_barrier, sync_directory_barrier, DeterministicVfs, FaultAction, OpenRequest, Operation};

/// Reopens one seeded crash image for a campaign case.
fn open_fork(seed: &Arc<DeterministicVfs>) -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let vfs = Arc::new(seed.fork_durable().unwrap());
    let database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, Arc::new(Mutex::new(database)))
}

/// One fault case per simulated operation, mirroring the offline campaigns:
/// every operation gets an error injection, writes additionally get a short
/// write, and sync barriers additionally get one silently dropped sync.
/// Occurrences are counted within `trace` so `arm_operation_fault` hits the
/// exact operation the baseline enumerated.
fn fault_cases(trace: &[Operation]) -> Vec<(Operation, usize, FaultAction)> {
    let mut cases = Vec::new();
    for (index, operation) in trace.iter().enumerate() {
        let occurrence = trace[..=index].iter().filter(|op| *op == operation).count();
        cases.push((
            *operation,
            occurrence,
            FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        ));
        match operation {
            Operation::Write => cases.push((*operation, occurrence, FaultAction::ShortWrite(7))),
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory => {
                cases.push((*operation, occurrence, FaultAction::DropSync))
            }
            _ => {}
        }
    }
    cases
}

/// The (kind, occurrence) pair targeting the traced operation at `index`.
fn occurrence_at(trace: &[Operation], index: usize) -> (Operation, usize) {
    let operation = trace[index];
    let occurrence = trace[..=index].iter().filter(|op| **op == operation).count();
    (operation, occurrence)
}

/// Indexes of CONTROL slot writes inside one epoch trace. CONTROL
/// publications reuse the cached handle opened at initialization and always
/// end in the double `sync_all` barrier, so a CONTROL write never follows an
/// `Open` (or a previous write on the same build handle, which covers the
/// segment-create header/index/trailer rewrites) and is always followed by
/// `SyncAll`.
fn control_write_indexes(trace: &[Operation]) -> Vec<usize> {
    trace
        .iter()
        .enumerate()
        .filter(|(index, operation)| {
            **operation == Operation::Write
                && !matches!(
                    index.checked_sub(1).and_then(|previous| trace.get(previous)),
                    Some(Operation::Open | Operation::Write)
                )
                && matches!(trace.get(index + 1), Some(Operation::SyncAll))
        })
        .map(|(index, _)| index)
        .collect()
}

/// Live entity value + committed payload block + one loose orphan block +
/// one staged temporary block file; optionally one unpublished orphan payload
/// segment packing the orphan's bytes. Returns the durable crash image.
struct LooseSeed {
    vfs: Arc<DeterministicVfs>,
    key: EntityKey,
    live_payload_id: BlockId,
    live_payload: Vec<u8>,
    orphan: BlockId,
    temp_path: PathBuf,
}

fn loose_seed(with_orphan_segment: bool) -> LooseSeed {
    let (vfs, database) = authority();
    let key = EntityKey::new(7, 11, "record", b"live").unwrap();
    let orphan;
    let live_payload = b"online-gc-fault-live-payload".to_vec();
    let live_payload_id;
    {
        let mut database_guard = database.lock().unwrap();
        let mut transaction = database_guard.begin(7, 11).unwrap();
        database_guard
            .entity_put(&mut transaction, key.clone(), b"live-value".to_vec())
            .unwrap();
        database_guard.commit(transaction).unwrap();
        live_payload_id = database_guard
            .commit_block_payload_for_test(&live_payload)
            .unwrap();
        orphan = database_guard
            .write_orphan_block_for_test(b"online-gc-fault-orphan")
            .unwrap();
        if with_orphan_segment {
            database_guard
                .create_orphan_payload_segment_for_test(orphan)
                .unwrap();
        }
    }
    let temp_path = create_temporary_block_file(vfs.as_ref(), orphan);
    drop(database);
    vfs.crash().unwrap();
    LooseSeed {
        vfs,
        key,
        live_payload_id,
        live_payload,
        orphan,
        temp_path,
    }
}

/// Same-shard payload pair (mirrors the offline `two_payloads_in_one_shard`):
/// the catalog reference shard is the first block id's shard byte, so a
/// partially-live victim and its retained set must live in one shard for the
/// rewrite publish to keep the catalog shard.
fn two_payloads_in_one_shard() -> (Vec<u8>, Vec<u8>) {
    let mut first_by_shard = std::collections::BTreeMap::<u8, Vec<u8>>::new();
    for value in 0..10_000_u32 {
        let payload = format!("online-gc-rewrite-pair-{value}").into_bytes();
        let shard = BlockId::for_payload(&payload).0[0];
        if let Some(first) = first_by_shard.remove(&shard) {
            return (first, payload);
        }
        first_by_shard.insert(shard, payload);
    }
    panic!("failed to find same-shard online GC rewrite fixtures");
}

/// One partially-live payload segment: a committed live payload block packed
/// with a dead orphan block (mirrors the offline
/// `authority_with_partially_live_payload_segment` fixture, plus a live
/// entity value for the no-false-delete invariant). With `checkpoint`, the
/// WAL is retired after packing so the segment copy is the ONLY durable copy
/// of the live payload (an un-checkpointed WAL restages it into the pending
/// overlay at reopen and would mask a lost segment on reads).
struct RewriteSeed {
    vfs: Arc<DeterministicVfs>,
    key: EntityKey,
    live_id: BlockId,
    live_payload: Vec<u8>,
    dead_id: BlockId,
    victim_segment: uuid::Uuid,
}

fn rewrite_seed(checkpoint: bool) -> RewriteSeed {
    let (vfs, database) = authority();
    let key = EntityKey::new(7, 11, "record", b"live").unwrap();
    let (live_payload, dead_payload) = two_payloads_in_one_shard();
    let (live_id, dead_id, victim_segment) = {
        let mut database_guard = database.lock().unwrap();
        let mut transaction = database_guard.begin(7, 11).unwrap();
        database_guard
            .entity_put(&mut transaction, key.clone(), b"live-value".to_vec())
            .unwrap();
        database_guard.commit(transaction).unwrap();
        let live_id = database_guard
            .commit_block_payload_for_test(&live_payload)
            .unwrap();
        let dead_id = database_guard
            .write_orphan_block_for_test(&dead_payload)
            .unwrap();
        database_guard
            .compact_payload_blocks_for_test(&[live_id, dead_id])
            .unwrap();
        if checkpoint {
            database_guard.checkpoint_for_test().unwrap();
        }
        let victim_segment =
            database_guard.engine_mut_for_test().payload_segment_references()[0].segment_id;
        (live_id, dead_id, victim_segment)
    };
    drop(database);
    vfs.crash().unwrap();
    RewriteSeed {
        vfs,
        key,
        live_id,
        live_payload,
        dead_id,
        victim_segment,
    }
}

/// Baseline trace of the collector's `traced_epoch`-th epoch on one fork of
/// the seed (epoch 1 runs untraced first when `traced_epoch == 2`, so the
/// two-mark deletion lanes are armed).
fn epoch_trace(seed: &Arc<DeterministicVfs>, traced_epoch: usize) -> Vec<Operation> {
    let (vfs, database) = open_fork(seed);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    if traced_epoch == 2 {
        run_epoch(&mut collector, &database);
    }
    vfs.arm_fault(None).unwrap();
    run_epoch(&mut collector, &database);
    vfs.trace().unwrap()
}

/// Runs one campaign case: prepares a fresh fork, runs the clean first epoch
/// when the faulted epoch is the second, arms the operation fault, and runs
/// the faulted epoch. Retries on fresh forks until the fault actually fires
/// (randomized temp/segment names can shift one interleaving).
fn exercise_epoch_fault(
    seed: &Arc<DeterministicVfs>,
    faulted_epoch: usize,
    operation: Operation,
    occurrence: usize,
    action: FaultAction,
) -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>, OnlineGarbageCollector) {
    for _ in 0..16 {
        let (vfs, database) = open_fork(seed);
        let mut collector = OnlineGarbageCollector::new(test_budget());
        if faulted_epoch == 2 {
            run_epoch(&mut collector, &database);
        }
        vfs.arm_operation_fault(operation, occurrence, action).unwrap();
        let _ = collector.run_epoch(&database, &AtomicBool::new(false));
        if vfs.operation_fault_fired().unwrap() {
            return (vfs, database, collector);
        }
    }
    panic!("unexercised online GC fault {operation:?} occurrence={occurrence} action={action:?}");
}

/// Drops the database without shutdown (no clean Engine drop semantics beyond
/// the latch-aware CONTROL catch-up), folds the simulated volume to its
/// durable crash image, and reopens: the alternating CONTROL slots must
/// validate — never a torn catalog (rule S5).
fn crash_and_reopen(
    vfs: &Arc<DeterministicVfs>,
    database: Arc<Mutex<AuthorityDatabase>>,
) -> Arc<Mutex<AuthorityDatabase>> {
    vfs.arm_fault(None).unwrap();
    drop(database);
    vfs.crash().unwrap();
    Arc::new(Mutex::new(
        AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap(),
    ))
}

/// Scenario 1: every fault point of the mark/scan phase (spill run writes,
/// renames, directory barriers, shard scans). The faulted first epoch must
/// error or abort cleanly without deleting anything; the fault-free rerun
/// completes and reclaims exactly the garbage.
#[test]
fn every_online_epoch_mark_fault_preserves_live_blocks() {
    let seed = loose_seed(false);
    let trace = epoch_trace(&seed.vfs, 1);
    assert!(trace.contains(&Operation::Write));
    assert!(trace.contains(&Operation::Rename));
    assert!(trace.contains(&Operation::SyncDirectory));

    for (operation, occurrence, action) in fault_cases(&trace) {
        let (vfs, database, mut collector) =
            exercise_epoch_fault(&seed.vfs, 1, operation, occurrence, action);
        // The first epoch only establishes marks: no deletion can have
        // happened no matter where the fault landed.
        assert_live_value(&database, &seed.key, b"live-value");
        {
            let database_guard = database.lock().unwrap();
            assert_eq!(
                database_guard.read_block_for_test(seed.live_payload_id).unwrap(),
                seed.live_payload
            );
            assert!(database_guard.read_block_for_test(seed.orphan).is_ok());
        }
        assert!(vfs.metadata(&seed.temp_path).is_ok());
        // Fault-free rerun: two clean epochs confirm and reclaim the orphan
        // and the temporary file; nothing live is ever touched.
        vfs.arm_fault(None).unwrap();
        run_epoch(&mut collector, &database);
        run_epoch(&mut collector, &database);
        assert_live_value(&database, &seed.key, b"live-value");
        {
            let database_guard = database.lock().unwrap();
            assert_eq!(
                database_guard.read_block_for_test(seed.live_payload_id).unwrap(),
                seed.live_payload
            );
            assert_eq!(
                database_guard
                    .read_block_for_test(seed.orphan)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::NotFound
            );
        }
        assert_eq!(
            vfs.metadata(&seed.temp_path).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        let final_metrics = metrics(&collector);
        assert_eq!(final_metrics.total_reclaimed_blocks, 1);
        assert_eq!(final_metrics.total_temporary_files_removed, 1);
    }
}

/// Scenario 6: every fault point of the deleting epoch (temporary-file
/// removal, loose-victim batch, orphan-segment sweep, and the mark/scan work
/// in front of them). After each fault the database is dropped without
/// shutdown and the volume is folded to its durable image: reopening must
/// validate the CONTROL slots, and a fresh collector must converge every
/// lane the faulted epoch only partially finished.
#[test]
fn every_online_epoch_delete_fault_keeps_control_slots_reopenable() {
    let seed = loose_seed(true);
    let trace = epoch_trace(&seed.vfs, 2);
    assert!(trace.contains(&Operation::RemoveFile));
    assert!(trace.contains(&Operation::SyncDirectory));

    for (operation, occurrence, action) in fault_cases(&trace) {
        let (vfs, database, _collector) =
            exercise_epoch_fault(&seed.vfs, 2, operation, occurrence, action);
        let database = crash_and_reopen(&vfs, database);
        assert_live_value(&database, &seed.key, b"live-value");
        {
            let database_guard = database.lock().unwrap();
            assert_eq!(
                database_guard.read_block_for_test(seed.live_payload_id).unwrap(),
                seed.live_payload
            );
        }
        let mut collector = OnlineGarbageCollector::new(test_budget());
        run_epoch(&mut collector, &database);
        run_epoch(&mut collector, &database);
        assert_live_value(&database, &seed.key, b"live-value");
        {
            let database_guard = database.lock().unwrap();
            assert_eq!(
                database_guard.read_block_for_test(seed.live_payload_id).unwrap(),
                seed.live_payload
            );
            assert_eq!(
                database_guard
                    .read_block_for_test(seed.orphan)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::NotFound
            );
        }
        assert_eq!(
            vfs.metadata(&seed.temp_path).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        assert!(
            vfs.read_directory(Path::new("/data/payload-segments"))
                .unwrap()
                .is_empty(),
            "orphan segment swept without leaking: {operation:?} occurrence={occurrence} action={action:?}"
        );
    }
}

/// Scenario 2 (broad half): every fault before the durable catalog mutation
/// of a segment-rewrite epoch — mark/scan, replacement build off-mutex, and
/// the manifest block's loose write. The replacement never becomes catalog
/// referenced on these paths, so the collector's discard-on-error is always
/// safe here; live data must survive every case and a fresh collector must
/// finish the rewrite.
#[test]
fn every_online_epoch_rewrite_build_fault_preserves_live_blocks() {
    let seed = rewrite_seed(false);
    let trace = epoch_trace(&seed.vfs, 2);
    let control_writes = control_write_indexes(&trace);
    assert_eq!(
        control_writes.len(),
        2,
        "rewrite epoch publishes and stabilizes CONTROL: {trace:?}"
    );
    let boundary = control_writes[0];

    for (operation, occurrence, action) in fault_cases(&trace[..boundary]) {
        let (vfs, database, _collector) =
            exercise_epoch_fault(&seed.vfs, 2, operation, occurrence, action);
        let database = crash_and_reopen(&vfs, database);
        // Iron invariant: every live block reads back after the crash.
        assert_live_value(&database, &seed.key, b"live-value");
        {
            let database_guard = database.lock().unwrap();
            assert_eq!(
                database_guard.read_block_for_test(seed.live_id).unwrap(),
                seed.live_payload
            );
        }
        // Convergence: a fresh collector re-confirms the victim two marks in
        // a row and finishes (or observes the finished) rewrite without
        // leaking the replacement or the old segment.
        let mut collector = OnlineGarbageCollector::new(test_budget());
        run_epoch(&mut collector, &database);
        run_epoch(&mut collector, &database);
        assert_live_value(&database, &seed.key, b"live-value");
        {
            let mut database_guard = database.lock().unwrap();
            assert_eq!(
                database_guard.read_block_for_test(seed.live_id).unwrap(),
                seed.live_payload
            );
            assert_eq!(
                database_guard
                    .read_block_for_test(seed.dead_id)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::NotFound
            );
            assert_eq!(database_guard.payload_segment_count_for_test(), 1);
            assert_ne!(
                database_guard.engine_mut_for_test().payload_segment_references()[0].segment_id,
                seed.victim_segment
            );
        }
        // The retired victim is parked, not unlinked by the epoch; the
        // deferred-retirement sweep performs the removal (zero floor here).
        database
            .lock()
            .unwrap()
            .block_store_shared()
            .sweep_deferred_segment_retirements(std::time::Duration::ZERO)
            .unwrap();
        let segments = vfs.read_directory(Path::new("/data/payload-segments")).unwrap();
        assert_eq!(
            segments.len(),
            1,
            "exactly the replacement segment remains: {operation:?} occurrence={occurrence} action={action:?}"
        );
    }
}

/// Scenario 2 (targeted): the replacement segment is built and the fault
/// lands on the manifest publication's CONTROL write. The publication fails
/// before any durable catalog change: the old segment stays a catalog
/// member, every live block stays readable, the unpublished replacement is
/// cleaned (or swept as an orphan), and a clean rerun completes the rewrite.
#[test]
fn online_gc_rewrite_publish_control_fault_keeps_old_segment_until_clean_rerun() {
    let seed = rewrite_seed(false);
    let trace = epoch_trace(&seed.vfs, 2);
    let control_writes = control_write_indexes(&trace);
    assert_eq!(
        control_writes.len(),
        2,
        "rewrite epoch publishes and stabilizes CONTROL: {trace:?}"
    );
    let (operation, occurrence) = occurrence_at(&trace, control_writes[0]);

    let (vfs, database) = open_fork(&seed.vfs);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    vfs.arm_operation_fault(operation, occurrence, FaultAction::ErrorBefore(io::ErrorKind::Interrupted))
        .unwrap();
    let error = collector
        .run_epoch(&database, &AtomicBool::new(false))
        .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::Interrupted);
    assert!(vfs.operation_fault_fired().unwrap());
    // A failed CONTROL publication is durability-ambiguous: the engine
    // latches restart-required by design, so the continuation is a reopen.
    let database = crash_and_reopen(&vfs, database);
    assert_live_value(&database, &seed.key, b"live-value");
    {
        let mut database_guard = database.lock().unwrap();
        assert_eq!(
            database_guard.read_block_for_test(seed.live_id).unwrap(),
            seed.live_payload
        );
        assert!(database_guard.read_block_for_test(seed.dead_id).is_ok());
        assert_eq!(database_guard.payload_segment_count_for_test(), 1);
        assert_eq!(
            database_guard.engine_mut_for_test().payload_segment_references()[0].segment_id,
            seed.victim_segment,
            "old segment is still the catalog member"
        );
    }
    // The publish never became durable, but the collector no longer
    // discards the built replacement on non-WouldBlock errors: it stays as
    // an unpublished orphan that the clean rerun sweeps two epochs later.
    assert_eq!(
        vfs.read_directory(Path::new("/data/payload-segments"))
            .unwrap()
            .len(),
        2,
        "victim plus the unpublished replacement orphan"
    );
    // Clean rerun from a fresh two-mark state completes the rewrite.
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    assert_live_value(&database, &seed.key, b"live-value");
    {
        let mut database_guard = database.lock().unwrap();
        assert_eq!(
            database_guard.read_block_for_test(seed.live_id).unwrap(),
            seed.live_payload
        );
        assert_eq!(
            database_guard
                .read_block_for_test(seed.dead_id)
                .unwrap_err()
                .kind(),
            io::ErrorKind::NotFound
        );
        assert_eq!(database_guard.payload_segment_count_for_test(), 1);
        assert_ne!(
            database_guard.engine_mut_for_test().payload_segment_references()[0].segment_id,
            seed.victim_segment
        );
    }
    assert_eq!(metrics(&collector).total_segments_rewritten, 1);
    // The retired victim is parked; force the deferred sweep to reclaim it
    // before counting files.
    database
        .lock()
        .unwrap()
        .block_store_shared()
        .sweep_deferred_segment_retirements(std::time::Duration::ZERO)
        .unwrap();
    assert_eq!(
        vfs.read_directory(Path::new("/data/payload-segments"))
            .unwrap()
            .len(),
        1
    );
}

/// Scenario 3: the manifest publication succeeded and the deferred sweep's
/// unlink of the parked segment file fails. The error must surface, the
/// reopened catalog must keep serving every retained live block (the
/// published replacement is catalog referenced from that moment on), and the
/// orphaned old segment file must be swept by a later epoch: the in-memory
/// park never survives the crash, so the unreferenced file is an ordinary
/// orphan candidate.
#[test]
fn online_gc_rewrite_old_segment_removal_fault_preserves_live_blocks() {
    let seed = rewrite_seed(true);
    let trace = epoch_trace(&seed.vfs, 2);
    let control_writes = control_write_indexes(&trace);
    assert_eq!(
        control_writes.len(),
        2,
        "rewrite epoch publishes and stabilizes CONTROL: {trace:?}"
    );

    let (vfs, database) = open_fork(&seed.vfs);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    // The rewrite published the replacement and parked the retired victim;
    // the unlink belongs to the deferred-retirement sweep, not the epoch.
    {
        let mut database_guard = database.lock().unwrap();
        assert_ne!(
            database_guard.engine_mut_for_test().payload_segment_references()[0].segment_id,
            seed.victim_segment,
            "replacement is the catalog member"
        );
    }
    // Arm the sweep's unlink of the parked victim to fail.
    vfs.arm_operation_fault(
        Operation::RemoveFile,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
    )
    .unwrap();
    let error = database
        .lock()
        .unwrap()
        .block_store_shared()
        .sweep_deferred_segment_retirements(std::time::Duration::ZERO)
        .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::Interrupted);
    assert!(vfs.operation_fault_fired().unwrap());
    vfs.arm_fault(None).unwrap();
    let database = crash_and_reopen(&vfs, database);
    // The publication was durable: the replacement is the catalog member and
    // must still hold the retained live block.
    assert_live_value(&database, &seed.key, b"live-value");
    {
        let database_guard = database.lock().unwrap();
        assert_eq!(
            database_guard.read_block_for_test(seed.live_id).unwrap(),
            seed.live_payload,
            "published replacement lost the retained live block"
        );
        assert_eq!(
            database_guard
                .read_block_for_test(seed.dead_id)
                .unwrap_err()
                .kind(),
            io::ErrorKind::NotFound
        );
    }
    // The park did not survive the crash, so the unreferenced old segment
    // file is orphan-swept by later epochs: no leak, no false delete.
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    assert_live_value(&database, &seed.key, b"live-value");
    {
        let database_guard = database.lock().unwrap();
        assert_eq!(
            database_guard.read_block_for_test(seed.live_id).unwrap(),
            seed.live_payload
        );
    }
    database.lock().unwrap().block_store_shared()
        .sweep_deferred_segment_retirements(std::time::Duration::ZERO).unwrap();
    let segments = vfs.read_directory(Path::new("/data/payload-segments")).unwrap();
    // Physical layout may include an independently carried coverage cursor.
    // Require exactly the published catalog files, with no orphan or loss.
    let expected = database.lock().unwrap().engine_mut_for_test()
        .payload_segment_references().iter()
        .map(|reference| Path::new("/data/payload-segments").join(format!("{}.pseg", reference.segment_id.simple())))
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(segments.iter().cloned().collect::<std::collections::BTreeSet<_>>(), expected,
        "exact catalog members remain; all retired/orphan files swept");
    assert!(
        !segments.iter().any(|path| path
            .to_string_lossy()
            .contains(&seed.victim_segment.simple().to_string())),
        "retired victim file swept: {segments:?}"
    );
}

/// Scenario 4: the loose-victim batch fails at the second victim's
/// `remove_file`. The batch is partially applied and the epoch reports the
/// error; the next fault-free epoch re-confirms and deletes the survivor
/// (the already-deleted victim never reappears as a candidate — two-mark
/// rule), and every live block stays intact.
#[test]
fn online_gc_loose_batch_mid_removal_fault_recovers_remaining_victims() {
    let (vfs, database) = authority();
    let key = EntityKey::new(7, 11, "record", b"live").unwrap();
    let (first_orphan, second_orphan) = {
        let mut database_guard = database.lock().unwrap();
        let mut transaction = database_guard.begin(7, 11).unwrap();
        database_guard
            .entity_put(&mut transaction, key.clone(), b"live-value".to_vec())
            .unwrap();
        database_guard.commit(transaction).unwrap();
        let first_orphan = database_guard
            .write_orphan_block_for_test(b"online-gc-batch-orphan-a")
            .unwrap();
        let second_orphan = database_guard
            .write_orphan_block_for_test(b"online-gc-batch-orphan-b")
            .unwrap();
        (first_orphan, second_orphan)
    };
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    // The deleting epoch removes the two confirmed victims back to back; the
    // second removal in the trace is the second victim (the marks-run cleanup
    // removal comes last).
    vfs.arm_operation_fault(
        Operation::RemoveFile,
        2,
        FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
    )
    .unwrap();
    let error = collector
        .run_epoch(&database, &AtomicBool::new(false))
        .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::Interrupted);
    assert!(vfs.operation_fault_fired().unwrap());
    assert_live_value(&database, &key, b"live-value");
    let surviving = [first_orphan, second_orphan]
        .into_iter()
        .filter(|block_id| {
            database
                .lock()
                .unwrap()
                .read_block_for_test(*block_id)
                .is_ok()
        })
        .count();
    assert_eq!(surviving, 1, "exactly one victim was removed before the fault");
    // Fault-free rerun: the survivor is re-confirmed and deleted.
    vfs.arm_fault(None).unwrap();
    run_epoch(&mut collector, &database);
    assert_live_value(&database, &key, b"live-value");
    {
        let database_guard = database.lock().unwrap();
        for orphan in [first_orphan, second_orphan] {
            assert_eq!(
                database_guard
                    .read_block_for_test(orphan)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::NotFound
            );
        }
    }
    let final_metrics = metrics(&collector);
    assert_eq!(final_metrics.epochs_completed, 2);
    assert_eq!(final_metrics.deferred_epochs, 1);
    // The partial removal in the faulted epoch is reported, not counted: the
    // metrics only ever publish fully accounted batches.
    assert_eq!(final_metrics.total_reclaimed_blocks, 1);
}

/// Scenario 5: process restart mid-epoch. The database is dropped without
/// shutdown after the mark build of epoch two, the volume is folded to its
/// durable image, and the database is reopened: spill residue under
/// `authority-gc/online/` must be wiped by the next epoch's `SpillMarkSet`
/// initialization, all live data must be intact, and a fresh collector must
/// complete two clean epochs and reclaim the garbage.
#[test]
fn online_gc_process_restart_mid_epoch_recovers_and_cleans_spill_residue() {
    let seed = loose_seed(false);
    let (vfs, database) = open_fork(&seed.vfs);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    // Drive epoch two through the snapshot hold and the off-mutex mark build,
    // then abandon the working state without any cleanup: its spilled mark
    // run stays on disk exactly as a dead process would leave it.
    let epoch = marked_epoch_for_test(&collector, &database);
    drop(epoch);
    drop(collector);
    drop(database);
    vfs.crash().unwrap();
    let marks_runs = Path::new("/data/authority-gc/online/marks/gc-runs");
    let suspects_runs = Path::new("/data/authority-gc/online/suspects-0/gc-runs");
    assert!(
        !vfs.read_directory(marks_runs).unwrap().is_empty(),
        "mid-epoch mark run survived the crash"
    );
    assert!(
        !vfs.read_directory(suspects_runs).unwrap().is_empty(),
        "previous epoch's suspect runs survived the crash"
    );
    // A second, distinctly named stale suspect run proves the next epoch's
    // SpillMarkSet init wipes every stale run, not just the ones it rewrites.
    let stale_run = suspects_runs.join("run-0099.mark");
    {
        let mut file = vfs
            .open(
                &stale_run,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(0, b"stale-suspect-run").unwrap();
        sync_all_barrier(file.as_mut()).unwrap();
        sync_directory_barrier(vfs.as_ref(), suspects_runs).unwrap();
    }
    let database = Arc::new(Mutex::new(
        AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap(),
    ));
    assert_live_value(&database, &seed.key, b"live-value");
    {
        let database_guard = database.lock().unwrap();
        assert_eq!(
            database_guard.read_block_for_test(seed.live_payload_id).unwrap(),
            seed.live_payload
        );
        assert!(database_guard.read_block_for_test(seed.orphan).is_ok());
    }
    assert!(vfs.metadata(&seed.temp_path).is_ok());
    // A fresh collector starts from a clean two-mark state; the first epoch
    // wipes the stale spill runs as part of SpillMarkSet initialization.
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    assert!(
        vfs.read_directory(marks_runs).unwrap().is_empty(),
        "stale mark runs wiped by the next epoch's mark init"
    );
    assert_eq!(
        vfs.metadata(&stale_run).unwrap_err().kind(),
        io::ErrorKind::NotFound,
        "stale suspect runs wiped by the next epoch's suspects init"
    );
    run_epoch(&mut collector, &database);
    assert_live_value(&database, &seed.key, b"live-value");
    {
        let database_guard = database.lock().unwrap();
        assert_eq!(
            database_guard
                .read_block_for_test(seed.orphan)
                .unwrap_err()
                .kind(),
            io::ErrorKind::NotFound
        );
    }
    assert_eq!(
        vfs.metadata(&seed.temp_path).unwrap_err().kind(),
        io::ErrorKind::NotFound
    );
    let final_metrics = metrics(&collector);
    assert_eq!(final_metrics.epochs_completed, 2);
    assert_eq!(final_metrics.total_reclaimed_blocks, 1);
    assert_eq!(final_metrics.total_temporary_files_removed, 1);
}

/// Drives one epoch through the snapshot hold and the off-mutex mark build,
/// then returns the working epoch without running any cleanup (mirrors
/// `run_epoch_inner` phases 1–2; `Epoch` and `SpillMarkSet` have no drop-time
/// cleanup, exactly like a dead process).
fn marked_epoch_for_test(
    collector: &OnlineGarbageCollector,
    database: &Arc<Mutex<AuthorityDatabase>>,
) -> Epoch {
    let capture = database.lock().unwrap().online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    let suspects_directory = capture
        .data_dir
        .join(WORK_DIRECTORY)
        .join(SUSPECTS_DIRECTORIES[collector.suspects_parity]);
    let mut epoch = Epoch {
        started: Instant::now(),
        capture,
        marks: None,
        suspects_directory,
        suspects_next: None,
        temp_candidate_next: None,
        orphan_candidate_next: None,
        segment_dead_next: BTreeMap::new(),
        confirmed_loose: Vec::new(),
        confirmed_temp: None,
        rewrite_victim: None,
        compaction_ids: Vec::new(),
        confirmed_loose_planned: false,
        rewrite_planned: false,
        compaction_planned: false,
        tracked_suspects: 0,
        reclaimed_bytes: 0,
        reclaimed_blocks: 0,
        temporary_files_removed: 0,
        orphan_segments_removed: 0,
        segments_rewritten: 0,
        segments_retired: 0,
        compacted_blocks: 0,
        consolidations: 0,
        consolidated_victims: 0,
        consolidation: None,
        pressure_candidates: Vec::new(),
    };
    collector
        .build_marks(&mut epoch, &AtomicBool::new(false))
        .unwrap();
    epoch
}

/// Scenario 6 (publish lanes): one silently dropped sync at every sync
/// barrier of the rewrite epoch, including the CONTROL slot writes. The
/// doubled barriers absorb each single loss, so the epoch completes; after
/// the crash the reopened CONTROL slots must describe the finished rewrite —
/// never a torn or regressed catalog.
#[test]
fn every_online_epoch_rewrite_sync_loss_keeps_control_slots_reopenable() {
    let seed = rewrite_seed(false);
    let trace = epoch_trace(&seed.vfs, 2);
    let sync_cases = trace
        .iter()
        .enumerate()
        .filter(|(_, operation)| {
            matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            )
        })
        .map(|(index, operation)| (*operation, occurrence_at(&trace, index).1))
        .collect::<Vec<_>>();
    assert!(!sync_cases.is_empty());

    for (operation, occurrence) in sync_cases {
        let mut exercised = None;
        for _ in 0..16 {
            let (vfs, database) = open_fork(&seed.vfs);
            let mut collector = OnlineGarbageCollector::new(test_budget());
            run_epoch(&mut collector, &database);
            vfs.arm_operation_fault(operation, occurrence, FaultAction::DropSync)
                .unwrap();
            // A dropped sync never surfaces an error: the doubled barriers
            // complete the epoch in every case.
            run_epoch(&mut collector, &database);
            if vfs.operation_fault_fired().unwrap() {
                exercised = Some((vfs, database));
                break;
            }
        }
        let (vfs, database) = exercised.unwrap_or_else(|| {
            panic!("unexercised rewrite sync loss {operation:?} occurrence={occurrence}")
        });
        let database = crash_and_reopen(&vfs, database);
        assert_live_value(&database, &seed.key, b"live-value");
        {
            let mut database_guard = database.lock().unwrap();
            assert_eq!(
                database_guard.read_block_for_test(seed.live_id).unwrap(),
                seed.live_payload
            );
            assert_eq!(
                database_guard
                    .read_block_for_test(seed.dead_id)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::NotFound
            );
            assert_eq!(database_guard.payload_segment_count_for_test(), 1);
            assert_ne!(
                database_guard.engine_mut_for_test().payload_segment_references()[0].segment_id,
                seed.victim_segment,
                "rewrite finished and stayed published across the crash"
            );
        }
        // The parked victim did not survive the crash (the park is
        // in-memory); the unreferenced file is orphan-swept by a later
        // epoch before counting files.
        let mut collector = OnlineGarbageCollector::new(test_budget());
        run_epoch(&mut collector, &database);
        run_epoch(&mut collector, &database);
        assert_eq!(
            vfs.read_directory(Path::new("/data/payload-segments"))
                .unwrap()
                .len(),
            1,
            "old segment retired, replacement published: {operation:?} occurrence={occurrence}"
        );
    }
}
