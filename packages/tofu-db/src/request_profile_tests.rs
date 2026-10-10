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
