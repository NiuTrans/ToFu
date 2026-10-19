//! Embedded lifecycle binding for the shared checkpoint runtime owner.
//! Public methods retain the existing API; journal and store transitions live
//! in checkpoint_runtime.rs and callbacks join this runtime's close boundary.
use super::*;
use crate::checkpoint_runtime::CheckpointRuntime;
use std::sync::{atomic::Ordering, RwLockReadGuard};
use tofu_runtime::checkpoint_journal::{ActorHandoffSpec, Error, OwnedLease, ResumeLineage, Scope};

impl CheckpointRuntime for EmbeddedRuntime {
    fn checkpoint_guard(&self) -> Result<RwLockReadGuard<'_, ()>, Error> {
        let guard = self.checkpoint_calls.read().map_err(|_| Error::Storage)?;
        if *self.closed.lock().map_err(|_| Error::Storage)?
            || self.drop_disarmed.load(Ordering::Acquire)
        {
            return Err(Error::Storage);
        }
        Ok(guard)
    }
    fn checkpoint_database(&self) -> &Arc<Mutex<AuthorityDatabase>> { &self.database }
    fn checkpoint_scope(&self) -> crate::semantic::AuthenticatedScope { self.scope }
}

impl EmbeddedRuntime {
    pub fn checkpoint_goal_claim(&self, owner:u64, header_bytes:usize, snapshot_bytes:usize, request:&serde_json::Value, evidence_bytes:usize) -> Result<serde_json::Value, Error> {
        CheckpointRuntime::checkpoint_goal_claim(self, owner, header_bytes, snapshot_bytes, request, evidence_bytes)
    }
    pub fn checkpoint_goal_handoff(self:&Arc<Self>, owner:u64, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, specs:Vec<ActorHandoffSpec>, authorization:&serde_json::Value, evidence_bytes:usize) -> Result<Vec<OwnedLease>, Error> {
        CheckpointRuntime::checkpoint_goal_handoff(self, owner, header_bytes, snapshot_bytes, identity_bytes, specs, authorization, evidence_bytes)
    }
    pub fn checkpoint_frontier(&self, owner:u64, task:&str, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize) -> Result<Option<serde_json::Value>, Error> {
        CheckpointRuntime::checkpoint_frontier(self, owner, task, header_bytes, snapshot_bytes, identity_bytes)
    }
    pub fn checkpoint_invoice(&self, owner:u64, task:&str, header_bytes:usize) -> Result<Option<serde_json::Value>, Error> {
        CheckpointRuntime::checkpoint_invoice(self, owner, task, header_bytes)
    }
    pub fn checkpoint_lease(self:&Arc<Self>, scope:Scope, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, token:&str) -> Result<OwnedLease, Error> {
        CheckpointRuntime::checkpoint_lease(self, scope, header_bytes, snapshot_bytes, identity_bytes, token)
    }
    pub fn checkpoint_recover(self:&Arc<Self>, scope:Scope, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, token:&str) -> Result<OwnedLease, Error> {
        CheckpointRuntime::checkpoint_recover(self, scope, header_bytes, snapshot_bytes, identity_bytes, token)
    }
    pub fn checkpoint_handoff(self:&Arc<Self>, scope:Scope, header_bytes:usize, snapshot_bytes:usize, identity_bytes:usize, token:&str, lineage:&ResumeLineage) -> Result<OwnedLease, Error> {
        CheckpointRuntime::checkpoint_handoff(self, scope, header_bytes, snapshot_bytes, identity_bytes, token, lineage)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        authority::AuthorityDatabase,
        native_checkpoint_store::test_support::{wait_for_append, BarrierTask},
        vfs::RealVfs,
    };
    use std::time::{Duration, Instant};
    fn scope() -> Scope {
        Scope {
            owner: 7,
            task_id: "embedded-task".into(),
            epoch: "epoch".into(),
            binding: "model-tools-account".into(),
        }
    }
    fn runtime(path: &std::path::Path) -> Arc<EmbeddedRuntime> {
        let mut secret = b"embedded-checkpoint-test-secret-0001".to_vec();
        Arc::new(EmbeddedRuntime::open(path, 7, Some(3), None, &mut secret).unwrap())
    }
    #[test]
    fn owned_lease_uses_the_running_authority_and_refuses_foreign_scope_and_closed_calls() {
        let directory = tempfile::tempdir().unwrap();
        drop(AuthorityDatabase::initialize(directory.path()).unwrap());
        let runtime = runtime(directory.path());
        let mut foreign = scope();
        foreign.owner = 8;
        assert!(matches!(
            runtime.checkpoint_lease(foreign, 4096, 1048576, 1024, "foreign"),
            Err(Error::Invalid)
        ));
        let lease = runtime
            .checkpoint_lease(scope(), 4096, 1048576, 1024, "run")
            .unwrap();
        assert!(lease.load().unwrap().is_none());
        lease.begin(0).unwrap();
        lease.seal().unwrap();
        // Observe through the exact runtime's database, not a separately opened Store.
        let database = runtime.database.lock().unwrap();
        let mut tx = database.begin(3, 7).unwrap();
        let key = crate::versioned_document::record_key(
            3,
            7,
            crate::native_checkpoint_store::HEADERS,
            "embedded-task",
        )
        .unwrap();
        let (raw, _) = crate::versioned_document::get_value_with_version(
            &database,
            &mut tx,
            &key,
            crate::native_checkpoint_store::HEADERS,
            "embedded-task",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&raw).unwrap()["phase"],
            "terminal"
        );
        drop(tx);
        drop(database);
        runtime.close().unwrap();
        assert_eq!(lease.release(), Err(Error::Storage));
        let mut next = scope();
        next.task_id = "after-close".into();
        assert!(matches!(
            runtime.checkpoint_lease(next, 4096, 1048576, 1024, "closed"),
            Err(Error::Storage)
        ));
        drop(lease);
        drop(runtime);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = db.begin(3, 7).unwrap();
        let key = crate::versioned_document::record_key(
            3,
            7,
            crate::native_checkpoint_store::HEADERS,
            "embedded-task",
        )
        .unwrap();
        let (raw, _) = crate::versioned_document::get_value_with_version(
            &db,
            &mut tx,
            &key,
            crate::native_checkpoint_store::HEADERS,
            "embedded-task",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&raw).unwrap()["phase"],
            "terminal"
        );
        println!(
            "embedded_checkpoint_lifecycle_inline_bytes={}",
            std::mem::size_of::<std::sync::RwLock<()>>()
        );
    }
    #[test]
    fn runtime_close_joins_entered_checkpoint_flush_before_retiring_drivers() {
        let directory = tempfile::tempdir().unwrap();
        drop(AuthorityDatabase::initialize(directory.path()).unwrap());
        let runtime = runtime(directory.path());
        let barrier = crate::wal::WalBarrier::default();
        runtime
            .database
            .lock()
            .unwrap()
            .attach_wal_barrier(&barrier)
            .unwrap();
        barrier.sync_for_test(&RealVfs).unwrap();
        let before = runtime.database.lock().unwrap().wal_len().unwrap();
        let active = runtime.clone();
        let acquire = BarrierTask::spawn(&barrier, move || {
            active.checkpoint_lease(scope(), 4096, 1048576, 1024, "run")
        });
        wait_for_append(&runtime.database, before);
        assert!(!acquire.is_finished());
        let closing = runtime.clone();
        let close = BarrierTask::spawn(&barrier, move || closing.close());
        let until = Instant::now() + Duration::from_secs(3);
        while !*runtime.closed.lock().unwrap() {
            assert!(Instant::now() < until);
            std::thread::sleep(Duration::from_millis(1));
        }
        assert!(
            !close.is_finished(),
            "close retired a checkpoint's live barrier driver"
        );
        barrier.sync_for_test(&RealVfs).unwrap();
        let lease = acquire.finish().unwrap();
        close.finish().unwrap();
        assert_eq!(lease.load(), Err(Error::Storage));
        // This fixture replaced the runtime-owned coordinator with a manual
        // one. Retire that synthetic wait before authority Drop; no worker
        // exists to satisfy new shutdown-only checkpoint witnesses.
        barrier.inject_background_error_for_test(std::io::Error::other(
            "manual checkpoint fixture retired",
        ));
    }
    #[test]
    fn disarmed_embedded_runtime_refuses_checkpoint_effects_without_opening_an_authority() {
        let directory = tempfile::tempdir().unwrap();
        drop(AuthorityDatabase::initialize(directory.path()).unwrap());
        let runtime = runtime(directory.path());
        runtime.disarm_drop();
        assert!(matches!(
            runtime.checkpoint_lease(scope(), 4096, 1048576, 1024, "run"),
            Err(Error::Storage)
        ));
        runtime.close().unwrap();
    }
}

#[cfg(test)]
mod measurements {
    use super::*;
    #[test]
    fn embedded_checkpoint_binding_metadata_cost() {
        let directory = tempfile::tempdir().unwrap();
        drop(crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap());
        let mut secret = b"checkpoint-runtime-cost-secret-0001".to_vec();
        let runtime = Arc::new(
            EmbeddedRuntime::open(directory.path(), 7, Some(3), None, &mut secret).unwrap(),
        );
        let threads_before = std::fs::read_dir("/proc/self/task").unwrap().count();
        let mut nanos = Vec::new();
        for index in 0..8 {
            let scope = Scope {
                owner: 7,
                task_id: format!("cost-{index}"),
                epoch: "epoch".into(),
                binding: "model-tools-account".into(),
            };
            let start = std::time::Instant::now();
            let lease = runtime
                .checkpoint_lease(scope, 4096, 1048576, 1024, "run")
                .unwrap();
            assert!(lease.load().unwrap().is_none());
            lease.release().unwrap();
            nanos.push(start.elapsed().as_nanos());
        }
        let threads_after = std::fs::read_dir("/proc/self/task").unwrap().count();
        runtime.close().unwrap();
        println!(
            "{}",
            serde_json::json!({"format":"tofu.embedded-checkpoint-cost/v1","acquire_load_release_ns":nanos,
            "lifecycle_inline_bytes":std::mem::size_of::<std::sync::RwLock<()>>(),"store_wrapper_inline_bytes":std::mem::size_of::<crate::checkpoint_runtime::RuntimeStore<EmbeddedRuntime>>(),
            "threads_before":threads_before,"threads_after":threads_after,"scope":"disposable-local-authority","build":"debug"})
        );
    }
}
