//! Shared allocation, rollback and joined pool lifetime, with real blocked jobs.
use std::{
    collections::BTreeSet,
    sync::{mpsc, Arc, Barrier},
    time::Duration,
};
use tofu_runtime::{
    capacity::{Demand, Error, Snapshot},
    capacity_allocation::Allocator,
    executor::Executor,
    tool_scheduler::Scheduler,
};
fn snapshot(bytes: Option<u64>) -> Snapshot {
    Snapshot {
        host_cpus: 8,
        affinity_cpus: Some(8),
        quota_cpus: None,
        effective_cpus: 8,
        memory_capacity_bytes: bytes,
        memory_available_bytes: bytes,
        volume_total_bytes: None,
        volume_available_bytes: None,
    }
}
fn demand() -> Demand {
    Demand {
        worker_bytes: 1024,
        job_bytes: 256,
        queued_jobs_per_worker: 4,
        memory_divisor: 2,
        cpu_units_per_worker: 1000,
    }
}
#[test]
fn mixed_pools_share_one_budget_and_release_on_close_or_drop() {
    let allocator = Allocator::new(snapshot(Some(8192)), 1).unwrap();
    let mut executor = Executor::with_capacity(&allocator, demand()).unwrap();
    let scheduler = Scheduler::with_capacity(&allocator, demand(), BTreeSet::new()).unwrap();
    let s = allocator.snapshot().unwrap();
    assert_eq!((s.pools, s.workers, s.weighted_bytes), (2, 4, 8192));
    let extra = Executor::with_capacity(&allocator, demand()).unwrap();
    drop(extra);
    scheduler.close().unwrap();
    scheduler.close().unwrap();
    assert_eq!(allocator.snapshot().unwrap().weighted_bytes, 4096);
    executor.close();
    executor.close();
    assert_eq!(allocator.snapshot().unwrap().weighted_bytes, 0);
    let _pool = Executor::with_capacity(&allocator, demand()).unwrap();
    assert_eq!(allocator.snapshot().unwrap().pools, 1);
    drop(_pool);
    assert_eq!(allocator.snapshot().unwrap().pools, 0);
}
#[test]
fn parallel_constructors_accept_all_owners_including_unknown_memory() {
    for bytes in [Some(8192), None] {
        let allocator = Allocator::new(snapshot(bytes), 1).unwrap();
        let barrier = Arc::new(Barrier::new(16));
        let release = Arc::new(Barrier::new(17));
        let (tx, rx) = mpsc::channel();
        std::thread::scope(|scope| {
            for _ in 0..16 {
                let allocator = allocator.clone();
                let barrier = barrier.clone();
                let release = release.clone();
                let tx = tx.clone();
                scope.spawn(move || {
                    barrier.wait();
                    let pool = Executor::with_capacity(&allocator, demand());
                    tx.send(pool.is_ok()).unwrap();
                    release.wait();
                    drop(pool);
                });
            }
            let accepted = (0..16)
                .filter(|_| rx.recv_timeout(Duration::from_secs(2)).unwrap())
                .count();
            assert_eq!(accepted, 16);
            let state = allocator.snapshot().unwrap();
            assert_eq!(state.pools, accepted);
            assert_eq!(state.pools, 16);
            release.wait();
        });
        assert_eq!(allocator.snapshot().unwrap().weighted_bytes, 0);
    }
}
#[test]
fn blocked_job_retains_allocation_until_close_has_joined() {
    let allocator = Allocator::new(snapshot(Some(8192)), 1).unwrap();
    let mut executor = Executor::with_capacity(&allocator, demand()).unwrap();
    let (entered, rx) = mpsc::channel();
    let (release, wait) = mpsc::channel();
    let job = executor
        .submit(256, move || {
            entered.send(()).unwrap();
            wait.recv().unwrap();
            42
        })
        .unwrap();
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let (closed, done) = mpsc::channel();
    let closer = std::thread::spawn(move || {
        executor.close();
        closed.send(()).unwrap();
    });
    assert!(done.recv_timeout(Duration::from_millis(30)).is_err());
    assert_eq!(allocator.snapshot().unwrap().pools, 1);
    allocator.close().unwrap();
    assert!(matches!(
        Executor::with_capacity(&allocator, demand()),
        Err(Error::Closed)
    ));
    release.send(()).unwrap();
    assert_eq!(job.wait().unwrap(), 42);
    done.recv_timeout(Duration::from_secs(2)).unwrap();
    closer.join().unwrap();
    let s = allocator.snapshot().unwrap();
    assert_eq!((s.pools, s.workers, s.weighted_bytes), (0, 0, 0));
}
#[test]
fn invalid_demand_leaves_no_charge_and_independent_pools_share_observations() {
    let allocator = Allocator::new(snapshot(Some(1000000)), 1).unwrap();
    let invalid = Demand {
        job_bytes: 0,
        ..demand()
    };
    assert!(matches!(
        Executor::with_capacity(&allocator, invalid),
        Err(Error::InvalidDemand)
    ));
    assert_eq!(allocator.snapshot().unwrap().pools, 0);
    let pool = Executor::with_capacity(
        &allocator,
        Demand {
            memory_divisor: 1,
            cpu_units_per_worker: 1000,
            ..demand()
        },
    )
    .unwrap();
    assert_eq!(allocator.snapshot().unwrap().workers, 8);
    let other = Scheduler::with_capacity(&allocator, demand(), BTreeSet::new()).unwrap();
    other.close().unwrap();
    drop(pool);
    assert_eq!(allocator.snapshot().unwrap().workers, 0);
}

#[test]
fn io_occupancy_can_expand_workers_without_multiplying_cpu_or_memory_budget() {
    let mut measured = snapshot(Some(1048576));
    measured.effective_cpus = 2;
    let allocator = Allocator::new(measured, 1).unwrap();
    let io = Demand {
        cpu_units_per_worker: 250,
        memory_divisor: 1,
        ..demand()
    };
    let pool = Executor::with_capacity(&allocator, io).unwrap();
    let s = allocator.snapshot().unwrap();
    assert_eq!((s.workers, s.cpu_units, s.cpu_limit), (8, 2000, 2000));
    assert_eq!(s.weighted_bytes, 16384);
    let extra = Executor::with_capacity(&allocator, demand()).unwrap();
    drop(extra);
    drop(pool);
    assert_eq!(allocator.snapshot().unwrap().cpu_units, 0);
    let cpu = Executor::with_capacity(
        &allocator,
        Demand {
            memory_divisor: 1,
            ..demand()
        },
    )
    .unwrap();
    assert_eq!(allocator.snapshot().unwrap().workers, 2);
    drop(cpu);
    for weight in [0, 1001] {
        assert!(matches!(
            Executor::with_capacity(
                &allocator,
                Demand {
                    cpu_units_per_worker: weight,
                    ..demand()
                }
            ),
            Err(Error::InvalidDemand)
        ));
    }
    assert_eq!(allocator.snapshot().unwrap().weighted_bytes, 0);
}
