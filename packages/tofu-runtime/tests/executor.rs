//! Native ownership contracts: bounds, unwind, receiver disposal and teardown.
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use tofu_runtime::executor::{AdmissionError, Budget, Executor, TaskError};

fn runtime(jobs: usize, bytes: usize) -> Executor {
    Executor::new(Budget {
        workers: 2,
        max_inflight_jobs: jobs,
        max_inflight_bytes: bytes,
    })
    .unwrap()
}

#[test]
fn invalid_budget_and_lazy_start() {
    assert!(matches!(
        Executor::new(Budget {
            workers: 0,
            max_inflight_jobs: 1,
            max_inflight_bytes: 1
        }),
        Err(AdmissionError::InvalidBudget)
    ));
    let executor = runtime(2, 16);
    assert_eq!(executor.resident_workers(), 0);
    assert_eq!(executor.snapshot().unwrap().inflight_bytes, 0);
}

#[test]
fn all_independent_jobs_start_above_idle_and_legacy_byte_hints() {
    use std::sync::Barrier;
    use std::time::Instant;
    let mut executor = Executor::new(Budget {
        workers: 1,
        max_inflight_jobs: 1,
        max_inflight_bytes: 1,
    })
    .unwrap();
    let barrier = Arc::new(Barrier::new(33));
    let started = Instant::now();
    let tasks = (0..32)
        .map(|index| {
            let barrier = barrier.clone();
            executor
                .submit(1024, move || {
                    barrier.wait();
                    index
                })
                .unwrap()
        })
        .collect::<Vec<_>>();
    assert_eq!(executor.snapshot().unwrap().inflight_jobs, 32);
    assert_eq!(executor.snapshot().unwrap().inflight_bytes, 32768);
    assert_eq!(executor.resident_workers(), 32);
    barrier.wait();
    for (index, task) in tasks.into_iter().enumerate() {
        assert_eq!(task.wait(), Ok(index));
    }
    executor.close();
    assert_eq!(executor.snapshot().unwrap().inflight_jobs, 0);
    assert_eq!(executor.snapshot().unwrap().inflight_bytes, 0);
    assert_eq!(executor.resident_workers(), 0);
    eprintln!(
        "32 native jobs / idle hint 1: {:?}; 0 workers",
        started.elapsed()
    );
}

#[test]
fn declared_job_weight_above_legacy_hint_still_runs() {
    let mut executor = runtime(4, 8);
    assert_eq!(executor.submit(9, || 19).unwrap().wait(), Ok(19));
    executor.close();
}

#[test]
fn unwind_returns_typed_failure_and_worker_remains_usable() {
    let mut executor = runtime(2, 16);
    assert_eq!(
        executor
            .submit(8, || -> u64 { panic!("owned native job failure") })
            .unwrap()
            .wait(),
        Err(TaskError::Panicked)
    );
    assert_eq!(executor.submit(8, || 19).unwrap().wait(), Ok(19));
    assert_eq!(executor.snapshot().unwrap().completed_jobs, 2);
}

#[test]
fn dropped_receiver_does_not_leak_admission_or_detach_work() {
    let mut executor = runtime(2, 16);
    let count = Arc::new(AtomicUsize::new(0));
    let capture = Arc::clone(&count);
    let task = executor
        .submit(8, move || capture.fetch_add(1, Ordering::SeqCst))
        .unwrap();
    drop(task);
    executor.close();
    assert_eq!(count.load(Ordering::SeqCst), 1);
    assert_eq!(executor.snapshot().unwrap().inflight_jobs, 0);
    assert_eq!(executor.resident_workers(), 0);
    assert!(matches!(
        executor.submit(0, || ()),
        Err(AdmissionError::Closed)
    ));
    executor.close();
}

#[test]
fn close_drains_queued_work_instead_of_losing_accepted_receipts() {
    let mut executor = Executor::new(Budget {
        workers: 1,
        max_inflight_jobs: 3,
        max_inflight_bytes: 24,
    })
    .unwrap();
    let first = executor.submit(8, || 11).unwrap();
    let second = executor.submit(8, || 22).unwrap();
    let third = executor.submit(8, || 33).unwrap();
    executor.close();
    assert_eq!(first.wait(), Ok(11));
    assert_eq!(second.wait(), Ok(22));
    assert_eq!(third.wait(), Ok(33));
    assert_eq!(executor.snapshot().unwrap().completed_jobs, 3);
}
