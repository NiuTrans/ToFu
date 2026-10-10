//! One bounded retired-WAL handoff between Engine and its existing pack worker.
//! Engine queues only after durable checkpoint publication and drains before
//! its next checkpoint. Reopen derives one predecessor from CONTROL; no scan.
//! Filesystem deletion runs under this private mutex, never the authority lock
//! when the worker owns it. Failure retains the slot and checkpoint backpressure.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::vfs::{sync_directory_barrier, Vfs};
use crate::wal::active_log_path;

#[derive(Default)]
struct RetirementState {
    generation: Option<u64>,
    worker_registered: bool,
}

pub(crate) struct WalRetirement {
    data_dir: PathBuf,
    vfs: Arc<dyn Vfs>,
    state: Mutex<RetirementState>,
}

impl WalRetirement {
    pub(crate) fn new(data_dir: &Path, vfs: Arc<dyn Vfs>, active_generation: u64) -> Self {
        Self {
            data_dir: data_dir.to_owned(),
            vfs,
            state: Mutex::new(RetirementState {
                generation: active_generation.checked_sub(1).filter(|value| *value > 0),
                worker_registered: false,
            }),
        }
    }

    pub(crate) fn register_worker(self: &Arc<Self>) -> io::Result<WalRetirementRegistration> {
        let mut state = self.state.lock().map_err(|_| poisoned())?;
        if state.worker_registered {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "WAL retirement worker already registered",
            ));
        }
        state.worker_registered = true;
        Ok(WalRetirementRegistration {
            retirement: Arc::clone(self),
            active: true,
        })
    }

    /// The single Engine producer must drain before its next checkpoint.
    /// Without a registered worker, preserve synchronous cleanup.
    pub(crate) fn retire(&self, generation: u64) -> io::Result<()> {
        active_log_path(&self.data_dir, generation)?;
        let mut state = self.state.lock().map_err(|_| poisoned())?;
        if state.generation.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "previous WAL retirement is still pending",
            ));
        }
        state.generation = Some(generation);
        if !state.worker_registered {
            // Publication already succeeded. Keep its original success/error
            // boundary; an unsuccessful cleanup stays pending and the next
            // checkpoint must retry it before publishing another generation.
            let _ = self.reclaim_locked(&mut state);
        }
        Ok(())
    }

    /// Admission must never wait behind worker deletion/fsync while holding
    /// the authority mutex. The worker owns cleanup; occupied means retry.
    pub(crate) fn ready_for_rotation(&self) -> io::Result<bool> {
        match self.state.try_lock() {
            Ok(state) => Ok(state.generation.is_none()),
            Err(std::sync::TryLockError::WouldBlock) => Ok(false),
            Err(std::sync::TryLockError::Poisoned(_)) => Err(poisoned()),
        }
    }

    pub(crate) fn reclaim(&self) -> io::Result<bool> {
        let mut state = self.state.lock().map_err(|_| poisoned())?;
        self.reclaim_locked(&mut state)
    }

    fn reclaim_locked(&self, state: &mut RetirementState) -> io::Result<bool> {
        let Some(generation) = state.generation else {
            return Ok(false);
        };
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_REMOVE);
        let path = active_log_path(&self.data_dir, generation)?;
        match self.vfs.remove_file(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        // A prior attempt may have unlinked but failed its directory barrier.
        // Even NotFound must finish that barrier before releasing the slot.
        sync_directory_barrier(self.vfs.as_ref(), &self.data_dir)?;
        state.generation = None;
        Ok(true)
    }
}

fn poisoned() -> io::Error {
    io::Error::other("WAL retirement state poisoned")
}

pub(crate) struct WalRetirementRegistration {
    retirement: Arc<WalRetirement>,
    active: bool,
}

impl WalRetirementRegistration {
    /// Cleanup and unregister share the producer mutex: a concurrent final
    /// checkpoint either joins this cleanup or observes synchronous fallback.
    pub(crate) fn finish(mut self) -> io::Result<bool> {
        let mut state = self.retirement.state.lock().map_err(|_| poisoned())?;
        let result = self.retirement.reclaim_locked(&mut state);
        state.worker_registered = false;
        self.active = false;
        result
    }
}

impl Drop for WalRetirementRegistration {
    fn drop(&mut self) {
        if self.active {
            if let Ok(mut state) = self.retirement.state.lock() {
                state.worker_registered = false;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::Engine;
    use crate::vfs::{
        BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, FaultAction, FaultRule,
        FileKind, OpenRequest, RealVfs, VfsFile,
    };
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Condvar,
    };
    use std::time::Duration;

    struct PausedWalDeletion {
        entered: mpsc::SyncSender<()>,
        armed: AtomicBool,
        deny_removal: AtomicBool,
        panic_removal: AtomicBool,
        gate_timed_out: AtomicBool,
        released: Mutex<bool>,
        wake: Condvar,
    }

    impl Vfs for PausedWalDeletion {
        fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
            RealVfs.open(path, request)
        }
        fn create_dir(&self, path: &Path) -> io::Result<()> {
            RealVfs.create_dir(path)
        }
        fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
            RealVfs.rename(source, destination)
        }
        fn metadata(&self, path: &Path) -> io::Result<FileKind> {
            RealVfs.metadata(path)
        }
        fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
            RealVfs.read_directory(path)
        }
        fn read_directory_bounded(
            &self,
            path: &Path,
            maximum: usize,
        ) -> io::Result<BoundedDirectoryEntries> {
            RealVfs.read_directory_bounded(path, maximum)
        }
        fn match_directory_prefix_bounded(
            &self,
            path: &Path,
            prefix: &str,
            maximum: usize,
            matches: usize,
        ) -> io::Result<BoundedDirectoryMatches> {
            RealVfs.match_directory_prefix_bounded(path, prefix, maximum, matches)
        }
        fn sync_directory(&self, path: &Path) -> io::Result<()> {
            RealVfs.sync_directory(path)
        }
        fn remove_file(&self, path: &Path) -> io::Result<()> {
            if path.file_name().is_some_and(|name| name == "active.wal")
                && self.armed.swap(false, Ordering::AcqRel)
            {
                self.entered.send(()).unwrap();
                let (released, _) = self
                    .wake
                    .wait_timeout_while(
                        self.released.lock().unwrap(),
                        Duration::from_secs(10),
                        |value| !*value,
                    )
                    .unwrap();
                if !*released {
                    self.gate_timed_out.store(true, Ordering::Release);
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "test deletion gate timed out",
                    ));
                }
            }
            assert!(
                !self.panic_removal.load(Ordering::Acquire),
                "injected WAL retirement panic"
            );
            if self.deny_removal.load(Ordering::Acquire) {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "injected persistent unlink failure",
                ));
            }
            RealVfs.remove_file(path)
        }
    }

    #[test]
    fn wal_retirement_worker_does_not_hold_authority_while_deletion_is_blocked() {
        let directory = tempfile::tempdir().unwrap();
        let (entered_sender, entered_receiver) = mpsc::sync_channel(1);
        let vfs = Arc::new(PausedWalDeletion {
            entered: entered_sender,
            armed: AtomicBool::new(true),
            deny_removal: AtomicBool::new(false),
            panic_removal: AtomicBool::new(false),
            gate_timed_out: AtomicBool::new(false),
            released: Mutex::new(false),
            wake: Condvar::new(),
        });
        let engine = Arc::new(Mutex::new(
            Engine::initialize_with_vfs(directory.path(), vfs.clone()).unwrap(),
        ));
        let packer = engine.lock().unwrap().start_background_packer().unwrap();
        {
            let mut engine = engine.lock().unwrap();
            engine.commit(b"first").unwrap();
            engine.checkpoint().unwrap();
        }
        let deletion_started = entered_receiver.recv_timeout(Duration::from_secs(2));
        let writing_engine = Arc::clone(&engine);
        let (sender, receiver) = mpsc::sync_channel(1);
        let writer = std::thread::spawn(move || {
            sender
                .send(writing_engine.lock().unwrap().commit(b"during-delete"))
                .unwrap();
        });
        let committed = receiver.recv_timeout(Duration::from_secs(2));
        // Always release and join before asserting, including on regression.
        *vfs.released.lock().unwrap() = true;
        vfs.wake.notify_all();
        writer.join().unwrap();
        packer.stop_and_join().unwrap();
        deletion_started.expect("worker must enter the real VFS deletion");
        committed
            .expect("ordinary commit must finish while deletion is blocked")
            .unwrap();
        assert!(!vfs.gate_timed_out.load(Ordering::Acquire));
        assert!(packer.metrics().retired_wals >= 1);
        assert!(!directory.path().join("active.wal").exists());
        drop(engine);
        let reopened = Engine::open(directory.path()).unwrap();
        assert_eq!(reopened.state().durable_sequence, 2);
        assert_eq!(
            reopened
                .transaction_at(2)
                .unwrap()
                .unwrap()
                .envelope
                .inline_payload,
            b"during-delete"
        );
    }

    #[test]
    fn wal_retirement_worker_shutdown_surfaces_error_and_engine_recovers_capacity() {
        let directory = tempfile::tempdir().unwrap();
        let (sender, receiver) = mpsc::sync_channel(1);
        let vfs = Arc::new(PausedWalDeletion {
            entered: sender,
            armed: AtomicBool::new(true),
            deny_removal: AtomicBool::new(false),
            panic_removal: AtomicBool::new(false),
            gate_timed_out: AtomicBool::new(false),
            released: Mutex::new(false),
            wake: Condvar::new(),
        });
        let mut engine = Engine::initialize_with_vfs(directory.path(), vfs.clone()).unwrap();
        let packer = engine.start_background_packer().unwrap();
        engine.commit(b"first").unwrap();
        engine.checkpoint().unwrap();
        let started = receiver.recv_timeout(Duration::from_secs(2));
        vfs.deny_removal.store(true, Ordering::Release);
        *vfs.released.lock().unwrap() = true;
        vfs.wake.notify_all();
        let stopped = packer.stop_and_join();
        started.unwrap();
        assert!(!vfs.gate_timed_out.load(Ordering::Acquire));
        assert_eq!(stopped.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert!(packer.metrics().failed_wal_retirements > 0);
        assert!(!engine.block_store_shared().background_packer_running());
        assert!(directory.path().join("active.wal").exists());
        engine.commit(b"second").unwrap();
        assert_eq!(
            engine.checkpoint().unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(engine.state().active_log_generation, 2);
        vfs.deny_removal.store(false, Ordering::Release);
        engine.checkpoint().unwrap();
        assert_eq!(engine.state().active_log_generation, 3);
        assert!(!directory.path().join("active.wal").exists());
        let successor = engine.start_background_packer().unwrap();
        successor.stop_and_join().unwrap();
        drop(engine);
        let reopened = Engine::open(directory.path()).unwrap();
        assert_eq!(reopened.state().durable_sequence, 2);
        assert_eq!(
            reopened
                .transaction_at(2)
                .unwrap()
                .unwrap()
                .envelope
                .inline_payload,
            b"second"
        );
    }

    #[test]
    fn wal_retirement_panic_reaches_observer_and_reopen_finishes_cleanup() {
        let directory = tempfile::tempdir().unwrap();
        let (sender, receiver) = mpsc::sync_channel(1);
        let vfs = Arc::new(PausedWalDeletion {
            entered: sender,
            armed: AtomicBool::new(true),
            deny_removal: AtomicBool::new(false),
            panic_removal: AtomicBool::new(false),
            gate_timed_out: AtomicBool::new(false),
            released: Mutex::new(false),
            wake: Condvar::new(),
        });
        let mut engine = Engine::initialize_with_vfs(directory.path(), vfs.clone()).unwrap();
        let packer = engine.start_background_packer().unwrap();
        engine.commit(b"before-panic").unwrap();
        engine.checkpoint().unwrap();
        let started = receiver.recv_timeout(Duration::from_secs(2));
        vfs.panic_removal.store(true, Ordering::Release);
        *vfs.released.lock().unwrap() = true;
        vfs.wake.notify_all();
        let stopped = packer.stop_and_join();
        started.unwrap();
        assert!(!vfs.gate_timed_out.load(Ordering::Acquire));
        assert!(stopped.is_err());
        assert!(packer.observer().require_healthy().is_err());
        assert!(!engine.block_store_shared().background_packer_running());
        drop(engine);
        let mut reopened = Engine::open(directory.path()).unwrap();
        assert_eq!(reopened.state().durable_sequence, 1);
        let successor = reopened.start_background_packer().unwrap();
        successor.stop_and_join().unwrap();
        assert!(!directory.path().join("active.wal").exists());
        reopened.commit(b"after-panic-reopen").unwrap();
        reopened.checkpoint().unwrap();
    }

    #[test]
    fn wal_retirement_shutdown_failure_releases_registration_and_retains_slot() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        let retirement = Arc::new(WalRetirement::new(Path::new("/data"), vfs.clone(), 1));
        let registration = retirement.register_worker().unwrap();
        retirement.retire(1).unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
        }))
        .unwrap();
        assert_eq!(
            registration.finish().unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        let state = retirement.state.lock().unwrap();
        assert!(!state.worker_registered);
        assert_eq!(state.generation, Some(1));
        drop(state);
        vfs.arm_fault(None).unwrap();
        assert!(retirement.reclaim().unwrap());
        assert!(retirement.register_worker().unwrap().finish().is_ok());
    }
}
