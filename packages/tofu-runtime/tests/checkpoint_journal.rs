//! Journal ownership/fault tests with atomic header/snapshot transactions.
use serde_json::{json, Value};
use std::sync::{atomic::Ordering, Arc, Barrier};
use tofu_runtime::checkpoint_journal::{Error, Journal, Scope, Store};

#[path = "support/checkpoint_store.rs"]
mod checkpoint_store;
use checkpoint_store::MemoryStore;
use tofu_runtime::checkpoint_journal::inspect_abandoned_frontier;

#[test]
fn retained_frontier_validation_checks_bytes_without_storage_or_writer_authority() {
    use tofu_runtime::checkpoint_journal::{Header,SnapshotWrite};
    struct NoIo;
    impl Store for NoIo {
        fn header(&self,_:u64,_:&str)->Result<Option<Header>,Error>{panic!("pure validation read storage")}
        fn snapshot(&self,_:u64,_:&str,_:u64)->Result<Option<Value>,Error>{panic!("pure validation read snapshot")}
        fn compare_exchange(&self,_:u64,_:&str,_:u64,_:&Value,_:SnapshotWrite<'_>)->Result<u64,Error>{panic!("pure validation wrote storage")}
    }
    for pending in [false,true] {
        let store=MemoryStore::default();let original=journal(&store);let lease=original.acquire("live").unwrap();
        lease.load().unwrap();lease.begin(0).unwrap();let state=if pending{pending_snapshot()}else{snapshot()};
        if pending{lease.pending(&state).unwrap();}else{lease.complete(&state).unwrap();}
        let header=store.header(7,"task").unwrap().unwrap();let before=store.record.lock().unwrap().clone();
        assert_eq!(journal(&NoIo).validate_retained_frontier(&header,&state),Ok(()));
        let mut changed=state.clone();changed["task_id"]=json!("foreign");
        assert_eq!(journal(&NoIo).validate_retained_frontier(&header,&changed),Err(Error::Invalid));
        let mut header=header;header.value["owner_user_id"]=json!(8);
        assert_eq!(journal(&NoIo).validate_retained_frontier(&header,&state),Err(Error::Invalid));
        assert_eq!(*store.record.lock().unwrap(),before);
        assert_eq!(original.recover("stolen").err(),Some(Error::Ineligible));
    }
}

#[test]
fn abandoned_frontier_observation_never_changes_header_lease_or_snapshot() {
    for pending in [false, true] {
        let store = MemoryStore::default();
        *store.writer.lock().unwrap() = Some(json!({"boot_id":"boot","pid_namespace":1,"pid":12,"start_ticks":10}));
        let journal = journal(&store); let lease = journal.acquire("original-token").unwrap();
        lease.load().unwrap(); lease.begin(0).unwrap();
        let state = if pending { pending_snapshot() } else { snapshot() };
        if pending { lease.pending(&state).unwrap(); } else { lease.complete(&state).unwrap(); }
        let before = store.record.lock().unwrap().clone();
        assert_eq!(inspect_abandoned_frontier(&store,7,"task",1048576,1024),Err(Error::Ineligible));
        assert_eq!(*store.snapshot_reads.lock().unwrap(),0);
        store.writer_dead.store(true,Ordering::Release);
        let observed = inspect_abandoned_frontier(&store,7,"task",1048576,1024).unwrap().unwrap();
        assert_eq!(observed["snapshot"],state); assert_eq!(observed["version"],before.as_ref().unwrap().0);
        assert_eq!(observed["phase"],if pending { "tools_pending" } else { "completed" });
        assert!(observed.get("lease").is_none()); assert!(observed.get("capability_id").is_none());
        assert_eq!(*store.record.lock().unwrap(),before);
        assert_eq!(inspect_abandoned_frontier(&store,7,"task",1048576,1024).unwrap(),Some(observed));
        assert_eq!(*store.record.lock().unwrap(),before);
    }
}

#[test]
fn frontier_refuses_unknown_phase_identity_bytes_and_missing_paid_state() {
    assert_eq!(inspect_abandoned_frontier(&MemoryStore::default(),7,"missing",1024,1024).unwrap(),None);
    for fault in ["new","inflight","terminal","writer","owner","task","snapshot","bound","identity"] {
        let store = MemoryStore::default();
        *store.writer.lock().unwrap() = Some(json!({"boot_id":"boot","pid_namespace":1,"pid":12,"start_ticks":10}));
        let journal = journal(&store);let lease=journal.acquire("old").unwrap();lease.load().unwrap();
        if fault != "new" { lease.begin(0).unwrap(); }
        if !matches!(fault,"new"|"inflight") { lease.complete(&snapshot()).unwrap(); }
        if fault == "terminal" { lease.seal().unwrap(); }
        store.writer_dead.store(true,Ordering::Release);
        if fault == "writer" {store.record.lock().unwrap().as_mut().unwrap().1.as_object_mut().unwrap().remove("writer_process");}
        if fault == "snapshot" {store.record.lock().unwrap().as_mut().unwrap().2=None;}
        let before=store.record.lock().unwrap().clone();
        assert!(inspect_abandoned_frontier(&store,if fault=="owner"{8}else{7},if fault=="task"{"other"}else{"task"},
            if fault=="bound"{1}else{1048576},if fault=="identity"{1}else{1024}).is_err(),"{fault}");
        assert_eq!(*store.record.lock().unwrap(),before);
    }
}

#[test]
fn frontier_refuses_changes_between_header_proof_snapshot_and_final_read() {
    use std::sync::atomic::AtomicUsize;
    use tofu_runtime::checkpoint_journal::{Header, SnapshotWrite};
    struct RacingStore {
        inner: MemoryStore,
        headers: AtomicUsize,
        boundary: &'static str,
    }
    impl RacingStore {
        fn advance(&self) { self.inner.record.lock().unwrap().as_mut().unwrap().0 += 1; }
    }
    impl Store for RacingStore {
        fn header(&self, owner: u64, task: &str) -> Result<Option<Header>, Error> {
            if self.headers.fetch_add(1, Ordering::AcqRel) == 1 && self.boundary == "header" {
                self.advance();
            }
            self.inner.header(owner, task)
        }
        fn writer_quiescent(&self, owner: u64, task: &str, identity: &Value) -> Result<bool, Error> {
            self.inner.writer_quiescent(owner, task, identity)
        }
        fn snapshot(&self, owner: u64, task: &str, version: u64) -> Result<Option<Value>, Error> {
            if self.boundary == "before_snapshot" { self.advance(); }
            let result = self.inner.snapshot(owner, task, version);
            if self.boundary == "after_snapshot" { self.advance(); }
            result
        }
        fn compare_exchange(&self, _: u64, _: &str, _: u64, _: &Value, _: SnapshotWrite<'_>) -> Result<u64, Error> {
            panic!("read-only frontier attempted a write")
        }
    }
    for boundary in ["header", "before_snapshot", "after_snapshot"] {
        let inner = MemoryStore::default();
        *inner.writer.lock().unwrap() = Some(json!({"boot_id":"boot","pid_namespace":1,"pid":12,"start_ticks":10}));
        {
            let journal = journal(&inner); let lease = journal.acquire("writer").unwrap();
            lease.load().unwrap(); lease.begin(0).unwrap(); lease.pending(&pending_snapshot()).unwrap();
        }
        inner.writer_dead.store(true, Ordering::Release);
        let before = inner.record.lock().unwrap().clone().unwrap();
        let store = RacingStore { inner, headers: AtomicUsize::new(0), boundary };
        assert_eq!(inspect_abandoned_frontier(&store,7,"task",1048576,1024),Err(Error::Conflict),"{boundary}");
        let after = store.inner.record.lock().unwrap().clone().unwrap();
        assert_eq!(after.0, before.0 + 1); assert_eq!(after.1, before.1); assert_eq!(after.2, before.2);
    }
}
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
fn recovery_requires_store_death_proof_and_keeps_history_under_a_new_generation() {
    let store = MemoryStore::default();
    *store.writer.lock().unwrap() = Some(json!({"boot_id":"boot", "pid_namespace":1,
                                              "pid":12, "start_ticks":10}));
    let first = journal(&store);
    let old = first.acquire("old").unwrap();
    old.load().unwrap();
    old.begin(0).unwrap();
    old.complete(&snapshot()).unwrap();
    assert_eq!(first.recover("new").err(), Some(Error::Ineligible));
    store.writer_dead.store(true, Ordering::Release);
    *store.writer.lock().unwrap() = Some(json!({"boot_id":"boot", "pid_namespace":1,
                                              "pid":13, "start_ticks":11}));
    let next = first.recover("new").unwrap();
    assert_eq!(*store.snapshot_reads.lock().unwrap(), 0);
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 1);
    assert_eq!(old.begin(1), Err(Error::Conflict));
    assert_eq!(old.release(), Err(Error::Poisoned));
    assert_eq!(next.load().unwrap(), Some(snapshot()));
    next.begin(1).unwrap();
}

#[test]
fn recovery_never_rewinds_inflight_terminal_legacy_or_released_frontiers() {
    for phase in ["inflight", "terminal", "legacy", "released"] {
        let store = MemoryStore::default();
        if phase != "legacy" {
            *store.writer.lock().unwrap() = Some(json!({"boot_id":"boot", "pid_namespace":1,
                                                      "pid":12, "start_ticks":10}));
        }
        store.writer_dead.store(true, Ordering::Release);
        let first = journal(&store);
        let old = first.acquire("old").unwrap();
        old.load().unwrap();
        old.begin(0).unwrap();
        if phase != "inflight" {
            old.complete(&snapshot()).unwrap();
        }
        if phase == "terminal" {
            old.seal().unwrap();
        }
        if phase == "released" {
            old.release().unwrap();
        }
        assert_eq!(first.recover("new").err(), Some(Error::Ineligible));
    }
}

#[test]
fn unknown_recovery_delivery_does_not_allow_another_acquisition() {
    for after in [false, true] {
        let store = MemoryStore::default();
        *store.writer.lock().unwrap() = Some(json!({"boot_id":"boot", "pid_namespace":1,
                                                  "pid":12, "start_ticks":10}));
        let first = journal(&store);
        let old = first.acquire("old").unwrap();
        old.load().unwrap();
        old.begin(0).unwrap();
        old.complete(&snapshot()).unwrap();
        store.writer_dead.store(true, Ordering::Release);
        if after {
            store.fail_after.store(true, Ordering::Release);
        } else {
            store.fail_before.store(true, Ordering::Release);
        }
        assert_eq!(first.recover("new").err(), Some(Error::Storage));
        // The trusted Store must now report the new writer as live when the
        // CAS landed; never turn unknown delivery into an automatic retry.
        if after {
            store.writer_dead.store(false, Ordering::Release);
            assert_eq!(first.recover("again").err(), Some(Error::Ineligible));
        } else {
            assert_eq!(old.begin(1), Ok(()));
        }
    }
}

#[test]
fn optional_limits_preserve_completed_frontier_and_finite_refusal() {
    for rounds in [json!(null), json!(3), json!(1), json!(0), json!(-1)] {
        for tools in [json!(null), json!(3), json!(0), json!(-1)] {
            let store = MemoryStore::default();
            let first = journal(&store);
            let lease = first.acquire("worker-a").unwrap();
            lease.load().unwrap();
            lease.begin(0).unwrap();
            let mut state = snapshot();
            state["round_limit"] = rounds.clone();
            state["tool_limit"] = tools.clone();
            let allowed = (rounds.is_null() || rounds == 3) && (tools.is_null() || tools == 3);
            if allowed {
                lease.complete(&state).unwrap();
                lease.release().unwrap();
                let second = journal(&store);
                let resumed = second.acquire("worker-b").unwrap();
                assert_eq!(resumed.load().unwrap(), Some(state));
                resumed.begin(1).unwrap();
            } else {
                assert_eq!(lease.complete(&state), Err(Error::Invalid));
                assert_eq!(*store.snapshot_writes.lock().unwrap(), 0);
            }
        }
    }
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
        let persistence = policy.terminal_persistence(ports.clone()).unwrap();

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
        assert!(policy
            .terminal_persistence(Arc::new(MemoryStore::default()), ports.clone())
            .is_err());
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
                                rounds: Some(3),
                                tool_calls: Some(3),
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
    tofu_runtime::checkpoint_journal::OwnedLease::acquire(scope(), store, 1048576, 1024, token)
        .unwrap()
}
#[test]
fn owned_callbacks_outlive_stack_journal_and_keep_load_and_generation_fences() {
    let store = Arc::new(MemoryStore::default());
    let first = Arc::new(owned(store.clone(), "same"));
    let callback = {
        let lease = first.clone();
        move || lease.load()
    };
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
        if after {
            store.fail_after.store(true, Ordering::Release);
        } else {
            store.fail_before.store(true, Ordering::Release);
        }
        assert_eq!(lease.begin(0), Err(Error::Storage));
        assert_eq!(next_callback.begin(0), Err(Error::Poisoned));
        assert_eq!(next_callback.release(), Err(Error::Poisoned));
        drop(next_callback);
        drop(lease);
        assert_eq!(
            tofu_runtime::checkpoint_journal::OwnedLease::acquire(
                scope(),
                store,
                1048576,
                1024,
                "b"
            )
            .err(),
            Some(Error::Ineligible)
        );
    }
}
#[test]
fn owned_parallel_load_callbacks_have_one_winner() {
    let store = Arc::new(MemoryStore::default());
    let lease = Arc::new(owned(store.clone(), "a"));
    let barrier = Arc::new(Barrier::new(8));
    let callbacks = (0..8)
        .map(|_| {
            let lease = lease.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                lease.load().is_ok()
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(
        callbacks
            .into_iter()
            .map(|c| usize::from(c.join().unwrap()))
            .sum::<usize>(),
        1
    );
    lease.begin(0).unwrap();
    lease.complete(&snapshot()).unwrap();
    lease.release().unwrap();
}
#[test]
fn owned_policy_restoration_failure_poisoning_survives_callback_return() {
    use tofu_runtime::{
        agent_checkpoint::{Identity, Port},
        checkpoint_journal::{OwnedPolicy, Recovery},
        session::Registry,
    };
    struct Failed;
    impl Recovery for Failed {
        fn restore(&self, _: Identity<'_>, _: &Value) -> Result<(), Value> {
            Err(json!({"kind":"restore-denied"}))
        }
    }
    let store = Arc::new(MemoryStore::default());
    let previous = owned(store.clone(), "a");
    previous.load().unwrap();
    previous.begin(0).unwrap();
    previous.complete(&snapshot()).unwrap();
    previous.release().unwrap();
    let policy = OwnedPolicy::new(owned(store.clone(), "b"), Arc::new(Failed), false);
    let sessions = Registry::new();
    let session = sessions.start("restore", "task", 7, "", None).unwrap();
    let identity = Identity {
        owner: 7,
        task_id: "task",
        epoch: "epoch",
        binding: "model-tools-account",
        session: &session,
    };
    assert_eq!(policy.load(identity), Err(json!({"kind":"restore-denied"})));
    assert_eq!(
        policy.begin(identity, 1),
        Err(json!({"kind":"checkpoint_journal","code":"poisoned"}))
    );
    assert_eq!(policy.release(), Err(Error::Poisoned));
}
#[test]
fn owned_store_lifetime_is_exact_and_handle_never_retains_snapshot_history() {
    use tofu_runtime::checkpoint_journal::OwnedLease;
    let store = Arc::new(MemoryStore::default());
    let weak = Arc::downgrade(&store);
    let lease = owned(store.clone(), "a");
    drop(store);
    lease.load().unwrap();
    lease.begin(0).unwrap();
    let mut large = snapshot();
    large["messages"][1]["content"] = json!("x".repeat(524288));
    lease.complete(&large).unwrap();
    drop(large);
    lease.release().unwrap();
    let store = weak.upgrade().unwrap();
    assert_eq!(*store.snapshot_reads.lock().unwrap(), 0);
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 1);
    println!(
        "owned_checkpoint_handle_bytes={} scope_bytes={} history_bytes=524288 snapshot_reads=0",
        std::mem::size_of::<OwnedLease>(),
        scope().task_id.len() + scope().epoch.len() + scope().binding.len()
    );
    drop(store);
    drop(lease);
    assert!(weak.upgrade().is_none());
}

fn pending_snapshot() -> Value {
    let mut value = snapshot();
    value["tools_pending"] = json!(true);
    value["deadline_unix_ms"] = json!(123456789);
    value["tool_recovery"] = json!({"operation_id":"domain-owned-identity"});
    value["model_account"] = json!({"tokens":23,"last_ordinal":1,"complete":true,
        "policy":{"encoding":"cl100k_base","output_field":"max_tokens","total_tokens":100}});
    value["messages"][1] = json!({"role":"assistant","tool_calls":[
        {"id":"write-1","type":"function","function":{"name":"write_file","arguments":"{\"path\":\"result.txt\"}"}}
    ]});
    value
}
fn reconciled_snapshot(pending: &Value) -> Value {
    let mut value = pending.clone();
    value.as_object_mut().unwrap().remove("tools_pending");
    value["messages"].as_array_mut().unwrap().push(json!({"role":"tool","tool_call_id":"write-1","content":"publication proved"}));
    value
}

#[test]
fn paid_tool_frontier_requires_dead_writer_and_exact_reconciled_prefix() {
    let store = MemoryStore::default();
    *store.writer.lock().unwrap() = Some(json!({"boot_id":"boot","pid_namespace":1,"pid":12,"start_ticks":10}));
    let journal = journal(&store);
    let first = journal.acquire("first").unwrap();
    first.load().unwrap(); first.begin(0).unwrap();
    let pending = pending_snapshot();
    first.pending(&pending).unwrap();
    assert_eq!(first.begin(1), Err(Error::Ineligible));
    assert_eq!(first.release(), Err(Error::Ineligible));
    assert_eq!(journal.recover("second").err(), Some(Error::Ineligible));
    store.writer_dead.store(true, Ordering::Release);
    let recovered = journal.recover("second").unwrap();
    assert_eq!(recovered.load().unwrap(), Some(pending.clone()));
    for key in ["usage", "round_limit", "tool_limit", "tool_calls", "deadline_unix_ms", "model_account", "tool_recovery"] {
        let mut changed = reconciled_snapshot(&pending);
        if key == "model_account" { changed[key]["tokens"] = json!(0); }
        else if key == "tool_recovery" { changed[key]["operation_id"] = json!("another-operation"); }
        else { changed[key] = match key { "usage" => json!([{"cost":0}]), _ => json!(2) }; }
        assert_eq!(recovered.complete(&changed), Err(Error::Invalid), "{key}");
    }
    let mut changed = reconciled_snapshot(&pending);
    changed["messages"][2]["tool_call_id"] = json!("another-write");
    assert_eq!(recovered.complete(&changed), Err(Error::Invalid));
    recovered.complete(&reconciled_snapshot(&pending)).unwrap();
    recovered.begin(1).unwrap();
    assert_eq!(*store.snapshot_writes.lock().unwrap(), 2);
}

#[test]
fn pending_requires_a_complete_unique_valid_tool_batch_and_paid_frontier() {
    for mode in ["empty", "duplicate", "arguments", "paid_usage", "missing_marker"] {
        let store=MemoryStore::default();let journal=journal(&store);
        let lease=journal.acquire("first").unwrap();lease.load().unwrap();lease.begin(0).unwrap();
        let mut state=pending_snapshot();
        match mode {
            "empty" => state["messages"][1]["tool_calls"]=json!([]),
            "duplicate" => { let call=state["messages"][1]["tool_calls"][0].clone();
                state["messages"][1]["tool_calls"].as_array_mut().unwrap().push(call);state["tool_calls"]=json!(2); },
            "arguments" => state["messages"][1]["tool_calls"][0]["function"]["arguments"]=json!("{bad"),
            "paid_usage" => state["usage"]=json!([]),
            _ => { state.as_object_mut().unwrap().remove("tools_pending"); },
        }
        assert_eq!(lease.pending(&state),Err(Error::Invalid),"{mode}");
        assert_eq!(*store.snapshot_writes.lock().unwrap(),0);
    }
}

#[test]
fn pending_snapshot_lost_ack_never_releases_or_retries_effects() {
    for after in [false,true] {
        let store = MemoryStore::default();
        let journal = journal(&store); let lease = journal.acquire("writer").unwrap();
        lease.load().unwrap(); lease.begin(0).unwrap();
        if after { store.fail_after.store(true,Ordering::Release); }
        else { store.fail_before.store(true,Ordering::Release); }
        assert_eq!(lease.pending(&pending_snapshot()),Err(Error::Storage));
        assert_eq!(lease.pending(&pending_snapshot()),Err(Error::Poisoned));
        assert_eq!(lease.release(),Err(Error::Poisoned));
        assert_eq!(journal.acquire("replacement").err(),Some(Error::Ineligible));
        assert_eq!(*store.snapshot_writes.lock().unwrap(),usize::from(after));
    }
}

#[test]
fn pending_completion_can_only_add_bounded_root_tool_consumption() {
    let store = MemoryStore::default();
    let journal = journal(&store);
    let lease = journal.acquire("writer").unwrap();
    lease.load().unwrap(); lease.begin(0).unwrap();
    let mut pending = pending_snapshot();
    pending["shared_model_account"] = json!({"format":"tofu.native-shared-account/v1",
        "owner_user_id":7,"root_task_id":"task","tokens":23,"tool_calls":1,"control_tool_calls":1,
        "model_ordinal":1,"tool_ordinal":0,"complete":true,
        "limits":{"max_tokens":100,"max_tool_calls":3,"deadline":null},
        "counter_policy":{"encoding":"cl100k_base","output_field":"max_tokens","wire_model":"fixture"}});
    lease.pending(&pending).unwrap();
    for mode in ["refund","overflow","unequal","tokens","ordinal","model","limit","new_field","missing"] {
        let mut value = reconciled_snapshot(&pending);
        let account = &mut value["shared_model_account"];
        account["tool_calls"] = json!(2); account["control_tool_calls"] = json!(2);
        match mode {
            "refund" => { account["tool_calls"]=json!(0); account["control_tool_calls"]=json!(0); },
            "overflow" => { account["tool_calls"]=json!(3); account["control_tool_calls"]=json!(3); },
            "unequal" => account["control_tool_calls"]=json!(1),
            "tokens" => account["tokens"]=json!(0),
            "ordinal" => account["tool_ordinal"]=json!(1),
            "model" => account["model_ordinal"]=json!(0),
            "limit" => account["limits"]["max_tool_calls"]=json!(10),
            "new_field" => account["root_tokens"]=json!(22),
            _ => { value.as_object_mut().unwrap().remove("shared_model_account"); },
        }
        assert_eq!(lease.complete(&value),Err(Error::Invalid),"{mode}");
    }
    let mut value = reconciled_snapshot(&pending);
    value["shared_model_account"]["tool_calls"]=json!(2);
    value["shared_model_account"]["control_tool_calls"]=json!(2);
    lease.complete(&value).unwrap();
    lease.begin(1).unwrap();
}

#[test]
fn actor_frontier_preserves_shared_root_and_only_advances_local_tool_consumption() {
    let store = MemoryStore::default();
    let journal = journal(&store);
    let lease = journal.acquire("actor-writer").unwrap();
    lease.load().unwrap(); lease.begin(0).unwrap();
    let mut pending = pending_snapshot();
    pending["shared_model_account"] = json!({"format":"tofu.native-shared-account/v1",
        "owner_user_id":7,"root_task_id":"shared-root","actor_task_id":"task",
        "tokens":23,"tool_calls":0,"control_tool_calls":0,"model_ordinal":19,"tool_ordinal":0,
        "complete":true,"limits":{"max_tokens":100,"max_tool_calls":3,"deadline":null},
        "counter_policy":{"encoding":"cl100k_base","output_field":"max_tokens","wire_model":"fixture"}});
    lease.pending(&pending).unwrap();
    let mut completed = reconciled_snapshot(&pending);
    completed["shared_model_account"]["tool_calls"] = json!(1);
    completed["shared_model_account"]["control_tool_calls"] = json!(1);
    for field in ["root_task_id", "actor_task_id", "model_ordinal"] {
        let mut changed = completed.clone();
        changed["shared_model_account"][field] = if field == "model_ordinal" { json!(20) } else { json!("other") };
        assert_eq!(lease.complete(&changed), Err(Error::Invalid), "{field}");
    }
    lease.complete(&completed).unwrap();
    lease.begin(1).unwrap();
}
