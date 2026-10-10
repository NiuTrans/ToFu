//! Native task side-effect ordering, retry debt and live execution fences.
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc, Mutex,
};
use tofu_runtime::{
    execution_contract::{ExecutionPhase as Phase, ResourceName},
    session::{Registry, ReleaseAck, ReleaseError},
    task::*,
};
#[derive(Default)]
struct Ports {
    writes: AtomicUsize,
    deliveries: AtomicUsize,
    fail_write: AtomicUsize,
    fail_delivery: AtomicUsize,
    events: Mutex<Vec<Value>>,
    order: Mutex<Vec<&'static str>>,
}
impl TerminalPersistence for Ports {
    fn persist_once(&self, c: &TerminalCandidate) -> Result<(), PortError> {
        assert!(c.receipt.outcome.is_terminal());
        self.writes.fetch_add(1, Ordering::SeqCst);
        self.order.lock().unwrap().push("persist");
        if self
            .fail_write
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |v| v.checked_sub(1))
            .is_ok()
        {
            return Err(PortError);
        }
        Ok(())
    }
}
impl TerminalPublisher for Ports {
    fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
        self.deliveries.fetch_add(1, Ordering::SeqCst);
        self.order.lock().unwrap().push("publish");
        if self
            .fail_delivery
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |v| v.checked_sub(1))
            .is_ok()
        {
            return Err(PortError);
        }
        self.events.lock().unwrap().push(event.clone());
        Ok(())
    }
}
fn setup(p: Arc<Ports>) -> (Registry, Arc<tofu_runtime::session::ExecutionSession>, Task) {
    let r = Registry::new();
    let s = r.start("task", "task", 7, "", None).unwrap();
    let t = Task::new(
        TaskIdentity {
            task_id: "task".into(),
            owner_user_id: 7,
        },
        s.clone(),
        Some(p.clone()),
        p,
    )
    .unwrap();
    (r, s, t)
}
#[test]
fn settlement_precedes_commit_and_canonical_publication() {
    let p = Arc::new(Ports::default());
    let (_r, s, t) = setup(p.clone());
    let release = p.clone();
    s.hold(
        ResourceName::Route,
        0,
        false,
        Box::new(move |_| {
            release.order.lock().unwrap().push("release");
            Ok(ReleaseAck::Released)
        }),
    )
    .unwrap();
    let c = t.run(7, |_| Ok(json!({"answer":42}))).unwrap();
    assert_eq!(c.status, TaskStatus::Done);
    assert_eq!(
        *p.order.lock().unwrap(),
        vec!["release", "persist", "publish"]
    );
    assert_eq!(
        p.events.lock().unwrap()[0],
        json!({"type":"done","status":"done","result":{"answer":42}})
    );
    assert_eq!(
        t.finish(7, None, None).unwrap_err(),
        TaskError::AlreadyTerminal
    );
}
#[test]
fn persistence_debt_freezes_result_and_never_reexecutes_or_releases() {
    let p = Arc::new(Ports::default());
    p.fail_write.store(1, Ordering::SeqCst);
    let (_r, s, t) = setup(p.clone());
    let released = Arc::new(AtomicUsize::new(0));
    let capture = released.clone();
    s.hold(
        ResourceName::Route,
        0,
        false,
        Box::new(move |_| {
            capture.fetch_add(1, Ordering::SeqCst);
            Ok(ReleaseAck::Released)
        }),
    )
    .unwrap();
    assert_eq!(
        t.run(7, |_| Ok(json!("first"))).unwrap_err(),
        TaskError::PersistencePending
    );
    assert_eq!(p.deliveries.load(Ordering::SeqCst), 0);
    assert_eq!(t.status(7).unwrap(), TaskStatus::Running);
    assert!(!t.request_abort(7, "late").unwrap());
    assert_eq!(
        t.run(7, |_| panic!("must never rerun")).unwrap_err(),
        TaskError::AlreadyTerminal
    );
    let c = t
        .finish(7, Some(json!("second")), Some(json!({"kind":"late"})))
        .unwrap();
    assert_eq!(c.result, Some(json!("first")));
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert_eq!(p.writes.load(Ordering::SeqCst), 2);
}
#[test]
fn delivery_retry_skips_successful_persistence() {
    let p = Arc::new(Ports::default());
    p.fail_delivery.store(1, Ordering::SeqCst);
    let (_r, _s, t) = setup(p.clone());
    assert_eq!(
        t.run(7, |_| Ok(json!(1))).unwrap_err(),
        TaskError::DeliveryPending
    );
    assert_eq!(t.status(7).unwrap(), TaskStatus::Done);
    t.finish(7, None, None).unwrap();
    assert_eq!(p.writes.load(Ordering::SeqCst), 1);
    assert_eq!(p.events.lock().unwrap().len(), 1);
}
#[test]
fn cancellation_before_dispatch_and_inside_execution_use_aborted_event() {
    for before in [true, false] {
        let p = Arc::new(Ports::default());
        let (_r, _s, t) = setup(p.clone());
        if before {
            t.request_abort(7, "stop").unwrap();
        }
        let c = t
            .run(7, |s| {
                assert!(!before);
                s.request_cancel("stop").unwrap();
                Ok(json!("discard"))
            })
            .unwrap();
        assert_eq!(c.status, TaskStatus::Aborted);
        assert!(c.result.is_none());
        assert_eq!(
            p.events.lock().unwrap()[0],
            json!({"type":"aborted","status":"aborted"})
        );
    }
}
#[test]
fn cleanup_failure_and_execution_panic_cannot_publish_success() {
    for cleanup in [true, false] {
        let p = Arc::new(Ports::default());
        let (_r, s, t) = setup(p.clone());
        if cleanup {
            s.hold(
                ResourceName::Route,
                0,
                false,
                Box::new(|_| Err(ReleaseError)),
            )
            .unwrap();
        }
        let c = t
            .run(7, |_| {
                if !cleanup {
                    panic!("execution failure");
                }
                Ok(json!(1))
            })
            .unwrap();
        assert_eq!(c.status, TaskStatus::Error);
        assert!(c.result.is_none());
        assert!(c.error.is_some());
        assert_eq!(p.events.lock().unwrap()[0]["type"], "error");
    }
}
#[test]
fn owner_and_session_identity_are_checked_before_side_effects() {
    let p = Arc::new(Ports::default());
    let (_r, s, t) = setup(p.clone());
    assert_eq!(t.status(8), Err(TaskError::Unauthorized));
    assert_eq!(
        t.run(8, |_| panic!("unauthorized")).unwrap_err(),
        TaskError::Unauthorized
    );
    assert_eq!(
        t.finish(8, None, None).unwrap_err(),
        TaskError::Unauthorized
    );
    assert!(matches!(
        Task::new(
            TaskIdentity {
                task_id: "different".into(),
                owner_user_id: 7
            },
            s,
            None,
            p.clone()
        ),
        Err(TaskError::Unauthorized)
    ));
    assert_eq!(p.writes.load(Ordering::SeqCst), 0);
}
#[test]
fn live_execution_refuses_early_terminalization_but_accepts_abort() {
    let p = Arc::new(Ports::default());
    let (_r, _s, t) = setup(p);
    let c = t
        .run(7, |_| {
            assert_eq!(t.finish(7, None, None).unwrap_err(), TaskError::Busy);
            assert_eq!(t.run(7, |_| Ok(Value::Null)).unwrap_err(), TaskError::Busy);
            assert!(t.request_abort(7, "stop").unwrap());
            Ok(json!(1))
        })
        .unwrap();
    assert_eq!(c.status, TaskStatus::Aborted);
}
#[test]
fn expired_deadline_skips_provider_and_settles_cancelled() {
    use std::time::Duration;
    let now = Arc::new(AtomicUsize::new(0));
    let clock = now.clone();
    let r = Registry::with_clock(Arc::new(move || {
        Duration::from_secs(clock.load(Ordering::SeqCst) as u64)
    }));
    let s = r
        .start("deadline", "task", 7, "", Some(Duration::from_secs(1)))
        .unwrap();
    let p = Arc::new(Ports::default());
    let t = Task::new(
        TaskIdentity {
            task_id: "deadline".into(),
            owner_user_id: 7,
        },
        s,
        None,
        p,
    )
    .unwrap();
    now.store(2, Ordering::SeqCst);
    let c = t
        .run(7, |_| panic!("expired execution dispatched"))
        .unwrap();
    assert_eq!(c.receipt.outcome, Phase::Cancelled);
}
#[test]
fn competing_terminalizer_is_fenced_while_commit_is_in_flight() {
    struct Blocking {
        entered: std::sync::Barrier,
        resume: std::sync::Barrier,
    }
    impl TerminalPersistence for Blocking {
        fn persist_once(&self, _: &TerminalCandidate) -> Result<(), PortError> {
            self.entered.wait();
            self.resume.wait();
            Ok(())
        }
    }
    let r = Registry::new();
    let s = r.start("race", "task", 7, "", None).unwrap();
    let b = Arc::new(Blocking {
        entered: std::sync::Barrier::new(2),
        resume: std::sync::Barrier::new(2),
    });
    let p = Arc::new(Ports::default());
    let t = Arc::new(
        Task::new(
            TaskIdentity {
                task_id: "race".into(),
                owner_user_id: 7,
            },
            s,
            Some(b.clone()),
            p.clone(),
        )
        .unwrap(),
    );
    let worker = t.clone();
    let handle = std::thread::spawn(move || worker.finish(7, Some(json!("winner")), None));
    b.entered.wait();
    assert_eq!(t.finish(7, None, None).unwrap_err(), TaskError::Busy);
    assert!(!t.request_abort(7, "late").unwrap());
    b.resume.wait();
    assert_eq!(
        handle.join().unwrap().unwrap().result,
        Some(json!("winner"))
    );
    assert_eq!(p.events.lock().unwrap().len(), 1);
}
#[test]
fn panic_after_publication_keeps_frozen_identity_for_idempotent_retry() {
    struct Ambiguous {
        calls: AtomicUsize,
        event: Mutex<Option<Value>>,
    }
    impl TerminalPublisher for Ambiguous {
        fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
            let first = self.calls.fetch_add(1, Ordering::SeqCst) == 0;
            {
                let mut stored = self.event.lock().unwrap();
                if let Some(existing) = stored.as_ref() {
                    assert_eq!(existing, event);
                } else {
                    *stored = Some(event.clone());
                }
            }
            if first {
                panic!("ack lost after durable publication");
            }
            Ok(())
        }
    }
    let r = Registry::new();
    let s = r.start("ambiguous", "task", 7, "", None).unwrap();
    let p = Arc::new(Ambiguous {
        calls: AtomicUsize::new(0),
        event: Mutex::new(None),
    });
    let t = Task::new(
        TaskIdentity {
            task_id: "ambiguous".into(),
            owner_user_id: 7,
        },
        s,
        None,
        p.clone(),
    )
    .unwrap();
    assert_eq!(
        t.run(7, |_| Ok(json!("original"))).unwrap_err(),
        TaskError::DeliveryPending
    );
    let c = t.finish(7, Some(json!("different")), None).unwrap();
    assert_eq!(c.result, Some(json!("original")));
    assert_eq!(p.calls.load(Ordering::SeqCst), 2);
}
