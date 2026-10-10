//! Joined background checkpoint probes; unwind wakes pending barrier waits.
use crate::{authority::AuthorityDatabase, wal::WalBarrier};
use std::{
    sync::{Arc, Mutex},
    thread::JoinHandle,
    time::{Duration, Instant},
};
pub(crate) struct BarrierTask<T> {
    barrier: WalBarrier,
    worker: Option<JoinHandle<T>>,
}
impl<T: Send + 'static> BarrierTask<T> {
    pub(crate) fn spawn(barrier: &WalBarrier, call: impl FnOnce() -> T + Send + 'static) -> Self {
        Self {
            barrier: barrier.clone(),
            worker: Some(std::thread::spawn(call)),
        }
    }
    pub(crate) fn is_finished(&self) -> bool {
        self.worker.as_ref().unwrap().is_finished()
    }
    pub(crate) fn finish(mut self) -> T {
        self.worker.take().unwrap().join().unwrap()
    }
}
impl<T> Drop for BarrierTask<T> {
    fn drop(&mut self) {
        if let Some(worker) = self.worker.take() {
            if !worker.is_finished() {
                self.barrier
                    .inject_background_error_for_test(std::io::Error::other(
                        "checkpoint test unwind",
                    ));
            }
            let _ = worker.join();
        }
    }
}
pub(crate) fn wait_for_append(database: &Arc<Mutex<AuthorityDatabase>>, previous: u64) {
    let until = Instant::now() + Duration::from_secs(3);
    loop {
        if let Ok(database) = database.try_lock() {
            if database.wal_len().unwrap() > previous {
                drop(database);
                // Give an incorrectly early-returning callback time to finish;
                // the positive path remains blocked on its unsynced witness.
                std::thread::sleep(Duration::from_millis(10));
                return;
            }
        }
        assert!(
            Instant::now() < until,
            "checkpoint did not release writer after append"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
}
