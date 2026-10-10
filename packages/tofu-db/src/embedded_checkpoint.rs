//! Owned checkpoint callbacks on the exact running embedded authority.
//! Runtime lifecycle guards join entered Store calls before driver retirement.
//! No authority open, worker, alternate backend, token attachment or journal logic.
use super::EmbeddedRuntime;
use crate::native_checkpoint_store::NativeCheckpointStore;
use std::sync::{atomic::Ordering, Arc, RwLockReadGuard};
use tofu_runtime::checkpoint_journal::{Error, Header, OwnedLease, Scope, SnapshotWrite, Store};

struct RuntimeStore {
    runtime: Arc<EmbeddedRuntime>,
    store: NativeCheckpointStore,
}
impl EmbeddedRuntime {
    fn checkpoint_guard(&self) -> Result<RwLockReadGuard<'_, ()>, Error> {
        let guard = self.checkpoint_calls.read().map_err(|_| Error::Storage)?;
        if *self.closed.lock().map_err(|_| Error::Storage)?
            || self.drop_disarmed.load(Ordering::Acquire)
        {
            return Err(Error::Storage);
        }
        Ok(guard)
    }
    /// Bind an owned journal lease to this runtime's authenticated owner/tenant.
    /// Callers supply task/epoch/binding, token and explicit byte bounds. The
    /// shared Store and original runtime remain alive until the lease drops.
    /// Close/abandon joins entered calls; all later callbacks refuse before I/O.
    pub fn checkpoint_lease(
        self: &Arc<Self>,
        scope: Scope,
        header_bytes: usize,
        snapshot_bytes: usize,
        identity_bytes: usize,
        token: &str,
    ) -> Result<OwnedLease, Error> {
        if scope.owner != self.scope.owner_id {
            return Err(Error::Invalid);
        }
        let guard = self.checkpoint_guard()?;
        let store = NativeCheckpointStore::new(
            self.database.clone(),
            self.scope.tenant_id.unwrap_or(0),
            self.scope.owner_id,
            header_bytes,
            snapshot_bytes,
        )?;
        drop(guard);
        OwnedLease::acquire(
            scope,
            Arc::new(RuntimeStore {
                runtime: self.clone(),
                store,
            }),
            snapshot_bytes,
            identity_bytes,
            token,
        )
    }
}
impl Store for RuntimeStore {
    fn header(&self, owner: u64, task: &str) -> Result<Option<Header>, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store.header(owner, task)
    }
    fn compare_exchange(
        &self,
        owner: u64,
        task: &str,
        expected: u64,
        header: &serde_json::Value,
        snapshot: SnapshotWrite<'_>,
    ) -> Result<u64, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store
            .compare_exchange(owner, task, expected, header, snapshot)
    }
    fn snapshot(
        &self,
        owner: u64,
        task: &str,
        version: u64,
    ) -> Result<Option<serde_json::Value>, Error> {
        let _guard = self.runtime.checkpoint_guard()?;
        self.store.snapshot(owner, task, version)
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
        barrier.inject_background_error_for_test(std::io::Error::other("manual checkpoint fixture retired"));
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
            "lifecycle_inline_bytes":std::mem::size_of::<std::sync::RwLock<()>>(),"store_wrapper_inline_bytes":std::mem::size_of::<RuntimeStore>(),
            "threads_before":threads_before,"threads_after":threads_after,"scope":"disposable-local-authority","build":"debug"})
        );
    }
}
