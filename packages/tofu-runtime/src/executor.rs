//! Demand native jobs, atomic publication, exact accounting and joined teardown.
//! All admission state belongs to one mutex; worker startup precedes enqueue.
//! A shutdown drains accepted work and refuses new work. No detached workers.
use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};

#[derive(Clone, Copy, Debug)]
pub struct Budget {
    pub workers: usize, // Idle reuse hint, never an active worker quota.
    pub max_inflight_jobs: usize,
    pub max_inflight_bytes: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdmissionError {
    InvalidBudget,
    Closed,
    Capacity, // Actual allocation/accounting overflow only.
    WorkerStart,
    StatePoisoned,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskError {
    Panicked,
    WorkerUnavailable,
}

pub struct TaskHandle<T> {
    result: mpsc::Receiver<Result<T, TaskError>>,
}
impl<T> TaskHandle<T> {
    pub fn wait(self) -> Result<T, TaskError> {
        self.result
            .recv()
            .unwrap_or(Err(TaskError::WorkerUnavailable))
    }
}

type Job = Box<dyn FnOnce() + Send + 'static>;
struct QueuedJob {
    run: Job,
}
struct State {
    queue: VecDeque<QueuedJob>,
    closed: bool,
    inflight_jobs: usize,
    inflight_bytes: usize,
    completed_jobs: u64,
    live_workers: usize,
    idle_reuse_workers: usize,
}
struct Shared {
    state: Mutex<State>,
    wake: Condvar,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Snapshot {
    pub inflight_jobs: usize,
    pub inflight_bytes: usize,
    pub queued_jobs: usize,
    pub completed_jobs: u64,
    pub closed: bool,
}

pub struct Executor {
    shared: Arc<Shared>,
    threads: Vec<JoinHandle<()>>,
    worker_start: fn(String, Arc<Shared>) -> std::io::Result<JoinHandle<()>>,
    capacity_lease: Option<crate::capacity_allocation::Lease>,
}
impl Executor {
    pub fn with_capacity(
        allocator: &Arc<crate::capacity_allocation::Allocator>,
        demand: crate::capacity::Demand,
    ) -> Result<Self, crate::capacity::Error> {
        let lease = allocator.reserve(demand)?;
        let mut executor =
            Self::new(lease.budget()).map_err(|_| crate::capacity::Error::InvalidDemand)?;
        executor.capacity_lease = Some(lease);
        Ok(executor)
    }
    pub fn new(budget: Budget) -> Result<Self, AdmissionError> {
        if budget.workers == 0 {
            return Err(AdmissionError::InvalidBudget);
        }
        Ok(Self {
            shared: Arc::new(Shared {
                state: Mutex::new(State {
                    queue: VecDeque::new(),
                    closed: false,
                    inflight_jobs: 0,
                    inflight_bytes: 0,
                    completed_jobs: 0,
                    live_workers: 0,
                    idle_reuse_workers: budget.workers,
                }),
                wake: Condvar::new(),
            }),
            threads: Vec::new(),
            worker_start: start_worker,
            capacity_lease: None,
        })
    }

    pub fn snapshot(&self) -> Result<Snapshot, AdmissionError> {
        let state = self
            .shared
            .state
            .lock()
            .map_err(|_| AdmissionError::StatePoisoned)?;
        Ok(Snapshot {
            inflight_jobs: state.inflight_jobs,
            inflight_bytes: state.inflight_bytes,
            queued_jobs: state.queue.len(),
            completed_jobs: state.completed_jobs,
            closed: state.closed,
        })
    }

    pub fn resident_workers(&self) -> usize {
        self.shared
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .live_workers
    }

    pub fn submit<F, T>(
        &mut self,
        weight: usize,
        function: F,
    ) -> Result<TaskHandle<T>, AdmissionError>
    where
        F: FnOnce() -> T + Send + 'static,
        T: Send + 'static,
    {
        let mut index = 0;
        while index < self.threads.len() {
            if self.threads[index].is_finished() {
                let _ = self.threads.swap_remove(index).join();
            } else {
                index += 1;
            }
        }
        let (sender, result) = mpsc::channel();
        let mut state = self
            .shared
            .state
            .lock()
            .map_err(|_| AdmissionError::StatePoisoned)?;
        if state.closed {
            return Err(AdmissionError::Closed);
        }
        let next_jobs = state
            .inflight_jobs
            .checked_add(1)
            .ok_or(AdmissionError::Capacity)?;
        let next_bytes = state
            .inflight_bytes
            .checked_add(weight)
            .ok_or(AdmissionError::Capacity)?;
        // Start before publishing this job. A partial startup failure leaves
        // only idle owned workers; the rejected closure can never run later.
        let needed = next_jobs;
        while state.live_workers < needed {
            let shared = Arc::clone(&self.shared);
            let worker = (self.worker_start)(format!("tofu-native-{}", self.threads.len()), shared)
                .map_err(|_| AdmissionError::WorkerStart)?;
            self.threads.push(worker);
            state.live_workers += 1;
        }
        state
            .queue
            .try_reserve(1)
            .map_err(|_| AdmissionError::Capacity)?;
        let shared = Arc::clone(&self.shared);
        let run = Box::new(move || {
            let outcome = catch_unwind(AssertUnwindSafe(function)).map_err(|_| TaskError::Panicked);
            // Release admission before publishing the terminal receipt.
            let mut state = match shared.state.lock() {
                Ok(state) => state,
                Err(poison) => poison.into_inner(),
            };
            state.inflight_jobs -= 1;
            state.inflight_bytes -= weight;
            state.completed_jobs += 1;
            drop(state);
            let _ = sender.send(outcome); // A dropped receiver does not cancel accepted work.
        });
        state.inflight_jobs = next_jobs;
        state.inflight_bytes = next_bytes;
        state.queue.push_back(QueuedJob { run });
        drop(state);
        self.shared.wake.notify_one();
        Ok(TaskHandle { result })
    }

    pub fn close(&mut self) {
        {
            let mut state = match self.shared.state.lock() {
                Ok(state) => state,
                Err(poison) => poison.into_inner(),
            };
            state.closed = true;
        }
        self.shared.wake.notify_all();
        for worker in self.threads.drain(..) {
            // Job panics are captured; a worker panic still cannot be detached.
            let _ = worker.join();
        }
        drop(self.capacity_lease.take());
    }
}
impl Drop for Executor {
    fn drop(&mut self) {
        self.close();
    }
}

fn start_worker(name: String, shared: Arc<Shared>) -> std::io::Result<JoinHandle<()>> {
    thread::Builder::new()
        .name(name)
        .spawn(move || worker_loop(shared))
}

fn worker_loop(shared: Arc<Shared>) {
    loop {
        let job = {
            let mut state = match shared.state.lock() {
                Ok(state) => state,
                Err(poison) => poison.into_inner(),
            };
            while state.queue.is_empty() && !state.closed {
                if state.live_workers > state.idle_reuse_workers {
                    state.live_workers -= 1;
                    return;
                }
                state = match shared.wake.wait(state) {
                    Ok(state) => state,
                    Err(poison) => poison.into_inner(),
                };
            }
            match state.queue.pop_front() {
                Some(job) => job,
                None => {
                    state.live_workers -= 1;
                    return;
                }
            }
        };
        (job.run)();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn failed_worker_start_never_publishes_rejected_job() {
        let mut executor = Executor::new(Budget {
            workers: 2,
            max_inflight_jobs: 2,
            max_inflight_bytes: 16,
        })
        .unwrap();
        let (release, wait) = mpsc::channel();
        let first = executor.submit(8, move || wait.recv().unwrap()).unwrap();
        executor.worker_start = |_, _| Err(std::io::Error::other("owned startup failure"));
        let runs = Arc::new(AtomicUsize::new(0));
        let captured = Arc::clone(&runs);
        assert!(matches!(
            executor.submit(8, move || captured.fetch_add(1, Ordering::SeqCst)),
            Err(AdmissionError::WorkerStart)
        ));
        assert_eq!(executor.snapshot().unwrap().inflight_jobs, 1);
        release.send(()).unwrap();
        assert_eq!(first.wait(), Ok(()));
        executor.close();
        assert_eq!(runs.load(Ordering::SeqCst), 0);
        assert_eq!(executor.snapshot().unwrap().completed_jobs, 1);
    }
}
