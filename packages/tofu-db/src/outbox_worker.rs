//! FIFO logical-outbox sink delivery with immediate demand acceptance.
//!
//! Callers validate and enqueue owned batches with an absolute deadline.
//! No queue slot/byte settings refuse or delay accepted work. Metrics count
//! actual queued and active residency; allocation failures stay visible.
//! Shutdown stops acceptance and drains queued work. Ambiguous/integrity sink
//! failures close the worker and fan the terminal error to every queued request.

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

const DROP_DRAIN_WAIT: Duration = Duration::from_millis(100);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LogicalOutboxWorkerConfig {
    pub publish_budget: LogicalOutboxPublishBudget,
}

impl Default for LogicalOutboxWorkerConfig {
    fn default() -> Self {
        Self {
            publish_budget: LogicalOutboxPublishBudget::default(),
        }
    }
}

impl LogicalOutboxWorkerConfig {
    fn validate(self) -> io::Result<Self> {
        LogicalOutboxPublishBudget::new(
            self.publish_budget.max_records,
            self.publish_budget.max_bytes,
        )?;
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
    let pending = std::mem::take(&mut guard.requests);
    let failed = pending.len() as u64;
    guard.queued_bytes = 0;
    guard.metrics.queued_batches = 0;
    guard.metrics.resident_bytes = guard.active_bytes;
    guard.metrics.failed_batches = guard.metrics.failed_batches.saturating_add(failed);
    drop(guard);
    shared.ready.notify_all();
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
        if Instant::now() >= deadline {
            let mut guard = state(self.shared.as_ref())?;
            guard.metrics.deadline_expirations =
                guard.metrics.deadline_expirations.saturating_add(1);
            return Err(timed_out());
        }
        let (response_sender, response_receiver) = mpsc::sync_channel(1);
        let deadline_counted = Arc::new(AtomicBool::new(false));
        let mut guard = state(self.shared.as_ref())?;
        if !guard.accepting {
            return Err(closed_error(&guard));
        }
        guard.requests.try_reserve(1).map_err(|error| {
            io::Error::other(format!("logical outbox queue allocation failed: {error}"))
        })?;
        guard.queued_bytes = guard
            .queued_bytes
            .checked_add(resident_bytes)
            .ok_or_else(|| invalid_input("logical outbox resident byte count overflow"))?;
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

    pub(super) fn pending(sequence: u64) -> Vec<PendingLogicalOutboxRecord> {
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
    fn deadline_can_expire_while_sink_finishes_for_idempotent_retry() {
        struct GatedSink {
            inner: TestSink,
            started: SyncSender<()>,
            release: Option<mpsc::Receiver<()>>,
        }
        impl LogicalOutboxSink for GatedSink {
            fn append_durable(
                &mut self,
                record: &crate::logical_outbox::SealedLogicalOutboxRecord,
            ) -> io::Result<DurableLogicalOutboxReceipt> {
                if let Some(release) = self.release.take() {
                    self.started.send(()).unwrap();
                    release.recv_timeout(Duration::from_secs(5)).unwrap();
                }
                self.inner.append_durable(record)
            }

            fn is_restart_required(&self) -> bool {
                self.inner.is_restart_required()
            }
        }
        let calls = Arc::new(AtomicUsize::new(0));
        let (started_tx, started_rx) = mpsc::sync_channel(1);
        let (release_tx, release_rx) = mpsc::sync_channel(1);
        let worker = LogicalOutboxWorker::start(
            GatedSink {
                inner: TestSink {
                    delay: Duration::ZERO,
                    calls: Arc::clone(&calls),
                    terminal_after_first: false,
                    restart_required: false,
                },
                started: started_tx,
                release: Some(release_rx),
            },
            LogicalOutboxWorkerConfig::default(),
        )
        .unwrap();
        // Establish that the sink really started before expiring the caller.
        // A 2 ms queued deadline could previously skip the sink entirely and
        // leave the test spinning forever while waiting for its call count.
        let ticket = worker
            .enqueue(pending(1), Instant::now() + Duration::from_secs(1))
            .unwrap();
        started_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(ticket.wait().unwrap_err().kind(), io::ErrorKind::TimedOut);
        release_tx.send(()).unwrap();
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
                LogicalOutboxWorkerConfig::default(),
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
    fn caller_deadline_expires_while_fifo_sink_is_busy_and_shutdown_drains() {
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
            LogicalOutboxWorkerConfig::default(),
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
    fn concrete_engine_sink_runs_only_on_the_owned_worker() {
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

#[cfg(test)]
mod demand_queue_tests {
    use super::*;
    use crate::outbox_publisher::DurableLogicalOutboxReceipt;
    struct GateSink {
        entered: Option<mpsc::Sender<()>>,
        release: mpsc::Receiver<()>,
        seen: Vec<u64>,
    }
    impl LogicalOutboxSink for GateSink {
        fn append_durable(
            &mut self,
            record: &crate::logical_outbox::SealedLogicalOutboxRecord,
        ) -> io::Result<DurableLogicalOutboxReceipt> {
            if let Some(entered) = self.entered.take() {
                entered.send(()).unwrap();
                self.release.recv().unwrap();
            }
            self.seen.push(record.identity.sequence);
            Ok(DurableLogicalOutboxReceipt {
                tenant_id: record.identity.tenant_id,
                owner_user_id: record.identity.owner_user_id,
                sequence: record.identity.sequence,
                event_id: record.event_id,
            })
        }
    }
    #[test]
    fn blocked_sink_accepts_every_ticket_without_slot_or_byte_wait_and_drains_fifo() {
        let (entered, blocked) = mpsc::channel();
        let (release, released) = mpsc::channel();
        let worker = LogicalOutboxWorker::start(
            GateSink {
                entered: Some(entered),
                release: released,
                seen: Vec::new(),
            },
            LogicalOutboxWorkerConfig {
                publish_budget: LogicalOutboxPublishBudget::new(1, 1).unwrap(),
            },
        )
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let first = worker.enqueue(super::tests::pending(1), deadline).unwrap();
        blocked.recv_timeout(Duration::from_secs(1)).unwrap();
        let started = Instant::now();
        let mut tickets = vec![first];
        for sequence in 2..=32 {
            tickets.push(
                worker
                    .enqueue(super::tests::pending(sequence), deadline)
                    .unwrap(),
            );
        }
        let enqueue_elapsed = started.elapsed();
        let snapshot = worker.metrics().unwrap();
        assert_eq!(snapshot.submitted_batches, 32);
        assert_eq!(snapshot.queued_batches, 31);
        assert!(snapshot.resident_bytes > 1);
        release.send(()).unwrap();
        for ticket in tickets {
            assert_eq!(ticket.wait().unwrap().receipts.len(), 1);
        }
        let (sink, _, metrics) = worker.shutdown_until(deadline).unwrap();
        assert_eq!(sink.seen, (1..=32).collect::<Vec<_>>());
        assert_eq!(metrics.resident_bytes, 0);
        eprintln!(
            "outbox_demand queued=31 enqueue_us={} peak_resident_bytes={} remaining_workers=0",
            enqueue_elapsed.as_micros(),
            metrics.peak_resident_bytes
        );
    }
}
