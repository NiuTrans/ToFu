//! Tool callback quiescence, orthogonal stop facts and one resource receipt.
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicU64, AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};
use tofu_runtime::{
    execution_contract::{ExecutionPhase as Phase, ResourceName},
    session::{Registry as Sessions, ReleaseAck, ReleaseError},
    tool_contract::*,
    tool_execution::{Scope, StartError},
};
struct Allow;
impl Authorization for Allow {
    fn allow(&self, owner: u64, _: &str, _: Permission, _: &Value, _: Option<&Value>) -> bool {
        owner == 7
    }
}
fn catalog() -> Catalog {
    Catalog::compile(7,"epoch",&json!({"work":{"name":"work","contractVersion":"tofu.tool-contract/v2","permission":"read","arguments_schema":{"type":"object"}}})).unwrap()
}
struct Worker {
    calls: Arc<AtomicUsize>,
    released: Arc<AtomicUsize>,
    cleanup_fails: bool,
    fail: bool,
    panic: bool,
    cancel: bool,
    clock: Option<Arc<AtomicU64>>,
}
impl Handler for Worker {
    fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let released = self.released.clone();
        let cleanup_fails = self.cleanup_fails;
        context
            .runtime
            .bind_resource(
                ResourceName::ToolEnvironment,
                0,
                false,
                Box::new(move |_| {
                    released.fetch_add(1, Ordering::SeqCst);
                    if cleanup_fails {
                        Err(ReleaseError)
                    } else {
                        Ok(ReleaseAck::Released)
                    }
                }),
                Box::new(|| {}),
            )
            .unwrap();
        if let Some(clock) = &self.clock {
            clock.store(2, Ordering::SeqCst);
        }
        if self.cancel {
            context.runtime.request_cancel("user_stop").unwrap();
        }
        if self.panic {
            panic!("handler panic")
        }
        if self.fail {
            return Err(json!({"kind":"handler_failure"}));
        }
        Ok(json!(42))
    }
}
fn worker() -> Worker {
    Worker {
        calls: Arc::new(AtomicUsize::new(0)),
        released: Arc::new(AtomicUsize::new(0)),
        cleanup_fails: false,
        fail: false,
        panic: false,
        cancel: false,
        clock: None,
    }
}
#[test]
fn cleanup_precedes_return_and_failure_or_panic_never_adopts_success() {
    for mode in 0..4 {
        let mut w = worker();
        w.cleanup_fails = mode == 1;
        w.fail = mode == 2;
        w.panic = mode == 3;
        let calls = w.calls.clone();
        let released = w.released.clone();
        let handlers = Registry::new(7, "epoch", vec![("work".into(), Arc::new(w))]).unwrap();
        let sessions = Sessions::new();
        let scope = Scope::new(&sessions, 7, "occurrence", "", None).unwrap();
        let call = catalog()
            .admit(7, "work", json!({}), &Value::Null, &Allow)
            .unwrap();
        let result = handlers.dispatch_scoped(call, &Allow, &scope).unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(released.load(Ordering::SeqCst), 1);
        assert!(scope.snapshot().unwrap().held_resources.is_empty());
        assert!(sessions.snapshots(10).unwrap().is_empty());
        if mode == 0 {
            assert_eq!(result.value, Some(json!(42)));
            assert_eq!(result.receipt.outcome, Phase::Completed)
        } else {
            assert!(result.value.is_none());
            assert!(result.error.is_some());
            assert_eq!(result.receipt.outcome, Phase::Failed)
        }
        let next = catalog()
            .admit(7, "work", json!({}), &Value::Null, &Allow)
            .unwrap();
        assert!(matches!(
            handlers.dispatch_scoped(next, &Allow, &scope),
            Err(StartError::AlreadyDispatched)
        ));
    }
}
#[test]
fn cancellation_and_timeout_are_independent_of_handler_failure() {
    for fail in [false, true] {
        let clock = Arc::new(AtomicU64::new(0));
        let capture = clock.clone();
        let sessions = Sessions::with_clock(Arc::new(move || {
            Duration::from_secs(capture.load(Ordering::SeqCst))
        }));
        let scope = Scope::new(&sessions, 7, "stop", "", Some(Duration::from_secs(1))).unwrap();
        let mut w = worker();
        w.cancel = true;
        w.fail = fail;
        w.clock = Some(clock);
        let released = w.released.clone();
        let handlers = Registry::new(7, "epoch", vec![("work".into(), Arc::new(w))]).unwrap();
        let call = catalog()
            .admit(7, "work", json!({}), &Value::Null, &Allow)
            .unwrap();
        let result = handlers.dispatch_scoped(call, &Allow, &scope).unwrap();
        assert!(result.cancellation_requested && result.deadline_exceeded);
        assert!(result.value.is_none());
        assert_eq!(released.load(Ordering::SeqCst), 1);
        assert_eq!(
            result.receipt.outcome,
            if fail { Phase::Failed } else { Phase::TimedOut }
        );
        if fail {
            assert!(matches!(result.error, Some(DispatchError::Failed(_))))
        } else {
            assert!(matches!(result.error, Some(DispatchError::TimedOut)))
        }
    }
}
#[test]
fn already_stopped_or_overdue_scopes_do_not_enter_handlers() {
    for timed_out in [false, true] {
        let now = Arc::new(AtomicU64::new(0));
        let capture = now.clone();
        let sessions = Sessions::with_clock(Arc::new(move || {
            Duration::from_secs(capture.load(Ordering::SeqCst))
        }));
        let scope = Scope::new(
            &sessions,
            7,
            "before-dispatch",
            "",
            Some(Duration::from_secs(1)),
        )
        .unwrap();
        if timed_out {
            now.store(2, Ordering::SeqCst)
        } else {
            scope.request_cancel(7, "stop").unwrap();
        }
        let w = worker();
        let calls = w.calls.clone();
        let handlers = Registry::new(7, "epoch", vec![("work".into(), Arc::new(w))]).unwrap();
        let call = catalog()
            .admit(7, "work", json!({}), &Value::Null, &Allow)
            .unwrap();
        let result = handlers.dispatch_scoped(call, &Allow, &scope).unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        assert!(!scope.snapshot().unwrap().dispatch_started);
        assert_eq!(
            result.receipt.outcome,
            if timed_out {
                Phase::TimedOut
            } else {
                Phase::Cancelled
            }
        );
    }
}
#[test]
fn denied_dispatch_releases_prepared_resources_without_dispatch_evidence() {
    let sessions = Sessions::new();
    let scope = Scope::new(&sessions, 7, "denied", "", None).unwrap();
    let released = Arc::new(AtomicUsize::new(0));
    let capture = released.clone();
    scope
        .bind_resource(
            ResourceName::Admission,
            0,
            false,
            Box::new(move |_| {
                capture.fetch_add(1, Ordering::SeqCst);
                Ok(ReleaseAck::Released)
            }),
            Box::new(|| {}),
        )
        .unwrap();
    let handlers = Registry::new(7, "epoch", vec![("work".into(), Arc::new(worker()))]).unwrap();
    let call = catalog()
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let result = handlers.dispatch_scoped(call, &DenyAll, &scope).unwrap();
    assert!(matches!(result.error, Some(DispatchError::Denied)));
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert!(!scope.snapshot().unwrap().dispatch_started);
    assert!(!scope.request_cancel(7, "late").unwrap());
    assert!(matches!(
        scope.request_cancel(8, "foreign"),
        Err(StartError::WrongOwner)
    ));
}
#[test]
fn stop_waits_for_handler_quiescence_before_releasing_or_publishing() {
    use std::sync::Barrier;
    let entered = Arc::new(Barrier::new(2));
    let observed = Arc::new(Barrier::new(2));
    let may_return = Arc::new(Barrier::new(2));
    let released = Arc::new(AtomicUsize::new(0));
    struct Blocking {
        entered: Arc<Barrier>,
        observed: Arc<Barrier>,
        may_return: Arc<Barrier>,
        released: Arc<AtomicUsize>,
    }
    impl Handler for Blocking {
        fn invoke(&self, context: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            let release = self.released.clone();
            context
                .runtime
                .bind_resource(
                    ResourceName::ToolEnvironment,
                    0,
                    false,
                    Box::new(move |_| {
                        release.fetch_add(1, Ordering::SeqCst);
                        Ok(ReleaseAck::Released)
                    }),
                    Box::new(|| {}),
                )
                .unwrap();
            self.entered.wait();
            while context.runtime.checkpoint().is_ok() {
                std::thread::yield_now();
            }
            self.observed.wait();
            self.may_return.wait();
            Ok(json!("late success"))
        }
    }
    let handlers = Arc::new(
        Registry::new(
            7,
            "epoch",
            vec![(
                "work".into(),
                Arc::new(Blocking {
                    entered: entered.clone(),
                    observed: observed.clone(),
                    may_return: may_return.clone(),
                    released: released.clone(),
                }),
            )],
        )
        .unwrap(),
    );
    let sessions = Sessions::new();
    let scope = Arc::new(Scope::new(&sessions, 7, "live-stop", "", None).unwrap());
    let call = catalog()
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let worker_registry = handlers.clone();
    let worker_scope = scope.clone();
    let thread = std::thread::spawn(move || {
        worker_registry
            .dispatch_scoped(call, &Allow, &worker_scope)
            .unwrap()
    });
    entered.wait();
    assert!(scope.request_cancel(7, "user_stop").unwrap());
    observed.wait();
    assert_eq!(released.load(Ordering::SeqCst), 0);
    assert_eq!(scope.snapshot().unwrap().held_resources.len(), 1);
    may_return.wait();
    let result = thread.join().unwrap();
    assert!(result.value.is_none());
    assert!(matches!(result.error, Some(DispatchError::Cancelled)));
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn repeated_default_dispatch_retires_every_scope_and_resource() {
    let w = worker();
    let calls = w.calls.clone();
    let released = w.released.clone();
    let handlers = Registry::new(7, "epoch", vec![("work".into(), Arc::new(w))]).unwrap();
    let catalog = catalog();
    for _ in 0..10000 {
        let call = catalog
            .admit(7, "work", json!({}), &Value::Null, &Allow)
            .unwrap();
        assert_eq!(handlers.dispatch(call, &Allow).unwrap(), json!(42));
    }
    assert_eq!(handlers.active_invocations().unwrap(), 0);
    assert_eq!(calls.load(Ordering::SeqCst), 10000);
    assert_eq!(released.load(Ordering::SeqCst), 10000);
}
