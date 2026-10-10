//! Bounded parking for appends that arrive while a WAL rotation blocks admission.
//!
//! A generation counter distinguishes "the install I waited for already
//! happened" from "still in flight". The rotation driver signals after every
//! finish attempt. Request waiters use a separate completed generation, so
//! preparatory doorbells and transient drain retries do not redispatch a
//! command whose publication fence is still closed.

use std::sync::{Condvar, Mutex};
use std::time::Duration;

pub(crate) struct RotationBarrier {
    generation: Mutex<u64>,
    completed: std::sync::atomic::AtomicU64,
    condvar: Condvar,
    completion_condvar: Condvar,
}

impl RotationBarrier {
    pub(crate) fn new() -> Self {
        Self {
            generation: Mutex::new(0),
            completed: std::sync::atomic::AtomicU64::new(0),
            condvar: Condvar::new(),
            completion_condvar: Condvar::new(),
        }
    }

    pub(crate) fn generation(&self) -> u64 {
        *self
            .generation
            .lock()
            .unwrap_or_else(|error| error.into_inner())
    }

    pub(crate) fn signal_install(&self) {
        let mut guard = self
            .generation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        *guard = guard.wrapping_add(1);
        drop(guard);
        self.condvar.notify_all();
    }

    pub(crate) fn completed_generation(&self) -> u64 {
        self.completed.load(std::sync::atomic::Ordering::Acquire)
    }

    /// Success or a terminal failure releases request waiters. A transient
    /// defer only signals the driver's ordinary wake generation.
    pub(crate) fn signal_completion(&self) {
        let mut guard = self.generation.lock().unwrap_or_else(|error| error.into_inner());
        self.completed.fetch_add(1, std::sync::atomic::Ordering::Release);
        *guard = guard.wrapping_add(1);
        drop(guard);
        self.condvar.notify_all();
        self.completion_condvar.notify_all();
    }

    pub(crate) fn park_until_completed(&self, since: u64, slice: Duration) -> u64 {
        let guard = self.generation.lock().unwrap_or_else(|error| error.into_inner());
        let (_guard, _) = self.completion_condvar.wait_timeout_while(guard, slice,
            |_| self.completed_generation() == since)
            .unwrap_or_else(|error| error.into_inner());
        self.completed_generation()
    }

    /// Wait until the install generation advances past `since` or the slice
    /// elapses; returns the observed generation. The slice is only a liveness
    /// fallback — installs always signal — so it may be short.
    pub(crate) fn park(&self, since: u64, slice: Duration) -> u64 {
        let guard = self
            .generation
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let (guard, _) = self
            .condvar
            .wait_timeout_while(guard, slice, |generation| *generation == since)
            .unwrap_or_else(|error| error.into_inner());
        *guard
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::time::Instant;

    #[test]
    fn drain_retries_do_not_release_publication_waiters() {
        let barrier = Arc::new(RotationBarrier::new());
        let since = barrier.completed_generation();
        barrier.signal_install();
        assert_eq!(barrier.park_until_completed(since, Duration::ZERO), since);
        let waiter = Arc::clone(&barrier);
        let (tx, rx) = std::sync::mpsc::channel();
        let thread = std::thread::spawn(move || {
            tx.send(waiter.park_until_completed(since, Duration::from_secs(5))).unwrap();
        });
        barrier.signal_install();
        assert!(rx.recv_timeout(Duration::from_millis(20)).is_err());
        barrier.signal_completion();
        assert_ne!(rx.recv_timeout(Duration::from_secs(1)).unwrap(), since);
        thread.join().unwrap();
        assert_ne!(barrier.park_until_completed(since, Duration::ZERO), since);
    }

    #[test]
    fn park_returns_immediately_when_the_generation_already_advanced() {
        let barrier = RotationBarrier::new();
        let since = barrier.generation();
        barrier.signal_install();
        let began = Instant::now();
        let observed = barrier.park(since, Duration::from_millis(500));
        assert!(observed > since);
        assert!(began.elapsed() < Duration::from_millis(100));
    }

    #[test]
    fn park_wakes_on_the_install_signal() {
        let barrier = Arc::new(RotationBarrier::new());
        let since = barrier.generation();
        let signer = Arc::clone(&barrier);
        let wake = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(20));
            signer.signal_install();
        });
        let began = Instant::now();
        let observed = barrier.park(since, Duration::from_secs(5));
        assert!(observed > since);
        assert!(began.elapsed() >= Duration::from_millis(20));
        wake.join().unwrap();
    }

    #[test]
    fn park_times_out_at_the_slice_without_a_signal() {
        let barrier = RotationBarrier::new();
        let since = barrier.generation();
        let began = Instant::now();
        let observed = barrier.park(since, Duration::from_millis(10));
        assert_eq!(observed, since);
        assert!(began.elapsed() >= Duration::from_millis(10));
        assert!(began.elapsed() < Duration::from_millis(500));
    }
}
