//! Bounded numeric-loopback acceptor for authenticated storage.v2 connections.
//!
//! The accept loop owns no storage semantics. It enforces network locality,
//! socket and connection lifetimes, then delegates each admitted stream to the
//! storage.v2 connection loop while locking the authority for one request only.

use std::io::{self, Read};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::authority::AuthorityDatabase;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::generated_storage_v2::{
    MAX_ACCEPT_POLL_MILLISECONDS, MAX_CONNECTIONS, MAX_CONNECTION_STACK_BYTES,
    MAX_IO_TIMEOUT_MILLISECONDS, MIN_CONNECTION_STACK_BYTES,
};
use crate::server::{
    serve_connection_until, ConnectionMetrics, FrameAdmissionBudget, StorageV2Authenticator,
    StorageV2Stores,
};
use crate::turn_search_projection::TurnSearchProjection;

/// Read end of the inherited, nonblocking, empty parent-liveness pipe.
///
/// The parent keeps its write end open and never writes bytes. EOF is the only
/// normal shutdown signal, so credentials and control messages cannot leak
/// through this lifecycle channel.
#[derive(Debug)]
pub struct ParentLease<R> {
    reader: R,
}

impl<R: Read> ParentLease<R> {
    pub fn new(reader: R) -> Self {
        Self { reader }
    }

    pub fn poll_alive(&mut self) -> io::Result<bool> {
        let mut byte = [0_u8; 1];
        loop {
            match self.reader.read(&mut byte) {
                Ok(0) => return Ok(false),
                Ok(_) => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "storage.v2 parent lease pipe must remain empty",
                    ));
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(true),
                Err(error) => return Err(error),
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LoopbackListenerConfig {
    pub maximum_connections: usize,
    pub io_timeout: Duration,
    pub accept_poll_interval: Duration,
    pub connection_stack_bytes: usize,
}

impl Default for LoopbackListenerConfig {
    fn default() -> Self {
        Self {
            maximum_connections: MAX_CONNECTIONS,
            io_timeout: Duration::from_secs(30),
            accept_poll_interval: Duration::from_millis(25),
            connection_stack_bytes: 1024 * 1024,
        }
    }
}

impl LoopbackListenerConfig {
    fn validate(self) -> io::Result<Self> {
        if self.maximum_connections == 0 || self.maximum_connections > MAX_CONNECTIONS {
            return Err(invalid_input("invalid storage.v2 connection capacity"));
        }
        if self.io_timeout.is_zero()
            || self.io_timeout > Duration::from_millis(MAX_IO_TIMEOUT_MILLISECONDS)
        {
            return Err(invalid_input("invalid storage.v2 socket I/O timeout"));
        }
        if self.accept_poll_interval.is_zero()
            || self.accept_poll_interval > Duration::from_millis(MAX_ACCEPT_POLL_MILLISECONDS)
        {
            return Err(invalid_input("invalid storage.v2 accept poll interval"));
        }
        if !(MIN_CONNECTION_STACK_BYTES..=MAX_CONNECTION_STACK_BYTES)
            .contains(&self.connection_stack_bytes)
        {
            return Err(invalid_input("invalid storage.v2 connection stack size"));
        }
        Ok(self)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LoopbackListenerMetrics {
    pub accepted_connections: u64,
    pub rejected_connections: u64,
    pub completed_connections: u64,
    pub failed_connections: u64,
    pub active_connections: usize,
    pub peak_active_connections: usize,
    pub request_frames: u64,
    pub response_frames: u64,
}

#[derive(Debug, Default)]
struct ListenerState {
    metrics: LoopbackListenerMetrics,
}

#[derive(Clone, Debug)]
pub struct LoopbackListenerObserver {
    shared: Arc<Mutex<ListenerState>>,
}

impl LoopbackListenerObserver {
    pub fn metrics(&self) -> io::Result<LoopbackListenerMetrics> {
        self.shared
            .lock()
            .map(|state| state.metrics)
            .map_err(|_| io::Error::other("storage.v2 listener metrics are poisoned"))
    }
}

#[derive(Debug)]
pub struct LoopbackServer {
    listener: TcpListener,
    config: LoopbackListenerConfig,
    shared: Arc<Mutex<ListenerState>>,
}

impl LoopbackServer {
    pub fn bind(address: SocketAddr, config: LoopbackListenerConfig) -> io::Result<Self> {
        if !address.ip().is_loopback() {
            return Err(invalid_input(
                "storage.v2 listener requires a numeric loopback address",
            ));
        }
        let config = config.validate()?;
        let listener = TcpListener::bind(address)?;
        listener.set_nonblocking(true)?;
        Ok(Self {
            listener,
            config,
            shared: Arc::default(),
        })
    }

    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }

    pub fn observer(&self) -> LoopbackListenerObserver {
        LoopbackListenerObserver {
            shared: Arc::clone(&self.shared),
        }
    }

    pub fn run(
        self,
        database: Arc<Mutex<AuthorityDatabase>>,
        authenticator: Arc<StorageV2Authenticator>,
        frame_budget: FrameAdmissionBudget,
        parent_lease_alive: impl FnMut() -> io::Result<bool>,
    ) -> io::Result<LoopbackListenerMetrics> {
        self.run_with_search(
            database,
            None,
            authenticator,
            frame_budget,
            parent_lease_alive,
        )
    }

    pub fn run_with_search(
        self,
        database: Arc<Mutex<AuthorityDatabase>>,
        search_projection: Option<Arc<Mutex<TurnSearchProjection>>>,
        authenticator: Arc<StorageV2Authenticator>,
        frame_budget: FrameAdmissionBudget,
        parent_lease_alive: impl FnMut() -> io::Result<bool>,
    ) -> io::Result<LoopbackListenerMetrics> {
        self.run_with_search_and_gc_budget(
            database,
            search_projection,
            authenticator,
            frame_budget,
            AuthorityGarbageCollectionBudget::conservative(),
            parent_lease_alive,
        )
    }

    pub fn run_with_search_and_gc_budget(
        self,
        database: Arc<Mutex<AuthorityDatabase>>,
        search_projection: Option<Arc<Mutex<TurnSearchProjection>>>,
        authenticator: Arc<StorageV2Authenticator>,
        frame_budget: FrameAdmissionBudget,
        authority_gc_budget: AuthorityGarbageCollectionBudget,
        mut parent_lease_alive: impl FnMut() -> io::Result<bool>,
    ) -> io::Result<LoopbackListenerMetrics> {
        let mut workers = Vec::with_capacity(self.config.maximum_connections);
        let mut terminal_error = None;
        let stopping = Arc::new(AtomicBool::new(false));
        loop {
            match parent_lease_alive() {
                Ok(true) => {}
                Ok(false) => break,
                Err(error) => {
                    terminal_error = Some(error);
                    break;
                }
            }
            if let Err(error) = reap_finished_workers(&mut workers) {
                terminal_error = Some(error);
                break;
            }
            match self.listener.accept() {
                Ok((stream, peer)) => {
                    if !peer.ip().is_loopback() {
                        if let Err(error) = record_rejection(&self.shared) {
                            terminal_error = Some(error);
                            break;
                        }
                        continue;
                    }
                    let slot = match ConnectionSlot::try_acquire(
                        &self.shared,
                        self.config.maximum_connections,
                    ) {
                        Ok(Some(slot)) => slot,
                        Ok(None) => continue,
                        Err(error) => {
                            terminal_error = Some(error);
                            break;
                        }
                    };
                    if let Err(error) = configure_stream(&stream, self.config.io_timeout) {
                        terminal_error = Some(error);
                        break;
                    }
                    let mut reader = match stream.try_clone() {
                        Ok(reader) => reader,
                        Err(error) => {
                            terminal_error = Some(error);
                            break;
                        }
                    };
                    let mut writer = stream;
                    let database = Arc::clone(&database);
                    let search_projection = search_projection.clone();
                    let authenticator = Arc::clone(&authenticator);
                    let frame_budget = frame_budget.clone();
                    let connection_stopping = Arc::clone(&stopping);
                    let worker = thread::Builder::new()
                        .name("tofu-db-v2-connection".to_owned())
                        .stack_size(self.config.connection_stack_bytes)
                        .spawn(move || {
                            let result = serve_connection_until(
                                &mut reader,
                                &mut writer,
                                StorageV2Stores::new(&database, search_projection.as_deref())
                                    .with_authority_gc_budget(authority_gc_budget),
                                &authenticator,
                                &frame_budget,
                                current_unix_milliseconds,
                                || !connection_stopping.load(Ordering::Acquire),
                            );
                            slot.finish(result);
                        });
                    match worker {
                        Ok(worker) => workers.push(worker),
                        Err(error) => {
                            terminal_error = Some(error);
                            break;
                        }
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(self.config.accept_poll_interval);
                }
                Err(error) => {
                    terminal_error = Some(error);
                    break;
                }
            }
        }
        let observer = self.observer();
        drop(self.listener);
        stopping.store(true, Ordering::Release);
        for worker in workers {
            if worker.join().is_err() && terminal_error.is_none() {
                terminal_error = Some(io::Error::other("storage.v2 connection worker panicked"));
            }
        }
        if let Some(error) = terminal_error {
            return Err(error);
        }
        observer.metrics()
    }

    pub fn run_with_parent_lease<R: Read>(
        self,
        database: Arc<Mutex<AuthorityDatabase>>,
        authenticator: Arc<StorageV2Authenticator>,
        frame_budget: FrameAdmissionBudget,
        mut parent_lease: ParentLease<R>,
    ) -> io::Result<LoopbackListenerMetrics> {
        self.run(database, authenticator, frame_budget, || {
            parent_lease.poll_alive()
        })
    }
}

fn configure_stream(stream: &TcpStream, timeout: Duration) -> io::Result<()> {
    stream.set_nodelay(true)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))
}

fn current_unix_milliseconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|elapsed| u64::try_from(elapsed.as_millis()).ok())
        .unwrap_or(u64::MAX)
}

fn reap_finished_workers(workers: &mut Vec<JoinHandle<()>>) -> io::Result<()> {
    let mut index = 0;
    while index < workers.len() {
        if workers[index].is_finished() {
            let worker = workers.swap_remove(index);
            worker
                .join()
                .map_err(|_| io::Error::other("storage.v2 connection worker panicked"))?;
        } else {
            index += 1;
        }
    }
    Ok(())
}

fn record_rejection(shared: &Arc<Mutex<ListenerState>>) -> io::Result<()> {
    let mut state = shared
        .lock()
        .map_err(|_| io::Error::other("storage.v2 listener metrics are poisoned"))?;
    state.metrics.rejected_connections = state.metrics.rejected_connections.saturating_add(1);
    Ok(())
}

struct ConnectionSlot {
    shared: Arc<Mutex<ListenerState>>,
    finished: bool,
}

impl ConnectionSlot {
    fn try_acquire(
        shared: &Arc<Mutex<ListenerState>>,
        maximum_connections: usize,
    ) -> io::Result<Option<Self>> {
        let mut state = shared
            .lock()
            .map_err(|_| io::Error::other("storage.v2 listener metrics are poisoned"))?;
        if state.metrics.active_connections == maximum_connections {
            state.metrics.rejected_connections =
                state.metrics.rejected_connections.saturating_add(1);
            return Ok(None);
        }
        state.metrics.active_connections += 1;
        state.metrics.accepted_connections = state.metrics.accepted_connections.saturating_add(1);
        state.metrics.peak_active_connections = state
            .metrics
            .peak_active_connections
            .max(state.metrics.active_connections);
        drop(state);
        Ok(Some(Self {
            shared: Arc::clone(shared),
            finished: false,
        }))
    }

    fn finish(mut self, result: io::Result<ConnectionMetrics>) {
        if let Ok(mut state) = self.shared.lock() {
            state.metrics.active_connections = state.metrics.active_connections.saturating_sub(1);
            match result {
                Ok(connection) => {
                    state.metrics.completed_connections =
                        state.metrics.completed_connections.saturating_add(1);
                    state.metrics.request_frames = state
                        .metrics
                        .request_frames
                        .saturating_add(connection.request_frames);
                    state.metrics.response_frames = state
                        .metrics
                        .response_frames
                        .saturating_add(connection.response_frames);
                }
                Err(_) => {
                    state.metrics.failed_connections =
                        state.metrics.failed_connections.saturating_add(1);
                }
            }
            self.finished = true;
        }
    }
}

impl Drop for ConnectionSlot {
    fn drop(&mut self) {
        if self.finished {
            return;
        }
        let Ok(mut state) = self.shared.lock() else {
            return;
        };
        state.metrics.active_connections = state.metrics.active_connections.saturating_sub(1);
        state.metrics.failed_connections = state.metrics.failed_connections.saturating_add(1);
    }
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
    use crate::protocol::{read_frame, write_frame, Hello, Message, Request, PROTOCOL_VERSION};
    use crate::semantic::AuthenticatedScope;
    use serde_json::{json, Value};
    use std::net::{IpAddr, Ipv4Addr};
    use std::sync::atomic::{AtomicBool, Ordering};

    struct TestParentLease {
        alive: Arc<AtomicBool>,
    }

    impl Read for TestParentLease {
        fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
            if self.alive.load(Ordering::Acquire) {
                Err(io::Error::from(io::ErrorKind::WouldBlock))
            } else {
                Ok(0)
            }
        }
    }

    fn config() -> LoopbackListenerConfig {
        LoopbackListenerConfig {
            maximum_connections: 2,
            io_timeout: Duration::from_millis(200),
            accept_poll_interval: Duration::from_millis(2),
            connection_stack_bytes: 1024 * 1024,
        }
    }

    fn hello() -> Message {
        Message::Hello(Hello {
            correlation_id: [1; 16],
            minimum_version: PROTOCOL_VERSION,
            maximum_version: PROTOCOL_VERSION,
            schema_ids: vec![STORAGE_SCHEMA_VERSION],
            auth_token: [9; crate::protocol::AUTH_TOKEN_BYTES],
        })
    }

    #[test]
    fn bind_rejects_non_loopback_before_opening_a_socket() {
        let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 0);
        assert_eq!(
            LoopbackServer::bind(address, config()).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn real_loopback_acceptor_authenticates_executes_and_stops_with_parent_lease() {
        let directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(directory.path()).unwrap(),
        ));
        let authenticator = Arc::new(
            StorageV2Authenticator::new(
                &[9; crate::protocol::AUTH_TOKEN_BYTES],
                AuthenticatedScope {
                    owner_id: 11,
                    tenant_id: Some(7),
                },
            )
            .unwrap(),
        );
        let server = LoopbackServer::bind(
            SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
            config(),
        )
        .unwrap();
        let address = server.local_addr().unwrap();
        let parent_alive = Arc::new(AtomicBool::new(true));
        let parent_lease = ParentLease::new(TestParentLease {
            alive: Arc::clone(&parent_alive),
        });
        let worker = thread::spawn(move || {
            server.run_with_parent_lease(
                database,
                authenticator,
                FrameAdmissionBudget::maximum(),
                parent_lease,
            )
        });

        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        write_frame(&mut client, &hello()).unwrap();
        let Message::Response(acknowledgement) = read_frame(&mut client).unwrap() else {
            panic!("listener did not acknowledge Hello");
        };
        assert_eq!(acknowledgement.status, 0);
        let request = Message::Request(Request {
            correlation_id: [2; 16],
            deadline_unix_ms: current_unix_milliseconds() + 5_000,
            owner_id: 11,
            tenant_id: Some(7),
            command_id: None,
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: "desktop.egress_agent.get".to_owned(),
            payload: serde_json::to_vec(&json!({"owner_user_id": 11})).unwrap(),
        });
        write_frame(&mut client, &request).unwrap();
        let Message::Response(response) = read_frame(&mut client).unwrap() else {
            panic!("listener did not return a response");
        };
        assert_eq!(response.status, 0);
        assert_eq!(
            serde_json::from_slice::<Value>(&response.payload).unwrap(),
            json!({"present": false, "agent_id": "", "updated_at_ms": 0})
        );
        parent_alive.store(false, Ordering::Release);
        let metrics = worker.join().unwrap().unwrap();
        drop(client);
        assert_eq!(metrics.accepted_connections, 1);
        assert_eq!(metrics.completed_connections, 1);
        assert_eq!(metrics.failed_connections, 0);
        assert_eq!(metrics.active_connections, 0);
        assert_eq!(metrics.request_frames, 1);
        assert_eq!(metrics.response_frames, 2);
    }

    #[test]
    fn parent_lease_is_alive_only_while_empty_pipe_has_a_writer() {
        let alive = Arc::new(AtomicBool::new(true));
        let mut lease = ParentLease::new(TestParentLease {
            alive: Arc::clone(&alive),
        });
        assert!(lease.poll_alive().unwrap());
        alive.store(false, Ordering::Release);
        assert!(!lease.poll_alive().unwrap());

        let error = ParentLease::new(io::Cursor::new(vec![1_u8]))
            .poll_alive()
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn connection_slots_reject_capacity_and_release_exactly_once() {
        let shared = Arc::new(Mutex::new(ListenerState::default()));
        let first = ConnectionSlot::try_acquire(&shared, 1).unwrap().unwrap();
        assert!(ConnectionSlot::try_acquire(&shared, 1).unwrap().is_none());
        first.finish(Ok(ConnectionMetrics {
            request_frames: 3,
            response_frames: 4,
        }));
        let metrics = shared.lock().unwrap().metrics;
        assert_eq!(metrics.accepted_connections, 1);
        assert_eq!(metrics.rejected_connections, 1);
        assert_eq!(metrics.completed_connections, 1);
        assert_eq!(metrics.active_connections, 0);
        assert_eq!(metrics.request_frames, 3);
        assert_eq!(metrics.response_frames, 4);
    }
}
