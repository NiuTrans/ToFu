//! Demand-created embedded job delivery with recently idle worker reuse.
//! One queue lock protects admission and waiter registration. Parking happens
//! after registration; unpark tokens close the submit-before-park race. The
//! unique sender closes admission and wakes every receiver on destruction.
//! Dispatch, watchdog, response ownership and byte budgets remain in embed_ffi.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::sync::mpsc::TrySendError;
use std::thread::{self, Thread};

struct State<T> {
    jobs: VecDeque<T>,
    idle: Vec<Thread>,
    closed: bool,
    receivers: usize,
}

struct Shared<T> {
    state: Mutex<State<T>>,
    idle_reuse_target: usize,
}

pub(crate) struct JobSender<T> { shared: Arc<Shared<T>> }
pub(crate) struct JobReceiver<T> { shared: Arc<Shared<T>> }

#[derive(Debug)]
pub(crate) enum ReceiveError { Closed, Poisoned }

pub(crate) fn delivery<T>(idle_reuse_target: usize) -> (JobSender<T>, JobReceiver<T>) {
    assert!(idle_reuse_target > 0, "embedded idle reuse target must be positive");
    let shared = Arc::new(Shared {
        state: Mutex::new(State { jobs: VecDeque::new(), idle: Vec::new(), closed: false, receivers: 1 }),
        idle_reuse_target,
    });
    (JobSender { shared: Arc::clone(&shared) }, JobReceiver { shared })
}

impl<T> Clone for JobReceiver<T> {
    fn clone(&self) -> Self {
        self.shared.state.lock().unwrap_or_else(|error| error.into_inner()).receivers += 1;
        Self { shared: Arc::clone(&self.shared) }
    }
}

impl<T> Drop for JobReceiver<T> {
    fn drop(&mut self) {
        let abandoned = {
            let mut state = self.shared.state.lock().unwrap_or_else(|error| error.into_inner());
            state.receivers -= 1;
            if state.receivers == 0 {
                state.closed = true;
                Some(std::mem::take(&mut state.jobs))
            } else { None }
        };
        // Release queued jobs and their byte reservations outside the lock,
        // matching a disconnected standard channel's last-receiver cleanup.
        drop(abandoned);
    }
}

impl<T> JobSender<T> {
    /// Claim a ready worker or return the owned job for a new worker.
    /// No job is published when no worker is ready, so thread-creation failure
    /// can drop that owned job without ambiguous dispatch or duplicate work.
    pub(crate) fn try_handoff(&self, job: T) -> Result<Option<T>, T> {
        let wake = {
            let lock = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_QUEUE_LOCK);
            let Ok(mut state) = self.shared.state.lock() else { return Err(job); };
            drop(lock);
            let _admit = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_QUEUE_ADMIT);
            if state.closed { return Err(job); }
            let Some(worker) = state.idle.pop() else { return Ok(Some(job)); };
            state.jobs.push_back(job);
            worker
        };
        let _notify = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_QUEUE_NOTIFY);
        wake.unpark();
        Ok(None)
    }

    #[cfg(test)]
    pub(crate) fn try_send(&self, job: T) -> Result<(), TrySendError<T>> {
        let wake = {
            let lock = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_QUEUE_LOCK);
            let Ok(mut state) = self.shared.state.lock() else {
                return Err(TrySendError::Disconnected(job));
            };
            drop(lock);
            let _admit = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_QUEUE_ADMIT);
            if state.closed { return Err(TrySendError::Disconnected(job)); }
            state.jobs.push_back(job);
            // Job order stays FIFO. Choose the last idle worker to reuse its
            // hot dispatch state instead of migrating a sequential caller.
            state.idle.pop()
        };
        if let Some(worker) = wake {
            let _notify = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_QUEUE_NOTIFY);
            worker.unpark();
        }
        Ok(())
    }
}

impl<T> Drop for JobSender<T> {
    fn drop(&mut self) {
        // No user callback or job destructor runs inside this critical section.
        // Recover poison only to close and wake: receivers still fail closed.
        let idle = {
            let mut state = self.shared.state.lock().unwrap_or_else(|error| error.into_inner());
            state.closed = true;
            std::mem::take(&mut state.idle)
        };
        for worker in idle { worker.unpark(); }
    }
}

impl<T> JobReceiver<T> {
    /// Advertise the nonblocking completion tail before waking its caller.
    /// Pending jobs retain FIFO order. An early unpark remains a token until
    /// recv claims the next job or registers/parks; no callback runs here.
    pub(crate) fn prepare_for_next_job(&self) -> Result<(), ReceiveError> {
        let current = thread::current();
        let lock = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_WORKER_QUEUE_LOCK);
        let mut state = match self.shared.state.try_lock() {
            Ok(state) => state,
            Err(std::sync::TryLockError::Poisoned(_)) => return Err(ReceiveError::Poisoned),
            Err(std::sync::TryLockError::WouldBlock) => return Ok(()),
        };
        drop(lock);
        if state.closed { return Ok(()); }
        let _register = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_WORKER_REGISTER);
        if state.idle.len() < self.shared.idle_reuse_target
                && !state.idle.iter().any(|worker| worker.id() == current.id()) {
            state.idle.push(current);
        }
        Ok(())
    }

    pub(crate) fn recv(&self) -> Result<T, ReceiveError> {
        let current = thread::current();
        loop {
            {
                let lock = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_WORKER_QUEUE_LOCK);
                let mut state = self.shared.state.lock().map_err(|_| ReceiveError::Poisoned)?;
                drop(lock);
                let claim = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_WORKER_QUEUE_CLAIM);
                if let Some(job) = state.jobs.pop_front() {
                    // park permits spurious wakeups. A receiver can therefore
                    // claim a job before a selected waiter; remove its previous
                    // registration so it cannot be selected twice.
                    if let Some(index) = state.idle.iter().position(|worker| worker.id() == current.id()) {
                        state.idle.swap_remove(index);
                    }
                    return Ok(job);
                }
                drop(claim);
                if state.closed { return Err(ReceiveError::Closed); }
                if !state.idle.iter().any(|worker| worker.id() == current.id())
                        && state.idle.len() >= self.shared.idle_reuse_target {
                    return Err(ReceiveError::Closed); // Retire only this excess carrier.
                }
                let _register = crate::fsync_stats::StageTimer::diagnostic(crate::fsync_stats::STAGE_EMBEDDED_WORKER_REGISTER);
                if !state.idle.iter().any(|worker| worker.id() == current.id()) {
                    state.idle.push(current.clone());
                }
            }
            thread::park();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::Duration;

    #[test]
    fn a_busy_delivery_returns_owned_jobs_for_demand_workers_without_queuing() {
        let (sender, receiver) = delivery(1);
        for job in 0..128 {
            assert_eq!(sender.try_handoff(job).unwrap(), Some(job));
        }
        assert!(receiver.shared.state.lock().unwrap().jobs.is_empty());
    }

    #[test]
    fn ready_worker_handoff_preserves_close_and_exact_ownership() {
        let (sender, receiver) = delivery(1);
        let (ready, ready_rx) = mpsc::channel();
        let (done, done_rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            receiver.prepare_for_next_job().unwrap();
            ready.send(()).unwrap();
            done.send(receiver.recv().unwrap()).unwrap();
            assert!(receiver.recv().is_err());
        });
        ready_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(sender.try_handoff(42).unwrap(), None);
        assert_eq!(done_rx.recv_timeout(Duration::from_secs(5)).unwrap(), 42);
        drop(sender);
        worker.join().unwrap();
    }

    #[test]
    fn excess_ready_carriers_retire_without_closing_the_delivery() {
        let (sender, receiver) = delivery(1);
        receiver.prepare_for_next_job().unwrap();
        let extra = receiver.clone();
        assert!(thread::spawn(move || extra.recv()).join().unwrap().is_err());
        assert_eq!(sender.try_handoff(7).unwrap(), None);
        assert_eq!(receiver.recv().unwrap(), 7);
    }

    #[test]
    fn completion_ready_worker_accepts_a_job_before_returning_to_recv() {
        let (sender, receiver) = delivery(2);
        let shared = Arc::clone(&receiver.shared);
        let (ready, ack) = mpsc::channel();
        let (finish, release) = mpsc::channel();
        let worker = thread::spawn(move || {
            assert_eq!(receiver.recv().unwrap(), 1);
            receiver.prepare_for_next_job().unwrap();
            receiver.prepare_for_next_job().unwrap();
            ready.send(thread::current().id()).unwrap();
            release.recv().unwrap();
            assert_eq!(receiver.recv().unwrap(), 2);
            assert!(receiver.recv().is_err());
        });
        sender.try_send(1).unwrap();
        let id = ack.recv_timeout(Duration::from_secs(5)).unwrap();
        let state = shared.state.lock().unwrap();
        assert_eq!(state.idle.len(), 1);
        assert_eq!(state.idle[0].id(), id);
        drop(state);
        sender.try_send(2).unwrap();
        assert!(shared.state.lock().unwrap().idle.is_empty());
        drop(sender);
        finish.send(()).unwrap();
        worker.join().unwrap();
    }

    #[test]
    fn completion_hint_never_waits_for_a_busy_queue() {
        let (_sender, receiver) = delivery::<u8>(1);
        let lock = receiver.shared.state.lock().unwrap();
        receiver.prepare_for_next_job().unwrap();
        assert!(lock.idle.is_empty());
    }

    #[test]
    fn completion_registration_respects_closed_and_poisoned_queues() {
        let (sender, receiver) = delivery::<u8>(1);
        drop(sender);
        receiver.prepare_for_next_job().unwrap();
        assert!(receiver.shared.state.lock().unwrap().idle.is_empty());
        let (sender, receiver) = delivery::<u8>(1);
        let shared = Arc::clone(&receiver.shared);
        assert!(thread::spawn(move || { let _lock=shared.state.lock().unwrap(); panic!("completion registration fault"); }).join().is_err());
        assert!(matches!(receiver.prepare_for_next_job(), Err(ReceiveError::Poisoned)));
        drop(sender);
    }

    #[test]
    fn handoff_fifo_and_close_drain_are_preserved() {
        let (sender, receiver) = delivery(2);
        sender.try_send(10).unwrap();
        sender.try_send(20).unwrap();
        sender.try_send(30).unwrap();
        drop(sender);
        assert_eq!(receiver.recv().unwrap(), 10);
        assert_eq!(receiver.recv().unwrap(), 20);
        assert_eq!(receiver.recv().unwrap(), 30);
        assert!(receiver.recv().is_err());
    }

    #[test]
    fn last_receiver_disconnects_and_releases_queued_ownership() {
        struct Owned(Arc<std::sync::atomic::AtomicUsize>);
        impl Drop for Owned {
            fn drop(&mut self) { self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst); }
        }
        let dropped = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let (sender, receiver) = delivery(2);
        assert!(sender.try_send(Owned(Arc::clone(&dropped))).is_ok());
        let second = receiver.clone();
        drop(receiver);
        assert_eq!(dropped.load(std::sync::atomic::Ordering::SeqCst), 0);
        drop(second);
        assert_eq!(dropped.load(std::sync::atomic::Ordering::SeqCst), 1);
        assert!(matches!(sender.try_send(Owned(Arc::clone(&dropped))), Err(TrySendError::Disconnected(_))));
        assert_eq!(dropped.load(std::sync::atomic::Ordering::SeqCst), 2);
    }

    #[test]
    fn poison_fails_closed_and_sender_drop_can_still_close() {
        let (sender, receiver) = delivery(2);
        let state = Arc::clone(&receiver.shared);
        assert!(thread::spawn(move || {
            let _guard = state.state.lock().unwrap();
            panic!("queue lock fault");
        }).join().is_err());
        assert!(matches!(sender.try_send(1), Err(TrySendError::Disconnected(1))));
        assert!(matches!(receiver.recv(), Err(ReceiveError::Poisoned)));
        drop(sender);
    }

    #[test]
    fn the_most_recent_idle_worker_receives_the_next_job() {
        let (sender, receiver) = delivery(2);
        let (done, results) = mpsc::channel();
        let mut workers = Vec::new();
        for index in 0..2 {
            let worker_receiver = receiver.clone(); let done = done.clone();
            workers.push(thread::spawn(move || { done.send((index, worker_receiver.recv().ok())).unwrap(); }));
            let deadline = std::time::Instant::now() + Duration::from_secs(5);
            while receiver.shared.state.lock().unwrap().idle.len() != index + 1 {
                assert!(std::time::Instant::now() < deadline);
                thread::yield_now();
            }
        }
        sender.try_send(42).unwrap();
        assert_eq!(results.recv_timeout(Duration::from_secs(5)).unwrap(), (1, Some(42)));
        drop(sender);
        assert_eq!(results.recv_timeout(Duration::from_secs(5)).unwrap(), (0, None));
        for worker in workers { worker.join().unwrap(); }
    }

    #[test]
    fn closing_wakes_every_registered_worker() {
        let (sender, receiver) = delivery::<usize>(8);
        let (done, results) = mpsc::channel();
        let workers: Vec<_> = (0..8).map(|_| {
            let receiver = receiver.clone();
            let done = done.clone();
            thread::spawn(move || { done.send(receiver.recv().is_err()).unwrap(); })
        }).collect();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while receiver.shared.state.lock().unwrap().idle.len() != workers.len() {
            assert!(std::time::Instant::now() < deadline);
            thread::yield_now();
        }
        drop(sender);
        for _ in &workers { assert!(results.recv_timeout(Duration::from_secs(5)).unwrap()); }
        for worker in workers { worker.join().unwrap(); }
    }

    #[test]
    fn spurious_wakes_do_not_duplicate_waiters_or_lose_submission() {
        let (sender, receiver) = delivery(1);
        let observer = receiver.clone();
        let (done, result) = mpsc::channel();
        let worker = thread::spawn(move || { done.send(receiver.recv().unwrap()).unwrap(); });
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            if observer.shared.state.lock().unwrap().idle.len() == 1 { break; }
            assert!(std::time::Instant::now() < deadline);
            thread::yield_now();
        }
        for _ in 0..100 { worker.thread().unpark(); thread::yield_now(); }
        assert_eq!(observer.shared.state.lock().unwrap().idle.len(), 1);
        sender.try_send(42).unwrap();
        assert_eq!(result.recv_timeout(Duration::from_secs(5)).unwrap(), 42);
        worker.join().unwrap();
        assert!(observer.shared.state.lock().unwrap().idle.is_empty());
    }

    #[test]
    fn concurrent_producers_deliver_each_job_exactly_once() {
        let (sender, receiver) = delivery(32);
        let sender = Arc::new(sender);
        let (done, results) = mpsc::channel();
        let workers: Vec<_> = (0..8).map(|_| {
            let receiver = receiver.clone(); let done = done.clone();
            thread::spawn(move || { while let Ok(job) = receiver.recv() { done.send(job).unwrap(); } })
        }).collect();
        let producers: Vec<_> = (0..8).map(|producer| {
            let sender = Arc::clone(&sender);
            thread::spawn(move || {
                for ordinal in 0..1000 {
                    let mut job = producer * 1000 + ordinal;
                    loop {
                        match sender.try_send(job) {
                            Ok(()) => break,
                            Err(TrySendError::Full(returned)) => { job = returned; thread::yield_now(); }
                            Err(TrySendError::Disconnected(_)) => panic!("unexpected closed queue"),
                        }
                    }
                }
            })
        }).collect();
        for producer in producers { producer.join().unwrap(); }
        drop(sender);
        let mut received = Vec::new();
        for _ in 0..8000 { received.push(results.recv_timeout(Duration::from_secs(5)).unwrap()); }
        for worker in workers { worker.join().unwrap(); }
        received.sort_unstable();
        assert_eq!(received, (0..8000).collect::<Vec<_>>());
    }
}
