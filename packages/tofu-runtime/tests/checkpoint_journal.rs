//! Journal ownership/fault tests with atomic header/snapshot transactions.
use serde_json::{json, Value};
use std::sync::{atomic::Ordering, Arc, Barrier};
use tofu_runtime::checkpoint_journal::{Error, Journal, Scope, Store};

#[path = "support/checkpoint_store.rs"]
mod checkpoint_store;
use checkpoint_store::MemoryStore;
fn scope() -> Scope {
    Scope {
        owner: 7,
        task_id: "task".into(),
        epoch: "epoch".into(),
        binding: "model-tools-account".into(),
    }
}
fn journal(store: &dyn Store) -> Journal<'_> {
    Journal::new(scope(), store, 1048576, 1024).unwrap()
}
fn snapshot() -> Value {
    json!({"format":"tofu.native-agent-checkpoint/v1","owner_user_id":7,
    "task_id":"task","epoch":"epoch","binding":"model-tools-account","next_round":1,
    "tool_calls":1,"messages":[{"role":"user","content":"question"},{"role":"tool","content":"result"}],
    "usage":[{}],"round_limit":3,"tool_limit":3,"no_progress_limit":0,"failure_limit":0,
    "progress":{"codec":"sha256-token-sequence/v1","last_calls":null,"last_world":"","last_evidence":null,
        "last_failure":null,"no_progress_streak":0,"failure_streak":0}})
}

#[test]
fn owned_finish_reconciles_unknown_delivery_without_reading_history() {
    use tofu_runtime::{checkpoint_journal::OwnedLease, task::TaskStatus};
    for after in [false, true] {
        let store = Arc::new(MemoryStore::default());
        let lease = OwnedLease::acquire(scope(), store.clone(), 1048576, 1024, "worker").unwrap();
        lease.load().unwrap();
        lease.begin(0).unwrap();
        if after {
            store.fail_after.store(true, Ordering::Release);
        } else {
            store.fail_before.store(true, Ordering::Release);
        }
        assert_eq!(lease.finish(TaskStatus::Error, false), Err(Error::Storage));
        lease.finish(TaskStatus::Error, false).unwrap();
        let version = store.record.lock().unwrap().as_ref().unwrap().0;
        lease.finish(TaskStatus::Error, false).unwrap();
        assert_eq!(store.record.lock().unwrap().as_ref().unwrap().0, version);
        assert_eq!(
            journal(store.as_ref()).acquire("other").err(),
            Some(Error::Ineligible)
        );
        assert_eq!(*store.snapshot_reads.lock().unwrap(), 0);
        assert_eq!(*store.snapshot_writes.lock().unwrap(), 0);
    }
}

#[test]
fn owned_finish_preserves_only_completed_pause_and_fences_stale_generation() {
    use tofu_runtime::{checkpoint_journal::OwnedLease, task::TaskStatus};
    for status in [TaskStatus::Done, TaskStatus::Error, TaskStatus::Aborted] {
        for suspended in [false, true] {
            let store = Arc::new(MemoryStore::default());
            let lease = OwnedLease::acquire(scope(), store.clone(), 1048576, 1024, "same").unwrap();
            assert_eq!(
                lease.finish(TaskStatus::Running, false),
                Err(Error::Invalid)
            );
            lease.load().unwrap();
            lease.begin(0).unwrap();
            if suspended && status == TaskStatus::Aborted {
                assert_eq!(lease.finish(status, suspended), Err(Error::Ineligible));
            }
            lease.complete(&snapshot()).unwrap();
            lease.finish(status, suspended).unwrap();
            if suspended && status == TaskStatus::Aborted {
                lease.release().unwrap();
                let resumed =
                    OwnedLease::acquire(scope(), store.clone(), 1048576, 1024, "same").unwrap();
                assert_eq!(lease.finish(status, suspended), Err(Error::Conflict));
                assert_eq!(resumed.load().unwrap(), Some(snapshot()));
            } else {
                assert_eq!(lease.release(), Err(Error::Ineligible));
                assert_eq!(
                    journal(store.as_ref()).acquire("other").err(),
                    Some(Error::Ineligible)
                );
            }
        }
    }
}

#[test]
fn completed_frontier_resumes_under_new_lease_without_history_metadata_copies() {
    let store = MemoryStore::default();
    let first = journal(&store);
    let lease = first.acquire("worker-a").unwrap();
    assert_eq!(lease.load().unwrap(), None);
    assert_eq!(lease.load().err(), Some(Error::Ineligible));
    lease.begin(0).unwrap();
    lease.complete(&snapshot()).unwrap();
    assert_eq!(first.acquire("worker-b").err(), Some(Error::Ineligible));
    lease.release().unwrap();
    let second = journal(&store);
    let resumed = second.acquire("worker-b").unwrap();
    assert_eq!(resumed.load().unwrap(), Some(snapshot()));
    resumed.begin(1).unwrap();
    assert_eq!(*store.snapshot_reads.lock().unwrap(), 1);
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 1);
    assert_eq!(lease.begin(1), Err(Error::Conflict));
}
#[test]
fn crashed_inflight_and_terminal_frontiers_cannot_resume_or_release() {
    for terminal in [false, true] {
        let store = MemoryStore::default();
        let first = journal(&store);
        let lease = first.acquire("a").unwrap();
        lease.load().unwrap();
        lease.begin(0).unwrap();
        assert_eq!(lease.release(), Err(Error::Ineligible));
        if terminal {
            lease.seal().unwrap();
            assert_eq!(lease.begin(0), Err(Error::Ineligible));
            assert_eq!(lease.seal(), Err(Error::Ineligible));
        }
        drop(lease);
        let restarted = journal(&store);
        assert_eq!(restarted.acquire("b").err(), Some(Error::Ineligible));
    }
}
#[test]
fn scope_corruption_and_stale_frontier_cannot_commit_effects() {
    let store = MemoryStore::default();
    let j = journal(&store);
    let lease = j.acquire("a").unwrap();
    assert_eq!(lease.begin(0), Err(Error::Ineligible));
    lease.load().unwrap();
    lease.begin(0).unwrap();
    for mode in 0..9 {
        let mut s = snapshot();
        match mode {
            0 => s["owner_user_id"] = json!(8),
            1 => s["task_id"] = json!("foreign"),
            2 => s["epoch"] = json!("other"),
            3 => s["binding"] = json!("other"),
            4 => s["next_round"] = json!(2),
            5 => s["usage"] = json!([]),
            6 => s["messages"][1]["role"] = json!("assistant"),
            7 => s["tool_calls"] = json!(0),
            _ => s["unexpected"] = json!(true),
        }
        assert_eq!(lease.complete(&s), Err(Error::Invalid));
    }
    lease.complete(&snapshot()).unwrap();
    assert_eq!(lease.begin(0), Err(Error::Ineligible));
    lease.release().unwrap();
    let mut foreign = scope();
    foreign.owner = 8;
    assert_eq!(
        Journal::new(foreign, &store, 1048576, 1024)
            .unwrap()
            .acquire("b")
            .err(),
        Some(Error::Invalid)
    );
}
#[test]
fn unknown_commit_delivery_poisoning_keeps_round_fenced() {
    for after in [false, true] {
        let store = MemoryStore::default();
        let j = journal(&store);
        let lease = j.acquire("a").unwrap();
        lease.load().unwrap();
        if after {
            store.fail_after.store(true, Ordering::Release);
        } else {
            store.fail_before.store(true, Ordering::Release);
        }
        assert_eq!(lease.begin(0), Err(Error::Storage));
        assert_eq!(lease.begin(0), Err(Error::Poisoned));
        assert_eq!(lease.release(), Err(Error::Poisoned));
        assert_eq!(j.acquire("b").err(), Some(Error::Ineligible));
    }
}
#[test]
fn lease_generation_prevents_reused_token_from_reviving_old_handle() {
    let store = MemoryStore::default();
    let j = journal(&store);
    let first = j.acquire("same-token").unwrap();
    first.load().unwrap();
    first.release().unwrap();
    let second = j.acquire("same-token").unwrap();
    second.load().unwrap();
    assert_eq!(first.begin(0), Err(Error::Conflict));
    second.begin(0).unwrap();
}
#[test]
fn simultaneous_acquisitions_have_one_owner() {
    let store = Arc::new(MemoryStore::default());
    let barrier = Arc::new(Barrier::new(16));
    let workers = (0..16)
        .map(|i| {
            let store = store.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let j = journal(store.as_ref());
                barrier.wait();
                j.acquire(&format!("worker-{i}")).is_ok()
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(
        workers
            .into_iter()
            .map(|w| usize::from(w.join().unwrap()))
            .sum::<usize>(),
        1
    );
}
#[test]
fn byte_bounds_refuse_snapshot_without_retained_encoding() {
    let store = MemoryStore::default();
    let j = Journal::new(scope(), &store, 128, 1024).unwrap();
    let lease = j.acquire("a").unwrap();
    lease.load().unwrap();
    lease.begin(0).unwrap();
    assert_eq!(lease.complete(&snapshot()), Err(Error::Oversize));
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 0);
    assert_eq!(lease.release(), Err(Error::Ineligible));
    assert_eq!(
        Journal::new(scope(), &store, 128, 1).err(),
        Some(Error::Invalid)
    );
}

#[test]
fn failed_completed_publication_and_corrupt_headers_never_reopen_effects() {
    for after in [false, true] {
        let store = MemoryStore::default();
        let j = journal(&store);
        let lease = j.acquire("a").unwrap();
        lease.load().unwrap();
        lease.begin(0).unwrap();
        if after {
            store.fail_after.store(true, Ordering::Release);
        } else {
            store.fail_before.store(true, Ordering::Release);
        }
        assert_eq!(lease.complete(&snapshot()), Err(Error::Storage));
        assert_eq!(lease.begin(1), Err(Error::Poisoned));
        assert!(j.acquire("other").is_err());
    }
    for mode in 0..5 {
        let store = MemoryStore::default();
        let j = journal(&store);
        let lease = j.acquire("a").unwrap();
        lease.release().unwrap();
        let mut record = store.record.lock().unwrap();
        let (_, h, _) = record.as_mut().unwrap();
        match mode {
            0 => h["extra"] = json!(true),
            1 => h["next_round"] = json!(1),
            2 => h["phase"] = json!("inflight"),
            3 => h["lease_generation"] = json!(u64::MAX),
            _ => h["owner_user_id"] = json!(8),
        }
        drop(record);
        assert_eq!(j.acquire("b").err(), Some(Error::Invalid));
    }
}

struct Recovery;
impl tofu_runtime::checkpoint_journal::Recovery for Recovery {
    fn restore(
        &self,
        _: tofu_runtime::agent_checkpoint::Identity<'_>,
        _: &Value,
    ) -> Result<(), Value> {
        Ok(())
    }
}
struct TerminalPorts {
    store: Arc<MemoryStore>,
    released: Arc<std::sync::atomic::AtomicUsize>,
    writes: std::sync::atomic::AtomicUsize,
    published: std::sync::atomic::AtomicUsize,
    fail_write: std::sync::atomic::AtomicBool,
    pause: bool,
}
impl tofu_runtime::task::TerminalPersistence for TerminalPorts {
    fn persist_once(
        &self,
        c: &tofu_runtime::task::TerminalCandidate,
    ) -> Result<(), tofu_runtime::task::PortError> {
        assert_eq!(self.released.load(Ordering::SeqCst), 1);
        assert!(c.receipt.outcome.is_terminal());
        assert_eq!(
            self.store.record.lock().unwrap().as_ref().unwrap().1["phase"],
            if self.pause { "completed" } else { "terminal" }
        );
        self.writes.fetch_add(1, Ordering::SeqCst);
        if self.fail_write.swap(false, Ordering::AcqRel) {
            return Err(tofu_runtime::task::PortError);
        }
        Ok(())
    }
}
impl tofu_runtime::task::TerminalPublisher for TerminalPorts {
    fn publish_once(
        &self,
        _: &tofu_runtime::task::TerminalCandidate,
        _: &Value,
    ) -> Result<(), tofu_runtime::task::PortError> {
        assert!(self.writes.load(Ordering::SeqCst) > 0);
        self.published.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}
#[test]
fn task_terminal_fence_precedes_persistence_and_unknown_seal_retry_never_reexecutes() {
    use tofu_runtime::{
        checkpoint_journal::{OwnedLease, OwnedPolicy},
        execution_contract::ResourceName,
        session::{Registry, ReleaseAck},
        task::{Task, TaskError, TaskIdentity},
    };
    for mode in ["normal", "seal_before", "seal_after", "delegate"] {
        let store = Arc::new(MemoryStore::default());
        let lease = OwnedLease::acquire(scope(), store.clone(), 1048576, 1024, "a").unwrap();
        lease.load().unwrap();
        lease.begin(0).unwrap();
        let policy = OwnedPolicy::new(lease, Arc::new(Recovery), false);
        let released = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let ports = Arc::new(TerminalPorts {
            store: store.clone(),
            released: released.clone(),
            writes: 0.into(),
            published: 0.into(),
            fail_write: (mode == "delegate").into(),
            pause: false,
        });
        let persistence = policy
            .terminal_persistence(ports.clone())
            .unwrap();

        let sessions = Registry::new();
        let session = sessions.start("task", "task", 7, "", None).unwrap();
        let release = released.clone();
        session
            .hold(
                ResourceName::Route,
                0,
                false,
                Box::new(move |_| {
                    release.fetch_add(1, Ordering::SeqCst);
                    Ok(ReleaseAck::Released)
                }),
            )
            .unwrap();
        let task = Task::new(
            TaskIdentity {
                owner_user_id: 7,
                task_id: "task".into(),
            },
            session,
            Some(persistence.clone()),
            ports.clone(),
        )
        .unwrap();
        let work = std::sync::atomic::AtomicUsize::new(0);
        let result = task.run(7, |_| {
            work.fetch_add(1, Ordering::SeqCst);
            if mode == "seal_before" {
                store.fail_before.store(true, Ordering::Release);
            }
            if mode == "seal_after" {
                store.fail_after.store(true, Ordering::Release);
            }
            Ok(json!({"answer":42}))
        });
        let candidate = if mode != "normal" {
            assert_eq!(result.unwrap_err(), TaskError::PersistencePending);
            assert_eq!(ports.published.load(Ordering::SeqCst), 0);
            assert_eq!(
                task.run(7, |_| panic!("terminal debt replayed execution"))
                    .unwrap_err(),
                TaskError::AlreadyTerminal
            );
            let sealed_version = store.record.lock().unwrap().as_ref().unwrap().0;
            let result = task.finish(7, Some(json!("replacement")), None).unwrap();
            assert_eq!(result.result, Some(json!({"answer":42})));
            if mode == "seal_after" {
                assert_eq!(
                    store.record.lock().unwrap().as_ref().unwrap().0,
                    sealed_version
                );
            }
            result
        } else {
            result.unwrap()
        };
        let mut invalid = (*candidate).clone();
        invalid.status = tofu_runtime::task::TaskStatus::Running;
        assert!(persistence.persist_once(&invalid).is_err());
        invalid.status = tofu_runtime::task::TaskStatus::Done;
        invalid.identity.owner_user_id = 8;
        assert!(persistence.persist_once(&invalid).is_err());
        assert_eq!(work.load(Ordering::SeqCst), 1);
        assert_eq!(released.load(Ordering::SeqCst), 1);
        assert_eq!(ports.published.load(Ordering::SeqCst), 1);
        assert_eq!(
            store.record.lock().unwrap().as_ref().unwrap().1["phase"],
            "terminal"
        );
        assert!(journal(store.as_ref()).acquire("other").is_err());
    }
}
#[test]
fn committed_suspend_keeps_frontier_but_ordinary_abort_and_cleanup_failure_seal() {
    use tofu_runtime::{
        agent_checkpoint::{Identity, Port, View},
        checkpoint_journal::Policy,
        execution_contract::ResourceName,
        session::{Registry, ReleaseAck, ReleaseError},
        task::{Task, TaskIdentity, TaskStatus},
    };
    for mode in ["suspend", "abort", "cleanup"] {
        let store = Arc::new(MemoryStore::default());
        let j = journal(store.as_ref());
        let policy = Policy::new(j.acquire("a").unwrap(), &Recovery, true);
        let released = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let ports = Arc::new(TerminalPorts {
            store: store.clone(),
            released: released.clone(),
            writes: 0.into(),
            published: 0.into(),
            fail_write: false.into(),
            pause: mode == "suspend",
        });
        assert!(policy.terminal_persistence(Arc::new(MemoryStore::default()), ports.clone()).is_err());
        let persistence = policy
            .terminal_persistence(store.clone(), ports.clone())
            .unwrap();
        let sessions = Registry::new();
        let session = sessions.start("task", "task", 7, "", None).unwrap();
        let release = released.clone();
        session
            .hold(
                ResourceName::Route,
                0,
                false,
                Box::new(move |_| {
                    release.fetch_add(1, Ordering::SeqCst);
                    if mode == "cleanup" {
                        Err(ReleaseError)
                    } else {
                        Ok(ReleaseAck::Released)
                    }
                }),
            )
            .unwrap();
        let task = Task::new(
            TaskIdentity {
                owner_user_id: 7,
                task_id: "task".into(),
            },
            session,
            Some(persistence),
            ports,
        )
        .unwrap();
        let result = task
            .run(7, |session| {
                let identity = Identity {
                    owner: 7,
                    task_id: "task",
                    epoch: "epoch",
                    binding: "model-tools-account",
                    session,
                };
                policy.load(identity).unwrap();
                policy.begin(identity, 0).unwrap();
                if mode != "abort" {
                    let state = snapshot();
                    let ledger = tofu_runtime::progress::Ledger::default();
                    policy
                        .complete(View {
                            identity,
                            next_round: 1,
                            tool_calls: 1,
                            messages: state["messages"].as_array().unwrap(),
                            usage: state["usage"].as_array().unwrap(),
                            progress: &ledger,
                            budget: tofu_runtime::agent::Budget {
                                rounds: 3,
                                tool_calls: 3,
                                retained_bytes: 1048576,
                                feedback_bytes: 1048576,
                            },
                            limits: tofu_runtime::progress::Limits::default(),
                            model_account: None,
                            deadline_unix_ms: None,
                        })
                        .unwrap();
                }
                session.request_cancel("stop").unwrap();
                Ok(Value::Null)
            })
            .unwrap();
        assert_eq!(
            result.status,
            if mode == "cleanup" {
                TaskStatus::Error
            } else {
                TaskStatus::Aborted
            }
        );
        if mode == "suspend" {
            policy.lease.release().unwrap();
            assert_eq!(
                j.acquire("resumed").unwrap().load().unwrap(),
                Some(snapshot())
            );
        } else {
            assert!(j.acquire("resumed").is_err());
        }
    }
}

fn owned(store: Arc<dyn Store>, token: &str) -> tofu_runtime::checkpoint_journal::OwnedLease {
    tofu_runtime::checkpoint_journal::OwnedLease::acquire(scope(), store, 1048576, 1024, token).unwrap()
}
#[test]
fn owned_callbacks_outlive_stack_journal_and_keep_load_and_generation_fences() {
    let store = Arc::new(MemoryStore::default());
    let first = Arc::new(owned(store.clone(), "same"));
    let callback = { let lease = first.clone(); move || lease.load() };
    assert!(callback().unwrap().is_none());
    assert_eq!(callback().err(), Some(Error::Ineligible));
    first.begin(0).unwrap();
    first.complete(&snapshot()).unwrap();
    first.release().unwrap();
    let second = owned(store.clone(), "same");
    assert_eq!(second.load().unwrap(), Some(snapshot()));
    assert_eq!(first.begin(1), Err(Error::Conflict));
    second.begin(1).unwrap();
    assert_eq!(*store.snapshot_reads.lock().unwrap(), 1);
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 1);
    assert_eq!(second.release(), Err(Error::Ineligible));
}
#[test]
fn owned_callbacks_share_unknown_delivery_poisoning_and_never_release_on_drop() {
    for after in [false, true] {
        let store = Arc::new(MemoryStore::default());
        let lease = Arc::new(owned(store.clone(), "a"));
        lease.load().unwrap();
        let next_callback = lease.clone();
        if after { store.fail_after.store(true, Ordering::Release); }
        else { store.fail_before.store(true, Ordering::Release); }
        assert_eq!(lease.begin(0), Err(Error::Storage));
        assert_eq!(next_callback.begin(0), Err(Error::Poisoned));
        assert_eq!(next_callback.release(), Err(Error::Poisoned));
        drop(next_callback); drop(lease);
        assert_eq!(tofu_runtime::checkpoint_journal::OwnedLease::acquire(
            scope(), store, 1048576, 1024, "b").err(), Some(Error::Ineligible));
    }
}
#[test]
fn owned_parallel_load_callbacks_have_one_winner() {
    let store = Arc::new(MemoryStore::default());
    let lease = Arc::new(owned(store.clone(), "a"));
    let barrier = Arc::new(Barrier::new(8));
    let callbacks = (0..8).map(|_| {
        let lease = lease.clone(); let barrier = barrier.clone();
        std::thread::spawn(move || { barrier.wait(); lease.load().is_ok() })
    }).collect::<Vec<_>>();
    assert_eq!(callbacks.into_iter().map(|c| usize::from(c.join().unwrap())).sum::<usize>(), 1);
    lease.begin(0).unwrap();
    lease.complete(&snapshot()).unwrap();
    lease.release().unwrap();
}
#[test]
fn owned_policy_restoration_failure_poisoning_survives_callback_return() {
    use tofu_runtime::{agent_checkpoint::{Identity, Port}, checkpoint_journal::{OwnedPolicy, Recovery}, session::Registry};
    struct Failed;
    impl Recovery for Failed {
        fn restore(&self, _: Identity<'_>, _: &Value) -> Result<(), Value> { Err(json!({"kind":"restore-denied"})) }
    }
    let store = Arc::new(MemoryStore::default());
    let previous = owned(store.clone(), "a");
    previous.load().unwrap(); previous.begin(0).unwrap(); previous.complete(&snapshot()).unwrap(); previous.release().unwrap();
    let policy = OwnedPolicy::new(owned(store.clone(), "b"), Arc::new(Failed), false);
    let sessions = Registry::new();
    let session = sessions.start("restore", "task", 7, "", None).unwrap();
    let identity = Identity { owner:7, task_id:"task", epoch:"epoch", binding:"model-tools-account", session:&session };
    assert_eq!(policy.load(identity), Err(json!({"kind":"restore-denied"})));
    assert_eq!(policy.begin(identity, 1), Err(json!({"kind":"checkpoint_journal","code":"poisoned"})));
    assert_eq!(policy.release(), Err(Error::Poisoned));
}
#[test]
fn owned_store_lifetime_is_exact_and_handle_never_retains_snapshot_history() {
    use tofu_runtime::checkpoint_journal::OwnedLease;
    let store = Arc::new(MemoryStore::default());
    let weak = Arc::downgrade(&store);
    let lease = owned(store.clone(), "a");
    drop(store);
    lease.load().unwrap(); lease.begin(0).unwrap();
    let mut large = snapshot(); large["messages"][1]["content"] = json!("x".repeat(524288));
    lease.complete(&large).unwrap(); drop(large);
    lease.release().unwrap();
    let store = weak.upgrade().unwrap();
    assert_eq!(*store.snapshot_reads.lock().unwrap(), 0);
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 1);
    println!("owned_checkpoint_handle_bytes={} scope_bytes={} history_bytes=524288 snapshot_reads=0", std::mem::size_of::<OwnedLease>(), scope().task_id.len()+scope().epoch.len()+scope().binding.len());
    drop(store); drop(lease);
    assert!(weak.upgrade().is_none());
}
