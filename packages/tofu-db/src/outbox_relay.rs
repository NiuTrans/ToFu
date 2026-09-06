//! Automatic bounded delivery from explicit authority owner scopes to a durable sink.
//!
//! The relay holds the authority mutex only while fetching a bounded pending
//! page or committing one ordered acknowledgement. Sink I/O always happens on
//! the isolated outbox worker with no authority borrow. Polling is bounded and
//! can also be explicitly notified after a foreground commit.

use std::collections::BTreeSet;
use std::io;
use std::panic::{self, AssertUnwindSafe};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crate::authority::AuthorityDatabase;
pub use crate::outbox_publisher::LogicalOutboxOwnerScope;
use crate::outbox_publisher::{LogicalOutboxPublisherMetrics, LogicalOutboxSink};
use crate::outbox_worker::{LogicalOutboxWorker, LogicalOutboxWorkerMetrics};

pub const MIN_OUTBOX_RELAY_POLL_INTERVAL: Duration = Duration::from_millis(5);
pub const MAX_OUTBOX_RELAY_POLL_INTERVAL: Duration = Duration::from_secs(1);
pub const MIN_OUTBOX_RELAY_OPERATION_TIMEOUT: Duration = Duration::from_millis(10);
pub const MAX_OUTBOX_RELAY_OPERATION_TIMEOUT: Duration = Duration::from_secs(60);
pub const MAX_OUTBOX_RELAY_OWNER_SCOPES: usize = 64;
const DROP_DRAIN_WAIT: Duration = Duration::from_millis(100);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LogicalOutboxRelayConfig {
    pub poll_interval: Duration,
    pub operation_timeout: Duration,
}

impl Default for LogicalOutboxRelayConfig {
    fn default() -> Self {
        Self {
            poll_interval: Duration::from_millis(250),
            operation_timeout: Duration::from_secs(5),
        }
    }
}

impl LogicalOutboxRelayConfig {
    fn validate(self) -> io::Result<Self> {
        if !(MIN_OUTBOX_RELAY_POLL_INTERVAL..=MAX_OUTBOX_RELAY_POLL_INTERVAL)
            .contains(&self.poll_interval)
        {
            return Err(invalid_input(
                "logical outbox relay poll interval is outside 5 ms to 1 s",
            ));
        }
        if !(MIN_OUTBOX_RELAY_OPERATION_TIMEOUT..=MAX_OUTBOX_RELAY_OPERATION_TIMEOUT)
            .contains(&self.operation_timeout)
        {
            return Err(invalid_input(
                "logical outbox relay operation timeout is outside 10 ms to 60 s",
            ));
        }
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LogicalOutboxRelayMetrics {
    pub polls: u64,
    pub empty_polls: u64,
    pub published_batches: u64,
    pub published_records: u64,
    pub acknowledged_records: u64,
    pub retryable_failures: u64,
    pub deadline_expirations: u64,
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

struct RelayState {
    stopping: bool,
    metrics: LogicalOutboxRelayMetrics,
    terminal_error: Option<ErrorSnapshot>,
}

struct Shared {
    state: Mutex<RelayState>,
    wake: Condvar,
}

pub struct LogicalOutboxRelay<S: LogicalOutboxSink + 'static> {
    shared: Arc<Shared>,
    worker: Option<Arc<LogicalOutboxWorker<S>>>,
    relay: Option<JoinHandle<()>>,
    finished: Option<mpsc::Receiver<()>>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn validate_scopes(scopes: &[LogicalOutboxOwnerScope]) -> io::Result<()> {
    if scopes.is_empty() || scopes.len() > MAX_OUTBOX_RELAY_OWNER_SCOPES {
        return Err(invalid_input(
            "logical outbox relay must have between 1 and 64 owner scopes",
        ));
    }
    let mut unique_scopes = BTreeSet::new();
    for scope in scopes {
        LogicalOutboxOwnerScope::new(scope.tenant_id, scope.owner_user_id)?;
        if !unique_scopes.insert(*scope) {
            return Err(invalid_input(
                "logical outbox relay owner scopes must be unique",
            ));
        }
    }
    Ok(())
}

fn state(shared: &Shared) -> MutexGuard<'_, RelayState> {
    shared
        .state
        .lock()
        .unwrap_or_else(|error| error.into_inner())
}

fn should_stop(shared: &Shared) -> bool {
    state(shared).stopping
}

fn terminal_kind(kind: io::ErrorKind) -> bool {
    matches!(
        kind,
        io::ErrorKind::InvalidData
            | io::ErrorKind::InvalidInput
            | io::ErrorKind::PermissionDenied
            | io::ErrorKind::BrokenPipe
            | io::ErrorKind::NotFound
    )
}

fn record_failure(shared: &Shared, error: &io::Error, authority_restart_required: bool) -> bool {
    let mut guard = state(shared);
    if error.kind() == io::ErrorKind::TimedOut {
        guard.metrics.deadline_expirations = guard.metrics.deadline_expirations.saturating_add(1);
    }
    if authority_restart_required || terminal_kind(error.kind()) {
        guard.stopping = true;
        guard.metrics.terminal = true;
        guard.terminal_error = Some(ErrorSnapshot::capture(error));
        true
    } else {
        guard.metrics.retryable_failures = guard.metrics.retryable_failures.saturating_add(1);
        false
    }
}

fn close_after_panic(shared: &Shared) {
    let mut guard = state(shared);
    guard.stopping = true;
    guard.metrics.terminal = true;
    guard.terminal_error = Some(ErrorSnapshot {
        kind: io::ErrorKind::Other,
        message: "logical outbox relay panicked".to_owned(),
    });
    drop(guard);
    shared.wake.notify_all();
}

fn wait_for_work(shared: &Shared, interval: Duration) {
    let guard = state(shared);
    if !guard.stopping {
        drop(
            shared
                .wake
                .wait_timeout(guard, interval)
                .unwrap_or_else(|error| error.into_inner()),
        );
    }
}

fn run_relay<S: LogicalOutboxSink + 'static>(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    worker: &Arc<LogicalOutboxWorker<S>>,
    shared: &Shared,
    scopes: &[LogicalOutboxOwnerScope],
    config: LogicalOutboxRelayConfig,
) {
    let budget = worker.publish_budget();
    let mut scope_index = 0_usize;
    let mut consecutive_empty_scopes = 0_usize;
    while !should_stop(shared) {
        let scope = scopes[scope_index];
        {
            let mut guard = state(shared);
            guard.metrics.polls = guard.metrics.polls.saturating_add(1);
        }
        let pending = match authority.lock() {
            Ok(database) => database.logical_outbox_pending_bounded(
                scope.tenant_id,
                scope.owner_user_id,
                budget.max_records,
                budget.max_bytes,
            ),
            Err(_) => Err(io::Error::other(
                "logical outbox authority lock is poisoned",
            )),
        };
        let pending = match pending {
            Ok(pending) => pending,
            Err(error) => {
                let restart_required = authority
                    .lock()
                    .map(|database| database.is_restart_required())
                    .unwrap_or(true);
                if record_failure(shared, &error, restart_required) {
                    break;
                }
                wait_for_work(shared, config.poll_interval);
                scope_index = (scope_index + 1) % scopes.len();
                consecutive_empty_scopes = 0;
                continue;
            }
        };
        if pending.is_empty() {
            let mut guard = state(shared);
            guard.metrics.empty_polls = guard.metrics.empty_polls.saturating_add(1);
            drop(guard);
            scope_index = (scope_index + 1) % scopes.len();
            consecutive_empty_scopes += 1;
            if consecutive_empty_scopes == scopes.len() {
                wait_for_work(shared, config.poll_interval);
                consecutive_empty_scopes = 0;
            }
            continue;
        }
        consecutive_empty_scopes = 0;

        let deadline = Instant::now() + config.operation_timeout;
        let result = match worker.submit(pending, deadline) {
            Ok(result) => result,
            Err(error) => {
                let worker_terminal = worker
                    .metrics()
                    .map(|metrics| metrics.terminal)
                    .unwrap_or(true);
                if record_failure(shared, &error, worker_terminal) {
                    break;
                }
                wait_for_work(shared, config.poll_interval);
                scope_index = (scope_index + 1) % scopes.len();
                consecutive_empty_scopes = 0;
                continue;
            }
        };
        {
            let mut guard = state(shared);
            guard.metrics.published_batches = guard.metrics.published_batches.saturating_add(1);
            guard.metrics.published_records = guard
                .metrics
                .published_records
                .saturating_add(result.receipts.len() as u64);
        }

        let mut retry = false;
        for receipt in result.receipts {
            if should_stop(shared) {
                return;
            }
            if Instant::now() >= deadline {
                record_failure(
                    shared,
                    &io::Error::new(
                        io::ErrorKind::TimedOut,
                        "logical outbox relay ACK deadline expired",
                    ),
                    false,
                );
                retry = true;
                break;
            }
            let acknowledgement = match authority.lock() {
                Ok(mut database) => database.logical_outbox_acknowledge(
                    receipt.tenant_id,
                    receipt.owner_user_id,
                    receipt.sequence,
                    receipt.event_id,
                ),
                Err(_) => Err(io::Error::other(
                    "logical outbox authority lock is poisoned",
                )),
            };
            if let Err(error) = acknowledgement {
                let restart_required = authority
                    .lock()
                    .map(|database| database.is_restart_required())
                    .unwrap_or(true);
                if record_failure(shared, &error, restart_required) {
                    return;
                }
                retry = true;
                break;
            }
            let mut guard = state(shared);
            guard.metrics.acknowledged_records =
                guard.metrics.acknowledged_records.saturating_add(1);
        }
        if retry {
            wait_for_work(shared, config.poll_interval);
        }
        scope_index = (scope_index + 1) % scopes.len();
    }
}

impl<S: LogicalOutboxSink + 'static> LogicalOutboxRelay<S> {
    pub fn start(
        authority: Arc<Mutex<AuthorityDatabase>>,
        worker: LogicalOutboxWorker<S>,
        tenant_id: u64,
        owner_user_id: u64,
        config: LogicalOutboxRelayConfig,
    ) -> io::Result<Self> {
        Self::start_scopes(
            authority,
            worker,
            vec![LogicalOutboxOwnerScope::new(tenant_id, owner_user_id)?],
            config,
        )
    }

    pub fn start_scopes(
        authority: Arc<Mutex<AuthorityDatabase>>,
        worker: LogicalOutboxWorker<S>,
        scopes: Vec<LogicalOutboxOwnerScope>,
        config: LogicalOutboxRelayConfig,
    ) -> io::Result<Self> {
        let config = config.validate()?;
        validate_scopes(&scopes)?;
        let shared = Arc::new(Shared {
            state: Mutex::new(RelayState {
                stopping: false,
                metrics: LogicalOutboxRelayMetrics::default(),
                terminal_error: None,
            }),
            wake: Condvar::new(),
        });
        let worker = Arc::new(worker);
        let relay_authority = Arc::clone(&authority);
        let relay_worker = Arc::clone(&worker);
        let relay_shared = Arc::clone(&shared);
        let (finished_sender, finished_receiver) = mpsc::sync_channel(1);
        let relay = thread::Builder::new()
            .name("tofu-db-outbox-relay".to_owned())
            .spawn(move || {
                let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
                    run_relay(
                        &relay_authority,
                        &relay_worker,
                        relay_shared.as_ref(),
                        &scopes,
                        config,
                    );
                }));
                if outcome.is_err() {
                    close_after_panic(relay_shared.as_ref());
                }
                let _ = finished_sender.send(());
            })?;
        Ok(Self {
            shared,
            worker: Some(worker),
            relay: Some(relay),
            finished: Some(finished_receiver),
        })
    }

    pub fn notify_new_work(&self) {
        self.shared.wake.notify_one();
    }

    pub fn metrics(&self) -> LogicalOutboxRelayMetrics {
        state(self.shared.as_ref()).metrics
    }

    pub fn terminal_error(&self) -> Option<io::Error> {
        state(self.shared.as_ref())
            .terminal_error
            .clone()
            .map(ErrorSnapshot::into_error)
    }

    fn stop(&self) {
        state(self.shared.as_ref()).stopping = true;
        self.shared.wake.notify_all();
    }

    pub fn shutdown_until(
        mut self,
        deadline: Instant,
    ) -> io::Result<(
        S,
        LogicalOutboxPublisherMetrics,
        LogicalOutboxWorkerMetrics,
        LogicalOutboxRelayMetrics,
    )> {
        self.stop();
        let finished = self
            .finished
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "outbox relay is stopped"))?;
        if matches!(
            finished.recv_timeout(deadline.saturating_duration_since(Instant::now())),
            Err(mpsc::RecvTimeoutError::Timeout)
        ) {
            self.relay.take();
            self.worker.take();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "logical outbox relay shutdown deadline expired",
            ));
        }
        self.relay
            .take()
            .expect("relay handle exists while receiver exists")
            .join()
            .map_err(|_| io::Error::other("logical outbox relay join failed"))?;
        let worker = Arc::try_unwrap(
            self.worker
                .take()
                .expect("relay owns its outbox worker until shutdown"),
        )
        .map_err(|_| io::Error::other("logical outbox worker is still referenced"))?;
        let relay_metrics = self.metrics();
        let (sink, publisher_metrics, worker_metrics) = worker.shutdown_until(deadline)?;
        Ok((sink, publisher_metrics, worker_metrics, relay_metrics))
    }
}

impl<S: LogicalOutboxSink + 'static> Drop for LogicalOutboxRelay<S> {
    fn drop(&mut self) {
        self.stop();
        if let (Some(finished), Some(relay)) = (self.finished.take(), self.relay.take()) {
            if finished.recv_timeout(DROP_DRAIN_WAIT).is_ok() {
                let _ = relay.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;
    use crate::entity::EntityKey;
    use crate::logical_outbox::{LogicalOutboxCapture, SealedLogicalOutboxRecord};
    use crate::outbox_multi_sink::{
        MultiplexedEngineLogicalOutboxSink, MultiplexedEngineLogicalOutboxSinkConfig,
    };
    use crate::outbox_publisher::{DurableLogicalOutboxReceipt, LogicalOutboxPublishBudget};
    use crate::outbox_sink::EngineLogicalOutboxSink;
    use crate::outbox_worker::LogicalOutboxWorkerConfig;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Vfs};

    struct ProbeSink {
        authority: Arc<Mutex<AuthorityDatabase>>,
        durable: BTreeMap<(u64, u64, u64), [u8; 32]>,
        append_order: Vec<LogicalOutboxOwnerScope>,
        append_calls: usize,
        lose_first_response: bool,
        retryable_failure_owner: Option<u64>,
    }

    impl LogicalOutboxSink for ProbeSink {
        fn append_durable(
            &mut self,
            record: &SealedLogicalOutboxRecord,
        ) -> io::Result<DurableLogicalOutboxReceipt> {
            assert_eq!(thread::current().name(), Some("tofu-db-outbox-worker"));
            assert!(
                self.authority.try_lock().is_ok(),
                "sink I/O ran while the authority mutex was held"
            );
            self.append_calls += 1;
            let scope = LogicalOutboxOwnerScope {
                tenant_id: record.identity.tenant_id,
                owner_user_id: record.identity.owner_user_id,
            };
            self.append_order.push(scope);
            if self.retryable_failure_owner == Some(record.identity.owner_user_id) {
                return Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "simulated owner-local retryable failure",
                ));
            }
            let key = (
                record.identity.tenant_id,
                record.identity.owner_user_id,
                record.identity.sequence,
            );
            match self.durable.get(&key) {
                Some(event_id) if event_id != &record.event_id => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "durable event identity differs",
                    ));
                }
                Some(_) => {}
                None => {
                    self.durable.insert(key, record.event_id);
                    if self.lose_first_response {
                        self.lose_first_response = false;
                        return Err(io::Error::new(
                            io::ErrorKind::Interrupted,
                            "simulated lost sink response",
                        ));
                    }
                }
            }
            Ok(DurableLogicalOutboxReceipt {
                tenant_id: record.identity.tenant_id,
                owner_user_id: record.identity.owner_user_id,
                sequence: record.identity.sequence,
                event_id: record.event_id,
            })
        }
    }

    fn commit_outbox_scope(
        database: &mut AuthorityDatabase,
        tenant_id: u64,
        owner_user_id: u64,
        ordinal: u8,
    ) {
        let mut transaction = database.begin(tenant_id, owner_user_id).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(tenant_id, owner_user_id, "relay-test", &[ordinal]).unwrap(),
                vec![ordinal],
            )
            .unwrap();
        database
            .logical_outbox_capture(
                &mut transaction,
                LogicalOutboxCapture {
                    schema_version: 57,
                    registry_version: 37,
                    operation: "artifact.create".to_owned(),
                    request_id: format!("request-{ordinal}"),
                    request_digest: [ordinal; 32],
                    command_id: Some(format!("command-{ordinal}")),
                    committed_at_ms: 4_000 + u64::from(ordinal),
                    clear_payload: vec![ordinal; 512],
                },
            )
            .unwrap();
        database.commit(transaction).unwrap();
    }

    fn commit_outbox(database: &mut AuthorityDatabase, ordinal: u8) {
        commit_outbox_scope(database, 7, 11, ordinal);
    }

    fn start_relay(
        authority: Arc<Mutex<AuthorityDatabase>>,
        lose_first_response: bool,
    ) -> LogicalOutboxRelay<ProbeSink> {
        let sink = ProbeSink {
            authority: Arc::clone(&authority),
            durable: BTreeMap::new(),
            append_order: Vec::new(),
            append_calls: 0,
            lose_first_response,
            retryable_failure_owner: None,
        };
        let worker = LogicalOutboxWorker::start(
            sink,
            LogicalOutboxWorkerConfig {
                queue_batches: 1,
                queue_bytes: 16 * 1024 * 1024,
                publish_budget: LogicalOutboxPublishBudget::new(16, 8 * 1024 * 1024).unwrap(),
            },
        )
        .unwrap();
        LogicalOutboxRelay::start(
            authority,
            worker,
            7,
            11,
            LogicalOutboxRelayConfig {
                poll_interval: Duration::from_millis(5),
                operation_timeout: Duration::from_secs(1),
            },
        )
        .unwrap()
    }

    fn wait_for_acknowledgements<S: LogicalOutboxSink + 'static>(
        relay: &LogicalOutboxRelay<S>,
        count: u64,
    ) {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if relay.metrics().acknowledged_records == count {
                return;
            }
            assert!(
                Instant::now() < deadline,
                "relay did not drain pending work"
            );
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn simulated_authority() -> (Arc<Mutex<AuthorityDatabase>>, Arc<DeterministicVfs>) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        database
            .configure_logical_outbox(&[43; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        commit_outbox(&mut database, 1);
        vfs.arm_fault(None).unwrap();
        (Arc::new(Mutex::new(database)), vfs)
    }

    fn wait_for_relay_outcome<S: LogicalOutboxSink + 'static>(relay: &LogicalOutboxRelay<S>) {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let metrics = relay.metrics();
            if metrics.acknowledged_records == 1 || metrics.terminal {
                return;
            }
            assert!(
                Instant::now() < deadline,
                "relay produced no terminal outcome"
            );
            thread::sleep(Duration::from_millis(2));
        }
    }

    #[test]
    fn relay_automatically_publishes_and_acknowledges_without_holding_authority() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[41; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        for ordinal in 1..=3 {
            commit_outbox(&mut database, ordinal);
        }
        let authority = Arc::new(Mutex::new(database));
        let relay = start_relay(Arc::clone(&authority), false);
        relay.notify_new_work();
        wait_for_acknowledgements(&relay, 3);
        let (sink, publisher, worker, metrics) = relay
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(sink.durable.len(), 3);
        assert_eq!(sink.append_calls, 3);
        assert_eq!(publisher.published_records, 3);
        assert_eq!(worker.completed_batches, 1);
        assert_eq!(metrics.published_records, 3);
        assert_eq!(metrics.acknowledged_records, 3);
        assert_eq!(metrics.retryable_failures, 0);
        assert!(!metrics.terminal);
    }

    #[test]
    fn one_bounded_worker_round_robins_hot_and_quiet_owner_scopes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[45; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        for ordinal in 1..=4 {
            commit_outbox_scope(&mut database, 7, 11, ordinal);
        }
        commit_outbox_scope(&mut database, 7, 12, 1);
        let authority = Arc::new(Mutex::new(database));
        let sink = ProbeSink {
            authority: Arc::clone(&authority),
            durable: BTreeMap::new(),
            append_order: Vec::new(),
            append_calls: 0,
            lose_first_response: false,
            retryable_failure_owner: None,
        };
        let worker = LogicalOutboxWorker::start(
            sink,
            LogicalOutboxWorkerConfig {
                queue_batches: 1,
                queue_bytes: 16 * 1024 * 1024,
                publish_budget: LogicalOutboxPublishBudget::new(1, 8 * 1024 * 1024).unwrap(),
            },
        )
        .unwrap();
        let hot = LogicalOutboxOwnerScope::new(7, 11).unwrap();
        let quiet = LogicalOutboxOwnerScope::new(7, 12).unwrap();
        let relay = LogicalOutboxRelay::start_scopes(
            Arc::clone(&authority),
            worker,
            vec![hot, quiet],
            LogicalOutboxRelayConfig {
                poll_interval: Duration::from_millis(5),
                operation_timeout: Duration::from_secs(1),
            },
        )
        .unwrap();
        wait_for_acknowledgements(&relay, 5);
        assert_eq!(
            authority
                .lock()
                .unwrap()
                .logical_outbox_status(hot.tenant_id, hot.owner_user_id)
                .unwrap()
                .published_sequence,
            4
        );
        assert_eq!(
            authority
                .lock()
                .unwrap()
                .logical_outbox_status(quiet.tenant_id, quiet.owner_user_id)
                .unwrap()
                .published_sequence,
            1
        );
        let (sink, publisher, worker, metrics) = relay
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(&sink.append_order[..2], &[hot, quiet]);
        assert_eq!(sink.durable.len(), 5);
        assert_eq!(publisher.published_records, 5);
        assert_eq!(worker.completed_batches, 5);
        assert_eq!(metrics.acknowledged_records, 5);
    }

    #[test]
    fn retryable_failure_for_one_owner_does_not_starve_another_owner() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[46; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        commit_outbox_scope(&mut database, 7, 11, 1);
        commit_outbox_scope(&mut database, 7, 12, 1);
        let authority = Arc::new(Mutex::new(database));
        let sink = ProbeSink {
            authority: Arc::clone(&authority),
            durable: BTreeMap::new(),
            append_order: Vec::new(),
            append_calls: 0,
            lose_first_response: false,
            retryable_failure_owner: Some(11),
        };
        let worker = LogicalOutboxWorker::start(
            sink,
            LogicalOutboxWorkerConfig {
                queue_batches: 1,
                queue_bytes: 16 * 1024 * 1024,
                publish_budget: LogicalOutboxPublishBudget::new(1, 8 * 1024 * 1024).unwrap(),
            },
        )
        .unwrap();
        let failing = LogicalOutboxOwnerScope::new(7, 11).unwrap();
        let healthy = LogicalOutboxOwnerScope::new(7, 12).unwrap();
        let relay = LogicalOutboxRelay::start_scopes(
            Arc::clone(&authority),
            worker,
            vec![failing, healthy],
            LogicalOutboxRelayConfig {
                poll_interval: Duration::from_millis(5),
                operation_timeout: Duration::from_secs(1),
            },
        )
        .unwrap();
        wait_for_acknowledgements(&relay, 1);
        // Stop the retrying sink before the test thread inspects authority.
        // Otherwise this test's own lock can make ProbeSink falsely report
        // that relay I/O retained the mutex.
        let (sink, publisher, _, metrics) = relay
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(
            authority
                .lock()
                .unwrap()
                .logical_outbox_status(failing.tenant_id, failing.owner_user_id)
                .unwrap()
                .published_sequence,
            0
        );
        assert_eq!(
            authority
                .lock()
                .unwrap()
                .logical_outbox_status(healthy.tenant_id, healthy.owner_user_id)
                .unwrap()
                .published_sequence,
            1
        );
        assert_eq!(&sink.append_order[..2], &[failing, healthy]);
        assert!(publisher.failures >= 1);
        assert_eq!(metrics.retryable_failures, publisher.failures);
        assert_eq!(metrics.acknowledged_records, 1);
        assert!(!metrics.terminal);
    }

    #[test]
    fn aggregate_owner_scope_admission_is_bounded_unique_and_nonzero() {
        assert!(validate_scopes(&[]).is_err());
        assert!(validate_scopes(
            &[LogicalOutboxOwnerScope {
                tenant_id: 7,
                owner_user_id: 11,
            }; MAX_OUTBOX_RELAY_OWNER_SCOPES + 1]
        )
        .is_err());
        assert!(validate_scopes(&[
            LogicalOutboxOwnerScope::new(7, 11).unwrap(),
            LogicalOutboxOwnerScope::new(7, 11).unwrap(),
        ])
        .is_err());
        assert!(LogicalOutboxOwnerScope::new(0, 11).is_err());
        assert!(LogicalOutboxOwnerScope::new(7, 0).is_err());
        let maximum = (1..=MAX_OUTBOX_RELAY_OWNER_SCOPES)
            .map(|owner_user_id| LogicalOutboxOwnerScope {
                tenant_id: 7,
                owner_user_id: owner_user_id as u64,
            })
            .collect::<Vec<_>>();
        validate_scopes(&maximum).unwrap();
    }

    #[test]
    fn lost_sink_response_is_retried_and_acknowledged_exactly_once() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[42; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        commit_outbox(&mut database, 1);
        let authority = Arc::new(Mutex::new(database));
        let relay = start_relay(Arc::clone(&authority), true);
        wait_for_acknowledgements(&relay, 1);
        let (sink, publisher, _, metrics) = relay
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(sink.durable.len(), 1);
        assert_eq!(sink.append_calls, 2);
        assert_eq!(publisher.failures, 1);
        assert_eq!(publisher.published_records, 1);
        assert_eq!(metrics.retryable_failures, 1);
        assert_eq!(metrics.acknowledged_records, 1);
        assert!(!metrics.terminal);
    }

    #[test]
    fn every_relay_authority_io_failure_retries_or_stops_on_restart_required_state() {
        let (baseline_authority, baseline_vfs) = simulated_authority();
        let baseline_relay = start_relay(Arc::clone(&baseline_authority), false);
        wait_for_relay_outcome(&baseline_relay);
        baseline_relay
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        let operation_count = baseline_vfs.trace().unwrap().len() as u64;

        for operation_number in 1..=operation_count {
            let (authority, vfs) = simulated_authority();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let relay = start_relay(Arc::clone(&authority), false);
            wait_for_relay_outcome(&relay);
            let metrics = relay.metrics();
            if metrics.terminal {
                assert!(authority.lock().unwrap().is_restart_required());
                assert!(relay.terminal_error().is_some());
            } else {
                assert_eq!(metrics.acknowledged_records, 1);
            }
            relay
                .shutdown_until(Instant::now() + Duration::from_secs(1))
                .unwrap();
        }
    }

    #[test]
    fn concrete_authority_relay_and_engine_sink_reopen_at_the_same_prefix() {
        let directory = tempfile::tempdir().unwrap();
        let authority_path = directory.path().join("authority");
        let sink_path = directory.path().join("sink");
        let mut database = AuthorityDatabase::initialize(&authority_path).unwrap();
        database
            .configure_logical_outbox(&[44; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        commit_outbox(&mut database, 1);
        commit_outbox(&mut database, 2);
        let authority = Arc::new(Mutex::new(database));
        let sink = EngineLogicalOutboxSink::initialize(&sink_path, 7, 11, 1024 * 1024).unwrap();
        let worker =
            LogicalOutboxWorker::start(sink, LogicalOutboxWorkerConfig::default()).unwrap();
        let relay = LogicalOutboxRelay::start(
            Arc::clone(&authority),
            worker,
            7,
            11,
            LogicalOutboxRelayConfig {
                poll_interval: Duration::from_millis(5),
                operation_timeout: Duration::from_secs(1),
            },
        )
        .unwrap();
        wait_for_acknowledgements(&relay, 2);
        let (sink, _, _, metrics) = relay
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(sink.status().durable_sequence, 2);
        assert_eq!(metrics.acknowledged_records, 2);
        drop(sink);
        drop(authority);

        let mut reopened_authority = AuthorityDatabase::open(&authority_path).unwrap();
        reopened_authority
            .configure_logical_outbox(&[44; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            reopened_authority
                .logical_outbox_status(7, 11)
                .unwrap()
                .published_sequence,
            2
        );
        let reopened_sink = EngineLogicalOutboxSink::open(&sink_path, 7, 11, 1024 * 1024).unwrap();
        assert_eq!(reopened_sink.status().durable_sequence, 2);
    }

    #[test]
    fn multiplexed_engine_sink_drains_two_authorities_and_reopens_without_scanning() {
        let directory = tempfile::tempdir().unwrap();
        let authority_path = directory.path().join("authority");
        let sink_path = directory.path().join("sink");
        let hot = LogicalOutboxOwnerScope::new(7, 11).unwrap();
        let quiet = LogicalOutboxOwnerScope::new(7, 12).unwrap();
        let sink_config = MultiplexedEngineLogicalOutboxSinkConfig {
            administrative_scope: LogicalOutboxOwnerScope::new(99, 101).unwrap(),
            source_scopes: vec![hot, quiet],
            max_logical_bytes: 1024 * 1024,
        };
        let mut database = AuthorityDatabase::initialize(&authority_path).unwrap();
        database
            .configure_logical_outbox(&[47; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        commit_outbox_scope(&mut database, hot.tenant_id, hot.owner_user_id, 1);
        commit_outbox_scope(&mut database, hot.tenant_id, hot.owner_user_id, 2);
        commit_outbox_scope(&mut database, quiet.tenant_id, quiet.owner_user_id, 1);
        let authority = Arc::new(Mutex::new(database));
        let sink = MultiplexedEngineLogicalOutboxSink::initialize(&sink_path, sink_config.clone())
            .unwrap();
        let worker =
            LogicalOutboxWorker::start(sink, LogicalOutboxWorkerConfig::default()).unwrap();
        let relay = LogicalOutboxRelay::start_scopes(
            Arc::clone(&authority),
            worker,
            vec![hot, quiet],
            LogicalOutboxRelayConfig {
                poll_interval: Duration::from_millis(5),
                operation_timeout: Duration::from_secs(1),
            },
        )
        .unwrap();
        wait_for_acknowledgements(&relay, 3);
        let (sink, _, worker_metrics, relay_metrics) = relay
            .shutdown_until(Instant::now() + Duration::from_secs(1))
            .unwrap();
        assert_eq!(sink.status().active_owner_scopes, 2);
        assert_eq!(sink.status().durable_records, 3);
        assert_eq!(worker_metrics.completed_batches, 2);
        assert_eq!(relay_metrics.acknowledged_records, 3);
        drop(sink);
        drop(authority);

        let reopened = MultiplexedEngineLogicalOutboxSink::open(&sink_path, sink_config).unwrap();
        assert_eq!(reopened.status().active_owner_scopes, 2);
        assert_eq!(reopened.status().durable_records, 3);
    }
}
