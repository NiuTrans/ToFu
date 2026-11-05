//! Actual overlap, ordered writes, cancellation, admission and joined teardown.
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tofu_runtime::{
    execution_contract::{ExecutionPhase as Phase, ResourceName},
    session::{Registry as Sessions, ReleaseAck},
    tool_contract::*,
    tool_scheduler::*,
};
struct Allow;
impl Authorization for Allow {
    fn allow(&self, owner: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
        owner == 7
    }
}
fn catalog() -> Catalog {
    Catalog::compile(7,"epoch",&json!({"read":{"name":"read","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object"}},"write":{"name":"write","contractVersion":"tofu.tool-contract/v2","permission":"write","arguments_schema":{"type":"object"}}})).unwrap()
}
fn call(c: &Catalog, position: usize, name: &str) -> Invocation {
    Invocation {
        position,
        proof: c
            .admit(7, name, json!({"position":position}), &Value::Null, &Allow)
            .unwrap(),
    }
}
fn budget() -> Budget {
    Budget {
        workers: 2,
        inflight_calls: 8,
        inflight_bytes: 65536,
    }
}
struct Overlap {
    live: Arc<AtomicUsize>,
    peak: Arc<AtomicUsize>,
    entered: std::sync::mpsc::Sender<usize>,
    gate: Arc<(Mutex<bool>, std::sync::Condvar)>,
    releases: Arc<AtomicUsize>,
}
impl Handler for Overlap {
    fn invoke(&self, context: InvocationContext<'_>, value: &Value) -> Result<Value, Value> {
        let released = self.releases.clone();
        context
            .runtime
            .bind_resource(
                ResourceName::ToolEnvironment,
                0,
                false,
                Box::new(move |_| {
                    released.fetch_add(1, Ordering::SeqCst);
                    Ok(ReleaseAck::Released)
                }),
                Box::new(|| {}),
            )
            .unwrap();
        let live = self.live.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(live, Ordering::SeqCst);
        self.entered
            .send(value["position"].as_u64().unwrap() as usize)
            .unwrap();
        let (lock, wake) = &*self.gate;
        let held = lock.lock().unwrap();
        let (held, timeout) = wake
            .wait_timeout_while(held, Duration::from_secs(2), |open| !*open)
            .unwrap();
        assert!(!timeout.timed_out() && *held);
        self.live.fetch_sub(1, Ordering::SeqCst);
        Ok(value.clone())
    }
}
#[test]
fn thirty_two_reads_overlap_with_one_worker_and_byte_hints() {
    let sessions = Sessions::new();
    let parent = sessions.start("parallel", "task", 7, "", None).unwrap();
    let scheduler = Arc::new(
        Scheduler::new(
            Budget {
                workers: 1,
                inflight_calls: 1,
                inflight_bytes: 1,
            },
            BTreeSet::from(["read".into()]),
        )
        .unwrap(),
    );
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
    let (tx, rx) = std::sync::mpsc::channel();
    let gate = Arc::new((Mutex::new(false), std::sync::Condvar::new()));
    let peak = Arc::new(AtomicUsize::new(0));
    let releases = Arc::new(AtomicUsize::new(0));
    let handler = Arc::new(Overlap {
        live: Arc::new(AtomicUsize::new(0)),
        peak: peak.clone(),
        entered: tx,
        gate: gate.clone(),
        releases: releases.clone(),
    });
    let registry = Registry::new(7, "epoch", vec![("read".into(), handler)]).unwrap();
    let worker_scheduler = scheduler.clone();
    let worker_parent = parent.clone();
    let worker_sessions = sessions.clone();
    let thread = std::thread::spawn(move || {
        let c = catalog();
        worker_scheduler
            .dispatch(Batch {
                owner: 7,
                epoch: "epoch",
                task_id: "parallel",
                round: 0,
                parent: worker_parent,
                sessions: &worker_sessions,
                handlers: &registry,
                authorization: &Allow,
                calls: (0..32).rev().map(|p| call(&c, p, "read")).collect(),
            })
            .unwrap()
    });
    let started = std::time::Instant::now();
    for _ in 0..32 {
        rx.recv_timeout(Duration::from_secs(2)).unwrap();
    }
    eprintln!(
        "32 demand tool workers rendezvous in {:?}",
        started.elapsed()
    );
    assert_eq!(peak.load(Ordering::SeqCst), 32);
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 32);
    assert_eq!(releases.load(Ordering::SeqCst), 0);
    *gate.0.lock().unwrap() = true;
    gate.1.notify_all();
    let results = thread.join().unwrap();
    assert_eq!(
        results.iter().map(|r| r.position).collect::<Vec<_>>(),
        (0..32).collect::<Vec<_>>()
    );
    assert_eq!(releases.load(Ordering::SeqCst), 32);
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
    assert_eq!(scheduler.snapshot().unwrap().inflight_calls, 0);
    scheduler.close().unwrap();
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
    parent.settle(Phase::Completed, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}
struct Ordered {
    live: AtomicUsize,
    peak: AtomicUsize,
    order: Mutex<Vec<usize>>,
}
impl Handler for Ordered {
    fn invoke(&self, _: InvocationContext<'_>, value: &Value) -> Result<Value, Value> {
        let n = self.live.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(n, Ordering::SeqCst);
        self.order
            .lock()
            .unwrap()
            .push(value["position"].as_u64().unwrap() as usize);
        std::thread::sleep(Duration::from_millis(5));
        self.live.fetch_sub(1, Ordering::SeqCst);
        Ok(value.clone())
    }
}
#[test]
fn listed_write_tools_remain_ordered_without_starting_pool() {
    let sessions = Sessions::new();
    let parent = sessions.start("writes", "task", 7, "", None).unwrap();
    let scheduler = Scheduler::new(budget(), BTreeSet::from(["write".into()])).unwrap();
    let handler = Arc::new(Ordered {
        live: AtomicUsize::new(0),
        peak: AtomicUsize::new(0),
        order: Mutex::new(vec![]),
    });
    let registry = Registry::new(7, "epoch", vec![("write".into(), handler.clone())]).unwrap();
    let c = catalog();
    let results = scheduler
        .dispatch(Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "writes",
            round: 0,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &registry,
            authorization: &Allow,
            calls: vec![call(&c, 0, "write"), call(&c, 1, "write")],
        })
        .unwrap();
    assert_eq!(results.len(), 2);
    assert_eq!(handler.peak.load(Ordering::SeqCst), 1);
    assert_eq!(*handler.order.lock().unwrap(), vec![0, 1]);
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
    parent.settle(Phase::Completed, "").unwrap();
}
#[test]
fn legacy_count_and_byte_hints_do_not_refuse_valid_batches() {
    let sessions = Sessions::new();
    let parent = sessions.start("capacity", "task", 7, "", None).unwrap();
    let handler = Arc::new(Ordered {
        live: AtomicUsize::new(0),
        peak: AtomicUsize::new(0),
        order: Mutex::new(vec![]),
    });
    let registry = Registry::new(7, "epoch", vec![("read".into(), handler.clone())]).unwrap();
    let c = catalog();
    for b in [
        Budget {
            inflight_calls: 1,
            ..budget()
        },
        Budget {
            inflight_bytes: 1,
            ..budget()
        },
    ] {
        let scheduler = Scheduler::new(b, BTreeSet::from(["read".into()])).unwrap();
        let result = scheduler.dispatch(Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "capacity",
            round: 0,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &registry,
            authorization: &Allow,
            calls: vec![call(&c, 0, "read"), call(&c, 1, "read")],
        });
        assert_eq!(result.unwrap().len(), 2);
        assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
        assert_eq!(scheduler.snapshot().unwrap().inflight_calls, 0);
    }
    assert_eq!(handler.order.lock().unwrap().len(), 4);
    parent.settle(Phase::Completed, "").unwrap();
}
struct WaitStop {
    entered: std::sync::mpsc::Sender<()>,
    calls: Arc<AtomicUsize>,
    released: Arc<AtomicUsize>,
}
impl Handler for WaitStop {
    fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let released = self.released.clone();
        context
            .runtime
            .bind_resource(
                ResourceName::ToolEnvironment,
                0,
                false,
                Box::new(move |_| {
                    released.fetch_add(1, Ordering::SeqCst);
                    Ok(ReleaseAck::Released)
                }),
                Box::new(|| {}),
            )
            .unwrap();
        self.entered.send(()).unwrap();
        let until = std::time::Instant::now() + Duration::from_secs(2);
        while context.runtime.checkpoint().is_ok() {
            assert!(std::time::Instant::now() < until);
            std::thread::sleep(Duration::from_millis(2));
        }
        Ok(json!("late"))
    }
}
#[test]
fn parent_stop_drains_all_demand_workers_and_refuses_later_calls() {
    let sessions = Sessions::new();
    let parent = sessions.start("stop", "task", 7, "", None).unwrap();
    let scheduler = Arc::new(
        Scheduler::new(
            Budget {
                workers: 1,
                ..budget()
            },
            BTreeSet::from(["read".into()]),
        )
        .unwrap(),
    );
    let (tx, rx) = std::sync::mpsc::channel();
    let calls = Arc::new(AtomicUsize::new(0));
    let released = Arc::new(AtomicUsize::new(0));
    let registry = Registry::new(
        7,
        "epoch",
        vec![(
            "read".into(),
            Arc::new(WaitStop {
                entered: tx,
                calls: calls.clone(),
                released: released.clone(),
            }),
        )],
    )
    .unwrap();
    let registry = Arc::new(registry);
    let worker_registry = registry.clone();
    let worker_parent = parent.clone();
    let worker_sessions = sessions.clone();
    let worker_scheduler = scheduler.clone();
    let thread = std::thread::spawn(move || {
        let c = catalog();
        worker_scheduler
            .dispatch(Batch {
                owner: 7,
                epoch: "epoch",
                task_id: "stop",
                round: 0,
                parent: worker_parent,
                sessions: &worker_sessions,
                handlers: &worker_registry,
                authorization: &Allow,
                calls: (0..4).map(|p| call(&c, p, "read")).collect(),
            })
            .unwrap()
    });
    for _ in 0..4 {
        rx.recv_timeout(Duration::from_secs(2)).unwrap();
    }
    parent.request_cancel("user_stop").unwrap();
    let results = thread.join().unwrap();
    assert_eq!(results.len(), 4);
    assert_eq!(calls.load(Ordering::SeqCst), 4);
    assert_eq!(released.load(Ordering::SeqCst), 4);
    assert!(results
        .iter()
        .all(|r| r.outcome.as_ref().unwrap().cancellation_requested));
    let c = catalog();
    let later = scheduler.dispatch(Batch {
        owner: 7,
        epoch: "epoch",
        task_id: "stop",
        round: 1,
        parent: parent.clone(),
        sessions: &sessions,
        handlers: &registry,
        authorization: &Allow,
        calls: vec![call(&c, 4, "read")],
    });
    let later = later.unwrap();
    assert!(later[0].outcome.as_ref().unwrap().cancellation_requested);
    assert_eq!(calls.load(Ordering::SeqCst), 4);
    assert_eq!(released.load(Ordering::SeqCst), 4);
    assert_eq!(scheduler.snapshot().unwrap().inflight_bytes, 0);
    scheduler.close().unwrap();
    parent.settle(Phase::Cancelled, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}

struct Reenter(Arc<Scheduler>);
impl Handler for Reenter {
    fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
        assert_eq!(self.0.close(), Err(Error::ReentrantClose));
        Ok(json!("refused close"))
    }
}
#[test]
fn callback_close_is_refused_in_both_lanes() {
    for name in ["read", "write"] {
        let sessions = Sessions::new();
        let parent = sessions.start(name, "task", 7, "", None).unwrap();
        let allocator = tofu_runtime::capacity_allocation::Allocator::new(
            tofu_runtime::capacity::Snapshot {
                host_cpus: 1,
                affinity_cpus: Some(1),
                quota_cpus: None,
                effective_cpus: 1,
                memory_capacity_bytes: Some(1048576),
                memory_available_bytes: Some(1048576),
                volume_total_bytes: None,
                volume_available_bytes: None,
            },
            1,
        )
        .unwrap();
        let scheduler = Arc::new(
            Scheduler::with_capacity(
                &allocator,
                tofu_runtime::capacity::Demand {
                    worker_bytes: 1024,
                    job_bytes: 16384,
                    queued_jobs_per_worker: 4,
                    memory_divisor: 1,
                    cpu_units_per_worker: 1000,
                },
                BTreeSet::from([name.into()]),
            )
            .unwrap(),
        );
        let registry = Registry::new(
            7,
            "epoch",
            vec![(name.into(), Arc::new(Reenter(scheduler.clone())))],
        )
        .unwrap();
        let c = catalog();
        let results = scheduler
            .dispatch(Batch {
                owner: 7,
                epoch: "epoch",
                task_id: name,
                round: 0,
                parent: parent.clone(),
                sessions: &sessions,
                handlers: &registry,
                authorization: &Allow,
                calls: vec![call(&c, 0, name)],
            })
            .unwrap();
        assert!(results[0].outcome.as_ref().unwrap().error.is_none());
        assert_eq!(allocator.snapshot().unwrap().pools, 1);
        scheduler.close().unwrap();
        assert_eq!(allocator.snapshot().unwrap().pools, 0);
        assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
        parent.settle(Phase::Completed, "").unwrap();
    }
}
struct CleanupFault;
impl Handler for CleanupFault {
    fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
        context
            .runtime
            .bind_resource(
                ResourceName::ToolEnvironment,
                0,
                false,
                Box::new(|_| Err(tofu_runtime::session::ReleaseError)),
                Box::new(|| {}),
            )
            .unwrap();
        Ok(json!("late"))
    }
}
#[test]
fn cleanup_fault_stops_pending_ordered_siblings() {
    let sessions = Sessions::new();
    let parent = sessions.start("cleanup", "task", 7, "", None).unwrap();
    let c = catalog();
    let registry =
        Registry::new(7, "epoch", vec![("write".into(), Arc::new(CleanupFault))]).unwrap();
    let scheduler = Scheduler::new(budget(), BTreeSet::new()).unwrap();
    let results = scheduler
        .dispatch(Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "cleanup",
            round: 0,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &registry,
            authorization: &Allow,
            calls: vec![call(&c, 0, "write"), call(&c, 1, "write")],
        })
        .unwrap();
    assert!(
        !results[0]
            .outcome
            .as_ref()
            .unwrap()
            .receipt
            .invariants_satisfied
    );
    assert!(results[1].outcome.as_ref().unwrap().cancellation_requested);
    assert!(results[1]
        .outcome
        .as_ref()
        .unwrap()
        .receipt
        .resource_dispositions
        .is_empty());
    assert_eq!(scheduler.snapshot().unwrap().inflight_calls, 0);
    parent.settle(Phase::Failed, "").unwrap();
}

#[test]
fn concurrent_close_waits_for_active_tools_and_joins_workers() {
    let sessions = Sessions::new();
    let parent = sessions.start("close", "task", 7, "", None).unwrap();
    let allocator = tofu_runtime::capacity_allocation::Allocator::new(
        tofu_runtime::capacity::Snapshot {
            host_cpus: 1,
            affinity_cpus: Some(1),
            quota_cpus: None,
            effective_cpus: 1,
            memory_capacity_bytes: Some(1048576),
            memory_available_bytes: Some(1048576),
            volume_total_bytes: None,
            volume_available_bytes: None,
        },
        1,
    )
    .unwrap();
    let scheduler = Arc::new(
        Scheduler::with_capacity(
            &allocator,
            tofu_runtime::capacity::Demand {
                worker_bytes: 1024,
                job_bytes: 16384,
                queued_jobs_per_worker: 4,
                memory_divisor: 1,
                cpu_units_per_worker: 1000,
            },
            BTreeSet::from(["read".into()]),
        )
        .unwrap(),
    );
    let (tx, rx) = std::sync::mpsc::channel();
    let gate = Arc::new((Mutex::new(false), std::sync::Condvar::new()));
    let registry = Registry::new(
        7,
        "epoch",
        vec![(
            "read".into(),
            Arc::new(Overlap {
                live: Arc::new(AtomicUsize::new(0)),
                peak: Arc::new(AtomicUsize::new(0)),
                entered: tx,
                gate: gate.clone(),
                releases: Arc::new(AtomicUsize::new(0)),
            }),
        )],
    )
    .unwrap();
    let worker_scheduler = scheduler.clone();
    let worker_parent = parent.clone();
    let worker_sessions = sessions.clone();
    let thread = std::thread::spawn(move || {
        let c = catalog();
        worker_scheduler
            .dispatch(Batch {
                owner: 7,
                epoch: "epoch",
                task_id: "close",
                round: 0,
                parent: worker_parent,
                sessions: &worker_sessions,
                handlers: &registry,
                authorization: &Allow,
                calls: vec![call(&c, 0, "read")],
            })
            .unwrap()
    });
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let (closed, close_rx) = std::sync::mpsc::channel();
    let mut closers = vec![];
    for _ in 0..2 {
        let scheduler = scheduler.clone();
        let closed = closed.clone();
        let allocator = allocator.clone();
        closers.push(std::thread::spawn(move || {
            scheduler.close().unwrap();
            assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
            assert_eq!(allocator.snapshot().unwrap().weighted_bytes, 0);
            closed.send(()).unwrap();
        }));
    }
    let until = std::time::Instant::now() + Duration::from_secs(2);
    while !scheduler.snapshot().unwrap().closed {
        assert!(std::time::Instant::now() < until);
        std::thread::yield_now();
    }
    assert!(matches!(
        close_rx.try_recv(),
        Err(std::sync::mpsc::TryRecvError::Empty)
    ));
    assert_eq!(allocator.snapshot().unwrap().pools, 1);
    let c = catalog();
    let empty = Registry::new(7, "epoch", vec![]).unwrap();
    assert!(matches!(
        scheduler.dispatch(Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "close",
            round: 1,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &empty,
            authorization: &Allow,
            calls: vec![call(&c, 1, "read")]
        }),
        Err(Error::Closed)
    ));
    *gate.0.lock().unwrap() = true;
    gate.1.notify_all();
    thread.join().unwrap();
    for closer in closers {
        closer.join().unwrap();
    }
    close_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    close_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(scheduler.snapshot().unwrap().inflight_calls, 0);
    parent.settle(Phase::Completed, "").unwrap();
}

#[test]
fn model_round_is_carried_by_ordered_and_parallel_dispatch() {
    struct Round(Arc<Mutex<Vec<Option<usize>>>>);
    impl Handler for Round {
        fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            self.0.lock().unwrap().push(context.model_round);
            Ok(Value::Null)
        }
    }
    for parallel in [false, true] {
        let sessions = Sessions::new();
        let parent = sessions.start("round-owner", "task", 7, "", None).unwrap();
        let observed = Arc::new(Mutex::new(Vec::new()));
        let registry = Registry::new(
            7,
            "epoch",
            vec![("read".into(), Arc::new(Round(observed.clone())))],
        )
        .unwrap();
        let scheduler = Scheduler::new(budget(), BTreeSet::from(["read".into()])).unwrap();
        let catalog = catalog();
        for round in [0, 1, 9] {
            let batch = Batch {
                owner: 7,
                epoch: "epoch",
                task_id: "round-owner",
                round,
                parent: parent.clone(),
                sessions: &sessions,
                handlers: &registry,
                authorization: &Allow,
                calls: vec![call(&catalog, 0, "read")],
            };
            let result = if parallel {
                scheduler.dispatch(batch)
            } else {
                ordered(batch)
            }
            .unwrap();
            assert!(result
                .into_iter()
                .all(|item| item.outcome.unwrap().into_result().is_ok()));
        }
        scheduler.close().unwrap();
        assert_eq!(*observed.lock().unwrap(), vec![Some(0), Some(1), Some(9)]);
        // A standalone scoped call has no fabricated model-round identity.
        registry
            .dispatch(
                catalog
                    .admit(7, "read", json!({}), &Value::Null, &Allow)
                    .unwrap(),
                &Allow,
            )
            .unwrap();
        assert_eq!(observed.lock().unwrap().last(), Some(&None));
        parent.settle(Phase::Completed, "").unwrap();
    }
}

#[test]
fn parallel_callback_dispatch_is_refused_before_empty_batch() {
    struct Nested {
        scheduler: Arc<Scheduler>,
        sessions: Sessions,
        parent: Arc<tofu_runtime::session::ExecutionSession>,
    }
    impl Handler for Nested {
        fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            let registry = Registry::new(7, "epoch", vec![]).unwrap();
            let result = self.scheduler.dispatch(Batch {
                owner: 7,
                epoch: "epoch",
                task_id: "nested",
                round: 0,
                parent: self.parent.clone(),
                sessions: &self.sessions,
                handlers: &registry,
                authorization: &Allow,
                calls: vec![],
            });
            assert!(matches!(result, Err(Error::ReentrantDispatch)));
            Ok(Value::Null)
        }
    }
    let sessions = Sessions::new();
    let parent = sessions.start("nested", "task", 7, "", None).unwrap();
    let scheduler = Arc::new(Scheduler::new(budget(), BTreeSet::from(["read".into()])).unwrap());
    let registry = Registry::new(
        7,
        "epoch",
        vec![(
            "read".into(),
            Arc::new(Nested {
                scheduler: scheduler.clone(),
                sessions: sessions.clone(),
                parent: parent.clone(),
            }),
        )],
    )
    .unwrap();
    let c = catalog();
    let results = scheduler
        .dispatch(Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "nested",
            round: 0,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &registry,
            authorization: &Allow,
            calls: vec![call(&c, 0, "read")],
        })
        .unwrap();
    assert!(
        results[0]
            .outcome
            .as_ref()
            .unwrap()
            .receipt
            .invariants_satisfied
    );
    scheduler.close().unwrap();
    parent.settle(Phase::Completed, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}

/// Blocks each call until the test releases its position, ignoring Stop.
struct Released {
    entered: std::sync::mpsc::Sender<usize>,
    gate: Arc<(Mutex<BTreeSet<usize>>, std::sync::Condvar)>,
    calls: Arc<AtomicUsize>,
}
impl Handler for Released {
    fn invoke(&self, _: InvocationContext<'_>, value: &Value) -> Result<Value, Value> {
        let position = value["position"].as_u64().unwrap() as usize;
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.entered.send(position).unwrap();
        let until = std::time::Instant::now() + Duration::from_secs(5);
        let (lock, wake) = &*self.gate;
        let mut open = lock.lock().unwrap();
        while !open.contains(&position) {
            assert!(std::time::Instant::now() < until, "test did not release position {position}");
            open = wake.wait_timeout(open, Duration::from_millis(5)).unwrap().0;
        }
        Ok(value.clone())
    }
}
fn release(gate: &(Mutex<BTreeSet<usize>>, std::sync::Condvar), position: usize) {
    gate.0.lock().unwrap().insert(position);
    gate.1.notify_all();
}
fn released() -> (
    Registry,
    std::sync::mpsc::Receiver<usize>,
    Arc<(Mutex<BTreeSet<usize>>, std::sync::Condvar)>,
    Arc<AtomicUsize>,
) {
    let (entered, rx) = std::sync::mpsc::channel();
    let gate = Arc::new((Mutex::new(BTreeSet::new()), std::sync::Condvar::new()));
    let calls = Arc::new(AtomicUsize::new(0));
    let handler = Arc::new(Released { entered, gate: gate.clone(), calls: calls.clone() });
    (Registry::new(7, "epoch", vec![("write".into(), handler)]).unwrap(), rx, gate, calls)
}
#[test]
fn concurrent_writes_start_together_and_keep_only_declared_order() {
    let sessions = Sessions::new();
    let parent = sessions.start("concurrent", "task", 7, "", None).unwrap();
    // The idle hint stays 1: it must not cap how many writes start at once.
    let scheduler = Arc::new(Scheduler::concurrent(Budget { workers: 1, ..budget() }).unwrap());
    let (registry, entered, gate, calls) = released();
    let worker_scheduler = scheduler.clone();
    let worker_parent = parent.clone();
    let worker_sessions = sessions.clone();
    let thread = std::thread::spawn(move || {
        let c = catalog();
        // Position 2 mutates position 0's target; positions 1 and 3 are independent.
        worker_scheduler
            .dispatch_in_order(
                Batch {
                    owner: 7,
                    epoch: "epoch",
                    task_id: "concurrent",
                    round: 0,
                    parent: worker_parent,
                    sessions: &worker_sessions,
                    handlers: &registry,
                    authorization: &Allow,
                    calls: (0..4).map(|p| call(&c, p, "write")).collect(),
                },
                &[vec![], vec![], vec![0], vec![]],
            )
            .unwrap()
    });
    let first: BTreeSet<usize> = (0..3)
        .map(|_| entered.recv_timeout(Duration::from_secs(2)).unwrap())
        .collect();
    assert_eq!(first, BTreeSet::from([0, 1, 3]));
    assert!(entered.recv_timeout(Duration::from_millis(100)).is_err());
    release(&gate, 1);
    release(&gate, 3);
    assert!(
        entered.recv_timeout(Duration::from_millis(100)).is_err(),
        "a same-target successor entered before its predecessor finished"
    );
    release(&gate, 0);
    assert_eq!(entered.recv_timeout(Duration::from_secs(2)).unwrap(), 2);
    release(&gate, 2);
    let results = thread.join().unwrap();
    assert_eq!(results.iter().map(|r| r.position).collect::<Vec<_>>(), vec![0, 1, 2, 3]);
    assert!(results.iter().all(|r| r.outcome.as_ref().unwrap().error.is_none()));
    assert_eq!(calls.load(Ordering::SeqCst), 4);
    assert_eq!(scheduler.snapshot().unwrap().resident_workers, 0);
    scheduler.close().unwrap();
    parent.settle(Phase::Completed, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn concurrent_order_skips_undispatched_predecessors_and_refuses_forward_ones() {
    let sessions = Sessions::new();
    let parent = sessions.start("order", "task", 7, "", None).unwrap();
    let scheduler = Scheduler::concurrent(budget()).unwrap();
    let handler = Arc::new(Ordered {
        live: AtomicUsize::new(0),
        peak: AtomicUsize::new(0),
        order: Mutex::new(vec![]),
    });
    let registry = Registry::new(7, "epoch", vec![("write".into(), handler.clone())]).unwrap();
    let c = catalog();
    // Position 0 never reached dispatch (for example a denied approval).
    let results = scheduler
        .dispatch_in_order(
            Batch {
                owner: 7,
                epoch: "epoch",
                task_id: "order",
                round: 0,
                parent: parent.clone(),
                sessions: &sessions,
                handlers: &registry,
                authorization: &Allow,
                calls: vec![call(&c, 1, "write")],
            },
            &[vec![], vec![0]],
        )
        .unwrap();
    assert!(results[0].outcome.as_ref().unwrap().error.is_none());
    let forward = scheduler.dispatch_in_order(
        Batch {
            owner: 7,
            epoch: "epoch",
            task_id: "order",
            round: 1,
            parent: parent.clone(),
            sessions: &sessions,
            handlers: &registry,
            authorization: &Allow,
            calls: vec![call(&c, 0, "write"), call(&c, 1, "write")],
        },
        &[vec![1], vec![]],
    );
    assert!(matches!(forward, Err(Error::Binding)));
    assert_eq!(*handler.order.lock().unwrap(), vec![1]);
    assert_eq!(scheduler.snapshot().unwrap().inflight_calls, 0);
    scheduler.close().unwrap();
    parent.settle(Phase::Completed, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn parent_stop_settles_a_waiting_successor_without_entering_it() {
    let sessions = Sessions::new();
    let parent = sessions.start("waiting", "task", 7, "", None).unwrap();
    let scheduler = Arc::new(Scheduler::concurrent(budget()).unwrap());
    // The running predecessor ignores Stop until released, so only the
    // successor's own wait can observe it.
    let (registry, entered, gate, calls) = released();
    let worker_scheduler = scheduler.clone();
    let worker_parent = parent.clone();
    let worker_sessions = sessions.clone();
    let thread = std::thread::spawn(move || {
        let c = catalog();
        worker_scheduler
            .dispatch_in_order(
                Batch {
                    owner: 7,
                    epoch: "epoch",
                    task_id: "waiting",
                    round: 0,
                    parent: worker_parent,
                    sessions: &worker_sessions,
                    handlers: &registry,
                    authorization: &Allow,
                    calls: vec![call(&c, 0, "write"), call(&c, 1, "write")],
                },
                &[vec![], vec![0]],
            )
            .unwrap()
    });
    assert_eq!(entered.recv_timeout(Duration::from_secs(2)).unwrap(), 0);
    assert_eq!(sessions.active_count().unwrap(), 3);
    parent.request_cancel("user_stop").unwrap();
    let until = std::time::Instant::now() + Duration::from_secs(2);
    while sessions.active_count().unwrap() != 2 {
        assert!(std::time::Instant::now() < until, "waiting successor did not settle on Stop");
        std::thread::sleep(Duration::from_millis(2));
    }
    release(&gate, 0);
    let results = thread.join().unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(results.iter().all(|r| r.outcome.as_ref().unwrap().cancellation_requested));
    assert!(results[1]
        .outcome
        .as_ref()
        .unwrap()
        .receipt
        .resource_dispositions
        .is_empty());
    scheduler.close().unwrap();
    parent.settle(Phase::Cancelled, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn parent_deadline_settles_a_waiting_successor_without_entering_it() {
    let sessions = Sessions::new();
    let parent = sessions
        .start("expiring", "task", 7, "", Some(Duration::from_millis(300)))
        .unwrap();
    let scheduler = Arc::new(Scheduler::concurrent(budget()).unwrap());
    let (registry, entered, gate, calls) = released();
    let worker_scheduler = scheduler.clone();
    let worker_parent = parent.clone();
    let worker_sessions = sessions.clone();
    let thread = std::thread::spawn(move || {
        let c = catalog();
        worker_scheduler
            .dispatch_in_order(
                Batch {
                    owner: 7,
                    epoch: "epoch",
                    task_id: "expiring",
                    round: 0,
                    parent: worker_parent,
                    sessions: &worker_sessions,
                    handlers: &registry,
                    authorization: &Allow,
                    calls: vec![call(&c, 0, "write"), call(&c, 1, "write")],
                },
                &[vec![], vec![0]],
            )
            .unwrap()
    });
    assert_eq!(entered.recv_timeout(Duration::from_secs(2)).unwrap(), 0);
    // Only the declared deadline can end the successor's wait: the predecessor
    // ignores it until released and nobody requests Stop.
    let until = std::time::Instant::now() + Duration::from_secs(3);
    while sessions.active_count().unwrap() != 2 {
        assert!(std::time::Instant::now() < until, "waiting successor outlived the deadline");
        std::thread::sleep(Duration::from_millis(2));
    }
    release(&gate, 0);
    let results = thread.join().unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let successor = results[1].outcome.as_ref().unwrap();
    assert!(successor.deadline_exceeded && successor.receipt.resource_dispositions.is_empty());
    scheduler.close().unwrap();
    parent.settle(Phase::Cancelled, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}
