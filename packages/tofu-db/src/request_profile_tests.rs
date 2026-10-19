//! Deterministic accounting, privacy, bounded retention and request isolation.
use super::*;

fn state() -> RequestState {
    RequestState { sample: Sample::new(1, "record.get", None), frames: Vec::new(),
        next_span: 0, covered: 0 }
}

#[test]
fn nested_and_recursive_spans_partition_dispatch_without_double_counting() {
    let mut state = state();
    let parent = state.enter(0).unwrap();
    let child = state.enter(0).unwrap();
    state.exit(child, 20);
    let child = state.enter(1).unwrap();
    state.exit(child, 30);
    state.exit(parent, 100);
    let sample = state.finish(150, "ok");
    assert_eq!(sample.stages[0].inclusive, 120);
    assert_eq!(sample.stages[0].exclusive, 70);
    assert_eq!(sample.stages[1].exclusive, 30);
    assert_eq!(sample.unattributed, 50);
    assert_eq!(sample.invalid_spans, 0);
    assert_eq!(sample.stages.iter().map(|s| s.exclusive).sum::<u64>() + sample.unattributed, 150);
}

#[test]
fn crossing_unclosed_or_overflowing_spans_invalidate_accounting() {
    let mut crossing = state();
    let first = crossing.enter(0).unwrap();
    let second = crossing.enter(1).unwrap();
    crossing.exit(first, 5);
    crossing.exit(second, 8);
    assert!(crossing.finish(12, "ok").invalid_spans > 0);
    let mut overflow = state();
    for _ in 0..MAX_DEPTH { assert!(overflow.enter(0).is_some()); }
    assert!(overflow.enter(0).is_none());
    assert_eq!(overflow.frames.len(), MAX_DEPTH);
    assert!(overflow.finish(100, "ok").invalid_spans > 0);
}

#[test]
fn bounded_slowest_samples_keep_full_operation_histograms_and_errors() {
    let mut collector = Collector::default();
    for n in 1..=200 {
        let mut sample = Sample::new(n, "record.get", Some(n));
        sample.dispatch_nanos = n;
        sample.outcome = if n % 3 == 0 { "response_error" } else { "ok" };
        collector.add(sample);
    }
    assert_eq!(collector.slow.len(), SLOW_SAMPLES);
    assert_eq!(collector.slow.iter().map(|s| s.dispatch_nanos).min(), Some(169));
    let aggregate = &collector.operations[0];
    assert_eq!(aggregate.requests, 200);
    assert_eq!(aggregate.errors, 66);
    assert_eq!(aggregate.histogram.iter().sum::<u64>(), 200);
    assert_eq!(aggregate.queue_samples, 200);
    assert_eq!(aggregate.total.queue_nanos, Some(20100));
    let output = aggregate_json(aggregate);
    assert_eq!(output["kind"], "aggregate");
    assert_eq!(output["requests"], 200);
    assert_eq!(output["errors"], 66);
    assert!(output.get("outcome").is_none());
    assert!(output.get("sample").is_none());
    // Real slow samples still carry their actual outcomes and identities.
    assert_eq!(sample_json(&collector.slow[0])["sample"], collector.slow[0].sequence);
    assert_eq!(sample_json(&collector.slow[0])["outcome"], collector.slow[0].outcome);
}

#[test]
fn unknown_names_do_not_create_labels_or_leak_request_text() {
    let name = operation_name("private-user-id/secret-key/request-body");
    assert_eq!(name, "uncataloged");
    let encoded = sample_json(&Sample::new(1, name, None)).to_string();
    assert!(!encoded.contains("secret"));
    for op in crate::generated_storage_operations::STORAGE_OPERATIONS {
        assert_eq!(operation_name(op.name), op.name);
    }
}

#[test]
fn request_context_does_not_leak_across_threads_reuse_or_unwind() {
    assert!(enter(0).is_none());
    let guard = RequestGuard::begin(99, "record.get").unwrap();
    work(Work::EntityGet, 7);
    std::thread::spawn(|| { work(Work::EntityGet, 100); assert!(enter(0).is_none()); }).join().unwrap();
    ACTIVE.with(|s| assert_eq!(s.borrow().as_ref().unwrap().sample.work[0], 7));
    assert!(RequestGuard::begin(100, "record.get").is_none());
    drop(guard);
    assert!(ACTIVE.with(|s| s.borrow().is_none()));
    let _ = std::panic::catch_unwind(|| {
        let _guard = RequestGuard::begin(101, "record.get").unwrap();
        let _probe = Probe::new(VFS_READ);
        panic!("diagnostic unwind test");
    });
    assert!(ACTIVE.with(|s| s.borrow().is_none()));
    let guard = RequestGuard::begin(102, "record.get").unwrap();
    ACTIVE.with(|s| assert_eq!(s.borrow().as_ref().unwrap().sample.work, [0; WORK_NAMES.len()]));
    drop(guard);
}

#[test]
fn queue_is_separate_and_restored_after_each_worker_job() {
    assert_eq!(QUEUE.with(|q| q.get()), None);
    {
        let _queue = QueueContext::new(Some(800));
        let guard = RequestGuard::begin(123, "record.get").unwrap();
        ACTIVE.with(|s| assert_eq!(s.borrow().as_ref().unwrap().sample.queue_nanos, Some(800)));
        drop(guard);
    }
    assert_eq!(QUEUE.with(|q| q.get()), None);
}

#[test]
fn real_vfs_errors_remain_visible_without_retaining_paths() {
    use crate::vfs::{RealVfs, Vfs, OpenRequest};
    let guard = RequestGuard::begin(234, "record.get").unwrap();
    let missing = std::env::temp_dir().join(format!("tofu-profiler-missing-{}", uuid::Uuid::new_v4()));
    assert!(RealVfs.open(&missing, OpenRequest { read: true, ..Default::default() }).is_err());
    ACTIVE.with(|s| {
        let state = s.borrow(); let state = state.as_ref().unwrap();
        assert_eq!(state.sample.stages[VFS_OPEN].calls, 1);
        assert_eq!(state.sample.invalid_spans, 0);
        assert!(state.frames.is_empty());
        assert!(!sample_json(&state.sample).to_string().contains("tofu-profiler-missing"));
    });
    drop(guard);
}

#[test]
fn authority_commit_diagnostics_are_opt_in_and_partition_ir_commit() {
    let guard = RequestGuard::begin(901, "record.put").unwrap();
    let timer = crate::fsync_stats::StageTimer::diagnostic(
        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_VALIDATE_REBASE);
    assert_eq!(timer.is_some(), enabled());
    drop(timer);
    ACTIVE.with(|active| assert_eq!(active.borrow().as_ref().unwrap().sample.stages[
        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_VALIDATE_REBASE].calls,
        u64::from(enabled())));
    drop(guard);
    let mut state = state();
    let parent = state.enter(crate::fsync_stats::STAGE_IR_COMMIT).unwrap();
    for stage in [
        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_VALIDATE_REBASE,
        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_PRIVATE_BLOB_PUBLISH,
        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_INDEXES,
        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_OUTBOX_CAPTURE,
        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_STREAM_PREPARE,
    ] {
        let child = state.enter(stage).unwrap();
        state.exit(child, 10);
    }
    state.exit(parent, 100);
    let sample = state.finish(120, "ok");
    assert_eq!(sample.invalid_spans, 0);
    assert_eq!(sample.stages[crate::fsync_stats::STAGE_IR_COMMIT].exclusive, 50);
    assert_eq!(sample.unattributed, 20);
    assert_eq!(sample.stages.iter().map(|stage| stage.exclusive).sum::<u64>(), 100);
    let encoded = sample_json(&sample).to_string();
    assert!(encoded.contains("authority_commit_private_blob_publish"));
    assert!(!encoded.contains("text-conversation"));
}

#[test]
fn overlay_validation_probes_keep_fixed_labels_and_charge_the_existing_stage_arrays() {
    let indexes = [crate::fsync_stats::STAGE_OVERLAY_VALIDATE_HEAD_VERSIONS,
        crate::fsync_stats::STAGE_OVERLAY_VALIDATE_ORIGINAL_BLIND_VERSIONS,
        crate::fsync_stats::STAGE_OVERLAY_VALIDATE_RANGE_WITNESSES];
    let names = ["overlay_validate_head_versions", "overlay_validate_original_blind_versions",
        "overlay_validate_range_witnesses"];
    let mut request = state();
    for index in indexes {
        let span = request.enter(index).unwrap();
        request.exit(span, 7);
    }
    let sample = request.finish(21, "ok");
    assert_eq!(sample.invalid_spans, 0);
    let encoded = sample_json(&sample);
    let stages = encoded["stages"].as_array().unwrap();
    assert_eq!(stages.len(), 3);
    for (row, expected) in stages.iter().zip(names) {
        assert_eq!(row["stage"], expected);
        assert_eq!(row["calls"], 1);
        assert_eq!(row["exclusiveNanos"], 7);
    }
    assert_eq!(sample.stages.len(), crate::fsync_stats::STAGE_NAMES.len() + EXTRA_NAMES.len());
    assert_eq!(std::mem::size_of_val(&sample.stages), STAGES * std::mem::size_of::<Stage>());
    assert_eq!(std::mem::size_of::<Stage>(), 24);
    // Active and retained byte reports use size_of<RequestState/Sample/Aggregate>,
    // which already include every fixed stage slot, including these three.
    assert!(std::mem::size_of::<RequestState>() >= std::mem::size_of_val(&sample.stages));
    assert!(std::mem::size_of::<Aggregate>() >= std::mem::size_of_val(&sample.stages));
}

#[test]
fn completed_search_backfill_reuses_checkpoint_without_another_read() {
    // Keep this fixture beside the thread-local work accounting so it does
    // not add production counters or depend on process-wide profiler config.
    use crate::authority::AuthorityDatabase;
    use crate::maintenance::MaintenanceScope;
    use crate::turn_search_projection::TurnSearchProjection;
    use crate::turn_search_worker::process_turn_search_batch;
    use std::sync::{Arc, Mutex};

    let source_directory = tempfile::tempdir().unwrap();
    let target_directory = tempfile::tempdir().unwrap();
    let authority = Arc::new(Mutex::new(
        AuthorityDatabase::initialize(source_directory.path()).unwrap(),
    ));
    let projection = Arc::new(Mutex::new(
        TurnSearchProjection::initialize(target_directory.path(), 8 * 1024 * 1024).unwrap(),
    ));
    let scope = MaintenanceScope::new(7, 11).unwrap();
    process_turn_search_batch(&authority, &projection, scope).unwrap();
    assert!(!projection.lock().unwrap().owner_indexing(7, 11).unwrap());
    let before = authority.lock().unwrap().query_snapshot();

    let guard = RequestGuard::begin(902, "uncataloged").unwrap();
    let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
    let entity_gets = ACTIVE.with(|state| {
        state.borrow().as_ref().unwrap().sample.work[Work::EntityGet as usize]
    });
    drop(guard);
    assert_eq!(metrics.dirty_conversations, 0);
    assert_eq!(metrics.source_pages, 0);
    assert_eq!(authority.lock().unwrap().query_snapshot(), before);
    assert_eq!(authority.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0);
    // Whole-batch accounting includes two source activation reads and four
    // target format/gram/indexing reads. Only the checkpoint read is reusable.
    let required_non_checkpoint_reads = 2 + 4;
    eprintln!("completed_search_backfill_batch_entity_gets={entity_gets}");
    assert_eq!(entity_gets, required_non_checkpoint_reads + 1,
        "reuse consume_page's checkpoint, preserving activation and target checks");
}
