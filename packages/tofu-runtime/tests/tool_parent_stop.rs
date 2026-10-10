//! Parent stop/deadline propagation over native task and child tool lifetimes.
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
    session::{Registry as Sessions, ReleaseAck, SessionError},
    tool_contract::*,
    tool_execution::{Scope, Stop},
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
struct Wait {
    runtime: Arc<tokio::runtime::Runtime>,
    entered: std::sync::mpsc::Sender<()>,
    released: Arc<AtomicUsize>,
}
impl Handler for Wait {
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
        self.entered.send(()).unwrap();
        self.runtime.block_on(async {
            tokio::time::timeout(Duration::from_secs(2), context.runtime.cancelled())
                .await
                .expect("stop did not wake child");
        });
        assert!(matches!(
            context.runtime.checkpoint(),
            Err(Stop::Cancelled | Stop::TimedOut | Stop::ParentClosing)
        ));
        Ok(json!("late"))
    }
}
#[test]
fn parent_stop_wakes_silent_child_and_child_alone_settles_resources() {
    let sessions = Sessions::new();
    let parent = sessions.start("parent", "task", 7, "", None).unwrap();
    let scope = Arc::new(
        Scope::with_parent(&sessions, 7, "child", "", None, parent.clone(), "parent").unwrap(),
    );
    let runtime = Arc::new(
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap(),
    );
    let (tx, rx) = std::sync::mpsc::channel();
    let released = Arc::new(AtomicUsize::new(0));
    let registry = Arc::new(
        Registry::new(
            7,
            "epoch",
            vec![(
                "work".into(),
                Arc::new(Wait {
                    runtime,
                    entered: tx,
                    released: released.clone(),
                }),
            )],
        )
        .unwrap(),
    );
    let call = catalog()
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let worker_scope = scope.clone();
    let worker_registry = registry.clone();
    let thread = std::thread::spawn(move || {
        worker_registry
            .dispatch_scoped(call, &Allow, &worker_scope)
            .unwrap()
    });
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    parent.request_cancel("user_stop").unwrap();
    let outcome = thread.join().unwrap();
    assert!(outcome.cancellation_requested && !outcome.deadline_exceeded);
    assert!(matches!(outcome.error, Some(DispatchError::Cancelled)));
    assert!(outcome.value.is_none());
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert_eq!(sessions.active_count().unwrap(), 1);
    parent.settle(Phase::Cancelled, "user_stop").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn parent_deadline_wakes_without_polling_or_an_abort_flag() {
    let sessions = Sessions::new();
    let parent = sessions
        .start("parent", "task", 7, "", Some(Duration::from_millis(100)))
        .unwrap();
    let scope = Scope::with_parent(
        &sessions,
        7,
        "child",
        "",
        Some(Duration::from_secs(1)),
        parent.clone(),
        "parent",
    )
    .unwrap();
    let runtime = Arc::new(
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap(),
    );
    let (tx, _rx) = std::sync::mpsc::channel();
    let released = Arc::new(AtomicUsize::new(0));
    let registry = Registry::new(
        7,
        "epoch",
        vec![(
            "work".into(),
            Arc::new(Wait {
                runtime,
                entered: tx,
                released: released.clone(),
            }),
        )],
    )
    .unwrap();
    let call = catalog()
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let outcome = registry.dispatch_scoped(call, &Allow, &scope).unwrap();
    assert!(outcome.deadline_exceeded && !outcome.cancellation_requested);
    assert_eq!(outcome.receipt.outcome, Phase::TimedOut);
    assert_eq!(released.load(Ordering::SeqCst), 1);
    parent.settle(Phase::Completed, "").unwrap();
}
#[test]
fn closing_parent_signals_before_its_cleanup_joins_children() {
    let sessions = Sessions::new();
    let parent = sessions.start("parent", "task", 7, "", None).unwrap();
    let scope = Arc::new(
        Scope::with_parent(&sessions, 7, "child", "", None, parent.clone(), "parent").unwrap(),
    );
    let runtime = Arc::new(
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap(),
    );
    let (tx, rx) = std::sync::mpsc::channel();
    let released = Arc::new(AtomicUsize::new(0));
    let registry = Arc::new(
        Registry::new(
            7,
            "epoch",
            vec![(
                "work".into(),
                Arc::new(Wait {
                    runtime,
                    entered: tx,
                    released: released.clone(),
                }),
            )],
        )
        .unwrap(),
    );
    let call = catalog()
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let worker_scope = scope.clone();
    let worker_registry = registry.clone();
    let child = std::thread::spawn(move || {
        worker_registry
            .dispatch_scoped(call, &Allow, &worker_scope)
            .unwrap()
    });
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    parent
        .hold(
            ResourceName::ToolEnvironment,
            0,
            false,
            Box::new(move |_| {
                let outcome = child.join().unwrap();
                assert!(outcome.parent_closing && !outcome.cancellation_requested);
                assert!(matches!(outcome.error, Some(DispatchError::ParentClosing)));
                Ok(ReleaseAck::Released)
            }),
        )
        .unwrap();
    let receipt = parent.settle(Phase::Completed, "").unwrap();
    assert_eq!(receipt.outcome, Phase::Completed);
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn foreign_or_closed_parent_is_rejected_without_allocating_child() {
    let sessions = Sessions::new();
    let parent = sessions.start("parent", "task", 8, "", None).unwrap();
    assert!(matches!(
        Scope::with_parent(&sessions, 7, "child", "", None, parent.clone(), "parent"),
        Err(SessionError::InvalidIdentity)
    ));
    assert_eq!(sessions.active_count().unwrap(), 1);
    parent.settle(Phase::Completed, "").unwrap();
    assert!(matches!(
        Scope::with_parent(&sessions, 8, "child", "", None, parent, "parent"),
        Err(SessionError::InvalidIdentity)
    ));
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn child_deadline_never_extends_parent_and_ready_children_refuse_expired_work() {
    let now = Arc::new(AtomicU64::new(0));
    let clock = now.clone();
    let sessions = Sessions::with_clock(Arc::new(move || {
        Duration::from_secs(clock.load(Ordering::SeqCst))
    }));
    let parent = sessions
        .start("parent", "task", 7, "", Some(Duration::from_secs(1)))
        .unwrap();
    let scope = Scope::with_parent(
        &sessions,
        7,
        "child",
        "",
        Some(Duration::from_secs(10)),
        parent.clone(),
        "parent",
    )
    .unwrap();
    now.store(2, Ordering::SeqCst);
    let registry = Registry::new(7, "epoch", Vec::new()).unwrap();
    let call = catalog()
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let outcome = registry.dispatch_scoped(call, &Allow, &scope).unwrap();
    assert!(matches!(outcome.error, Some(DispatchError::TimedOut)));
    assert!(!scope.snapshot().unwrap().dispatch_started);
    parent.settle(Phase::Completed, "").unwrap();
}
#[test]
fn observing_child_does_not_pin_parent_and_abandonment_wakes_it() {
    let sessions = Sessions::new();
    let parent = sessions.start("parent", "task", 7, "", None).unwrap();
    let scope = Arc::new(
        Scope::with_parent(&sessions, 7, "child", "", None, parent.clone(), "parent").unwrap(),
    );
    assert_eq!(Arc::strong_count(&parent), 1);
    let runtime = Arc::new(
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap(),
    );
    let (tx, rx) = std::sync::mpsc::channel();
    let released = Arc::new(AtomicUsize::new(0));
    let registry = Arc::new(
        Registry::new(
            7,
            "epoch",
            vec![(
                "work".into(),
                Arc::new(Wait {
                    runtime,
                    entered: tx,
                    released: released.clone(),
                }),
            )],
        )
        .unwrap(),
    );
    let call = catalog()
        .admit(7, "work", json!({}), &Value::Null, &Allow)
        .unwrap();
    let child_scope = scope.clone();
    let child_registry = registry.clone();
    let child = std::thread::spawn(move || {
        child_registry
            .dispatch_scoped(call, &Allow, &child_scope)
            .unwrap()
    });
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    drop(parent);
    let outcome = child.join().unwrap();
    assert!(outcome.parent_closing);
    assert!(matches!(outcome.error, Some(DispatchError::ParentClosing)));
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn many_children_observe_parent_without_resource_or_identity_retention() {
    let sessions = Sessions::new();
    let parent = sessions.start("parent", "task", 7, "", None).unwrap();
    struct Echo;
    impl Handler for Echo {
        fn invoke(&self, _: InvocationContext<'_>, _: &Value) -> Result<Value, Value> {
            Ok(json!(1))
        }
    }
    let registry = Registry::new(7, "epoch", vec![("work".into(), Arc::new(Echo))]).unwrap();
    let catalog = catalog();
    for i in 0..10000 {
        let scope = Scope::with_parent(
            &sessions,
            7,
            &format!("child:{i}"),
            "",
            None,
            parent.clone(),
            "parent",
        )
        .unwrap();
        let call = catalog
            .admit(7, "work", json!({}), &Value::Null, &Allow)
            .unwrap();
        assert_eq!(
            registry
                .dispatch_scoped(call, &Allow, &scope)
                .unwrap()
                .value,
            Some(json!(1))
        );
    }
    assert_eq!(Arc::strong_count(&parent), 1);
    assert_eq!(sessions.active_count().unwrap(), 1);
    parent.settle(Phase::Completed, "").unwrap();
    assert_eq!(sessions.active_count().unwrap(), 0);
}
#[test]
fn owner_checked_task_abort_reaches_tool_wait_and_aborted_terminal_publication() {
    use std::sync::Mutex;
    use tofu_runtime::task::{
        PortError, Task, TaskIdentity, TaskStatus, TerminalCandidate, TerminalPublisher,
    };
    struct Publisher(Mutex<Vec<Value>>);
    impl TerminalPublisher for Publisher {
        fn publish_once(&self, _: &TerminalCandidate, event: &Value) -> Result<(), PortError> {
            self.0.lock().unwrap().push(event.clone());
            Ok(())
        }
    }
    let sessions = Arc::new(Sessions::new());
    let parent = sessions.start("task-parent", "task", 7, "", None).unwrap();
    let publisher = Arc::new(Publisher(Mutex::new(Vec::new())));
    let task = Arc::new(
        Task::new(
            TaskIdentity {
                owner_user_id: 7,
                task_id: "task-parent".into(),
            },
            parent.clone(),
            None,
            publisher.clone(),
        )
        .unwrap(),
    );
    let runtime = Arc::new(
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap(),
    );
    let (tx, rx) = std::sync::mpsc::channel();
    let released = Arc::new(AtomicUsize::new(0));
    let registry = Arc::new(
        Registry::new(
            7,
            "epoch",
            vec![(
                "work".into(),
                Arc::new(Wait {
                    runtime,
                    entered: tx,
                    released: released.clone(),
                }),
            )],
        )
        .unwrap(),
    );
    let worker_task = task.clone();
    let worker_sessions = sessions.clone();
    let child = std::thread::spawn(move || {
        worker_task
            .run(7, |_| {
                let scope = Scope::with_parent(
                    &worker_sessions,
                    7,
                    "tool-child",
                    "",
                    None,
                    parent,
                    "task-parent",
                )
                .unwrap();
                let call = catalog()
                    .admit(7, "work", json!({}), &Value::Null, &Allow)
                    .unwrap();
                let outcome = registry.dispatch_scoped(call, &Allow, &scope).unwrap();
                assert!(outcome.cancellation_requested);
                assert!(matches!(outcome.error, Some(DispatchError::Cancelled)));
                Ok(Value::Null)
            })
            .unwrap()
    });
    rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(task.request_abort(7, "user_stop").unwrap());
    let terminal = child.join().unwrap();
    assert_eq!(terminal.status, TaskStatus::Aborted);
    assert_eq!(released.load(Ordering::SeqCst), 1);
    assert_eq!(publisher.0.lock().unwrap().len(), 1);
    assert_eq!(publisher.0.lock().unwrap()[0]["type"], "aborted");
    assert_eq!(sessions.active_count().unwrap(), 0);
}
