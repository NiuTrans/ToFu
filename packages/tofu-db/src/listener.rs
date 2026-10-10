//! Demand-created numeric-loopback acceptor for authenticated storage.v2 connections.
//!
//! The accept loop owns no storage semantics. It enforces network locality,
//! socket and connection lifetimes, then delegates each admitted stream to the
//! storage.v2 connection loop while locking the authority for one request only.

use std::io::{self, Read};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::authority::AuthorityDatabase;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::generated_storage_v2::{
    MAX_ACCEPT_POLL_MILLISECONDS, MAX_CONNECTION_REUSE_TARGET, MAX_CONNECTION_STACK_BYTES,
    MAX_IO_TIMEOUT_MILLISECONDS, MAX_SESSION_IDLE_TIMEOUT_MILLISECONDS, MIN_CONNECTION_STACK_BYTES,
};
use crate::server::{
    serve_connection_until, ConnectionMetrics, FrameAdmissionBudget, StorageV2Authenticator,
    StorageV2Stores, DEFAULT_SESSION_IDLE_TIMEOUT,
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
    /// Initial worker-registry allocation hint, never an active count quota.
    pub initial_connection_reservations: usize,
    /// Mid-frame socket I/O bound: once the first byte of a frame arrives the
    /// rest must land inside this budget, bounding a wedged peer. Between
    /// frames the same socket timeout marks an *idle* session instead.
    pub io_timeout: Duration,
    /// Budget for retiring a session that stays idle between frames. Healthy
    /// pooled client sessions idle far longer than `io_timeout`, so idle
    /// waits re-arm until this budget lapses; the close then happens at a
    /// frame boundary, where the peer observes a clean EOF.
    pub idle_timeout: Duration,
    pub accept_poll_interval: Duration,
    pub connection_stack_bytes: usize,
}

impl Default for LoopbackListenerConfig {
    fn default() -> Self {
        Self {
            initial_connection_reservations: MAX_CONNECTION_REUSE_TARGET,
            io_timeout: Duration::from_secs(30),
            idle_timeout: DEFAULT_SESSION_IDLE_TIMEOUT,
            accept_poll_interval: Duration::from_millis(25),
            connection_stack_bytes: 1024 * 1024,
        }
    }
}

impl LoopbackListenerConfig {
    fn validate(self) -> io::Result<Self> {
        if self.initial_connection_reservations == 0 || self.initial_connection_reservations > MAX_CONNECTION_REUSE_TARGET {
            return Err(invalid_input("invalid storage.v2 connection capacity"));
        }
        if self.io_timeout.is_zero()
            || self.io_timeout > Duration::from_millis(MAX_IO_TIMEOUT_MILLISECONDS)
        {
            return Err(invalid_input("invalid storage.v2 socket I/O timeout"));
        }
        if self.idle_timeout < self.io_timeout
            || self.idle_timeout
                > Duration::from_millis(MAX_SESSION_IDLE_TIMEOUT_MILLISECONDS)
        {
            return Err(invalid_input("invalid storage.v2 session idle timeout"));
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

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
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
        let listener = bind_with_port_recovery(
            address,
            TcpListener::bind,
            || {
                #[cfg(target_os = "linux")]
                {
                    std::fs::read_to_string("/proc/sys/net/ipv4/ip_local_port_range")
                        .ok()
                        .and_then(|value| parse_ephemeral_port_range(&value))
                }
                #[cfg(not(target_os = "linux"))]
                { None }
            },
            || uuid::Uuid::new_v4().as_u128() as u32,
        )?;
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
            None,
            crate::control_ops::ControlObservers::default(),
            std::sync::Arc::new(crate::rotation_barrier::RotationBarrier::new()),
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
        wal_barrier: Option<crate::wal::WalBarrier>,
        control_observers: crate::control_ops::ControlObservers,
        rotation_barrier: std::sync::Arc<crate::rotation_barrier::RotationBarrier>,
        parent_lease_alive: impl FnMut() -> io::Result<bool>,
    ) -> io::Result<LoopbackListenerMetrics> {
        self.run_with_published_queries(database, search_projection, authenticator, frame_budget, authority_gc_budget, wal_barrier, control_observers, rotation_barrier, None, parent_lease_alive)
    }

    pub(crate) fn run_with_published_queries(
        self,
        database: Arc<Mutex<AuthorityDatabase>>,
        search_projection: Option<Arc<Mutex<TurnSearchProjection>>>,
        authenticator: Arc<StorageV2Authenticator>,
        frame_budget: FrameAdmissionBudget,
        authority_gc_budget: AuthorityGarbageCollectionBudget,
        wal_barrier: Option<crate::wal::WalBarrier>,
        control_observers: crate::control_ops::ControlObservers,
        rotation_barrier: std::sync::Arc<crate::rotation_barrier::RotationBarrier>,
        published_queries: Option<crate::authority::PublishedQueryView>,
        mut parent_lease_alive: impl FnMut() -> io::Result<bool>,
    ) -> io::Result<LoopbackListenerMetrics> {
        let mut workers = Vec::with_capacity(self.config.initial_connection_reservations);
        let mut terminal_error = None;
        let stopping = Arc::new(AtomicBool::new(false));
        let authority_admission = Arc::new(crate::authority_admission::AuthorityAdmission::default());
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
            // Release reconstructible burst registry storage once every
            // owned connection has finished; active workers are never evicted.
            if workers.is_empty() && workers.capacity() > self.config.initial_connection_reservations {
                workers.shrink_to(self.config.initial_connection_reservations);
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
                    let ownership = match ConnectionOwnership::acquire(&self.shared) {
                        Ok(ownership) => ownership,
                        Err(error) => {
                            terminal_error = Some(error);
                            break;
                        }
                    };
                    if let Err(error) = configure_stream(&stream, self.config.io_timeout) {
                        terminal_error = Some(error);
                        break;
                    }
                    // Share the socket without duplicating its descriptor. The
                    // acceptor retains a read-shutdown handle until the worker
                    // exits, so parent EOF can interrupt idle/partial frames.
                    let socket = Arc::new(stream);
                    let worker_socket = Arc::clone(&socket);
                    let idle_timeout = self.config.idle_timeout;
                    let database = Arc::clone(&database);
                    let authority_admission = Arc::clone(&authority_admission);
                    let published_queries = published_queries.clone();
                    let search_projection = search_projection.clone();
                    let authenticator = Arc::clone(&authenticator);
                    let frame_budget = frame_budget.clone();
                    let wal_barrier = wal_barrier.clone();
                    let control_observers = control_observers.clone();
                    let rotation_barrier = Arc::clone(&rotation_barrier);
                    let connection_stopping = Arc::clone(&stopping);
                    let worker = thread::Builder::new()
                        .name("tofu-db-v2-connection".to_owned())
                        .stack_size(self.config.connection_stack_bytes)
                        .spawn(move || {
                            let mut reader = worker_socket.as_ref();
                            let mut writer = worker_socket.as_ref();
                            let result = serve_connection_until(
                                &mut reader,
                                &mut writer,
                                {
                                    let stores = StorageV2Stores::new(
                                        &database,
                                        search_projection.as_deref(),
                                    )
                                    .with_authority_gc_budget(authority_gc_budget)
                                    .with_authority_admission(authority_admission)
                                    .with_control_observers(control_observers)
                                    .with_rotation_barrier(rotation_barrier);
                                    let stores = match published_queries { Some(view) => stores.with_published_queries(view), None => stores };
                                    match &wal_barrier {
                                        Some(barrier) => stores.with_wal_barrier(barrier.clone()),
                                        None => stores,
                                    }
                                },
                                &authenticator,
                                &frame_budget,
                                idle_timeout,
                                current_unix_milliseconds,
                                || !connection_stopping.load(Ordering::Acquire),
                            );
                            ownership.finish(result);
                        });
                    match worker {
                        Ok(worker) => workers.push(ConnectionWorker { socket, worker }),
                        Err(error) => {
                            terminal_error = Some(error);
                            break;
                        }
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    if let Err(error) =
                        wait_for_connection(&self.listener, self.config.accept_poll_interval)
                    {
                        terminal_error = Some(error);
                        break;
                    }
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
        // Waking a blocked read does not interrupt an admitted commit or its
        // response write. Every worker still joins before authority teardown.
        for connection in &workers {
            if let Err(error) = connection.socket.shutdown(Shutdown::Read) {
                if error.kind() != io::ErrorKind::NotConnected && terminal_error.is_none() {
                    terminal_error = Some(error);
                }
            }
        }
        for connection in workers {
            if connection.worker.join().is_err() && terminal_error.is_none() {
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

// Readiness wakes immediately for new connections. The timeout only bounds
// parent-lease observation; it must not become a per-request latency floor.
#[cfg(unix)]
fn wait_for_connection(listener: &TcpListener, timeout: Duration) -> io::Result<()> {
    use std::os::fd::AsRawFd;
    let mut descriptor = libc::pollfd {
        fd: listener.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let milliseconds = timeout.as_millis().max(1).min(i32::MAX as u128) as i32;
    // SAFETY: one initialized pollfd, live borrowed socket, bounded timeout.
    let result = unsafe { libc::poll(&mut descriptor, 1, milliseconds) };
    if result < 0 {
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
    Ok(())
}

#[cfg(not(unix))]
fn wait_for_connection(_listener: &TcpListener, timeout: Duration) -> io::Result<()> {
    thread::sleep(timeout);
    Ok(())
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

struct ConnectionWorker {
    socket: Arc<TcpStream>,
    worker: JoinHandle<()>,
}

fn reap_finished_workers(workers: &mut Vec<ConnectionWorker>) -> io::Result<()> {
    let mut index = 0;
    while index < workers.len() {
        if workers[index].worker.is_finished() {
            let worker = workers.swap_remove(index);
            worker.worker
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

struct ConnectionOwnership {
    shared: Arc<Mutex<ListenerState>>,
    finished: bool,
}

impl ConnectionOwnership {
    fn acquire(shared: &Arc<Mutex<ListenerState>>) -> io::Result<Self> {
        let mut state = shared
            .lock()
            .map_err(|_| io::Error::other("storage.v2 listener metrics are poisoned"))?;
        state.metrics.active_connections += 1;
        state.metrics.accepted_connections = state.metrics.accepted_connections.saturating_add(1);
        state.metrics.peak_active_connections = state
            .metrics
            .peak_active_connections
            .max(state.metrics.active_connections);
        drop(state);
        Ok(Self {
            shared: Arc::clone(shared),
            finished: false,
        })
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

impl Drop for ConnectionOwnership {
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

const MAX_PORT_RECOVERY_ATTEMPTS: u32 = 128;

fn parse_ephemeral_port_range(value: &str) -> Option<(u16, u16)> {
    let mut parts = value.split_whitespace();
    let low: u16 = parts.next()?.parse().ok()?;
    let high: u16 = parts.next()?.parse().ok()?;
    (low >= 1024 && low <= high && parts.next().is_none()).then_some((low, high))
}

/// An occupied outbound ephemeral range need not occupy loopback server ports.
/// Retain the first successfully bound socket: there is no probe/rebind race.
fn bind_with_port_recovery<T>(
    address: SocketAddr,
    mut bind: impl FnMut(SocketAddr) -> io::Result<T>,
    range: impl FnOnce() -> Option<(u16, u16)>,
    seed: impl FnOnce() -> u32,
) -> io::Result<T> {
    let first_error = match bind(address) {
        Ok(listener) => return Ok(listener),
        Err(error) if address.port() == 0 && error.kind() == io::ErrorKind::AddrInUse => error,
        Err(error) => return Err(error),
    };
    let Some((low, high)) = range() else {
        return Err(first_error);
    };
    if low < 1024 || low > high {
        return Err(first_error);
    }
    let below = u32::from(low) - 1024;
    let available = below + u32::from(u16::MAX - high);
    if available == 0 {
        return Err(first_error);
    }
    let start = seed() % available;
    for offset in 0..MAX_PORT_RECOVERY_ATTEMPTS.min(available) {
        let rank = (start + offset) % available;
        let port = if rank < below {
            1024 + rank
        } else {
            u32::from(high) + 1 + rank - below
        };
        let candidate = SocketAddr::new(address.ip(), port as u16);
        match bind(candidate) {
            Ok(listener) => {
                eprintln!(
                    "{{\"event\":\"loopback_port_recovery\",\"port\":{port},\"attempts\":{}}}",
                    offset + 1
                );
                return Ok(listener);
            }
            Err(error) if error.kind() == io::ErrorKind::AddrInUse => continue,
            Err(error) => return Err(error),
        }
    }
    Err(first_error)
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
    use std::io::Write;
    use std::net::{IpAddr, Ipv4Addr};
    use std::sync::atomic::{AtomicBool, Ordering};

    #[test]
    fn automatic_port_shortage_recovers_outside_the_kernel_range() {
        for range in [(32768, 60999), (1024, 65533), (1026, 65535)] {
            for seed in [0, 1, u32::MAX] {
                let mut calls = Vec::new();
                let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0);
                let chosen = bind_with_port_recovery(
                    address,
                    |candidate| {
                        calls.push(candidate);
                        if candidate.port() == 0 {
                            Err(io::ErrorKind::AddrInUse.into())
                        } else {
                            Ok(candidate)
                        }
                    },
                    || Some(range),
                    || seed,
                )
                .unwrap();
                assert_eq!(chosen.ip(), address.ip());
                assert!(chosen.port() >= 1024);
                assert!(chosen.port() < range.0 || chosen.port() > range.1);
                assert_eq!(calls.len(), 2);
                assert_eq!(calls.last(), Some(&chosen));
            }
        }
    }

    #[test]
    fn successful_or_explicit_port_binding_never_reads_a_recovery_policy() {
        let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0);
        assert_eq!(
            bind_with_port_recovery(address, |_| Ok(17), || panic!(), || panic!()).unwrap(),
            17
        );
        let explicit = SocketAddr::new(address.ip(), 12000);
        let error = bind_with_port_recovery::<()>(
            explicit,
            |_| Err(io::Error::new(io::ErrorKind::AddrInUse, "explicit owner")),
            || panic!(),
            || panic!(),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::AddrInUse);
        assert_eq!(error.to_string(), "explicit owner");
        for code in [13, 24, 99] {
            let error = bind_with_port_recovery::<()>(
                address,
                |_| Err(io::Error::from_raw_os_error(code)),
                || panic!(),
                || panic!(),
            )
            .unwrap_err();
            assert_eq!(error.raw_os_error(), Some(code));
        }
    }

    #[test]
    fn port_recovery_is_bounded_unique_and_never_steals_an_occupied_listener() {
        let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0);
        for range in [(32768, 60999), (1024, 65533), (1026, 65535), (1024, 65535)] {
            let mut calls = Vec::new();
            let error = bind_with_port_recovery::<()>(
                address,
                |candidate| {
                    calls.push(candidate);
                    Err(io::Error::new(io::ErrorKind::AddrInUse, "occupied"))
                },
                || Some(range),
                || u32::MAX,
            )
            .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::AddrInUse);
            assert_eq!(error.to_string(), "occupied");
            assert!(calls.len() <= 1 + MAX_PORT_RECOVERY_ATTEMPTS as usize);
            let mut distinct = calls.clone();
            distinct.sort();
            distinct.dedup();
            assert_eq!(distinct.len(), calls.len());
            for candidate in calls.iter().skip(1) {
                assert!(candidate.port() >= 1024);
                assert!(candidate.port() < range.0 || candidate.port() > range.1);
            }
        }
    }

    #[test]
    fn port_recovery_preserves_non_collision_errors_and_unknown_os_policy() {
        let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0);
        let mut calls = 0;
        let error = bind_with_port_recovery::<()>(
            address,
            |_| {
                calls += 1;
                Err(io::Error::from(if calls == 1 {
                    io::ErrorKind::AddrInUse
                } else {
                    io::ErrorKind::PermissionDenied
                }))
            },
            || Some((32768, 60999)),
            || 0,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(calls, 2);
        assert_eq!(
            bind_with_port_recovery::<()>(
                address,
                |_| Err(io::ErrorKind::AddrInUse.into()),
                || None,
                || panic!()
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::AddrInUse
        );
        assert_eq!(
            parse_ephemeral_port_range("32768\t60999\n"),
            Some((32768, 60999))
        );
        for invalid in [
            "",
            "0 65535",
            "65535 1024",
            "32768",
            "32768 60999 1",
            "32768 65536",
        ] {
            assert_eq!(parse_ephemeral_port_range(invalid), None);
        }
    }

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
            initial_connection_reservations: 2,
            io_timeout: Duration::from_millis(200),
            idle_timeout: Duration::from_secs(2),
            accept_poll_interval: Duration::from_millis(2),
            connection_stack_bytes: 1024 * 1024,
        }
    }

    fn egress_request() -> Message {
        Message::Request(Request {
            correlation_id: [2; 16],
            deadline_unix_ms: current_unix_milliseconds() + 5_000,
            owner_id: 11,
            tenant_id: Some(7),
            command_id: None,
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: "desktop.egress_agent.get".to_owned(),
            payload: serde_json::to_vec(&json!({"owner_user_id": 11})).unwrap(),
        })
    }

    fn start_server(
        config: LoopbackListenerConfig,
        directory: &tempfile::TempDir,
    ) -> (
        SocketAddr,
        Arc<AtomicBool>,
        JoinHandle<io::Result<LoopbackListenerMetrics>>,
    ) {
        start_server_with_frame_budget(config, directory, FrameAdmissionBudget::maximum())
    }

    fn start_server_with_frame_budget(
        config: LoopbackListenerConfig,
        directory: &tempfile::TempDir,
        frame_budget: FrameAdmissionBudget,
    ) -> (
        SocketAddr,
        Arc<AtomicBool>,
        JoinHandle<io::Result<LoopbackListenerMetrics>>,
    ) {
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
            config,
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
                frame_budget,
                parent_lease,
            )
        });
        (address, parent_alive, worker)
    }

    fn hello_acknowledged(client: &mut TcpStream) {
        write_frame(client, &hello()).unwrap();
        let Message::Response(acknowledgement) = read_frame(client).unwrap() else {
            panic!("listener did not acknowledge Hello");
        };
        assert_eq!(acknowledgement.status, 0);
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

    #[cfg(unix)]
    #[test]
    fn accept_readiness_wakes_before_lease_poll_deadline() {
        let listener = LoopbackServer::bind(
            SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0), config(),
        ).unwrap().listener;
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let connector = thread::spawn(move || {
            thread::sleep(Duration::from_millis(30));
            TcpStream::connect(address).unwrap()
        });
        let started = std::time::Instant::now();
        wait_for_connection(&listener, Duration::from_secs(1)).unwrap();
        assert!(started.elapsed() < Duration::from_millis(500));
        listener.accept().unwrap();
        connector.join().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn idle_accept_readiness_returns_for_parent_lease_check() {
        let listener = LoopbackServer::bind(
            SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0), config(),
        ).unwrap().listener;
        let started = std::time::Instant::now();
        wait_for_connection(&listener, Duration::from_millis(20)).unwrap();
        assert!(started.elapsed() >= Duration::from_millis(15));
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn partial_request_frames_exceed_retired_count_without_blocking_another_query() {
        let directory = tempfile::tempdir().unwrap();
        let budget = FrameAdmissionBudget::maximum();
        let mut settings = config();
        settings.initial_connection_reservations = 1;
        settings.io_timeout = Duration::from_secs(5);
        settings.idle_timeout = Duration::from_secs(60);
        let (address, parent_alive, worker) = start_server_with_frame_budget(
            settings, &directory, budget.clone());
        let mut clients = Vec::new();
        let count = 128;
        let started = std::time::Instant::now();
        let mut frame = Vec::new();
        write_frame(&mut frame, &egress_request()).unwrap();
        let mut held_metrics = None;
        let outcome = (|| -> io::Result<()> {
            for _ in 0..count {
                let client = TcpStream::connect(address)?;
                client.set_read_timeout(Some(Duration::from_secs(5)))?;
                clients.push(client);
                let client = clients.last_mut().unwrap();
                write_frame(client, &hello())?;
                match read_frame(client)? {
                    Message::Response(ack) if ack.status == 0 => {},
                    _ => return Err(io::Error::other("missing Hello acknowledgement")),
                }
            }
            for client in &mut clients {
                client.write_all(&frame[..5])?;
            }
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            loop {
                let metrics = budget.metrics()?;
                if metrics.in_flight_frames == count {
                    held_metrics = Some(metrics);
                    break;
                }
                if std::time::Instant::now() >= deadline {
                    return Err(io::Error::other(format!("partial frame reservations did not enter: {metrics:?}")));
                }
                thread::sleep(Duration::from_millis(1));
            }
            let client = TcpStream::connect(address)?;
            client.set_read_timeout(Some(Duration::from_secs(5)))?;
            clients.push(client);
            let observer = clients.last_mut().unwrap();
            write_frame(observer, &hello())?;
            match read_frame(observer)? {
                Message::Response(ack) if ack.status == 0 => {},
                _ => return Err(io::Error::other("observer Hello refused")),
            }
            write_frame(observer, &egress_request())?;
            match read_frame(observer)? {
                Message::Response(response) if response.status == 0 => {},
                _ => return Err(io::Error::other("unrelated query refused")),
            }
            for client in clients.iter_mut().take(count) {
                client.write_all(&frame[5..])?;
                match read_frame(client)? {
                    Message::Response(response) if response.status == 0 => {},
                    _ => return Err(io::Error::other("held request failed")),
                }
            }
            Ok(())
        })();
        parent_alive.store(false, Ordering::Release);
        drop(clients);
        let metrics = worker.join().unwrap().unwrap();
        outcome.unwrap();
        let held = held_metrics.unwrap();
        assert_eq!(held.in_flight_frames, count);
        assert_eq!(held.in_flight_bytes, (frame.len() - 8) * count);
        assert_eq!(metrics.request_frames, (count + 1) as u64);
        assert_eq!(metrics.rejected_connections, 0);
        assert_eq!(metrics.failed_connections, 0);
        assert_eq!(metrics.active_connections, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
        eprintln!("native-frame-burst held_frames={} held_bytes={} settled_ms={:.2}", held.in_flight_frames, held.in_flight_bytes, started.elapsed().as_secs_f64()*1000.0);
    }

    #[test]
    fn real_authenticated_burst_exceeds_initial_reservations_and_joins_all_workers() {
        let directory = tempfile::tempdir().unwrap();
        let mut settings = config();
        settings.initial_connection_reservations = 1;
        settings.idle_timeout = Duration::from_secs(60);
        let budget = FrameAdmissionBudget::new(crate::generated_storage_v2::MAX_FRAME_BODY_BYTES).unwrap();
        let held = budget.plan_frame(crate::generated_storage_v2::MAX_FRAME_BODY_BYTES).unwrap();
        let (address, parent_alive, worker) = start_server_with_frame_budget(settings, &directory, budget.clone());
        let mut clients = Vec::new();
        let started = std::time::Instant::now();
        let outcome = (|| -> io::Result<()> {
            for _ in 0..64 {
                let client = TcpStream::connect(address)?;
                client.set_read_timeout(Some(Duration::from_secs(5)))?;
                clients.push(client);
                let client = clients.last_mut().unwrap();
                write_frame(client, &hello())?;
                let Message::Response(ack) = read_frame(client)? else {
                    return Err(io::Error::other("missing Hello acknowledgement"));
                };
                if ack.status != 0 {
                    return Err(io::Error::other("Hello was refused"));
                }
                write_frame(client, &egress_request())?;
                let Message::Response(response) = read_frame(client)? else {
                    return Err(io::Error::other("missing semantic response"));
                };
                if response.status != 0 {
                    return Err(io::Error::other("semantic request was refused"));
                }
            }
            Ok(())
        })();
        let entered = started.elapsed();
        parent_alive.store(false, Ordering::Release);
        drop(clients);
        let metrics = worker.join().unwrap().unwrap();
        outcome.unwrap();
        assert_eq!(metrics.accepted_connections, 64);
        assert_eq!(metrics.peak_active_connections, 64);
        assert_eq!(metrics.rejected_connections, 0);
        assert_eq!(metrics.active_connections, 0);
        assert_eq!(metrics.request_frames, 64);
        assert_eq!(metrics.failed_connections, 0);
        assert_eq!(metrics.completed_connections, 64);
        assert_eq!(budget.metrics().unwrap().file_buffer_bytes, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 1);
        drop(held);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
        eprintln!("native-demand-burst connections=64 initial_reservations=1 memory_threshold_held=true entered_ms={:.2} joined_ms={:.2}", entered.as_secs_f64()*1000.0, started.elapsed().as_secs_f64()*1000.0);
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
            LoopbackListenerConfig {
                io_timeout: Duration::from_secs(30),
                idle_timeout: Duration::from_secs(60),
                ..config()
            },
        )
        .unwrap();
        let address = server.local_addr().unwrap();
        let parent_alive = Arc::new(AtomicBool::new(true));
        let parent_lease = ParentLease::new(TestParentLease {
            alive: Arc::clone(&parent_alive),
        });
        let (stopped_tx, stopped_rx) = std::sync::mpsc::channel();
        let worker = thread::spawn(move || {
            let result = server.run_with_parent_lease(
                database,
                authenticator,
                FrameAdmissionBudget::maximum(),
                parent_lease,
            );
            let _ = stopped_tx.send(());
            result
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
        let stopped = stopped_rx.recv_timeout(Duration::from_secs(2));
        drop(client); // Always unblock cleanup, including a regression failure.
        let metrics = worker.join().unwrap().unwrap();
        assert!(stopped.is_ok(), "parent EOF waited for the peer's I/O timeout");
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
    fn idle_session_survives_mid_frame_io_timeout_within_idle_budget() {
        // io_timeout = 200 ms, idle_timeout = 2 s: a healthy pooled session
        // idling 500 ms (> 2× the mid-frame bound) must stay connected.
        let directory = tempfile::tempdir().unwrap();
        let (address, parent_alive, worker) = start_server(config(), &directory);
        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        hello_acknowledged(&mut client);

        thread::sleep(Duration::from_millis(500));
        write_frame(&mut client, &egress_request()).unwrap();
        let Message::Response(response) = read_frame(&mut client).unwrap() else {
            panic!("listener retired an idle session inside the idle budget");
        };
        assert_eq!(response.status, 0);

        parent_alive.store(false, Ordering::Release);
        let metrics = worker.join().unwrap().unwrap();
        drop(client);
        assert_eq!(metrics.completed_connections, 1);
        assert_eq!(metrics.failed_connections, 0);
        assert_eq!(metrics.request_frames, 1);
    }

    #[test]
    fn idle_session_is_retired_at_a_frame_boundary_after_the_idle_budget() {
        // io_timeout = 100 ms, idle_timeout = 300 ms: after 600 ms idle the
        // daemon retires the session with a clean EOF, never a reset.
        let directory = tempfile::tempdir().unwrap();
        let mut configuration = config();
        configuration.io_timeout = Duration::from_millis(100);
        configuration.idle_timeout = Duration::from_millis(300);
        let (address, parent_alive, worker) = start_server(configuration, &directory);
        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        hello_acknowledged(&mut client);

        thread::sleep(Duration::from_millis(600));
        let mut byte = [0_u8; 1];
        assert_eq!(
            client.read(&mut byte).unwrap(),
            0,
            "idle-retired session must observe a clean EOF at a frame boundary"
        );

        parent_alive.store(false, Ordering::Release);
        let metrics = worker.join().unwrap().unwrap();
        drop(client);
        assert_eq!(metrics.completed_connections, 1);
        assert_eq!(metrics.failed_connections, 0);
    }

    #[test]
    fn mid_frame_stall_is_still_bounded_by_the_io_timeout() {
        // The idle policy must not widen the wedged-peer bound: once the
        // first byte of a frame arrives, the rest is owed within io_timeout.
        let directory = tempfile::tempdir().unwrap();
        let mut configuration = config();
        configuration.io_timeout = Duration::from_millis(100);
        let (address, parent_alive, worker) = start_server(configuration, &directory);
        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        hello_acknowledged(&mut client);

        // Two bytes of a four-byte length prefix, then silence: a wedged peer.
        client.write_all(&[0_u8, 0]).unwrap();
        thread::sleep(Duration::from_millis(400));
        let mut byte = [0_u8; 1];
        let closed = match client.read(&mut byte) {
            Ok(0) => true,
            Ok(_) => false,
            Err(_) => true,
        };
        assert!(closed, "mid-frame stall must kill the connection");

        parent_alive.store(false, Ordering::Release);
        let metrics = worker.join().unwrap().unwrap();
        drop(client);
        assert_eq!(metrics.failed_connections, 1);
        assert_eq!(metrics.completed_connections, 0);
    }

    #[test]
    fn idle_timeout_validation_rejects_inverted_or_unbounded_values() {
        let inverted = LoopbackListenerConfig {
            idle_timeout: Duration::from_millis(100),
            ..config()
        };
        assert_eq!(
            LoopbackServer::bind(
                SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
                inverted,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidInput
        );
        let unbounded = LoopbackListenerConfig {
            idle_timeout: Duration::from_millis(MAX_SESSION_IDLE_TIMEOUT_MILLISECONDS)
                + Duration::from_millis(1),
            ..config()
        };
        assert_eq!(
            LoopbackServer::bind(
                SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
                unbounded,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn connection_ownership_has_no_quota_and_releases_exactly_once() {
        let shared = Arc::new(Mutex::new(ListenerState::default()));
        let first = ConnectionOwnership::acquire(&shared).unwrap();
        let second = ConnectionOwnership::acquire(&shared).unwrap();
        drop(second);
        first.finish(Ok(ConnectionMetrics {
            request_frames: 3,
            response_frames: 4,
        }));
        let metrics = shared.lock().unwrap().metrics;
        assert_eq!(metrics.accepted_connections, 2);
        assert_eq!(metrics.rejected_connections, 0);
        assert_eq!(metrics.completed_connections, 1);
        assert_eq!(metrics.active_connections, 0);
        assert_eq!(metrics.request_frames, 3);
        assert_eq!(metrics.response_frames, 4);
    }
}
