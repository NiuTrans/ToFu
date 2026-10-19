//! Demand-accepted single-writer commits with sub-millisecond durability groups.
//! Enqueue owns each prepared transaction without a slot or residency quota;
//! completion waits for its durable result. WAL group bounds split accepted work.
//! Real allocation failures and ambiguous writes keep their error boundaries.

use std::collections::VecDeque;
use std::io;
use std::panic::{self, AssertUnwindSafe};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::engine::{BatchTransaction, CommitResult, Engine, PreparedBatchTransaction};
use crate::wal::{MAX_GROUP_ENCODED_BYTES, MAX_GROUP_TRANSACTIONS};

pub const MAX_BATCH_DELAY: Duration = Duration::from_millis(1);

/// Once this many requests already wait in the group, the coalescing window
/// closes immediately instead of stretching toward the batch delay: the
/// group is already worth a barrier, and queued writers pay the residual
/// window for no grouping benefit. Shallow groups keep the sub-1 ms
/// coalescing wait.
const IMMEDIATE_FILL_DEPTH: usize = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SequencerConfig {
    pub batch_delay: Duration,
}

impl Default for SequencerConfig {
    fn default() -> Self {
        Self {
            batch_delay: MAX_BATCH_DELAY,
        }
    }
}

impl SequencerConfig {
    fn validate(self) -> io::Result<Self> {
        if self.batch_delay.is_zero() || self.batch_delay > MAX_BATCH_DELAY {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "sequencer batch delay must be positive and at most 1 ms",
            ));
        }
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct SequencerMetrics {
    pub submitted_transactions: u64,
    pub committed_transactions: u64,
    pub failed_transactions: u64,
    pub durability_groups: u64,
    pub queued_transactions: usize,
    pub queued_bytes: usize,
    pub peak_queued_transactions: usize,
    pub peak_queued_bytes: usize,
    pub largest_group_transactions: usize,
    pub largest_group_logical_bytes: usize,
    pub largest_group_wal_bytes: usize,
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

type Response = Result<CommitResult, ErrorSnapshot>;

struct Request {
    transaction: PreparedBatchTransaction,
    response: SyncSender<Response>,
}

struct QueueState {
    requests: VecDeque<Request>,
    queued_bytes: usize,
    closed: bool,
    terminal_error: Option<ErrorSnapshot>,
    metrics: SequencerMetrics,
}

struct Shared {
    config: SequencerConfig,
    state: Mutex<QueueState>,
    ready: Condvar,
    /// Submitters between `submit` entry and queue push. The worker consults
    /// this before waiting for batch-mates: with no submission in flight the
    /// batch delay would be pure latency for a sequential client, so the
    /// group commits immediately. A racy under-count only yields a smaller
    /// group, never weaker durability.
    in_flight_submissions: AtomicUsize,
}

struct InFlightSubmission<'a> {
    shared: &'a Shared,
}

impl Drop for InFlightSubmission<'_> {
    fn drop(&mut self) {
        self.shared
            .in_flight_submissions
            .fetch_sub(1, Ordering::AcqRel);
    }
}

fn state(shared: &Shared) -> io::Result<MutexGuard<'_, QueueState>> {
    shared
        .state
        .lock()
        .map_err(|_| io::Error::other("sequencer queue lock is poisoned"))
}

fn closed_error(state: &QueueState) -> io::Error {
    state.terminal_error.clone().map_or_else(
        || io::Error::new(io::ErrorKind::BrokenPipe, "sequencer is closed"),
        ErrorSnapshot::into_error,
    )
}

fn pop_front(state: &mut QueueState) -> Request {
    let request = state.requests.pop_front().unwrap();
    state.queued_bytes -= request.transaction.resident_bytes();
    state.metrics.queued_transactions = state.requests.len();
    state.metrics.queued_bytes = state.queued_bytes;
    request
}

fn close(shared: &Shared, terminal_error: Option<ErrorSnapshot>) {
    let mut state = shared
        .state
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    state.closed = true;
    if state.terminal_error.is_none() {
        state.terminal_error = terminal_error;
    }
    let response_error = state
        .terminal_error
        .clone()
        .unwrap_or_else(|| ErrorSnapshot {
            kind: io::ErrorKind::BrokenPipe,
            message: "sequencer is closed".to_owned(),
        });
    let pending = std::mem::take(&mut state.requests);
    state.queued_bytes = 0;
    state.metrics.queued_transactions = 0;
    state.metrics.queued_bytes = 0;
    state.metrics.failed_transactions = state
        .metrics
        .failed_transactions
        .saturating_add(pending.len() as u64);
    drop(state);
    shared.ready.notify_all();
    for request in pending {
        let _ = request.response.send(Err(response_error.clone()));
    }
}

fn terminal_error_for(shared: &Shared) -> ErrorSnapshot {
    shared
        .state
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .terminal_error
        .clone()
        .unwrap_or_else(|| ErrorSnapshot {
            kind: io::ErrorKind::BrokenPipe,
            message: "sequencer is closed".to_owned(),
        })
}

fn first_request(shared: &Shared) -> io::Result<Option<Request>> {
    let mut guard = state(shared)?;
    loop {
        if !guard.requests.is_empty() {
            let request = pop_front(&mut guard);
            return Ok(Some(request));
        }
        if guard.closed {
            return Ok(None);
        }
        guard = shared
            .ready
            .wait(guard)
            .map_err(|_| io::Error::other("sequencer queue lock is poisoned"))?;
    }
}

fn fill_group(shared: &Shared, group: &mut Vec<Request>, deadline: Instant) -> io::Result<()> {
    let mut logical_bytes = group[0].transaction.logical_payload_bytes();
    let mut wal_bytes = group[0].transaction.wal_encoded_bytes();
    while group.len() < MAX_GROUP_TRANSACTIONS {
        let mut guard = state(shared)?;
        loop {
            if let Some(next) = guard.requests.front() {
                let next_logical =
                    logical_bytes.saturating_add(next.transaction.logical_payload_bytes());
                let next_wal = wal_bytes.saturating_add(next.transaction.wal_encoded_bytes());
                if next_logical > MAX_GROUP_ENCODED_BYTES || next_wal > MAX_GROUP_ENCODED_BYTES {
                    return Ok(());
                }
                logical_bytes = next_logical;
                wal_bytes = next_wal;
                let request = pop_front(&mut guard);
                group.push(request);
                break;
            }
            if guard.closed {
                return Ok(());
            }
            if group.len() >= IMMEDIATE_FILL_DEPTH {
                return Ok(());
            }
            if shared.in_flight_submissions.load(Ordering::Acquire) == 0 {
                return Ok(());
            }
            let now = Instant::now();
            if now >= deadline {
                return Ok(());
            }
            let (next_guard, timeout) = shared
                .ready
                .wait_timeout(guard, deadline.saturating_duration_since(now))
                .map_err(|_| io::Error::other("sequencer queue lock is poisoned"))?;
            guard = next_guard;
            if timeout.timed_out() && guard.requests.is_empty() {
                return Ok(());
            }
        }
    }
    Ok(())
}

fn record_group_metrics(shared: &Shared, group: &[Request], committed: bool) {
    if let Ok(mut state) = shared.state.lock() {
        let logical_bytes = group
            .iter()
            .map(|request| request.transaction.logical_payload_bytes())
            .sum();
        let wal_bytes = group
            .iter()
            .map(|request| request.transaction.wal_encoded_bytes())
            .sum();
        if committed {
            state.metrics.committed_transactions = state
                .metrics
                .committed_transactions
                .saturating_add(group.len() as u64);
            state.metrics.durability_groups = state.metrics.durability_groups.saturating_add(1);
            state.metrics.largest_group_transactions =
                state.metrics.largest_group_transactions.max(group.len());
            state.metrics.largest_group_logical_bytes =
                state.metrics.largest_group_logical_bytes.max(logical_bytes);
            state.metrics.largest_group_wal_bytes =
                state.metrics.largest_group_wal_bytes.max(wal_bytes);
        } else {
            state.metrics.failed_transactions = state
                .metrics
                .failed_transactions
                .saturating_add(group.len() as u64);
        }
    }
}

fn run_worker(mut engine: Engine, shared: &Shared) -> io::Result<()> {
    while let Some(first) = first_request(shared)? {
        let deadline = Instant::now() + shared.config.batch_delay;
        let mut group = Vec::with_capacity(MAX_GROUP_TRANSACTIONS);
        group.push(first);
        fill_group(shared, &mut group, deadline)?;
        let prepared = group
            .iter()
            .map(|request| &request.transaction)
            .collect::<Vec<_>>();
        // Prepared values are held by `group`; copying the small reference
        // vector is bounded to 64 entries, but payloads are never cloned here.
        let result = engine.commit_prepared_batch(&prepared);
        match result {
            Ok(results) => {
                record_group_metrics(shared, &group, true);
                for (request, result) in group.into_iter().zip(results) {
                    let _ = request.response.send(Ok(result));
                }
            }
            Err(error) => {
                record_group_metrics(shared, &group, false);
                let snapshot = ErrorSnapshot::capture(&error);
                // Publish a restart-required terminal state before any member
                // of the failed group can observe its response. Otherwise a
                // caller may immediately submit into the brief interval
                // between the response send and worker shutdown.
                let restart_required = engine.is_restart_required();
                if restart_required {
                    close(shared, Some(snapshot.clone()));
                }
                for request in group {
                    let _ = request.response.send(Err(snapshot.clone()));
                }
                if restart_required {
                    return Ok(());
                }
            }
        }
    }
    Ok(())
}

/// An accepted transaction's durable result. Dropping the ticket does not cancel it.
pub struct CommitTicket {
    shared: Arc<Shared>,
    response: mpsc::Receiver<Response>,
}

impl CommitTicket {
    pub fn wait(self) -> io::Result<CommitResult> {
        self.response
            .recv()
            .map_err(|_| terminal_error_for(self.shared.as_ref()).into_error())?
            .map_err(ErrorSnapshot::into_error)
    }
}

pub struct CommitSequencer {
    shared: Arc<Shared>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl CommitSequencer {
    pub fn initialize(data_dir: &Path, config: SequencerConfig) -> io::Result<Self> {
        let config = config.validate()?;
        Self::start_validated(Engine::initialize(data_dir)?, config)
    }

    pub fn open(data_dir: &Path, config: SequencerConfig) -> io::Result<Self> {
        let config = config.validate()?;
        Self::start_validated(Engine::open(data_dir)?, config)
    }

    pub fn start(engine: Engine, config: SequencerConfig) -> io::Result<Self> {
        let config = config.validate()?;
        Self::start_validated(engine, config)
    }

    fn start_validated(engine: Engine, config: SequencerConfig) -> io::Result<Self> {
        let shared = Arc::new(Shared {
            config,
            state: Mutex::new(QueueState {
                requests: VecDeque::new(),
                queued_bytes: 0,
                closed: false,
                terminal_error: None,
                metrics: SequencerMetrics::default(),
            }),
            ready: Condvar::new(),
            in_flight_submissions: AtomicUsize::new(0),
        });
        let worker_shared = Arc::clone(&shared);
        let worker = thread::Builder::new()
            .name("tofu-db-sequencer".to_owned())
            .spawn(move || {
                let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
                    run_worker(engine, worker_shared.as_ref())
                }));
                match outcome {
                    Ok(Ok(())) => close(worker_shared.as_ref(), None),
                    Ok(Err(error)) => {
                        close(worker_shared.as_ref(), Some(ErrorSnapshot::capture(&error)))
                    }
                    Err(_) => close(
                        worker_shared.as_ref(),
                        Some(ErrorSnapshot {
                            kind: io::ErrorKind::Other,
                            message: "sequencer worker panicked".to_owned(),
                        }),
                    ),
                }
            })?;
        Ok(Self {
            shared,
            worker: Mutex::new(Some(worker)),
        })
    }

    pub fn submit(&self, transaction: BatchTransaction) -> io::Result<CommitResult> {
        self.enqueue(transaction)?.wait()
    }

    /// Prepare and accept owned work; only the returned ticket waits for durability.
    pub fn enqueue(&self, transaction: BatchTransaction) -> io::Result<CommitTicket> {
        self.shared
            .in_flight_submissions
            .fetch_add(1, Ordering::AcqRel);
        let _in_flight = InFlightSubmission {
            shared: self.shared.as_ref(),
        };
        let transaction = transaction.prepare()?;
        let resident_bytes = transaction.resident_bytes();
        let (response_sender, response_receiver) = mpsc::sync_channel(1);
        let mut guard = state(self.shared.as_ref())?;
        if guard.closed {
            return Err(closed_error(&guard));
        }
        let queued_bytes = guard
            .queued_bytes
            .checked_add(resident_bytes)
            .ok_or_else(|| io::Error::other("sequencer residency accounting overflow"))?;
        guard
            .requests
            .try_reserve(1)
            .map_err(|_| io::Error::other("sequencer request allocation failed"))?;
        guard.queued_bytes = queued_bytes;
        guard.requests.push_back(Request {
            transaction,
            response: response_sender,
        });
        guard.metrics.submitted_transactions =
            guard.metrics.submitted_transactions.saturating_add(1);
        guard.metrics.queued_transactions = guard.requests.len();
        guard.metrics.queued_bytes = guard.queued_bytes;
        guard.metrics.peak_queued_transactions = guard
            .metrics
            .peak_queued_transactions
            .max(guard.requests.len());
        guard.metrics.peak_queued_bytes = guard.metrics.peak_queued_bytes.max(guard.queued_bytes);
        drop(guard);
        self.shared.ready.notify_one();
        // The request is visible in the queue now, so the in-flight window
        // (submit entry → queue push) ends here. Keeping it through the
        // response wait would count a sequential client's own queued
        // request as a pending batch-mate and make every solo commit wait
        // the full batch delay.
        drop(_in_flight);
        Ok(CommitTicket {
            shared: Arc::clone(&self.shared),
            response: response_receiver,
        })
    }

    pub fn metrics(&self) -> io::Result<SequencerMetrics> {
        Ok(state(self.shared.as_ref())?.metrics)
    }
}

impl Drop for CommitSequencer {
    fn drop(&mut self) {
        self.shared
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .closed = true;
        self.shared.ready.notify_all();
        let worker = self
            .worker
            .get_mut()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(worker) = worker.take() {
            let _ = worker.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::sync::Barrier;

    fn engine() -> (Arc<DeterministicVfs>, Engine) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn transaction(index: usize) -> BatchTransaction {
        BatchTransaction {
            inline_payload: format!("transaction-{index:04}").into_bytes(),
            block_payloads: Vec::new(),
        }
    }

    fn preloaded(
        transactions: Vec<BatchTransaction>,
    ) -> (Arc<Shared>, Vec<mpsc::Receiver<Response>>) {
        let mut requests = VecDeque::new();
        let mut receivers = Vec::new();
        let mut queued_bytes = 0_usize;
        for transaction in transactions {
            let transaction = transaction.prepare().unwrap();
            queued_bytes += transaction.resident_bytes();
            let (sender, receiver) = mpsc::sync_channel(1);
            requests.push_back(Request {
                transaction,
                response: sender,
            });
            receivers.push(receiver);
        }
        let count = requests.len();
        let shared = Arc::new(Shared {
            config: SequencerConfig::default(),
            state: Mutex::new(QueueState {
                requests,
                queued_bytes,
                closed: true,
                terminal_error: None,
                metrics: SequencerMetrics {
                    submitted_transactions: count as u64,
                    queued_transactions: count,
                    queued_bytes,
                    peak_queued_transactions: count,
                    peak_queued_bytes: queued_bytes,
                    ..SequencerMetrics::default()
                },
            }),
            ready: Condvar::new(),
            in_flight_submissions: AtomicUsize::new(0),
        });
        (shared, receivers)
    }

    #[test]
    fn preloaded_work_forms_exact_sixty_four_member_groups() {
        let (vfs, engine) = engine();
        let (shared, receivers) = preloaded((0..65).map(transaction).collect());
        run_worker(engine, shared.as_ref()).unwrap();
        let sequences = receivers
            .into_iter()
            .map(|receiver| receiver.recv().unwrap().unwrap().sequence)
            .collect::<Vec<_>>();
        assert_eq!(sequences, (1..=65).collect::<Vec<_>>());
        let metrics = shared.state.lock().unwrap().metrics;
        assert_eq!(metrics.committed_transactions, 65);
        assert_eq!(metrics.durability_groups, 2);
        assert_eq!(metrics.largest_group_transactions, 64);
        assert_eq!(
            vfs.trace()
                .unwrap()
                .iter()
                .filter(|operation| operation == &&Operation::SyncData)
                .count(),
            4
        );
    }

    #[test]
    fn logical_byte_limit_splits_a_preloaded_group_without_cloning_payloads() {
        let (vfs, engine) = engine();
        let transactions = (0..3)
            .map(|index| BatchTransaction {
                inline_payload: format!("large-{index}").into_bytes(),
                block_payloads: vec![vec![index as u8; 3 * 1024 * 1024]],
            })
            .collect();
        let (shared, receivers) = preloaded(transactions);
        run_worker(engine, shared.as_ref()).unwrap();
        assert!(receivers
            .into_iter()
            .all(|receiver| receiver.recv().unwrap().is_ok()));
        let metrics = shared.state.lock().unwrap().metrics;
        assert_eq!(metrics.durability_groups, 2);
        assert_eq!(metrics.largest_group_transactions, 2);
        assert!(metrics.largest_group_logical_bytes <= MAX_GROUP_ENCODED_BYTES);
        assert!(metrics.largest_group_wal_bytes <= MAX_GROUP_ENCODED_BYTES);
        assert_eq!(metrics.queued_bytes, 0);
        assert_eq!(metrics.queued_transactions, 0);
        assert_eq!(
            vfs.trace()
                .unwrap()
                .iter()
                .filter(|operation| operation == &&Operation::SyncData)
                .count(),
            4
        );
    }

    #[test]
    fn concurrent_submitters_receive_unique_ordered_sequences() {
        let (_vfs, engine) = engine();
        let sequencer =
            Arc::new(CommitSequencer::start(engine, SequencerConfig::default()).unwrap());
        let barrier = Arc::new(Barrier::new(33));
        let mut threads = Vec::new();
        for index in 0..32 {
            let sequencer = Arc::clone(&sequencer);
            let barrier = Arc::clone(&barrier);
            threads.push(thread::spawn(move || {
                barrier.wait();
                sequencer.submit(transaction(index)).unwrap().sequence
            }));
        }
        barrier.wait();
        let mut sequences = threads
            .into_iter()
            .map(|thread| thread.join().unwrap())
            .collect::<Vec<_>>();
        sequences.sort_unstable();
        assert_eq!(sequences, (1..=32).collect::<Vec<_>>());
        let metrics = sequencer.metrics().unwrap();
        assert_eq!(metrics.submitted_transactions, 32);
        assert_eq!(metrics.committed_transactions, 32);
        assert_eq!(metrics.failed_transactions, 0);
        assert!(metrics.durability_groups >= 1);
        assert!(metrics.durability_groups <= 32);
        assert!(metrics.largest_group_transactions <= MAX_GROUP_TRANSACTIONS);
    }

    #[test]
    fn invalid_batch_delay_fails_before_creating_authority() {
        let invalid = [
            SequencerConfig {
                batch_delay: Duration::ZERO,
                ..SequencerConfig::default()
            },
            SequencerConfig {
                batch_delay: MAX_BATCH_DELAY + Duration::from_nanos(1),
                ..SequencerConfig::default()
            },
        ];
        for config in invalid {
            let (_vfs, engine) = engine();
            assert_eq!(
                CommitSequencer::start(engine, config).err().unwrap().kind(),
                io::ErrorKind::InvalidInput
            );
        }

        let parent = tempfile::tempdir().unwrap();
        let data_dir = parent.path().join("must-not-be-created");
        assert_eq!(
            CommitSequencer::initialize(
                &data_dir,
                SequencerConfig {
                    batch_delay: Duration::ZERO,
                    ..SequencerConfig::default()
                },
            )
            .err()
            .unwrap()
            .kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(!data_dir.exists());
    }

    #[test]
    fn demand_above_former_slot_and_residency_quotas_drains_and_reopens() {
        let (vfs, engine) = engine();
        let (shared, _) = preloaded(Vec::new());
        shared.state.lock().unwrap().closed = false;
        let sequencer = CommitSequencer {
            shared: Arc::clone(&shared),
            worker: Mutex::new(None),
        };
        let started = Instant::now();
        let tickets = (0..1100)
            .map(|ordinal| {
                sequencer
                    .enqueue(BatchTransaction {
                        inline_payload: format!("demand-{ordinal}").into_bytes(),
                        block_payloads: vec![vec![(ordinal % 251) as u8; 16 * 1024]],
                    })
                    .unwrap()
            })
            .collect::<Vec<_>>();
        let metrics = sequencer.metrics().unwrap();
        assert_eq!(metrics.queued_transactions, 1100);
        assert!(metrics.queued_bytes > 16 * 1024 * 1024);
        eprintln!(
            "sequencer_demand accepted=1100 queued_bytes={} enqueue_us={}",
            metrics.queued_bytes,
            started.elapsed().as_micros()
        );
        shared.state.lock().unwrap().closed = true;
        let worker_shared = Arc::clone(&shared);
        *sequencer.worker.lock().unwrap() = Some(thread::spawn(move || {
            run_worker(engine, &worker_shared).unwrap()
        }));
        let mut blocks = Vec::new();
        for (ordinal, ticket) in tickets.into_iter().enumerate() {
            let result = ticket.wait().unwrap();
            assert_eq!(result.sequence, ordinal as u64 + 1);
            assert_eq!(result.block_ids.len(), 1);
            blocks.push((ordinal, result.block_ids[0]));
        }
        let metrics = sequencer.metrics().unwrap();
        assert_eq!(metrics.committed_transactions, 1100);
        assert_eq!(metrics.queued_bytes, 0);
        assert_eq!(metrics.failed_transactions, 0);
        assert!(metrics.durability_groups > 1);
        assert!(metrics.largest_group_transactions <= MAX_GROUP_TRANSACTIONS);
        drop(sequencer); // joins the owned writer before reopen
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.next_sequence().unwrap(), 1101);
        for (ordinal, block) in blocks {
            assert_eq!(
                reopened.read_block(block).unwrap(),
                vec![(ordinal % 251) as u8; 16 * 1024]
            );
        }
    }

    #[test]
    fn dropped_ticket_keeps_accepted_work_and_writer_close_joins_before_reopen() {
        let (vfs, engine) = engine();
        let sequencer = CommitSequencer::start(engine, SequencerConfig::default()).unwrap();
        drop(sequencer.enqueue(transaction(1)).unwrap());
        let second = sequencer.enqueue(transaction(2)).unwrap();
        drop(sequencer);
        assert_eq!(second.wait().unwrap().sequence, 2);
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.next_sequence().unwrap(), 3);
    }

    #[test]
    fn ambiguous_wal_failure_closes_the_worker_and_propagates_error() {
        let (baseline_vfs, mut baseline) = engine();
        baseline.commit_batch(&[transaction(0)]).unwrap();
        let first_write = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (vfs, engine) = engine();
        let sequencer = CommitSequencer::start(engine, SequencerConfig::default()).unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            sequencer.submit(transaction(0)).unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            sequencer.submit(transaction(1)).unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(vfs.trace().unwrap().is_empty());
        let metrics = sequencer.metrics().unwrap();
        assert_eq!(metrics.failed_transactions, 1);
        assert_eq!(metrics.committed_transactions, 0);
    }

    #[test]
    fn terminal_close_drains_waiters_even_after_queue_lock_poisoning() {
        let (shared, mut receivers) = preloaded(vec![transaction(0)]);
        let poison_target = Arc::clone(&shared);
        assert!(thread::spawn(move || {
            let _guard = poison_target.state.lock().unwrap();
            panic!("intentional queue poison");
        })
        .join()
        .is_err());
        close(
            shared.as_ref(),
            Some(ErrorSnapshot {
                kind: io::ErrorKind::Other,
                message: "worker terminated".to_owned(),
            }),
        );
        let error = receivers.pop().unwrap().recv().unwrap().unwrap_err();
        assert_eq!(error.kind, io::ErrorKind::Other);
        assert_eq!(error.message, "worker terminated");
        let state = shared
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        assert!(state.closed);
        assert!(state.requests.is_empty());
        assert_eq!(state.metrics.failed_transactions, 1);
    }
}
