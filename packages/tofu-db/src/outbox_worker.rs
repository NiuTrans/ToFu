//! Single bounded worker for logical-outbox sink I/O.
//!
//! Callers validate and enqueue owned publisher batches with an absolute
//! deadline. Queue admission counts both queued and in-flight resident bytes;
//! sink work therefore cannot exceed the configured memory budget when the
//! worker releases the foreground authority. Shutdown stops admission and
//! drains accepted work. An ambiguous or integrity-class sink failure closes
//! the worker and fans the same terminal error out to every queued request.

use std::collections::VecDeque;
use std::io;
use std::panic::{self, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::Duration;
use std::time::Instant;

use crate::authority::PendingLogicalOutboxRecord;
use crate::outbox_publisher::{
    validate_batch, LogicalOutboxPublishBudget, LogicalOutboxPublishResult, LogicalOutboxPublisher,
    LogicalOutboxPublisherMetrics, LogicalOutboxSink,
};

pub const MIN_OUTBOX_WORKER_QUEUE_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_OUTBOX_WORKER_QUEUE_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_OUTBOX_WORKER_QUEUE_BATCHES: usize = 4;
const TARGET_RESIDENT_BYTES_PER_BATCH: usize = 16 * 1024 * 1024;
const DROP_DRAIN_WAIT: Duration = Duration::from_millis(100);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LogicalOutboxWorkerConfig {
    pub queue_batches: usize,
    pub queue_bytes: usize,
    pub publish_budget: LogicalOutboxPublishBudget,
}

impl Default for LogicalOutboxWorkerConfig {
    fn default() -> Self {
        Self::lean_fallback()
    }
}

impl LogicalOutboxWorkerConfig {
    pub const fn lean_fallback() -> Self {
        Self {
            queue_batches: 1,
            queue_bytes: MIN_OUTBOX_WORKER_QUEUE_BYTES,
            publish_budget: LogicalOutboxPublishBudget {
                max_records: 16,
                max_bytes: 8 * 1024 * 1024,
            },
        }
    }

    pub fn from_memory_headroom(memory_headroom_bytes: u64) -> Self {
        if memory_headroom_bytes == 0 {
            return Self::lean_fallback();
        }
        let queue_bytes = usize::try_from(memory_headroom_bytes / 256)
            .unwrap_or(usize::MAX)
            .clamp(MIN_OUTBOX_WORKER_QUEUE_BYTES, MAX_OUTBOX_WORKER_QUEUE_BYTES);
        let queue_batches = (queue_bytes / TARGET_RESIDENT_BYTES_PER_BATCH)
            .clamp(1, MAX_OUTBOX_WORKER_QUEUE_BATCHES);
        Self {
            queue_batches,
            queue_bytes,
            publish_budget: LogicalOutboxPublishBudget::default(),
        }
    }

    fn validate(self) -> io::Result<Self> {
        LogicalOutboxPublishBudget::new(
            self.publish_budget.max_records,
            self.publish_budget.max_bytes,
        )?;
        if self.queue_batches == 0 || self.queue_batches > MAX_OUTBOX_WORKER_QUEUE_BATCHES {
            return Err(invalid_input(
                "outbox worker queue batch bound must be between 1 and 4",
            ));
        }
        if self.queue_bytes < MIN_OUTBOX_WORKER_QUEUE_BYTES
            || self.queue_bytes > MAX_OUTBOX_WORKER_QUEUE_BYTES
        {
            return Err(invalid_input(
                "outbox worker queue byte bound must be between 16 and 64 MiB",
            ));
        }
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LogicalOutboxWorkerMetrics {
    pub submitted_batches: u64,
    pub completed_batches: u64,
    pub failed_batches: u64,
    pub skipped_expired_batches: u64,
    pub deadline_expirations: u64,
    pub queued_batches: usize,
    pub active_batches: usize,
    pub resident_bytes: usize,
    pub peak_resident_bytes: usize,
    pub terminal: bool,
}

#[derive(Clone, Debug)]
struct ErrorSnapshot {
    kind: io::ErrorKind,
    message: String,
}

impl ErrorSnapshot {
    fn capture(error: &io::Error) -> Self {
        Self {
            kind: error.kind(),
            message: error.to_string(),
        }
    }

    fn into_error(self) -> io::Error {
        io::Error::new(self.kind, self.message)
    }
}

type PublishResponse = Result<LogicalOutboxPublishResult, ErrorSnapshot>;

struct Request {
    pending: Vec<PendingLogicalOutboxRecord>,
    resident_bytes: usize,
    deadline: Instant,
    deadline_counted: Arc<AtomicBool>,
    response: SyncSender<PublishResponse>,
}

struct QueueState {
    requests: VecDeque<Request>,
    queued_bytes: usize,
    active_bytes: usize,
    active_batches: usize,
    accepting: bool,
    terminal_error: Option<ErrorSnapshot>,
    metrics: LogicalOutboxWorkerMetrics,
}

struct Shared {
    config: LogicalOutboxWorkerConfig,
    state: Mutex<QueueState>,
    ready: Condvar,
    space: Condvar,
}

pub struct LogicalOutboxWorker<S: LogicalOutboxSink + 'static> {
    shared: Arc<Shared>,
    worker: Mutex<Option<JoinHandle<LogicalOutboxPublisher<S>>>>,
    finished: Mutex<Option<mpsc::Receiver<()>>>,
}

pub struct LogicalOutboxPublishTicket {
    shared: Arc<Shared>,
    response: mpsc::Receiver<PublishResponse>,
    deadline: Instant,
    deadline_counted: Arc<AtomicBool>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn timed_out() -> io::Error {
    io::Error::new(
        io::ErrorKind::TimedOut,
        "logical outbox publish deadline expired",
    )
}

fn state(shared: &Shared) -> io::Result<MutexGuard<'_, QueueState>> {
    shared
        .state
        .lock()
        .map_err(|_| io::Error::other("logical outbox worker lock is poisoned"))
}

fn closed_error(state: &QueueState) -> io::Error {
    state.terminal_error.clone().map_or_else(
        || io::Error::new(io::ErrorKind::BrokenPipe, "logical outbox worker is closed"),
        ErrorSnapshot::into_error,
    )
}

fn batch_resident_bytes(pending: &Vec<PendingLogicalOutboxRecord>) -> io::Result<usize> {
    let mut bytes = std::mem::size_of::<Vec<PendingLogicalOutboxRecord>>()
        .checked_add(
            pending
                .capacity()
                .checked_mul(std::mem::size_of::<PendingLogicalOutboxRecord>())
                .ok_or_else(|| invalid_input("outbox worker resident byte count overflow"))?,
        )
        .ok_or_else(|| invalid_input("outbox worker resident byte count overflow"))?;
    for pending_record in pending {
        let identity = &pending_record.record.identity;
        for capacity in [
            identity.operation.capacity(),
            identity.request_id.capacity(),
            identity.command_id.as_ref().map_or(0, String::capacity),
            pending_record.record.ciphertext.capacity(),
        ] {
            bytes = bytes
                .checked_add(capacity)
                .ok_or_else(|| invalid_input("outbox worker resident byte count overflow"))?;
        }
    }
    Ok(bytes)
}

fn mark_deadline(shared: &Shared, counted: &AtomicBool) {
    if !counted.swap(true, Ordering::AcqRel) {
        let mut guard = shared
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        guard.metrics.deadline_expirations = guard.metrics.deadline_expirations.saturating_add(1);
    }
}

fn pop_request(shared: &Shared) -> io::Result<Option<Request>> {
    let mut guard = state(shared)?;
    loop {
        if let Some(request) = guard.requests.pop_front() {
            guard.queued_bytes -= request.resident_bytes;
            guard.active_bytes += request.resident_bytes;
            guard.active_batches += 1;
            guard.metrics.queued_batches = guard.requests.len();
            guard.metrics.active_batches = guard.active_batches;
            return Ok(Some(request));
        }
        if !guard.accepting {
            return Ok(None);
        }
        guard = shared
            .ready
            .wait(guard)
            .map_err(|_| io::Error::other("logical outbox worker lock is poisoned"))?;
    }
}

fn finish_request(shared: &Shared, request: &Request, outcome: &PublishResponse, skipped: bool) {
    let mut guard = shared
        .state
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    guard.active_bytes = guard.active_bytes.saturating_sub(request.resident_bytes);
    guard.active_batches = guard.active_batches.saturating_sub(1);
    guard.metrics.active_batches = guard.active_batches;
    guard.metrics.resident_bytes = guard.queued_bytes + guard.active_bytes;
    if skipped {
        guard.metrics.skipped_expired_batches =
            guard.metrics.skipped_expired_batches.saturating_add(1);
    } else if outcome.is_ok() {
        guard.metrics.completed_batches = guard.metrics.completed_batches.saturating_add(1);
    } else {
        guard.metrics.failed_batches = guard.metrics.failed_batches.saturating_add(1);
    }
    drop(guard);
    shared.space.notify_all();
}

fn is_terminal_sink_error<S: LogicalOutboxSink>(
    publisher: &LogicalOutboxPublisher<S>,
    error: &ErrorSnapshot,
) -> bool {
    publisher.sink_restart_required()
        || matches!(
            error.kind,
            io::ErrorKind::InvalidData
                | io::ErrorKind::PermissionDenied
                | io::ErrorKind::BrokenPipe
                | io::ErrorKind::NotFound
        )
}

fn close_terminal(shared: &Shared, terminal_error: ErrorSnapshot) {
    let mut guard = shared
        .state
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    guard.accepting = false;
    if guard.terminal_error.is_none() {
        guard.terminal_error = Some(terminal_error.clone());
    }
    guard.metrics.terminal = true;
    let pending = guard.requests.drain(..).collect::<Vec<_>>();
    let failed = pending.len() as u64;
    guard.queued_bytes = 0;
    guard.metrics.queued_batches = 0;
    guard.metrics.resident_bytes = guard.active_bytes;
    guard.metrics.failed_batches = guard.metrics.failed_batches.saturating_add(failed);
    drop(guard);
    shared.ready.notify_all();
    shared.space.notify_all();
    for request in pending {
        let _ = request.response.send(Err(terminal_error.clone()));
    }
}

fn run_worker<S: LogicalOutboxSink>(
    publisher: &mut LogicalOutboxPublisher<S>,
    shared: &Shared,
) -> io::Result<()> {
    while let Some(request) = pop_request(shared)? {
        if Instant::now() >= request.deadline {
            mark_deadline(shared, request.deadline_counted.as_ref());
            let outcome = Err(ErrorSnapshot::capture(&timed_out()));
            finish_request(shared, &request, &outcome, true);
            let _ = request.response.send(outcome);
            continue;
        }
        let outcome = publisher
            .publish(&request.pending)
            .map_err(|error| ErrorSnapshot::capture(&error));
        let terminal = outcome
            .as_ref()
            .err()
            .is_some_and(|error| is_terminal_sink_error(publisher, error));
        finish_request(shared, &request, &outcome, false);
        let _ = request.response.send(outcome.clone());
        if terminal {
            close_terminal(shared, outcome.unwrap_err());
            return Ok(());
        }
    }
    Ok(())
}

impl LogicalOutboxPublishTicket {
    pub fn wait(self) -> io::Result<LogicalOutboxPublishResult> {
        let remaining = self.deadline.saturating_duration_since(Instant::now());
        match self.response.recv_timeout(remaining) {
            Ok(response) => response.map_err(ErrorSnapshot::into_error),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                mark_deadline(self.shared.as_ref(), self.deadline_counted.as_ref());
                Err(timed_out())
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(self
                .shared
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .terminal_error
                .clone()
                .map_or_else(
                    || io::Error::new(io::ErrorKind::BrokenPipe, "outbox worker exited"),
                    ErrorSnapshot::into_error,
                )),
        }
    }
}

impl<S: LogicalOutboxSink + 'static> LogicalOutboxWorker<S> {
    pub fn start(sink: S, config: LogicalOutboxWorkerConfig) -> io::Result<Self> {
        let config = config.validate()?;
        let publisher = LogicalOutboxPublisher::new(sink, config.publish_budget)?;
        let shared = Arc::new(Shared {
            config,
            state: Mutex::new(QueueState {
                requests: VecDeque::new(),
                queued_bytes: 0,
                active_bytes: 0,
                active_batches: 0,
                accepting: true,
                terminal_error: None,
                metrics: LogicalOutboxWorkerMetrics::default(),
            }),
            ready: Condvar::new(),
            space: Condvar::new(),
        });
        let worker_shared = Arc::clone(&shared);
        let (finished_sender, finished_receiver) = mpsc::sync_channel(1);
        let worker = thread::Builder::new()
            .name("tofu-db-outbox-worker".to_owned())
            .spawn(move || {
                let mut publisher = publisher;
                let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
                    run_worker(&mut publisher, worker_shared.as_ref())
                }));
                match outcome {
                    Ok(Ok(())) => {}
                    Ok(Err(error)) => {
                        close_terminal(worker_shared.as_ref(), ErrorSnapshot::capture(&error));
                    }
                    Err(_) => close_terminal(
                        worker_shared.as_ref(),
                        ErrorSnapshot {
                            kind: io::ErrorKind::Other,
                            message: "logical outbox worker panicked".to_owned(),
                        },
                    ),
                }
                let _ = finished_sender.send(());
                publisher
            })?;
        Ok(Self {
            shared,
            worker: Mutex::new(Some(worker)),
            finished: Mutex::new(Some(finished_receiver)),
        })
    }

    pub fn enqueue(
        &self,
        pending: Vec<PendingLogicalOutboxRecord>,
        deadline: Instant,
    ) -> io::Result<LogicalOutboxPublishTicket> {
        validate_batch(&pending, self.shared.config.publish_budget)?;
        let resident_bytes = batch_resident_bytes(&pending)?;
        if resident_bytes > self.shared.config.queue_bytes {
            return Err(invalid_input(
                "logical outbox batch exceeds worker resident byte budget",
            ));
        }
        if Instant::now() >= deadline {
            let mut guard = state(self.shared.as_ref())?;
            guard.metrics.deadline_expirations =
                guard.metrics.deadline_expirations.saturating_add(1);
            return Err(timed_out());
        }
        let (response_sender, response_receiver) = mpsc::sync_channel(1);
        let deadline_counted = Arc::new(AtomicBool::new(false));
        let mut guard = state(self.shared.as_ref())?;
        loop {
            if !guard.accepting {
                return Err(closed_error(&guard));
            }
            let resident_fits = guard
                .queued_bytes
                .checked_add(guard.active_bytes)
                .and_then(|bytes| bytes.checked_add(resident_bytes))
                .is_some_and(|bytes| bytes <= self.shared.config.queue_bytes);
            if guard.requests.len() + guard.active_batches < self.shared.config.queue_batches
                && resident_fits
            {
                break;
            }
            let now = Instant::now();
            if now >= deadline {
                drop(guard);
                mark_deadline(self.shared.as_ref(), deadline_counted.as_ref());
                return Err(timed_out());
            }
            let (next_guard, timeout) = self
                .shared
                .space
                .wait_timeout(guard, deadline.saturating_duration_since(now))
                .map_err(|_| io::Error::other("logical outbox worker lock is poisoned"))?;
            guard = next_guard;
            if timeout.timed_out() {
                drop(guard);
                mark_deadline(self.shared.as_ref(), deadline_counted.as_ref());
                return Err(timed_out());
            }
        }
        guard.queued_bytes += resident_bytes;
        guard.requests.push_back(Request {
            pending,
            resident_bytes,
            deadline,
            deadline_counted: Arc::clone(&deadline_counted),
            response: response_sender,
        });
        guard.metrics.submitted_batches = guard.metrics.submitted_batches.saturating_add(1);
        guard.metrics.queued_batches = guard.requests.len();
        guard.metrics.resident_bytes = guard.queued_bytes + guard.active_bytes;
        guard.metrics.peak_resident_bytes = guard
            .metrics
            .peak_resident_bytes
            .max(guard.metrics.resident_bytes);
        drop(guard);
        self.shared.ready.notify_one();
        Ok(LogicalOutboxPublishTicket {
            shared: Arc::clone(&self.shared),
            response: response_receiver,
            deadline,
            deadline_counted,
        })
    }

    pub fn submit(
        &self,
        pending: Vec<PendingLogicalOutboxRecord>,
        deadline: Instant,
    ) -> io::Result<LogicalOutboxPublishResult> {
        self.enqueue(pending, deadline)?.wait()
    }

    pub fn metrics(&self) -> io::Result<LogicalOutboxWorkerMetrics> {
        Ok(state(self.shared.as_ref())?.metrics)
    }

    pub fn publish_budget(&self) -> LogicalOutboxPublishBudget {
        self.shared.config.publish_budget
    }

    fn stop_accepting(&self) {
        let mut guard = self
            .shared
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        guard.accepting = false;
        drop(guard);
        self.shared.ready.notify_all();
        self.shared.space.notify_all();
    }

    pub fn shutdown_until(
        mut self,
        deadline: Instant,
    ) -> io::Result<(S, LogicalOutboxPublisherMetrics, LogicalOutboxWorkerMetrics)> {
        self.stop_accepting();
        let finished = self
            .finished
            .get_mut()
            .unwrap_or_else(|error| error.into_inner())
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "outbox worker is stopped"))?;
        let remaining = deadline.saturating_duration_since(Instant::now());
        if matches!(
            finished.recv_timeout(remaining),
            Err(mpsc::RecvTimeoutError::Timeout)
        ) {
            self.worker
                .get_mut()
                .unwrap_or_else(|error| error.into_inner())
                .take();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "logical outbox worker shutdown deadline expired",
            ));
        }
        let worker = self
            .worker
            .get_mut()
            .unwrap_or_else(|error| error.into_inner())
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "outbox worker is stopped"))?;
        let publisher = worker
            .join()
            .map_err(|_| io::Error::other("logical outbox worker join failed"))?;
        let worker_metrics = self.metrics()?;
        let publisher_metrics = publisher.metrics();
        Ok((publisher.into_sink(), publisher_metrics, worker_metrics))
    }
}

impl<S: LogicalOutboxSink + 'static> Drop for LogicalOutboxWorker<S> {
    fn drop(&mut self) {
        self.stop_accepting();
        let finished = self
            .finished
            .get_mut()
            .unwrap_or_else(|error| error.into_inner())
            .take();
        let worker = self
            .worker
            .get_mut()
            .unwrap_or_else(|error| error.into_inner());
        if let (Some(finished), Some(worker)) = (finished, worker.take()) {
            if finished.recv_timeout(DROP_DRAIN_WAIT).is_ok() {
                let _ = worker.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    use super::*;
    use crate::logical_outbox::{LogicalOutboxCipher, LogicalOutboxIdentity};
    use crate::outbox_publisher::DurableLogicalOutboxReceipt;
    use crate::outbox_sink::EngineLogicalOutboxSink;

    struct TestSink {
        delay: Duration,
        calls: Arc<AtomicUsize>,
        terminal_after_first: bool,
        restart_required: bool,
    }

    impl LogicalOutboxSink for TestSink {
        fn append_durable(
            &mut self,
            record: &crate::logical_outbox::SealedLogicalOutboxRecord,
        ) -> io::Result<DurableLogicalOutboxReceipt> {
            assert_eq!(thread::current().name(), Some("tofu-db-outbox-worker"));
            thread::sleep(self.delay);
            let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
            if self.terminal_after_first && call == 1 {
                self.restart_required = true;
                return Err(io::Error::other("ambiguous sink append"));
            }
            Ok(DurableLogicalOutboxReceipt {
                tenant_id: record.identity.tenant_id,
                owner_user_id: record.identity.owner_user_id,
                sequence: record.identity.sequence,
                event_id: record.event_id,
            })
        }

        fn is_restart_required(&self) -> bool {
            self.restart_required
        }
    }

    fn pending(sequence: u64) -> Vec<PendingLogicalOutboxRecord> {
        let record = LogicalOutboxCipher::new(&[61; 32])
            .seal(
                LogicalOutboxIdentity {
                    sequence,
                    tenant_id: 7,
                    owner_user_id: 11,
                    schema_version: 57,
                    registry_version: 37,
                    operation: "artifact.create".to_owned(),
                    request_id: format!("request-{sequence}"),
                    request_digest: [sequence as u8; 32],
                    command_id: Some(format!("command-{sequence}")),
                    committed_at_ms: 6_000 + sequence,
                },
                b"transaction IR",
            )
            .unwrap();
        vec![PendingLogicalOutboxRecord {
            record_bytes: record.encoded_len().unwrap() as u64,
            record,
        }]
    }

    #[test]
    fn queue_budget_derives_from_headroom_and_clamps_extremes() {
        assert_eq!(
            LogicalOutboxWorkerConfig::from_memory_headroom(0),
            LogicalOutboxWorkerConfig::lean_fallback()
        );
        assert_eq!(
            LogicalOutboxWorkerConfig::from_memory_headroom(u64::MAX).queue_bytes,
            MAX_OUTBOX_WORKER_QUEUE_BYTES
        );
        assert_eq!(
            LogicalOutboxWorkerConfig::from_memory_headroom(u64::MAX).queue_batches,
            MAX_OUTBOX_WORKER_QUEUE_BATCHES
        );
    }

    #[test]
    fn deadline_can_expire_while_sink_finishes_for_idempotent_retry() {
        let calls = Arc::new(AtomicUsize::new(0));
        let worker = LogicalOutboxWorker::start(
            TestSink {
                delay: Duration::from_millis(30),
                calls: Arc::clone(&calls),
                terminal_after_first: false,
                restart_required: false,
            },
            LogicalOutboxWorkerConfig::default(),
        )
        .unwrap();
        assert_eq!(
            worker
                .submit(pending(1), Instant::now() + Duration::from_millis(2))
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
        while calls.load(Ordering::SeqCst) == 0 {
            thread::yield_now();
        }
        let result = worker
            .submit(pending(1), Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(result.receipts.len(), 1);
        let (_sink, publisher_metrics, metrics) = worker
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(metrics.submitted_batches, 2);
        assert_eq!(metrics.completed_batches, 2);
        assert_eq!(metrics.deadline_expirations, 1);
        assert_eq!(metrics.resident_bytes, 0);
        assert_eq!(publisher_metrics.successful_batches, 2);
    }

    #[test]
    fn terminal_sink_failure_fans_out_and_closes_admission() {
        let calls = Arc::new(AtomicUsize::new(0));
        let worker = Arc::new(
            LogicalOutboxWorker::start(
                TestSink {
                    delay: Duration::from_millis(20),
                    calls,
                    terminal_after_first: true,
                    restart_required: false,
                },
                LogicalOutboxWorkerConfig {
                    queue_batches: 2,
                    queue_bytes: MIN_OUTBOX_WORKER_QUEUE_BYTES,
                    ..LogicalOutboxWorkerConfig::default()
                },
            )
            .unwrap(),
        );
        let barrier = Arc::new(std::sync::Barrier::new(3));
        let mut submitters = Vec::new();
        for sequence in 1..=2 {
            let worker = Arc::clone(&worker);
            let barrier = Arc::clone(&barrier);
            submitters.push(thread::spawn(move || {
                barrier.wait();
                worker.submit(pending(sequence), Instant::now() + Duration::from_secs(1))
            }));
        }
        barrier.wait();
        assert!(submitters
            .into_iter()
            .all(|submitter| submitter.join().unwrap().is_err()));
        assert!(worker
            .submit(pending(3), Instant::now() + Duration::from_secs(1))
            .is_err());
        let metrics = worker.metrics().unwrap();
        assert!(metrics.terminal);
        assert_eq!(metrics.resident_bytes, 0);
    }

    #[test]
    fn active_batch_counts_against_queue_admission_and_shutdown_drains() {
        let calls = Arc::new(AtomicUsize::new(0));
        let worker = Arc::new(
            LogicalOutboxWorker::start(
                TestSink {
                    delay: Duration::from_millis(40),
                    calls: Arc::clone(&calls),
                    terminal_after_first: false,
                    restart_required: false,
                },
                LogicalOutboxWorkerConfig::default(),
            )
            .unwrap(),
        );
        let first_worker = Arc::clone(&worker);
        let first = thread::spawn(move || {
            first_worker.submit(pending(1), Instant::now() + Duration::from_secs(1))
        });
        while worker.metrics().unwrap().active_batches == 0 {
            thread::yield_now();
        }
        assert_eq!(
            worker
                .submit(pending(2), Instant::now() + Duration::from_millis(2))
                .unwrap_err()
                .kind(),
            io::ErrorKind::TimedOut
        );
        assert!(first.join().unwrap().is_ok());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        let worker = Arc::try_unwrap(worker).ok().unwrap();
        let (_sink, _, metrics) = worker
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(metrics.completed_batches, 1);
        assert_eq!(metrics.deadline_expirations, 1);
        assert!(metrics.peak_resident_bytes > 0);
        assert!(metrics.peak_resident_bytes <= MIN_OUTBOX_WORKER_QUEUE_BYTES);
        assert_eq!(metrics.resident_bytes, 0);
    }

    #[test]
    fn stop_admission_drains_already_queued_batches() {
        let calls = Arc::new(AtomicUsize::new(0));
        let worker = LogicalOutboxWorker::start(
            TestSink {
                delay: Duration::from_millis(20),
                calls: Arc::clone(&calls),
                terminal_after_first: false,
                restart_required: false,
            },
            LogicalOutboxWorkerConfig {
                queue_batches: 2,
                queue_bytes: MIN_OUTBOX_WORKER_QUEUE_BYTES,
                ..LogicalOutboxWorkerConfig::default()
            },
        )
        .unwrap();
        let first = worker
            .enqueue(pending(1), Instant::now() + Duration::from_secs(1))
            .unwrap();
        let second = worker
            .enqueue(pending(2), Instant::now() + Duration::from_secs(1))
            .unwrap();
        worker.stop_accepting();
        assert!(worker
            .enqueue(pending(3), Instant::now() + Duration::from_secs(1))
            .is_err());
        assert!(first.wait().is_ok());
        assert!(second.wait().is_ok());
        let (_sink, _, metrics) = worker
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert_eq!(metrics.completed_batches, 2);
        assert_eq!(metrics.resident_bytes, 0);
    }

    #[test]
    fn shutdown_deadline_never_waits_for_a_stuck_sink() {
        let calls = Arc::new(AtomicUsize::new(0));
        let worker = LogicalOutboxWorker::start(
            TestSink {
                delay: Duration::from_millis(200),
                calls: Arc::clone(&calls),
                terminal_after_first: false,
                restart_required: false,
            },
            LogicalOutboxWorkerConfig::default(),
        )
        .unwrap();
        let _ticket = worker
            .enqueue(pending(1), Instant::now() + Duration::from_secs(1))
            .unwrap();
        while worker.metrics().unwrap().active_batches == 0 {
            thread::yield_now();
        }
        let started = Instant::now();
        let error = match worker.shutdown_until(Instant::now() + Duration::from_millis(2)) {
            Ok(_) => panic!("shutdown ignored its deadline"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert!(started.elapsed() < Duration::from_millis(100));
        while calls.load(Ordering::SeqCst) == 0 {
            thread::yield_now();
        }
    }

    #[test]
    fn concrete_engine_sink_runs_only_on_the_bounded_worker() {
        let directory = tempfile::tempdir().unwrap();
        let sink_path = directory.path().join("sink");
        let sink = EngineLogicalOutboxSink::initialize(&sink_path, 7, 11, 1024 * 1024).unwrap();
        let worker =
            LogicalOutboxWorker::start(sink, LogicalOutboxWorkerConfig::default()).unwrap();
        let result = worker
            .submit(pending(1), Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(result.receipts[0].sequence, 1);
        let (sink, publisher_metrics, worker_metrics) = worker
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(sink.status().durable_sequence, 1);
        assert_eq!(publisher_metrics.published_records, 1);
        assert_eq!(worker_metrics.completed_batches, 1);
    }
}
