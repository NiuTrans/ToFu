//! Authenticated storage.v2 session state and semantic dispatch.
//!
//! A transport authenticates its private connection token before constructing
//! this object. The session then fixes owner/tenant authority for its lifetime,
//! performs exactly one Hello negotiation, and is the only supported bridge
//! from decoded protocol requests to the semantic executor.

use std::fmt;
use std::io::{self, Read, Write};
use std::sync::{Arc, Mutex, MutexGuard, TryLockError};
use std::thread;
use std::time::{Duration, Instant};

use crate::authority::AuthorityDatabase;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::generated_storage_operations::{StorageOperationKind, STORAGE_SCHEMA_VERSION};
use crate::generated_storage_v2::{
    MAX_FRAME_BODY_BYTES, DEFAULT_FRAME_MEMORY_BYTES,
    MAX_STREAMED_REQUEST_CHUNKS, MAX_STREAMED_REQUEST_PAYLOAD_BYTES, MAX_STREAMED_RESPONSE_CHUNKS,
    MAX_STREAMED_RESPONSE_PAYLOAD_BYTES, STATUS_BAD_REQUEST, STATUS_CONFLICT,
    STATUS_DEADLINE_ELAPSED, STATUS_FORBIDDEN, STATUS_INTEGRITY_FAILURE, STATUS_NOT_IMPLEMENTED,
    STATUS_RESOURCE_EXHAUSTED, STATUS_UNAVAILABLE, STREAMED_RESPONSE_CHUNK_BYTES,
};
use crate::protocol::{
    read_frame_or_eof_with_admission, read_frame_or_eof_with_buffer_plan, read_frame_or_idle_with_buffer_plan,
    write_frame_with_buffer_plan, FrameBufferPlan, FrameRead, Hello, Message, NegotiatedProtocol, Request, Response,
    ResponseChunk,
};
use crate::semantic::{admit_request, admit_streamed_request, AdmissionError, AuthenticatedScope};
use crate::semantic_executor::{
    execute_admitted_request_with_notices, execute_search_admitted_request, SemanticExecutionError,
};
use crate::turn_search_projection::TurnSearchProjection;
#[path = "dispatch_response.rs"]
pub(crate) mod response;
use response::DispatchedResponse;

#[derive(Clone, Debug)]
pub struct FrameAdmissionBudget {
    shared: Arc<Mutex<FrameAdmissionState>>,
    maximum_bytes: usize,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FrameAdmissionMetrics {
    pub in_flight_frames: usize,
    pub in_flight_bytes: usize,
    pub peak_in_flight_frames: usize,
    pub peak_in_flight_bytes: usize,
    pub memory_buffer_bytes: usize,
    pub file_buffer_bytes: usize,
    pub owned_payload_bytes: usize,
}

#[derive(Debug, Default)]
struct FrameAdmissionState {
    metrics: FrameAdmissionMetrics,
}

#[derive(Debug)]
pub struct FrameAdmissionGuard {
    shared: Arc<Mutex<FrameAdmissionState>>,
    bytes: usize,
    storage: FrameStorage,
}

#[derive(Debug, Clone, Copy)]
enum FrameStorage { Memory, File, Payload }

impl FrameAdmissionBudget {
    pub fn new(maximum_bytes: usize) -> io::Result<Self> {
        if maximum_bytes < MAX_FRAME_BODY_BYTES
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid storage.v2 aggregate frame budget",
            ));
        }
        Ok(Self {
            shared: Arc::default(),
            maximum_bytes,
        })
    }

    pub fn maximum() -> Self {
        Self::new(DEFAULT_FRAME_MEMORY_BYTES)
            .expect("generated storage.v2 frame limits are valid")
    }

    pub fn admit(&self, bytes: usize) -> io::Result<FrameAdmissionGuard> {
        if bytes == 0 || bytes > MAX_FRAME_BODY_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage.v2 frame reservation is invalid",
            ));
        }
        self.admit_counted(bytes, FrameStorage::Memory)
    }

    pub fn plan_frame(&self, bytes: usize) -> io::Result<FrameBufferPlan<FrameAdmissionGuard>> {
        if bytes == 0 || bytes > MAX_FRAME_BODY_BYTES { return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid storage.v2 frame size")); }
        let mut state = self.shared.lock().map_err(|_| io::Error::other("storage.v2 frame budget is poisoned"))?;
        let spilled = state.metrics.memory_buffer_bytes.checked_add(bytes).is_none_or(|next| next > self.maximum_bytes);
        let storage = if spilled { FrameStorage::File } else { FrameStorage::Memory };
        let guard = self.admit_locked(&mut state, bytes, storage)?;
        Ok(FrameBufferPlan { guard, spilled })
    }

    fn admit_streamed_payload(&self, bytes: usize) -> io::Result<FrameAdmissionGuard> {
        if bytes == 0 || bytes > MAX_STREAMED_REQUEST_PAYLOAD_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage.v2 streamed payload reservation is invalid",
            ));
        }
        self.admit_counted(bytes, FrameStorage::Payload)
    }

    fn admit_counted(&self, bytes: usize, storage: FrameStorage) -> io::Result<FrameAdmissionGuard> {
        let mut state = self
            .shared
            .lock()
            .map_err(|_| io::Error::other("storage.v2 frame budget is poisoned"))?;
        self.admit_locked(&mut state, bytes, storage)
    }

    fn admit_locked(&self, state: &mut FrameAdmissionState, bytes: usize, storage: FrameStorage) -> io::Result<FrameAdmissionGuard> {
        let next_frames = state.metrics.in_flight_frames.checked_add(1)
            .ok_or_else(|| io::Error::other("storage.v2 frame count accounting overflow"))?;
        let next_bytes = state
            .metrics
            .in_flight_bytes
            .checked_add(bytes);
        if next_bytes.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "storage.v2 frame byte accounting overflow",
            ));
        }
        state.metrics.in_flight_frames = next_frames;
        state.metrics.in_flight_bytes = next_bytes.unwrap();
        state.metrics.peak_in_flight_frames = state.metrics.peak_in_flight_frames.max(next_frames);
        state.metrics.peak_in_flight_bytes = state
            .metrics
            .peak_in_flight_bytes
            .max(state.metrics.in_flight_bytes);
        match storage {
            FrameStorage::Memory => state.metrics.memory_buffer_bytes += bytes,
            FrameStorage::File => state.metrics.file_buffer_bytes += bytes,
            FrameStorage::Payload => state.metrics.owned_payload_bytes += bytes,
        }
        Ok(FrameAdmissionGuard {
            shared: Arc::clone(&self.shared),
            bytes,
            storage,
        })
    }

    pub fn metrics(&self) -> io::Result<FrameAdmissionMetrics> {
        self.shared
            .lock()
            .map(|state| state.metrics)
            .map_err(|_| io::Error::other("storage.v2 frame budget is poisoned"))
    }
}

impl Drop for FrameAdmissionGuard {
    fn drop(&mut self) {
        let Ok(mut state) = self.shared.lock() else {
            return;
        };
        state.metrics.in_flight_frames = state.metrics.in_flight_frames.saturating_sub(1);
        state.metrics.in_flight_bytes = state.metrics.in_flight_bytes.saturating_sub(self.bytes);
        match self.storage {
            FrameStorage::Memory => state.metrics.memory_buffer_bytes -= self.bytes,
            FrameStorage::File => state.metrics.file_buffer_bytes -= self.bytes,
            FrameStorage::Payload => state.metrics.owned_payload_bytes -= self.bytes,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ConnectionMetrics {
    pub request_frames: u64,
    pub response_frames: u64,
}

fn write_response_frames(
    writer: &mut impl Write,
    mut response: Response,
    frame_budget: &FrameAdmissionBudget,
) -> io::Result<u64> {
    if response.status != 0 || response.payload.len() <= STREAMED_RESPONSE_CHUNK_BYTES {
        write_frame_with_buffer_plan(writer, &Message::Response(response), |bytes| {
            frame_budget.plan_frame(bytes)
        })?;
        return Ok(1);
    }
    if response.payload.len() > MAX_STREAMED_RESPONSE_PAYLOAD_BYTES {
        response.payload.clear();
        response.status = STATUS_RESOURCE_EXHAUSTED;
        response.error_code = Some("response_too_large".to_owned());
        response.error_message = Some("response_too_large".to_owned());
        response.retryable = false;
        write_frame_with_buffer_plan(writer, &Message::Response(response), |bytes| {
            frame_budget.plan_frame(bytes)
        })?;
        return Ok(1);
    }

    let chunk_count = response
        .payload
        .len()
        .div_ceil(STREAMED_RESPONSE_CHUNK_BYTES);
    if chunk_count > MAX_STREAMED_RESPONSE_CHUNKS as usize {
        return Err(io::Error::other(
            "storage.v2 streamed response chunk contract is inconsistent",
        ));
    }
    let stream_id = response.correlation_id;
    for (chunk_index, payload) in response
        .payload
        .chunks(STREAMED_RESPONSE_CHUNK_BYTES)
        .enumerate()
    {
        let chunk = Message::ResponseChunk(ResponseChunk {
            correlation_id: response.correlation_id,
            schema_id: response.schema_id,
            payload: payload.to_vec(),
            stream_id,
            chunk_index: u32::try_from(chunk_index)
                .map_err(|_| io::Error::other("storage.v2 response chunk index overflow"))?,
            final_chunk: chunk_index + 1 == chunk_count,
        });
        write_frame_with_buffer_plan(writer, &chunk, |bytes| frame_budget.plan_frame(bytes))?;
    }
    response.payload.clear();
    write_frame_with_buffer_plan(writer, &Message::Response(response), |bytes| {
        frame_budget.plan_frame(bytes)
    })?;
    u64::try_from(chunk_count + 1)
        .map_err(|_| io::Error::other("storage.v2 response metric overflow"))
}

struct PendingStreamedRequest {
    correlation_id: [u8; 16],
    deadline_unix_ms: u64,
    owner_id: u64,
    tenant_id: Option<u64>,
    command_id: Option<String>,
    schema_id: u32,
    stream_id: [u8; 16],
    next_chunk_index: u32,
    complete: bool,
    total_payload_bytes: usize,
    payload: Vec<u8>,
    _reservation: FrameAdmissionGuard,
}

impl PendingStreamedRequest {
    fn append(
        pending: &mut Option<Self>,
        chunk: crate::protocol::BlobChunk,
        session: &StorageV2Session,
        frame_budget: &FrameAdmissionBudget,
        now_unix_ms: u64,
    ) -> io::Result<()> {
        let total_payload_bytes = usize::try_from(chunk.total_payload_bytes).map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage.v2 streamed request declared total exceeds this platform",
            )
        })?;
        if chunk.owner_id != session.authenticated_scope.owner_id
            || chunk.tenant_id != session.authenticated_scope.tenant_id
            || chunk.schema_id != session.negotiated.schema_id
            || chunk.deadline_unix_ms <= now_unix_ms
            || chunk.payload.is_empty()
            || total_payload_bytes == 0
            || total_payload_bytes > MAX_STREAMED_REQUEST_PAYLOAD_BYTES
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "storage.v2 streamed request identity or deadline differs",
            ));
        }
        if pending.is_none() {
            if chunk.chunk_index != 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "storage.v2 streamed request must begin at chunk zero",
                ));
            }
            let reservation = frame_budget.admit_streamed_payload(total_payload_bytes)?;
            let mut payload = Vec::new();
            payload
                .try_reserve_exact(total_payload_bytes)
                .map_err(|_| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "storage.v2 streamed request allocation failed",
                    )
                })?;
            *pending = Some(Self {
                correlation_id: chunk.correlation_id,
                deadline_unix_ms: chunk.deadline_unix_ms,
                owner_id: chunk.owner_id,
                tenant_id: chunk.tenant_id,
                command_id: chunk.command_id.clone(),
                schema_id: chunk.schema_id,
                stream_id: chunk.stream_id,
                next_chunk_index: 0,
                complete: false,
                total_payload_bytes,
                payload,
                _reservation: reservation,
            });
        }
        let state = pending.as_mut().expect("stream state was initialized");
        let next_payload_bytes = state
            .payload
            .len()
            .checked_add(chunk.payload.len())
            .filter(|next| *next <= state.total_payload_bytes);
        if state.complete
            || chunk.correlation_id != state.correlation_id
            || chunk.deadline_unix_ms != state.deadline_unix_ms
            || chunk.owner_id != state.owner_id
            || chunk.tenant_id != state.tenant_id
            || chunk.command_id != state.command_id
            || chunk.schema_id != state.schema_id
            || chunk.stream_id != state.stream_id
            || total_payload_bytes != state.total_payload_bytes
            || chunk.chunk_index != state.next_chunk_index
            || chunk.chunk_index >= MAX_STREAMED_REQUEST_CHUNKS
            || next_payload_bytes.is_none()
            || chunk.final_chunk != (next_payload_bytes == Some(state.total_payload_bytes))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage.v2 streamed request chunk sequence differs or exceeds its bound",
            ));
        }
        state.payload.extend_from_slice(&chunk.payload);
        state.next_chunk_index += 1;
        state.complete = chunk.final_chunk;
        Ok(())
    }

    fn consume(self, request: Request) -> io::Result<(Request, Vec<FrameAdmissionGuard>)> {
        if !self.complete
            || !request.payload.is_empty()
            || request.correlation_id != self.correlation_id
            || request.deadline_unix_ms != self.deadline_unix_ms
            || request.owner_id != self.owner_id
            || request.tenant_id != self.tenant_id
            || request.command_id != self.command_id
            || request.schema_id != self.schema_id
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage.v2 streamed request terminator differs from its chunks",
            ));
        }
        Ok((
            Request {
                payload: self.payload,
                ..request
            },
            vec![self._reservation],
        ))
    }
}
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use zeroize::Zeroize;

pub struct StorageV2Authenticator {
    authenticated_scope: AuthenticatedScope,
    token_witness: [u8; 32],
}

impl StorageV2Authenticator {
    pub fn new(
        auth_token: &[u8; crate::protocol::AUTH_TOKEN_BYTES],
        authenticated_scope: AuthenticatedScope,
    ) -> io::Result<Self> {
        if auth_token.iter().all(|byte| *byte == 0)
            || authenticated_scope.owner_id == 0
            || authenticated_scope.tenant_id == Some(0)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid storage.v2 authentication configuration",
            ));
        }
        Ok(Self {
            authenticated_scope,
            token_witness: Sha256::digest(auth_token).into(),
        })
    }

    pub fn authenticate(&self, hello: &Hello) -> io::Result<StorageV2Session> {
        let mut candidate_witness: [u8; 32] = Sha256::digest(hello.auth_token.as_slice()).into();
        let authenticated = bool::from(candidate_witness.ct_eq(&self.token_witness));
        candidate_witness.zeroize();
        if !authenticated {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "storage.v2 authentication failed",
            ));
        }
        let negotiated = hello.negotiate(&[STORAGE_SCHEMA_VERSION])?;
        Ok(StorageV2Session {
            authenticated_scope: self.authenticated_scope,
            negotiated,
        })
    }
}

impl fmt::Debug for StorageV2Authenticator {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("StorageV2Authenticator")
            .field("authenticated_scope", &self.authenticated_scope)
            .field("token_witness", &"<redacted>")
            .finish()
    }
}

impl Drop for StorageV2Authenticator {
    fn drop(&mut self) {
        self.token_witness.zeroize();
    }
}

#[derive(Debug)]
pub struct StorageV2Session {
    authenticated_scope: AuthenticatedScope,
    negotiated: NegotiatedProtocol,
}

#[path = "server_bindings.rs"]
mod bindings;
pub(crate) use bindings::{StorageV2Bindings, StorageV2Stores};

impl StorageV2Session {
    pub const fn negotiated(&self) -> NegotiatedProtocol {
        self.negotiated
    }

    pub fn dispatch(
        &self,
        database: &mut AuthorityDatabase,
        request: &Request,
        now_unix_ms: u64,
    ) -> Response {
        let admitted = match self.admit(request, now_unix_ms) {
            Ok(admitted) => admitted,
            Err(response) => return response,
        };
        if admitted.operation.name == "conversation.search" {
            return error_response(
                request,
                self.negotiated.schema_id,
                SessionDispatchError::Execution(SemanticExecutionError::StorageUnavailable),
            );
        }
        self.dispatch_admitted(
            database,
            admitted,
            now_unix_ms,
            AuthorityGarbageCollectionBudget::conservative(),
        ).into_response()
    }

    fn admit<'request>(
        &self,
        request: &'request Request,
        now_unix_ms: u64,
    ) -> Result<crate::semantic::AdmittedRequest<'request>, Response> {
        admit_request(
            request,
            self.negotiated,
            self.authenticated_scope,
            now_unix_ms,
        )
        .map_err(|error| {
            error_response(
                request,
                self.negotiated.schema_id,
                SessionDispatchError::Admission(error),
            )
        })
    }

    fn admit_streamed<'request>(
        &self,
        request: &'request Request,
        now_unix_ms: u64,
    ) -> Result<crate::semantic::AdmittedRequest<'request>, Response> {
        admit_streamed_request(
            request,
            self.negotiated,
            self.authenticated_scope,
            now_unix_ms,
            MAX_STREAMED_REQUEST_PAYLOAD_BYTES,
        )
        .map_err(|error| {
            error_response(
                request,
                self.negotiated.schema_id,
                SessionDispatchError::Admission(error),
            )
        })
    }

    fn dispatch_admitted(
        &self,
        database: &mut AuthorityDatabase,
        admitted: crate::semantic::AdmittedRequest<'_>,
        now_unix_ms: u64,
        authority_gc_budget: AuthorityGarbageCollectionBudget,
    ) -> DispatchedResponse {
        let mut notices = Vec::new();
        let result = execute_admitted_request_with_notices(
            database,
            admitted,
            now_unix_ms,
            authority_gc_budget,
            &mut notices,
        );
        self.finish_admitted(admitted, result, notices)
    }

    fn finish_admitted(
        &self,
        admitted: crate::semantic::AdmittedRequest<'_>,
        result: Result<crate::transaction_ir::IrResponse, SemanticExecutionError>,
        notices: Vec<crate::authority::ConversationCommitNotice>,
    ) -> DispatchedResponse {
        let request = admitted.request;
        let result = result.and_then(|payload| {
            if notices.is_empty() { return Ok(payload); }
            let value: serde_json::Value = serde_json::from_slice(&payload).map_err(|_| crate::semantic_executor::integrity_failure("commit notice payload is not valid JSON"))?;
            let events: Vec<_> = notices.into_iter().map(|notice| serde_json::json!({
                "contract": crate::generated_storage_v2::COMMIT_NOTICE_CONTRACT, "topic": "conversation",
                "userId": notice.user_id, "key": notice.key, "sequence": notice.sequence,
            })).collect();
            serde_json::to_vec(&serde_json::json!({"_storageCommitContract": crate::generated_storage_v2::COMMITTED_EVENTS_CONTRACT, "value": value, "events": events}))
                .map(crate::transaction_ir::IrResponse::unverified)
                .map_err(|_| crate::semantic_executor::integrity_failure("failed to serialize the commit-event envelope"))
        })
        .map_err(SessionDispatchError::Execution);
        match result {
            Ok(payload) => DispatchedResponse::successful(request, payload),
            Err(error) => error_response(request, self.negotiated.schema_id, error).into(),
        }
    }

    fn dispatch_search_admitted(
        &self,
        projection: &TurnSearchProjection,
        admitted: crate::semantic::AdmittedRequest<'_>,
        now_unix_ms: u64,
    ) -> Response {
        let request = admitted.request;
        let result = execute_search_admitted_request(projection, admitted, now_unix_ms)
            .map_err(SessionDispatchError::Execution);
        match result {
            Ok(payload) => Response {
                correlation_id: request.correlation_id,
                schema_id: request.schema_id,
                payload,
                status: 0,
                error_code: None,
                error_message: None,
                retryable: false,
            },
            Err(error) => error_response(request, self.negotiated.schema_id, error),
        }
    }

    pub fn dispatch_message(
        &self,
        database: &mut AuthorityDatabase,
        message: &Message,
        now_unix_ms: u64,
    ) -> io::Result<Message> {
        let Message::Request(request) = message else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "negotiated storage.v2 session accepts only request messages",
            ));
        };
        Ok(Message::Response(self.dispatch(
            database,
            request,
            now_unix_ms,
        )))
    }
}

enum AuthorityAcquisition<'database> {
    Acquired(AuthorityGuard<'database>),
    DeadlineElapsed,
    ConnectionClosed,
}

/// Field drop order releases the mutex before waking the next FIFO waiter.
/// Returning a bare mutex guard dropped the permit at acquisition and made
/// its successor poll at 1 ms intervals for the entire current transaction.
struct AuthorityGuard<'database> {
    authority: MutexGuard<'database, AuthorityDatabase>,
    _permit: crate::authority_admission::Permit<'database>,
    _profile_hold: crate::request_profile::Probe,
}

impl std::ops::Deref for AuthorityGuard<'_> {
    type Target = AuthorityDatabase;
    fn deref(&self) -> &Self::Target { &self.authority }
}

impl std::ops::DerefMut for AuthorityGuard<'_> {
    fn deref_mut(&mut self) -> &mut Self::Target { &mut self.authority }
}

/// Rotation-parking budget: a command rejected with rotation backpressure is
/// replayed in place up to this many install-window waits before the client
/// sees the same busy response it already retries.
const MAX_ROTATION_PARKS: u8 = 16;
/// Liveness fallback for one install-window wait. Installs always signal the
/// rotation barrier, so the slice stays short.
const ROTATION_PARK_SLICE: Duration = Duration::from_millis(25);
/// Do not park when the request deadline cannot fit another dispatch plus
/// its durability wait.
const MIN_ROTATION_REDISPATCH_MILLIS: u64 = 32;

fn acquire_authority_until<'database>(
    database: &'database Mutex<AuthorityDatabase>,
    remaining_millis: u64,
    connection_alive: &mut impl FnMut() -> bool,
    admission: &'database crate::authority_admission::AuthorityAdmission,
) -> io::Result<AuthorityAcquisition<'database>> {
    let _wait = crate::fsync_stats::ForegroundWait::start();
    let _stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_AUTHORITY_WAIT);
    let started_at = Instant::now();
    let wait_budget = Duration::from_millis(remaining_millis);
    let fifo = crate::request_profile::Probe::new(crate::request_profile::FIFO_WAIT);
    let _permit = match admission.acquire(started_at + wait_budget, connection_alive)? {
        crate::authority_admission::Admission::Entered(permit) => permit,
        crate::authority_admission::Admission::DeadlineElapsed => return Ok(AuthorityAcquisition::DeadlineElapsed),
        crate::authority_admission::Admission::ConnectionClosed => return Ok(AuthorityAcquisition::ConnectionClosed),
    };
    drop(fifo);
    let mutex_wait = crate::request_profile::Probe::new(crate::request_profile::MUTEX_WAIT);
    loop {
        match database.try_lock() {
            Ok(authority) => {
                authority.note_foreground_admission();
                drop(mutex_wait);
                drop(_stage);
                return Ok(AuthorityAcquisition::Acquired(AuthorityGuard {
                    authority, _permit,
                    _profile_hold: crate::request_profile::Probe::new(crate::request_profile::AUTHORITY_HOLD),
                }));
            },
            Err(TryLockError::Poisoned(_)) => {
                return Err(io::Error::other("storage.v2 authority mutex is poisoned"));
            }
            Err(TryLockError::WouldBlock) => {
                if !connection_alive() {
                    return Ok(AuthorityAcquisition::ConnectionClosed);
                }
                let elapsed = started_at.elapsed();
                if elapsed >= wait_budget {
                    return Ok(AuthorityAcquisition::DeadlineElapsed);
                }
                thread::sleep((wait_budget - elapsed).min(Duration::from_millis(1)));
            }
        }
    }
}

/// Pin saturation waits outside the authority mutex. Writer reservations
/// remain available, cancellation is polled at most 25 ms apart, and a pin
/// release wakes readers immediately without extending their deadline.
enum ReadReplicaAcquisition {
    Built(AuthorityDatabase),
    DeadlineElapsed,
    ConnectionClosed,
}

// Keep acquisition/wait failures as transport errors and replica-construction
// failures as semantic errors on commands, matching the original boundaries.
enum ReplicaBuildOutcome {
    Built(AuthorityDatabase),
    DeadlineElapsed,
    ConnectionClosed,
    Rejected(io::Error),
}

#[derive(Clone, Copy)]
enum ReplicaPurpose { Query, Preparation }

fn build_read_replica_until(
    database: &Mutex<AuthorityDatabase>, remaining_millis: u64,
    connection_alive: &mut impl FnMut() -> bool,
    admission: &crate::authority_admission::AuthorityAdmission,
) -> io::Result<ReadReplicaAcquisition> {
    match build_replica_until(database, remaining_millis, connection_alive, admission, ReplicaPurpose::Query, None)? {
        ReplicaBuildOutcome::Built(replica) => Ok(ReadReplicaAcquisition::Built(replica)),
        ReplicaBuildOutcome::DeadlineElapsed => Ok(ReadReplicaAcquisition::DeadlineElapsed),
        ReplicaBuildOutcome::ConnectionClosed => Ok(ReadReplicaAcquisition::ConnectionClosed),
        ReplicaBuildOutcome::Rejected(error) => Err(error),
    }
}

fn build_query_replica_until(
    database: &Mutex<AuthorityDatabase>, remaining_millis: u64,
    connection_alive: &mut impl FnMut() -> bool,
    admission: &crate::authority_admission::AuthorityAdmission,
    published: Option<&crate::authority::PublishedQueryView>,
) -> io::Result<ReadReplicaAcquisition> {
    match build_replica_until(database, remaining_millis, connection_alive, admission, ReplicaPurpose::Query, published)? {
        ReplicaBuildOutcome::Built(replica) => Ok(ReadReplicaAcquisition::Built(replica)),
        ReplicaBuildOutcome::DeadlineElapsed => Ok(ReadReplicaAcquisition::DeadlineElapsed),
        ReplicaBuildOutcome::ConnectionClosed => Ok(ReadReplicaAcquisition::ConnectionClosed),
        ReplicaBuildOutcome::Rejected(error) => Err(error),
    }
}

fn build_replica_until(
    database: &Mutex<AuthorityDatabase>, remaining_millis: u64,
    connection_alive: &mut impl FnMut() -> bool,
    admission: &crate::authority_admission::AuthorityAdmission,
    purpose: ReplicaPurpose,
    published: Option<&crate::authority::PublishedQueryView>,
) -> io::Result<ReplicaBuildOutcome> {
    let _wait = crate::fsync_stats::ForegroundWait::start();
    let started_at = Instant::now();
    let wait_budget = Duration::from_millis(remaining_millis);
    if !connection_alive() { return Ok(ReplicaBuildOutcome::ConnectionClosed); }
    if wait_budget.is_zero() { return Ok(ReplicaBuildOutcome::DeadlineElapsed); }
    if database.is_poisoned() {
        return Err(io::Error::other("storage.v2 authority mutex is poisoned"));
    }
    if let Some(view) = published.filter(|_| matches!(purpose, ReplicaPurpose::Query)) {
        let result = {
            let _probe = crate::request_profile::Probe::new(crate::request_profile::REPLICA_BUILD);
            view.try_build()
        };
        match result {
            Some(Ok(replica)) => return Ok(ReplicaBuildOutcome::Built(replica)),
            Some(Err(error)) => return Ok(ReplicaBuildOutcome::Rejected(error)),
            None => crate::request_profile::work(crate::request_profile::Work::PublishedReadFallback, 1),
        }
    }
    // Root capture still needs writer/GC exclusion; no separate capacity queue.
    let remaining = wait_budget.saturating_sub(started_at.elapsed());
    if remaining.as_millis() == 0 { return Ok(ReplicaBuildOutcome::DeadlineElapsed); }
    match acquire_authority_until(database, remaining.as_millis() as u64, connection_alive, admission)? {
        AuthorityAcquisition::Acquired(authority) => {
            let _probe = crate::request_profile::Probe::new(crate::request_profile::REPLICA_BUILD);
            let captured = match purpose {
                ReplicaPurpose::Query => authority.build_read_replica(),
                ReplicaPurpose::Preparation => authority.build_preparation_replica(),
            };
            match captured {
                Ok(replica) => Ok(ReplicaBuildOutcome::Built(replica)),
                Err(error) => Ok(ReplicaBuildOutcome::Rejected(error)),
            }
        },
        AuthorityAcquisition::DeadlineElapsed => Ok(ReplicaBuildOutcome::DeadlineElapsed),
        AuthorityAcquisition::ConnectionClosed => Ok(ReplicaBuildOutcome::ConnectionClosed),
    }
}

/// Default budget for retiring an idle authenticated session at a frame
/// boundary. Healthy pooled client sessions idle far longer than the socket
/// mid-frame I/O bound (30 s), so the connection loop must not treat the
/// first-byte wait lapsing as a wedged peer; only a full idle budget lapse
/// closes the session, and it closes cleanly — the client's pooled-session
/// liveness probe then observes the EOF before reusing the session.
pub(crate) const DEFAULT_SESSION_IDLE_TIMEOUT: Duration = Duration::from_secs(300);

pub fn serve_connection(
    reader: &mut impl Read,
    writer: &mut impl Write,
    database: &Mutex<AuthorityDatabase>,
    authenticator: &StorageV2Authenticator,
    frame_budget: &FrameAdmissionBudget,
    now_unix_ms: impl FnMut() -> u64,
) -> io::Result<ConnectionMetrics> {
    serve_connection_until(
        reader,
        writer,
        StorageV2Stores::new(database, None),
        authenticator,
        frame_budget,
        DEFAULT_SESSION_IDLE_TIMEOUT,
        now_unix_ms,
        || true,
    )
}

pub(crate) fn serve_connection_until(
    reader: &mut impl Read,
    writer: &mut impl Write,
    stores: StorageV2Stores<'_>,
    authenticator: &StorageV2Authenticator,
    frame_budget: &FrameAdmissionBudget,
    idle_timeout: Duration,
    mut now_unix_ms: impl FnMut() -> u64,
    mut connection_alive: impl FnMut() -> bool,
) -> io::Result<ConnectionMetrics> {
    let first = read_frame_or_eof_with_buffer_plan(reader, |bytes| frame_budget.plan_frame(bytes))?
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "storage.v2 connection closed before Hello",
            )
        })?;
    let Message::Hello(hello) = first else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "storage.v2 first frame must be Hello",
        ));
    };
    let session = authenticator.authenticate(&hello)?;
    let acknowledgement = Message::Response(Response {
        correlation_id: hello.correlation_id,
        schema_id: session.negotiated().schema_id,
        payload: Vec::new(),
        status: 0,
        error_code: None,
        error_message: None,
        retryable: false,
    });
    write_frame_with_buffer_plan(writer, &acknowledgement, |bytes| frame_budget.plan_frame(bytes))?;

    let mut metrics = ConnectionMetrics {
        request_frames: 0,
        response_frames: 1,
    };
    let mut pending_streamed_request = None;
    let mut idle_since: Option<Instant> = None;
    loop {
        if !connection_alive() {
            return Ok(metrics);
        }
        let message =
            match read_frame_or_idle_with_buffer_plan(reader, |bytes| frame_budget.plan_frame(bytes)) {
                Ok(FrameRead::Message(message)) => {
                    idle_since = None;
                    message
                }
                Ok(FrameRead::Eof) => return Ok(metrics),
                Ok(FrameRead::IdleLapsed) => {
                    if !connection_alive() {
                        return Ok(metrics);
                    }
                    // Between frames the socket read timeout marks an idle
                    // peer, not a wedged one — a stall after the first byte
                    // still errors out mid-frame below. Re-arm the wait until
                    // the session idle budget lapses; only then retire the
                    // connection, at a frame boundary, where the peer
                    // observes a clean close instead of a reset mid-request.
                    let started = idle_since.get_or_insert_with(Instant::now);
                    if started.elapsed() >= idle_timeout {
                        return Ok(metrics);
                    }
                    continue;
                }
                Err(error)
                    if !connection_alive()
                        && matches!(
                            error.kind(),
                            io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                        ) =>
                {
                    return Ok(metrics);
                }
                Err(error) => return Err(error),
            };
        let admitted_at_ms = now_unix_ms();
        let (request, streamed, _stream_reservations) = match message {
            Message::BlobChunk(chunk) => {
                PendingStreamedRequest::append(
                    &mut pending_streamed_request,
                    chunk,
                    &session,
                    frame_budget,
                    admitted_at_ms,
                )?;
                metrics.request_frames = metrics
                    .request_frames
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("storage.v2 request metric overflow"))?;
                continue;
            }
            Message::Request(request) => {
                if let Some(pending) = pending_streamed_request.take() {
                    let (request, reservations) = pending.consume(request)?;
                    (request, true, reservations)
                } else {
                    (request, false, Vec::new())
                }
            }
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "negotiated storage.v2 session accepts only request or blob-chunk messages",
                ));
            }
        };
        let response = match dispatch_one_request(
            &stores,
            &session,
            &request,
            streamed,
            admitted_at_ms,
            &mut now_unix_ms,
            &mut connection_alive,
        )? {
            RequestDispatch::Responded(response) => response,
            RequestDispatch::ConnectionClosed => return Ok(metrics),
        };
        metrics.request_frames = metrics
            .request_frames
            .checked_add(1)
            .ok_or_else(|| io::Error::other("storage.v2 request metric overflow"))?;
        drop(_stream_reservations);
        let written_response_frames = write_response_frames(writer, response.into_response(), frame_budget)?;
        metrics.response_frames = metrics
            .response_frames
            .checked_add(written_response_frames)
            .ok_or_else(|| io::Error::other("storage.v2 response metric overflow"))?;
    }
}

/// Outcome of one dispatched request on the shared pipeline.
pub(crate) enum RequestDispatch {
    Responded(DispatchedResponse),
    /// Only the framed connection loop can observe this (its liveness
    /// probe); the embedded in-process transport always reports alive.
    ConnectionClosed,
}

/// One-request dispatch pipeline shared by the framed connection loop and
/// the embedded in-process transport (`crate::embed`): control operations
/// answered from observer handles, semantic admission (single-frame or
/// reassembled streamed), the search-projection lane, the snapshot
/// read-replica lane for queries, and the command lane with the strict-mode
/// WAL-barrier durability wait plus bounded rotation parking. Semantics
/// live here exactly once; transports differ only in framing.
pub(crate) fn dispatch_one_request(
    stores: &StorageV2Stores<'_>,
    session: &StorageV2Session,
    request: &Request,
    streamed: bool,
    admitted_at_ms: u64,
    now_unix_ms: &mut impl FnMut() -> u64,
    connection_alive: &mut impl FnMut() -> bool,
) -> io::Result<RequestDispatch> {
    profile_request_dispatch(&request.operation, || dispatch_one_request_inner(
        stores, session, request, streamed, admitted_at_ms, now_unix_ms, connection_alive))
}

/// One profile/outcome boundary for framed and embedded dispatch. The embedded
/// caller includes its immutable-binding preparation inside this same ledger.
pub(crate) fn profile_request_dispatch(
    operation: &str,
    execute: impl FnOnce() -> io::Result<RequestDispatch>,
) -> io::Result<RequestDispatch> {
    let mut profile = crate::request_profile::RequestGuard::start(operation);
    let result = execute();
    if let Some(profile) = profile.as_mut() {
        profile.finish(match &result {
            Ok(RequestDispatch::Responded(response)) if response.status == 0 => "ok",
            Ok(RequestDispatch::Responded(_)) => "response_error",
            Ok(RequestDispatch::ConnectionClosed) => "connection_closed",
            Err(_) => "transport_error",
        });
    }
    result
}

pub(crate) fn dispatch_one_request_inner(
    stores: &StorageV2Stores<'_>,
    session: &StorageV2Session,
    request: &Request,
    streamed: bool,
    admitted_at_ms: u64,
    now_unix_ms: &mut impl FnMut() -> u64,
    connection_alive: &mut impl FnMut() -> bool,
) -> io::Result<RequestDispatch> {
    crate::fsync_stats::note_foreground_request();
    let response = if crate::control_ops::is_control_operation(&request.operation) {
        // Control operations observe the daemon from observer handles and
        // static launch data; they never touch the authority mutex, so
        // they answer even while a commit holds it, and they stay out of
        // semantic admission (and therefore out of the operation catalog).
        crate::control_ops::respond(
            request,
            session.negotiated.schema_id,
            stores.control_observers.as_ref(),
        ).into()
    } else {
        let admission = if streamed {
            session.admit_streamed(request, admitted_at_ms)
        } else {
            session.admit(request, admitted_at_ms)
        };
        match admission {
            Err(response) => response.into(),
            Ok(admitted) if admitted.operation.name == "conversation.search" => {
                match stores.search_projection {
                    None => error_response(
                        request,
                        session.negotiated.schema_id,
                        SessionDispatchError::Execution(SemanticExecutionError::StorageUnavailable),
                    ).into(),
                    Some(projection) => match acquire_search_projection_until(
                        projection,
                        admitted.remaining_millis,
                        connection_alive,
                    )? {
                        SearchProjectionAcquisition::Acquired(projection) => {
                            session.dispatch_search_admitted(&projection, admitted, now_unix_ms()).into()
                        }
                        SearchProjectionAcquisition::DeadlineElapsed => error_response(
                            request,
                            session.negotiated.schema_id,
                            SessionDispatchError::Admission(AdmissionError::DeadlineElapsed),
                        ).into(),
                        SearchProjectionAcquisition::ConnectionClosed => {
                            return Ok(RequestDispatch::ConnectionClosed)
                        }
                    },
                }
            }
            Ok(admitted) if admitted.operation.kind == StorageOperationKind::Query => {
                match build_query_replica_until(
                    stores.authority,
                    admitted.remaining_millis,
                    connection_alive,
                    &stores.authority_admission,
                    stores.published_queries.as_ref(),
                )? {
                    ReadReplicaAcquisition::Built(mut replica) => {
                        let _probe = crate::request_profile::Probe::new(crate::request_profile::QUERY_EXECUTE);
                        session.dispatch_admitted(&mut replica, admitted, now_unix_ms(), stores.authority_gc_budget)
                    },
                    ReadReplicaAcquisition::DeadlineElapsed => error_response(
                        request,
                        session.negotiated.schema_id,
                        SessionDispatchError::Admission(AdmissionError::DeadlineElapsed),
                    ).into(),
                    ReadReplicaAcquisition::ConnectionClosed => {
                        return Ok(RequestDispatch::ConnectionClosed)
                    }
                }
            }
            Ok(mut admitted) => {
                if let Some(outcome) = try_parallel_command(
                    stores, session, admitted, now_unix_ms, connection_alive,
                )? {
                    return Ok(outcome);
                }
                let is_command = admitted.request.command_id.is_some();
                let mut parks = 0_u8;
                let response = loop {
                    let mut rotation_since = None;
                    let attempt = match acquire_authority_until(
                        stores.authority,
                        admitted.remaining_millis,
                        connection_alive,
                        &stores.authority_admission,
                    )? {
                        AuthorityAcquisition::Acquired(mut authority) => {
                            let is_command = admitted.request.command_id.is_some();
                            let response = {
                                let _hold = crate::authority_watch::AuthorityHoldTimer::start(&request.operation);
                                let _stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_COMMAND_EXECUTE);
                                session.dispatch_admitted(
                                    &mut authority,
                                    admitted,
                                    now_unix_ms(),
                                    stores.authority_gc_budget,
                                )
                            };
                            if is_command
                                && response.error_code.as_deref() == Some("database_busy")
                                && authority.rotation_blocks_appends()
                            {
                                // Capture the predicate and wake generation
                                // in the dispatch hold. A second FIFO mutex
                                // acquisition here doubles the waiting convoy.
                                rotation_since = stores.rotation_barrier.as_ref()
                                    .map(|barrier| barrier.completed_generation());
                            }
                            if is_command {
                                if let Some(barrier) = &stores.wal_barrier {
                                    // Strict durability is preserved: the response is
                                    // written only after this commit's witness is
                                    // covered. The wait runs WITHOUT the authority
                                    // mutex, so concurrent connections pipeline
                                    // instead of queueing behind one fsync.
                                    let witness = barrier.witness(authority.wal_len()?)?;
                                    drop(authority);
                                    let _stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_WAL_BARRIER_WAIT);
                                    barrier.wait_witness(
                                        &witness,
                                        std::time::Instant::now() + crate::wal::MAX_BARRIER_WAIT,
                                    )?;
                                    response
                                } else {
                                    response
                                }
                            } else {
                                response
                            }
                        }
                        AuthorityAcquisition::DeadlineElapsed => error_response(
                            request,
                            session.negotiated.schema_id,
                            SessionDispatchError::Admission(AdmissionError::DeadlineElapsed),
                        ).into(),
                        AuthorityAcquisition::ConnectionClosed => {
                            return Ok(RequestDispatch::ConnectionClosed)
                        }
                    };
                    // Rotation parking: a command rejected with database_busy
                    // while a rotation blocks admission waits out the install
                    // window on the rotation barrier and replays in place,
                    // instead of bouncing the whole round trip off the
                    // client. The budget is bounded; exhaustion falls back to
                    // the same busy response the client already retries.
                    if !is_command
                        || attempt.status == 0
                        || attempt.error_code.as_deref() != Some("database_busy")
                        || parks >= MAX_ROTATION_PARKS
                    {
                        break attempt;
                    }
                    let Some(rotation_barrier) = &stores.rotation_barrier else {
                        break attempt;
                    };
                    // Wait for publication, not each transient drain retry.
                    // The monotonic deadline and small cancellation slices
                    // bound this park even if the clock callback stops moving.
                    let remaining_millis = request.deadline_unix_ms.saturating_sub(now_unix_ms());
                    if remaining_millis <= MIN_ROTATION_REDISPATCH_MILLIS {
                        break attempt;
                    }
                    let Some(since) = rotation_since else { break attempt; };
                    let park_deadline = Instant::now() + Duration::from_millis(
                        remaining_millis - MIN_ROTATION_REDISPATCH_MILLIS);
                    let _park = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_ROTATION_PARK);
                    loop {
                        if !connection_alive() {
                            return Ok(RequestDispatch::ConnectionClosed);
                        }
                        let remaining = park_deadline.saturating_duration_since(Instant::now());
                        if remaining.is_zero() { break; }
                        if rotation_barrier.park_until_completed(since, remaining.min(ROTATION_PARK_SLICE)) != since {
                            break;
                        }
                    }
                    if Instant::now() >= park_deadline { break attempt; }
                    parks += 1;
                    admitted = match session.admit(request, now_unix_ms()) {
                        Ok(admitted) => admitted,
                        Err(response) => break response.into(),
                    };
                };
                response
            }
        }
    };
    Ok(RequestDispatch::Responded(response))
}

/// Evaluate a certified transaction against a pinned snapshot without holding
/// the sequencer. A conflict falls back to the existing FIFO execution lane,
/// so hot keys cannot livelock through repeated optimistic retries.
fn try_parallel_command(
    stores: &StorageV2Stores<'_>,
    session: &StorageV2Session,
    admitted: crate::semantic::AdmittedRequest<'_>,
    now_unix_ms: &mut impl FnMut() -> u64,
    connection_alive: &mut impl FnMut() -> bool,
) -> io::Result<Option<RequestDispatch>> {
    let request = admitted.request;
    let compiled = {
        let _probe = crate::request_profile::Probe::new(crate::request_profile::PARALLEL_COMPILE);
        crate::semantic_executor::compile_parallel_transaction(admitted, now_unix_ms())
    };
    let ir = match compiled {
        Ok(Some(ir)) => ir,
        Ok(None) => return Ok(None),
        Err(error) => {
            return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
                admitted,
                Err(error),
                Vec::new(),
            ))))
        }
    };
    let remaining = request.deadline_unix_ms.saturating_sub(now_unix_ms());
    // Pin saturation is admission pressure, not a reason to execute the whole
    // command under the writer mutex. Share the query lane's bounded,
    // release-notified wait; current prepared holders remain free to commit.
    let mut replica = match build_replica_until(
        stores.authority, remaining, connection_alive, &stores.authority_admission,
        ReplicaPurpose::Preparation, stores.published_queries.as_ref(),
    )? {
        ReplicaBuildOutcome::Built(replica) => replica,
        ReplicaBuildOutcome::DeadlineElapsed => {
            return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
                admitted, Err(SemanticExecutionError::DeadlineElapsed), Vec::new(),
            ))));
        }
        ReplicaBuildOutcome::ConnectionClosed => return Ok(Some(RequestDispatch::ConnectionClosed)),
        ReplicaBuildOutcome::Rejected(error) if error.kind() == io::ErrorKind::WouldBlock
            || crate::engine::is_rotation_backpressure(&error) => {
            crate::fsync_stats::note_parallel_fallback(
                crate::fsync_stats::ParallelFallbackPhase::Snapshot, &error,
            );
            return Ok(None);
        }
        ReplicaBuildOutcome::Rejected(error) => return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
            admitted, Err(crate::semantic_executor::map_storage_error(error)), Vec::new(),
        )))),
    };
    let preparation_timer =
        crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PARALLEL_PREPARE);
    let prepared = crate::transaction_ir::prepare_transaction_ir_with_deadline(
        &mut replica,
        &ir,
        Duration::from_millis(request.deadline_unix_ms.saturating_sub(now_unix_ms())),
    );
    // The prepared transaction retains the snapshot registration and private
    // blob bytes; dropping the facade must not release either prematurely.
    drop(replica);
    drop(preparation_timer);
    let prepared = match prepared {
        Ok(prepared) => prepared,
        Err(error)
            if error.kind() == io::ErrorKind::WouldBlock
                || crate::engine::is_rotation_backpressure(&error) =>
        {
            crate::fsync_stats::note_parallel_fallback(
                crate::fsync_stats::ParallelFallbackPhase::Preparation, &error,
            );
            return Ok(None);
        }
        Err(error) => {
            return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
                admitted,
                Err(crate::semantic_executor::map_storage_error(error)),
                Vec::new(),
            ))))
        }
    };
    let remaining = request.deadline_unix_ms.saturating_sub(now_unix_ms());
    if remaining == 0 {
        return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
            admitted,
            Err(SemanticExecutionError::DeadlineElapsed),
            Vec::new(),
        ))));
    }
    let mut authority = match acquire_authority_until(
        stores.authority,
        remaining,
        connection_alive,
        &stores.authority_admission,
    )? {
        AuthorityAcquisition::Acquired(authority) => authority,
        AuthorityAcquisition::DeadlineElapsed => {
            return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
                admitted,
                Err(SemanticExecutionError::DeadlineElapsed),
                Vec::new(),
            ))))
        }
        AuthorityAcquisition::ConnectionClosed => {
            return Ok(Some(RequestDispatch::ConnectionClosed))
        }
    };
    // Admission may hand over an available mutex at the edge of its wait
    // budget. Recheck the request before any publication, as the exclusive
    // semantic executor does when it enters with a freshly sampled clock.
    if !connection_alive() {
        return Ok(Some(RequestDispatch::ConnectionClosed));
    }
    if now_unix_ms() >= request.deadline_unix_ms {
        return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
            admitted, Err(SemanticExecutionError::DeadlineElapsed), Vec::new(),
        ))));
    }
    let mut notices = Vec::new();
    if let Some(transaction) = prepared.transaction {
        let _hold = crate::authority_watch::AuthorityHoldTimer::start(&request.operation);
        let _commit = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_IR_COMMIT);
        let _parallel_commit =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PARALLEL_COMMIT);
        match authority.commit(transaction) {
            Ok(commit) => notices = commit.conversation_notices,
            Err(error)
                if error.kind() == io::ErrorKind::WouldBlock
                    || crate::engine::is_rotation_backpressure(&error) =>
            {
                crate::fsync_stats::note_parallel_fallback(
                    crate::fsync_stats::ParallelFallbackPhase::Commit, &error,
                );
                return Ok(None);
            }
            Err(error) => {
                return Ok(Some(RequestDispatch::Responded(session.finish_admitted(
                    admitted,
                    Err(crate::semantic_executor::map_storage_error(error)),
                    Vec::new(),
                ))))
            }
        }
    }
    // Capture the exact committed WAL boundary before relinquishing authority.
    // Response materialization consumes only request-owned bytes and notices;
    // successors can commit while this request encodes its private response.
    let durability = stores.wal_barrier.as_ref()
        .map(|barrier| Ok::<_, io::Error>((barrier, barrier.witness(authority.wal_len()?)?)))
        .transpose()?;
    drop(authority);
    let response = session.finish_admitted(admitted, Ok(prepared.response), notices);
    if let Some((barrier, witness)) = durability {
        let _wait = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_WAL_BARRIER_WAIT);
        barrier.wait_witness(&witness, Instant::now() + crate::wal::MAX_BARRIER_WAIT)?;
    }
    Ok(Some(RequestDispatch::Responded(response)))
}

enum SearchProjectionAcquisition<'projection> {
    Acquired(MutexGuard<'projection, TurnSearchProjection>),
    DeadlineElapsed,
    ConnectionClosed,
}

fn acquire_search_projection_until<'projection>(
    projection: &'projection Mutex<TurnSearchProjection>,
    remaining_millis: u64,
    connection_alive: &mut impl FnMut() -> bool,
) -> io::Result<SearchProjectionAcquisition<'projection>> {
    let started_at = Instant::now();
    let wait_budget = Duration::from_millis(remaining_millis);
    loop {
        match projection.try_lock() {
            Ok(projection) => return Ok(SearchProjectionAcquisition::Acquired(projection)),
            Err(TryLockError::Poisoned(_)) => {
                return Err(io::Error::other(
                    "storage.v2 search-projection mutex is poisoned",
                ));
            }
            Err(TryLockError::WouldBlock) => {
                if !connection_alive() {
                    return Ok(SearchProjectionAcquisition::ConnectionClosed);
                }
                let elapsed = started_at.elapsed();
                if elapsed >= wait_budget {
                    return Ok(SearchProjectionAcquisition::DeadlineElapsed);
                }
                thread::sleep((wait_budget - elapsed).min(Duration::from_millis(1)));
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum SessionDispatchError {
    Admission(AdmissionError),
    Execution(SemanticExecutionError),
}

fn error_response(
    request: &Request,
    response_schema_id: u32,
    error: SessionDispatchError,
) -> Response {
    // A corruption fence must leave a server-side trace: client logs only
    // carry what the wire sends, and pre-diagnostic builds echoed the bare
    // code, so this line is the durable record of WHICH fence fired.
    if let SessionDispatchError::Execution(SemanticExecutionError::Integrity(diagnostic)) = &error {
        crate::engine_log::error(
            "integrity_fence",
            &format!("operation={} diagnostic={}", request.operation, diagnostic),
        );
    }
    let (status, code, retryable, error_message) = match &error {
        SessionDispatchError::Admission(admission) => {
            let status = match admission {
                AdmissionError::AuthorityScopeMismatch => STATUS_FORBIDDEN,
                AdmissionError::DeadlineElapsed => STATUS_DEADLINE_ELAPSED,
                _ => STATUS_BAD_REQUEST,
            };
            let code = admission.code();
            (status, code, admission.retryable(), code.to_owned())
        }
        SessionDispatchError::Execution(execution) => {
            let status = match execution {
                SemanticExecutionError::InvalidPayload
                | SemanticExecutionError::DiagnosticInvalidPayload => STATUS_BAD_REQUEST,
                SemanticExecutionError::DiagnosticUnsupported => STATUS_NOT_IMPLEMENTED,
                SemanticExecutionError::NotFound => STATUS_BAD_REQUEST,
                SemanticExecutionError::AuthorityScopeMismatch => STATUS_FORBIDDEN,
                SemanticExecutionError::DeadlineElapsed => STATUS_DEADLINE_ELAPSED,
                SemanticExecutionError::UnsupportedOperation => STATUS_NOT_IMPLEMENTED,
                SemanticExecutionError::Conflict => STATUS_CONFLICT,
                // Legacy serves this code as a bare 500 fallthrough; tofu-db
                // classifies it as the caller-correctable conflict it is.
                SemanticExecutionError::PluginStorageIncompatible => STATUS_CONFLICT,
                SemanticExecutionError::TurnInProgress
                | SemanticExecutionError::TurnLaneAdvanced
                | SemanticExecutionError::TurnParentInvalid
                | SemanticExecutionError::TurnProjectionStale
                | SemanticExecutionError::TurnSupersededByHuman => STATUS_CONFLICT,
                SemanticExecutionError::Integrity(_) => STATUS_INTEGRITY_FAILURE,
                SemanticExecutionError::StorageUnavailable => STATUS_UNAVAILABLE,
                SemanticExecutionError::PayloadTooLarge
                | SemanticExecutionError::ResourceExhausted
                | SemanticExecutionError::CapacityBusy => STATUS_RESOURCE_EXHAUSTED,
            };
            let code = execution.code();
            // Integrity carries the exact fence diagnostic; every other code
            // keeps the historic behavior of echoing the code as the message.
            let error_message = match &execution {
                SemanticExecutionError::Integrity(message) => message.clone(),
                _ => code.to_owned(),
            };
            (status, code, execution.retryable(), error_message)
        }
    };
    Response {
        correlation_id: request.correlation_id,
        schema_id: response_schema_id,
        payload: Vec::new(),
        status,
        error_code: Some(code.to_owned()),
        error_message: Some(error_message),
        retryable,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{read_frame, write_frame, PROTOCOL_VERSION};
    use crate::turn_search_projection::TurnSearchDocument;
    use crate::vfs::{DeterministicVfs, Vfs};
    use serde_json::{json, Value};
    use std::io::Cursor;
    use std::sync::Arc;

    fn hello() -> Hello {
        Hello {
            correlation_id: [1; 16],
            minimum_version: PROTOCOL_VERSION,
            maximum_version: PROTOCOL_VERSION,
            schema_ids: vec![STORAGE_SCHEMA_VERSION],
            auth_token: [9; crate::protocol::AUTH_TOKEN_BYTES],
        }
    }

    fn request(operation: &str, owner_id: u64, payload: Value) -> Request {
        Request {
            correlation_id: [2; 16],
            deadline_unix_ms: 10_000,
            owner_id,
            tenant_id: Some(7),
            command_id: None,
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: operation.to_owned(),
            payload: serde_json::to_vec(&payload).unwrap(),
        }
    }

    fn scope() -> AuthenticatedScope {
        AuthenticatedScope {
            owner_id: 11,
            tenant_id: Some(7),
        }
    }

    fn authenticator() -> StorageV2Authenticator {
        StorageV2Authenticator::new(&[9; crate::protocol::AUTH_TOKEN_BYTES], scope()).unwrap()
    }

    #[test]
    fn canonical_dispatch_payload_survives_transfer_and_public_mutation_discards_proof() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let session = authenticator().authenticate(&hello()).unwrap();
        for (index, value) in [
            json!({"text":"plain"}),
            json!({"text":"\n\\\"中".repeat(10000)}),
            json!({"numbers":[u64::MAX, i64::MIN, 1.25, -0.0, 1e-200]}),
            json!({"escaped\nkey": [null, true, {"$serde_json::private::RawValue":"null"}]}),
        ].into_iter().enumerate() {
            let key = format!("proof-{index}");
            let mut put = request("record.put", 11,
                json!({"namespace":"proof", "key":key, "value":value, "user_id":11}));
            put.command_id = Some(format!("put-{key}"));
            assert_eq!(session.dispatch(&mut database, &put, 1000).status, 0);
            let get = request("record.get", 11,
                json!({"namespace":"proof", "key":key, "user_id":11}));
            let admitted = session.admit(&get, 1000).unwrap();
            let response = session.dispatch_admitted(&mut database, admitted, 1000,
                AuthorityGarbageCollectionBudget::conservative());
            assert_eq!(response.status, 0);
            assert_eq!(response.canonical_payload(), Some(response.payload.as_slice()));
            let encoded = crate::embed::dispatched_response_to_encoded_json(&response).unwrap();
            let envelope: Value = serde_json::from_slice(&encoded).unwrap();
            let payload: Value = serde_json::from_slice(&response.payload).unwrap();
            assert_eq!(envelope, json!({"status":0,"error_code":null,"error_message":null,
                "retryable":false,"payload":payload}));
            // The public API exposes a Vec only after consuming the capsule.
            // Rewrapping even byte-identical public bytes cannot mint a proof.
            let mut public = response.into_response();
            assert!(DispatchedResponse::from(public.clone()).canonical_payload().is_none());
            public.payload = b"not JSON".to_vec();
            let mutated = DispatchedResponse::from(public);
            let encoded = crate::embed::dispatched_response_to_encoded_json(&mutated).unwrap();
            assert!(serde_json::from_slice::<Value>(&encoded).unwrap()["payload"].is_null());
        }
        let missing = request("record.get", 11, json!({"namespace":"proof","key":"missing","user_id":11}));
        let response = session.dispatch_admitted(&mut database, session.admit(&missing,1000).unwrap(),1000,
            AuthorityGarbageCollectionBudget::conservative());
        assert!(response.canonical_payload().is_none());
        assert_eq!(response.payload,b"null");
        let denied = request("record.get",12,json!({"namespace":"proof","key":"proof-0","user_id":12}));
        assert_eq!(session.dispatch(&mut database,&denied,1000).status,STATUS_FORBIDDEN);
    }

    #[test]
    fn commit_notice_reencoding_and_unverified_literals_do_not_inherit_payload_proof() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let session = authenticator().authenticate(&hello()).unwrap();
        let mut put = request("record.put",11,json!({"namespace":"proof","key":"notice","value":{"v":1},"user_id":11}));
        put.command_id = Some("notice-put".to_owned());
        assert_eq!(session.dispatch(&mut database,&put,1000).status,0);
        let get = request("record.get",11,json!({"namespace":"proof","key":"notice","user_id":11}));
        let admitted = session.admit(&get,1000).unwrap();
        let selected = execute_admitted_request_with_notices(&mut database,admitted,1000,
            AuthorityGarbageCollectionBudget::conservative(),&mut Vec::new()).unwrap();
        assert!(selected.json_proven());
        let response = session.finish_admitted(admitted,Ok(selected),vec![crate::authority::ConversationCommitNotice {
            user_id:11,key:"conversation".to_owned(),sequence:1,
        }]);
        assert_eq!(response.status,0);
        assert!(response.canonical_payload().is_none());
        let envelope: Value = serde_json::from_slice(&crate::embed::dispatched_response_to_encoded_json(&response).unwrap()).unwrap();
        assert_eq!(envelope["payload"]["value"]["value"],json!({"v":1}));
        for bytes in [b"[18446744073709551616,1.25]".as_slice(), b"[-0,1.5]", b"[1,]", b"1e400"] {
            let response = session.finish_admitted(admitted,
                Ok(crate::transaction_ir::IrResponse::unverified(bytes.to_vec())),Vec::new());
            assert!(response.canonical_payload().is_none());
            let expected = serde_json::from_slice::<Value>(bytes).unwrap_or(Value::Null);
            let envelope: Value = serde_json::from_slice(&crate::embed::dispatched_response_to_encoded_json(&response).unwrap()).unwrap();
            assert_eq!(envelope["payload"],expected);
        }
    }

    #[test]
    fn integrity_error_response_carries_the_bounded_diagnostic() {
        let req = request("turn.event.record", 11, json!({}));
        let response = error_response(
            &req,
            STORAGE_SCHEMA_VERSION,
            SessionDispatchError::Execution(crate::semantic_executor::integrity_failure(
                "stored turn projection\nis missing segments",
            )),
        );
        assert_eq!(response.status, STATUS_INTEGRITY_FAILURE);
        assert_eq!(response.error_code.as_deref(), Some("database_integrity"));
        let message = response.error_message.unwrap();
        assert!(message.contains("stored turn projection is missing segments"));
        assert!(!message.contains('\n'));
    }

    #[test]
    fn framed_request_negotiates_dispatches_and_preserves_correlation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let session = authenticator().authenticate(&hello()).unwrap();
        assert_eq!(session.negotiated().schema_id, STORAGE_SCHEMA_VERSION);
        let request = request("desktop.egress_agent.get", 11, json!({"owner_user_id": 11}));
        let mut request_frame = Vec::new();
        write_frame(&mut request_frame, &Message::Request(request.clone())).unwrap();
        let decoded = read_frame(&mut Cursor::new(request_frame)).unwrap();
        let response = session
            .dispatch_message(&mut database, &decoded, 1_000)
            .unwrap();
        let mut response_frame = Vec::new();
        write_frame(&mut response_frame, &response).unwrap();
        let Message::Response(response) = read_frame(&mut Cursor::new(response_frame)).unwrap()
        else {
            panic!("server response was not a response message");
        };
        assert_eq!(response.status, 0);
        assert_eq!(response.correlation_id, request.correlation_id);
        assert_eq!(
            serde_json::from_slice::<Value>(&response.payload).unwrap(),
            json!({"present": false, "agent_id": "", "updated_at_ms": 0})
        );
    }

    #[test]
    fn authentication_and_cross_owner_fail_before_storage_io() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let authenticator = authenticator();
        let mut wrong_token = hello();
        wrong_token.auth_token = [8; crate::protocol::AUTH_TOKEN_BYTES];
        assert_eq!(
            authenticator.authenticate(&wrong_token).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
        let rendered = format!("{authenticator:?}");
        assert!(rendered.contains("<redacted>"));
        assert!(!rendered.contains("9, 9"));

        let session = authenticator.authenticate(&hello()).unwrap();
        let cross_owner_request =
            request("desktop.egress_agent.get", 12, json!({"owner_user_id": 12}));
        vfs.arm_fault(None).unwrap();
        let response = session.dispatch(&mut database, &cross_owner_request, 1_000);
        assert_eq!(response.status, STATUS_FORBIDDEN);
        assert_eq!(
            response.error_code.as_deref(),
            Some("authority_scope_mismatch")
        );
        assert!(vfs.trace().unwrap().is_empty());

        let mut wrong_schema =
            request("desktop.egress_agent.get", 11, json!({"owner_user_id": 11}));
        wrong_schema.schema_id += 1;
        let response = session.dispatch(&mut database, &wrong_schema, 1_000);
        assert_eq!(response.status, STATUS_BAD_REQUEST);
        assert_eq!(response.schema_id, STORAGE_SCHEMA_VERSION);
        assert_eq!(response.correlation_id, wrong_schema.correlation_id);
        assert_eq!(
            response.error_code.as_deref(),
            Some("schema_not_negotiated")
        );
    }

    #[test]
    fn authenticated_session_rejects_non_request_messages() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let session = authenticator().authenticate(&hello()).unwrap();
        assert_eq!(
            session
                .dispatch_message(&mut database, &Message::Hello(hello()), 1_000)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn connection_requires_authenticated_hello_and_runs_until_clean_boundary_eof() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let request = request("desktop.egress_agent.get", 11, json!({"owner_user_id": 11}));
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &Message::Request(request.clone())).unwrap();
        let mut reader = Cursor::new(input);
        let mut output = Vec::new();
        let frame_budget = FrameAdmissionBudget::maximum();
        let metrics = serve_connection(
            &mut reader,
            &mut output,
            &database,
            &authenticator(),
            &frame_budget,
            || 1_000,
        )
        .unwrap();
        assert_eq!(
            metrics,
            ConnectionMetrics {
                request_frames: 1,
                response_frames: 2,
            }
        );

        let mut responses = Cursor::new(output);
        let Message::Response(acknowledgement) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not emit a Hello acknowledgement");
        };
        assert_eq!(acknowledgement.correlation_id, hello().correlation_id);
        assert_eq!(acknowledgement.status, 0);
        assert!(acknowledgement.payload.is_empty());
        let Message::Response(response) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not emit a request response");
        };
        assert_eq!(response.correlation_id, request.correlation_id);
        assert_eq!(response.status, 0);
        assert!(read_frame_or_eof_with_admission(&mut responses, |_| Ok(()))
            .unwrap()
            .is_none());
        assert_eq!(frame_budget.metrics().unwrap().in_flight_frames, 0);
        assert_eq!(frame_budget.metrics().unwrap().in_flight_bytes, 0);
    }

    #[test]
    fn control_operations_answer_before_semantic_admission() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let health = request("system.health", 11, json!({}));
        let metrics_request = request("system.metrics", 11, json!({}));
        let unknown = request("system.nope", 11, json!({}));
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        for message in [&health, &metrics_request, &unknown] {
            write_frame(&mut input, &Message::Request(message.clone())).unwrap();
        }
        let mut reader = Cursor::new(input);
        let mut output = Vec::new();
        let frame_budget = FrameAdmissionBudget::maximum();
        let stores = StorageV2Stores::new(&database, None).with_control_observers(
            crate::control_ops::ControlObservers {
                wal_durability: Some("strict"),
                wal_flush_interval_milliseconds: Some(0),
                resource_budget: Some(json!({"memoryCapacityBytes": 4096})),
                ..crate::control_ops::ControlObservers::default()
            },
        );
        serve_connection_until(
            &mut reader,
            &mut output,
            stores,
            &authenticator(),
            &frame_budget,
            DEFAULT_SESSION_IDLE_TIMEOUT,
            || 1_000,
            || true,
        )
        .unwrap();

        let mut responses = Cursor::new(output);
        let Message::Response(_) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not emit a Hello acknowledgement");
        };
        let Message::Response(health_response) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not answer system.health");
        };
        assert_eq!(health_response.status, 0);
        assert_eq!(health_response.correlation_id, health.correlation_id);
        let health_payload: Value = serde_json::from_slice(&health_response.payload).unwrap();
        assert_eq!(
            health_payload["contract"],
            json!(crate::control_ops::SYSTEM_HEALTH_CONTRACT)
        );
        assert_eq!(health_payload["ready"], json!(true));
        assert_eq!(health_payload["walDurability"], json!("strict"));
        assert_eq!(
            health_payload["resourceBudget"],
            json!({"memoryCapacityBytes": 4096})
        );

        let Message::Response(metrics_response) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not answer system.metrics");
        };
        assert_eq!(metrics_response.status, 0);
        assert_eq!(
            metrics_response.correlation_id,
            metrics_request.correlation_id
        );
        let metrics_payload: Value = serde_json::from_slice(&metrics_response.payload).unwrap();
        assert_eq!(
            metrics_payload["contract"],
            json!(crate::control_ops::SYSTEM_METRICS_CONTRACT)
        );
        assert!(metrics_payload["commitStages"].is_array());
        assert!(metrics_payload["durability"].is_object());

        // Other system.* names still flow through semantic admission and fail
        // closed as unknown operations.
        let Message::Response(unknown_response) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not answer the unknown operation");
        };
        assert_eq!(unknown_response.status, STATUS_BAD_REQUEST);
        assert_eq!(
            unknown_response.error_code.as_deref(),
            Some("unknown_operation")
        );
    }

    #[test]
    fn command_parks_through_a_rotation_install_instead_of_bouncing_busy() {
        // Fill an authority to the hard WAL ceiling with the soft threshold
        // disabled, then reserve the rotation lane: appends backpressure
        // until the install completes. The framed command must be held by
        // the park loop and succeed after the install — an instant reject
        // (today's client-visible busy) fails this test.
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let rotation_barrier = std::sync::Arc::new(crate::rotation_barrier::RotationBarrier::new());
        let prepared = {
            let mut authority = database.lock().unwrap();
            let engine = authority.engine_mut_for_test();
            engine.set_rotation_threshold_bytes_for_test(crate::ACTIVE_LOG_MAX_BYTES);
            for index in 0..31_u8 {
                let payload = vec![index; 2 * 1024 * 1024];
                engine
                    .commit_transaction(b"large-seed", &[&payload])
                    .unwrap();
            }
            match engine.try_prepare_rotation_checkpoint().unwrap() {
                crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
                other => panic!("expected a ready rotation, got {other:?}"),
            }
        };
        let mut command = request(
            "record.put",
            11,
            json!({"namespace": "bench", "key": "parked", "value": {"v": 1}}),
        );
        command.command_id = Some("parked-command-1".to_owned());
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &Message::Request(command.clone())).unwrap();
        let mut reader = Cursor::new(input);
        let mut output = Vec::new();
        let frame_budget = FrameAdmissionBudget::maximum();
        let stores = StorageV2Stores::new(&database, None)
            .with_rotation_barrier(std::sync::Arc::clone(&rotation_barrier));
        std::thread::scope(|scope| {
            let connection = scope.spawn(move || {
                serve_connection_until(
                    &mut reader,
                    &mut output,
                    stores,
                    &authenticator(),
                    &frame_budget,
                    DEFAULT_SESSION_IDLE_TIMEOUT,
                    || 1_000,
                    || true,
                )
                .unwrap();
                output
            });
            // Let the command reach its park, then install the rotation and
            // signal — exactly what the daemon rotation driver does.
            std::thread::sleep(std::time::Duration::from_millis(50));
            {
                let mut authority = database.lock().unwrap();
                let outcome = crate::engine::encode_rotation_segments(&prepared);
                authority
                    .finish_rotation_checkpoint(prepared, outcome)
                    .unwrap();
            }
            rotation_barrier.signal_completion();
            let output = connection.join().unwrap();
            let mut responses = Cursor::new(output);
            let Message::Response(_) = read_frame(&mut responses).unwrap() else {
                panic!("connection did not emit a Hello acknowledgement");
            };
            let Message::Response(response) = read_frame(&mut responses).unwrap() else {
                panic!("connection did not answer the parked command");
            };
            assert_eq!(response.status, 0);
            assert_eq!(response.correlation_id, command.correlation_id);
        });
    }

    #[test]
    fn prepared_command_rechecks_deadline_before_publication() {
        let directory = tempfile::tempdir().unwrap();
        let authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        let wal_before = authority.wal_len().unwrap();
        let database = Mutex::new(authority);
        let stores = StorageV2Stores::new(&database, None);
        let session = authenticator().authenticate(&hello()).unwrap();
        let mut command = request("record.put", 11,
            json!({"namespace": "deadline-test", "key": "k", "value": {"v": 1}}));
        command.command_id = Some("prepared-deadline".to_owned());
        command.deadline_unix_ms = 2_000;
        // Advance wall time after the preparation budget was sampled. No
        // amount of available commit capacity authorizes an expired command.
        let mut fresh_reads = 4;
        let mut now = || {
            if fresh_reads > 0 { fresh_reads -= 1; 1_000 } else { 2_000 }
        };
        let RequestDispatch::Responded(response) = dispatch_one_request(
            &stores, &session, &command, false, 1_000, &mut now, &mut || true).unwrap()
            else { panic!("live connection lost"); };
        assert_eq!(response.error_code.as_deref(), Some("deadline_elapsed"));
        let authority = database.lock().unwrap();
        assert_eq!(authority.wal_len().unwrap(), wal_before);
        assert_eq!(authority.snapshot_pin_metrics().unwrap().active_handles, 0);
    }

    #[test]
    fn parked_rotation_observes_deadline_and_disconnect_without_mutation() {
        let directory = tempfile::tempdir().unwrap();
        let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        let engine = authority.engine_mut_for_test();
        let store = engine.block_store_shared();
        let registration = store.register_background_packer().unwrap();
        engine.commit_transaction(b"covered", &[b"covered payload"]).unwrap();
        let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
            crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
            other => panic!("expected rotation, got {other:?}"),
        };
        let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
        // Capture a legacy single-WAL snapshot before attaching the driver:
        // its install still retires window writes and requires this fence.
        engine.attach_rotation_driver();
        engine.commit_transaction(b"window", &[b"window payload"]).unwrap();
        let _adoption = engine.prepare_pack_publication_for_test();
        assert_eq!(engine.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        let wal_before = authority.wal_len().unwrap();
        let staged_before = store.pending_staged_bytes().unwrap();
        let database = Mutex::new(authority);
        let barrier = std::sync::Arc::new(crate::rotation_barrier::RotationBarrier::new());
        let stores = StorageV2Stores::new(&database, None).with_rotation_barrier(barrier);
        let session = authenticator().authenticate(&hello()).unwrap();
        let mut command = request("record.put", 11,
            json!({"namespace": "park-test", "key": "k", "value": {"v": 1}}));
        command.command_id = Some("park-deadline".to_owned());
        command.deadline_unix_ms = 1_200;
        let start = Instant::now();
        let RequestDispatch::Responded(response) = dispatch_one_request(
            &stores, &session, &command, false, 1_000, &mut || 1_000, &mut || true).unwrap()
            else { panic!("live connection lost"); };
        assert_eq!(response.error_code.as_deref(), Some("database_busy"));
        assert!(start.elapsed() >= Duration::from_millis(100));
        assert!(start.elapsed() < Duration::from_secs(3));
        command.deadline_unix_ms = 61_000;
        let mut checks = 0;
        let start = Instant::now();
        assert!(matches!(dispatch_one_request(&stores, &session, &command,
            false, 1_000, &mut || 1_000, &mut || { checks += 1; checks < 3 }).unwrap(),
            RequestDispatch::ConnectionClosed));
        assert!(start.elapsed() < Duration::from_secs(1));
        assert_eq!(database.lock().unwrap().wal_len().unwrap(), wal_before);
        assert_eq!(store.pending_staged_bytes().unwrap(), staged_before);
        drop(registration);
    }

    #[test]
    fn connection_reassembles_identity_bound_blob_chunks_under_shared_budget() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let payload = serde_json::to_vec(&json!({
            "user_id":11,
            "content":"streamed tool result",
            "media_type":"text/plain",
            "created_at_ms":1_000,
            "expires_at_ms":2_000
        }))
        .unwrap();
        let midpoint = payload.len() / 2;
        let correlation_id = [7; 16];
        let stream_id = [8; 16];
        let command_id = "streamed-tool-put".to_owned();
        let chunk = |index: u32, final_chunk: bool, bytes: &[u8]| {
            Message::BlobChunk(crate::protocol::BlobChunk {
                correlation_id,
                deadline_unix_ms: 10_000,
                owner_id: 11,
                tenant_id: Some(7),
                command_id: Some(command_id.clone()),
                schema_id: STORAGE_SCHEMA_VERSION,
                payload: bytes.to_vec(),
                stream_id,
                chunk_index: index,
                final_chunk,
                total_payload_bytes: payload.len() as u64,
            })
        };
        let terminator = Request {
            correlation_id,
            deadline_unix_ms: 10_000,
            owner_id: 11,
            tenant_id: Some(7),
            command_id: Some(command_id.clone()),
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: "tool_result_artifact.put".to_owned(),
            payload: Vec::new(),
        };
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &chunk(0, false, &payload[..midpoint])).unwrap();
        write_frame(&mut input, &chunk(1, true, &payload[midpoint..])).unwrap();
        write_frame(&mut input, &Message::Request(terminator)).unwrap();
        let mut output = Vec::new();
        let frame_budget = FrameAdmissionBudget::maximum();
        let metrics = serve_connection(
            &mut Cursor::new(input),
            &mut output,
            &database,
            &authenticator(),
            &frame_budget,
            || 1_000,
        )
        .unwrap();
        assert_eq!(metrics.request_frames, 3);
        assert_eq!(metrics.response_frames, 2);
        let mut responses = Cursor::new(output);
        let _acknowledgement = read_frame(&mut responses).unwrap();
        let Message::Response(response) = read_frame(&mut responses).unwrap() else {
            panic!("streamed request did not emit a response");
        };
        assert_eq!(response.status, 0);
        let response: Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(response["sizeBytes"], "streamed tool result".len());
        assert!(response["artifactRef"]
            .as_str()
            .unwrap()
            .starts_with("tool-result:"));
        let budget_metrics = frame_budget.metrics().unwrap();
        assert_eq!(budget_metrics.in_flight_frames, 0);
        assert_eq!(budget_metrics.in_flight_bytes, 0);
        assert!(budget_metrics.peak_in_flight_frames >= 2);
        assert!(budget_metrics.peak_in_flight_bytes >= payload.len());
    }

    #[test]
    fn streamed_transport_defers_operation_and_command_admission_to_semantics() {
        for (operation, command_id) in [
            ("swarm.agent.save", Some("save")), ("turn.projection.update", Some("turn")),
            ("raw_archive.put", Some("archive")), ("record.get", None),
        ] {
            let session = authenticator().authenticate(&hello()).unwrap();
            let budget = FrameAdmissionBudget::maximum();
            let mut pending = None;
            PendingStreamedRequest::append(&mut pending, crate::protocol::BlobChunk {
                correlation_id:[7;16], deadline_unix_ms:10_000, owner_id:11,
                tenant_id:Some(7), command_id:command_id.map(str::to_owned),
                schema_id:STORAGE_SCHEMA_VERSION, payload:b"{}".to_vec(), stream_id:[8;16],
                chunk_index:0, final_chunk:true, total_payload_bytes:2,
            }, &session, &budget, 1_000).unwrap();
            let (request, reservations) = pending.unwrap().consume(Request {
                correlation_id:[7;16], deadline_unix_ms:10_000, owner_id:11,
                tenant_id:Some(7), command_id:command_id.map(str::to_owned),
                schema_id:STORAGE_SCHEMA_VERSION, operation:operation.to_owned(), payload:Vec::new(),
            }).unwrap();
            assert_eq!(session.admit_streamed(&request, 1_000).unwrap().operation.name, operation);
            if command_id.is_some() {
                let missing_id = Request {command_id:None, ..request};
                assert_eq!(session.admit_streamed(&missing_id, 1_000).unwrap_err().error_code.as_deref(), Some("command_id_required"));
            }
            drop(reservations);
            assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
        }
    }

    #[test]
    fn streamed_request_rejects_gaps_and_reserves_declared_total_once() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let budget = FrameAdmissionBudget::maximum();
        let make_chunk =
            |chunk_index, final_chunk, total_payload_bytes| crate::protocol::BlobChunk {
                correlation_id: [7; 16],
                deadline_unix_ms: 10_000,
                owner_id: 11,
                tenant_id: Some(7),
                command_id: Some("stream-budget".to_owned()),
                schema_id: STORAGE_SCHEMA_VERSION,
                payload: vec![1],
                stream_id: [8; 16],
                chunk_index,
                final_chunk,
                total_payload_bytes,
            };

        let mut pending = None;
        assert_eq!(
            PendingStreamedRequest::append(
                &mut pending,
                make_chunk(1, false, 2),
                &session,
                &budget,
                1_000,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidInput
        );
        PendingStreamedRequest::append(
            &mut pending,
            make_chunk(0, false, 2),
            &session,
            &budget,
            1_000,
        )
        .unwrap();
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 1);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 2);
        assert_eq!(
            PendingStreamedRequest::append(
                &mut pending,
                make_chunk(1, true, 3),
                &session,
                &budget,
                1_000,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidInput
        );
        PendingStreamedRequest::append(
            &mut pending,
            make_chunk(1, true, 2),
            &session,
            &budget,
            1_000,
        )
        .unwrap();
        drop(pending);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
    }

    #[test]
    fn maximum_streamed_request_has_owned_accounting_without_raw_buffer_quota() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let chunk = || crate::protocol::BlobChunk {
            correlation_id: [7; 16],
            deadline_unix_ms: 10_000,
            owner_id: 11,
            tenant_id: Some(7),
            command_id: Some("maximum-stream-budget".to_owned()),
            schema_id: STORAGE_SCHEMA_VERSION,
            payload: vec![1],
            stream_id: [8; 16],
            chunk_index: 0,
            final_chunk: false,
            total_payload_bytes: MAX_STREAMED_REQUEST_PAYLOAD_BYTES as u64,
        };

        let constrained = FrameAdmissionBudget::new(MAX_FRAME_BODY_BYTES).unwrap();
        let mut requested = None;
        PendingStreamedRequest::append(&mut requested, chunk(), &session, &constrained, 1_000).unwrap();
        assert_eq!(constrained.metrics().unwrap().owned_payload_bytes, MAX_STREAMED_REQUEST_PAYLOAD_BYTES);
        assert_eq!(constrained.metrics().unwrap().memory_buffer_bytes, 0);
        drop(requested);
        assert_eq!(constrained.metrics().unwrap().in_flight_bytes, 0);

        let sufficient = FrameAdmissionBudget::maximum();
        let mut accepted = None;
        PendingStreamedRequest::append(&mut accepted, chunk(), &session, &sufficient, 1_000)
            .unwrap();
        let accepted_state = accepted.as_ref().unwrap();
        assert_eq!(
            accepted_state.payload.capacity(),
            MAX_STREAMED_REQUEST_PAYLOAD_BYTES
        );
        assert_eq!(accepted_state.payload.len(), 1);
        let metrics = sufficient.metrics().unwrap();
        assert_eq!(metrics.in_flight_frames, 1);
        assert_eq!(metrics.in_flight_bytes, MAX_STREAMED_REQUEST_PAYLOAD_BYTES);
        drop(accepted);
        let released = sufficient.metrics().unwrap();
        assert_eq!(released.in_flight_frames, 0);
        assert_eq!(released.in_flight_bytes, 0);
        assert_eq!(released.peak_in_flight_frames, 1);
        assert_eq!(
            released.peak_in_flight_bytes,
            MAX_STREAMED_REQUEST_PAYLOAD_BYTES
        );
    }

    #[test]
    fn large_success_response_streams_ordered_chunks_then_empty_terminator() {
        let payload = vec![7; STREAMED_RESPONSE_CHUNK_BYTES * 2 + 7];
        let response = Response {
            correlation_id: [12; 16],
            schema_id: STORAGE_SCHEMA_VERSION,
            payload: payload.clone(),
            status: 0,
            error_code: None,
            error_message: None,
            retryable: false,
        };
        let budget = FrameAdmissionBudget::maximum();
        let mut encoded = Vec::new();
        assert_eq!(
            write_response_frames(&mut encoded, response, &budget).unwrap(),
            4
        );

        let mut reader = Cursor::new(encoded);
        let mut reassembled = Vec::new();
        for expected_index in 0..3 {
            let Message::ResponseChunk(chunk) = read_frame(&mut reader).unwrap() else {
                panic!("large response did not emit an ordered response chunk");
            };
            assert_eq!(chunk.correlation_id, [12; 16]);
            assert_eq!(chunk.schema_id, STORAGE_SCHEMA_VERSION);
            assert_eq!(chunk.stream_id, [12; 16]);
            assert_eq!(chunk.chunk_index, expected_index);
            assert_eq!(chunk.final_chunk, expected_index == 2);
            reassembled.extend_from_slice(&chunk.payload);
        }
        let Message::Response(terminator) = read_frame(&mut reader).unwrap() else {
            panic!("large response did not emit its success terminator");
        };
        assert_eq!(terminator.correlation_id, [12; 16]);
        assert_eq!(terminator.status, 0);
        assert!(terminator.payload.is_empty());
        assert_eq!(reassembled, payload);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
    }

    #[test]
    fn response_above_stream_bound_becomes_one_resource_error() {
        let response = Response {
            correlation_id: [13; 16],
            schema_id: STORAGE_SCHEMA_VERSION,
            payload: vec![0; MAX_STREAMED_RESPONSE_PAYLOAD_BYTES + 1],
            status: 0,
            error_code: None,
            error_message: None,
            retryable: false,
        };
        let budget = FrameAdmissionBudget::maximum();
        let mut encoded = Vec::new();
        assert_eq!(
            write_response_frames(&mut encoded, response, &budget).unwrap(),
            1
        );
        let Message::Response(response) = read_frame(&mut Cursor::new(encoded)).unwrap() else {
            panic!("oversized response did not become a bounded error");
        };
        assert_eq!(response.correlation_id, [13; 16]);
        assert_eq!(response.status, STATUS_RESOURCE_EXHAUSTED);
        assert_eq!(response.error_code.as_deref(), Some("response_too_large"));
        assert!(response.payload.is_empty());
    }

    #[test]
    fn connection_streams_large_artifact_get_without_expanding_frame_limit() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let content = "z".repeat(STREAMED_RESPONSE_CHUNK_BYTES + 17);
        let mut create = request(
            "artifact.create",
            11,
            json!({
                "artifact_id":"large-artifact",
                "conv_id":"large-conversation",
                "source":"write_file",
                "source_ref":{"path":"large.md"},
                "format":"markdown",
                "title":"large.md",
                "content":content,
                "meta":{},
                "created_at":1_000
            }),
        );
        create.command_id = Some("create-large-artifact".to_owned());
        let get = request(
            "artifact.get",
            11,
            json!({"artifact_id":"large-artifact","include_content":true}),
        );
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &Message::Request(create)).unwrap();
        write_frame(&mut input, &Message::Request(get)).unwrap();
        let mut output = Vec::new();
        let budget = FrameAdmissionBudget::maximum();
        let metrics = serve_connection(
            &mut Cursor::new(input),
            &mut output,
            &database,
            &authenticator(),
            &budget,
            || 1_000,
        )
        .unwrap();
        assert_eq!(metrics.request_frames, 2);
        assert_eq!(metrics.response_frames, 5);

        let mut reader = Cursor::new(output);
        let _acknowledgement = read_frame(&mut reader).unwrap();
        let Message::Response(created) = read_frame(&mut reader).unwrap() else {
            panic!("artifact create response was not singular");
        };
        assert_eq!(created.status, 0);
        let mut reassembled = Vec::new();
        for expected_index in 0..2 {
            let Message::ResponseChunk(chunk) = read_frame(&mut reader).unwrap() else {
                panic!("artifact get response was not streamed");
            };
            assert_eq!(chunk.chunk_index, expected_index);
            assert_eq!(chunk.final_chunk, expected_index == 1);
            reassembled.extend_from_slice(&chunk.payload);
        }
        let Message::Response(terminator) = read_frame(&mut reader).unwrap() else {
            panic!("artifact get stream lacked its terminator");
        };
        assert_eq!(terminator.status, 0);
        assert!(terminator.payload.is_empty());
        let document: Value = serde_json::from_slice(&reassembled).unwrap();
        assert_eq!(document["content"].as_str().unwrap(), content);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 0);
    }

    #[test]
    fn conversation_search_uses_projection_without_acquiring_authority() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let database =
            Mutex::new(AuthorityDatabase::initialize(authority_directory.path()).unwrap());
        let mut projection =
            TurnSearchProjection::initialize(projection_directory.path(), 128 * 1024 * 1024)
                .unwrap();
        projection
            .begin_conversation_rebuild(7, 11, "conversation-1", "generation-1", 5_000)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation-1",
                "generation-1",
                &[TurnSearchDocument {
                    turn_id: "turn-1".to_owned(),
                    ordinal: 1,
                    search_text: "independent projection lane".to_owned(),
                    chunk_index: 0,
                    role: "user".into(),
                    match_field: "content".into(),
                }],
            )
            .unwrap();
        projection
            .finalize_conversation_rebuild(7, 11, "conversation-1", "generation-1")
            .unwrap();
        let projection = Mutex::new(projection);
        let authority_guard = database.lock().unwrap();
        let search = request(
            "conversation.search",
            11,
            json!({"user_id": 11, "query": "projection", "limit": 50}),
        );
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &Message::Request(search)).unwrap();
        let mut output = Vec::new();

        let metrics = serve_connection_until(
            &mut Cursor::new(input),
            &mut output,
            StorageV2Stores::new(&database, Some(&projection)),
            &authenticator(),
            &FrameAdmissionBudget::maximum(),
            DEFAULT_SESSION_IDLE_TIMEOUT,
            || 1_000,
            || true,
        )
        .unwrap();
        drop(authority_guard);

        assert_eq!(metrics.request_frames, 1);
        let mut responses = Cursor::new(output);
        read_frame(&mut responses).unwrap();
        let Message::Response(response) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not emit a search response");
        };
        assert_eq!(response.status, 0);
        assert_eq!(
            serde_json::from_slice::<Value>(&response.payload).unwrap(),
            json!([{"id": "conversation-1", "snippet": "…independent projection lane…",
                "role": "user", "match_field": "content"}])
        );
    }

    #[test]
    fn connection_rejects_expired_request_without_waiting_for_authority() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let authority_guard = database.lock().unwrap();
        let mut expired = request("desktop.egress_agent.get", 11, json!({"owner_user_id": 11}));
        expired.deadline_unix_ms = 999;
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &Message::Request(expired.clone())).unwrap();
        let mut output = Vec::new();

        let metrics = serve_connection(
            &mut Cursor::new(input),
            &mut output,
            &database,
            &authenticator(),
            &FrameAdmissionBudget::maximum(),
            || 1_000,
        )
        .unwrap();
        drop(authority_guard);

        assert_eq!(metrics.request_frames, 1);
        let mut responses = Cursor::new(output);
        read_frame(&mut responses).unwrap();
        let Message::Response(response) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not emit a deadline response");
        };
        assert_eq!(response.correlation_id, expired.correlation_id);
        assert_eq!(response.status, STATUS_DEADLINE_ELAPSED);
        assert_eq!(response.error_code.as_deref(), Some("deadline_elapsed"));
    }

    #[test]
    fn connection_authority_acquisition_is_bounded_by_request_deadline() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let authority_guard = database.lock().unwrap();
        let mut bounded = request("desktop.egress_agent.get", 11, json!({"owner_user_id": 11}));
        bounded.deadline_unix_ms = 1_002;
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &Message::Request(bounded.clone())).unwrap();
        let mut output = Vec::new();

        let started_at = Instant::now();
        let metrics = serve_connection(
            &mut Cursor::new(input),
            &mut output,
            &database,
            &authenticator(),
            &FrameAdmissionBudget::maximum(),
            || 1_000,
        )
        .unwrap();
        let elapsed = started_at.elapsed();
        drop(authority_guard);

        assert_eq!(metrics.request_frames, 1);
        assert!(elapsed < Duration::from_secs(1));
        let mut responses = Cursor::new(output);
        read_frame(&mut responses).unwrap();
        let Message::Response(response) = read_frame(&mut responses).unwrap() else {
            panic!("connection did not emit an acquisition deadline response");
        };
        assert_eq!(response.correlation_id, bounded.correlation_id);
        assert_eq!(response.status, STATUS_DEADLINE_ELAPSED);
        assert_eq!(response.error_code.as_deref(), Some("deadline_elapsed"));
    }

    #[test]
    fn authority_acquisition_releases_connection_worker_during_shutdown() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let authority_guard = database.lock().unwrap();
        let mut connection_alive = || false;

        let admission = crate::authority_admission::AuthorityAdmission::default();
        let outcome = acquire_authority_until(&database, 60_000, &mut connection_alive, &admission).unwrap();
        drop(authority_guard);

        assert!(matches!(outcome, AuthorityAcquisition::ConnectionClosed));
    }

    #[test]
    fn connection_rejects_wrong_token_without_ack_or_storage_io() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let database = Mutex::new(
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap(),
        );
        let mut invalid_hello = hello();
        invalid_hello.auth_token = [8; crate::protocol::AUTH_TOKEN_BYTES];
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(invalid_hello)).unwrap();
        let mut output = Vec::new();
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            serve_connection(
                &mut Cursor::new(input),
                &mut output,
                &database,
                &authenticator(),
                &FrameAdmissionBudget::maximum(),
                || 1_000,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::PermissionDenied
        );
        assert!(output.is_empty());
        assert!(vfs.trace().unwrap().is_empty());
    }

    #[test]
    fn connection_distinguishes_clean_eof_from_a_truncated_next_frame() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        input.extend_from_slice(&[0, 0, 1]);
        assert_eq!(
            serve_connection(
                &mut Cursor::new(input),
                &mut Vec::new(),
                &database,
                &authenticator(),
                &FrameAdmissionBudget::maximum(),
                || 1_000,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::UnexpectedEof
        );
    }

    #[test]
    fn small_frame_reservations_exceed_retired_count_quota_and_release_all_bytes() {
        let budget = FrameAdmissionBudget::new(MAX_FRAME_BODY_BYTES).unwrap();
        let guards: Vec<_> = (0..1024).map(|_| budget.admit(64).unwrap()).collect();
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 1024);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 65536);
        drop(guards);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
    }

    #[test]
    fn aggregate_frame_threshold_spills_without_refusal_and_releases_each_owner() {
        let budget = FrameAdmissionBudget::new(MAX_FRAME_BODY_BYTES).unwrap();
        let memory = budget.plan_frame(MAX_FRAME_BODY_BYTES).unwrap();
        let file = budget.plan_frame(1).unwrap();
        assert!(!memory.spilled && file.spilled);
        let metrics = budget.metrics().unwrap();
        assert_eq!(metrics.memory_buffer_bytes, MAX_FRAME_BODY_BYTES);
        assert_eq!(metrics.file_buffer_bytes, 1);
        assert_eq!(metrics.in_flight_bytes, MAX_FRAME_BODY_BYTES + 1);
        drop(memory); drop(file);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
    }

    /// A query admitted against a replica executes with NO authority
    /// mutex held: build the replica first (brief hold), then lock the
    /// authority and prove the dispatch still answers.
    #[test]
    fn query_dispatch_executes_outside_the_authority_mutex() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());

        // Seed one conversation through the writer lane.
        let mut create = request(
            "conversation.create",
            11,
            json!({"conv_id":"replica-lane-conv","user_id":11,"title":"replica lane"}),
        );
        create.command_id = Some("replica-lane-create-1".to_owned());
        let count = request("conversation.count", 11, json!({"user_id": 11}));
        let mut input = Vec::new();
        write_frame(&mut input, &Message::Hello(hello())).unwrap();
        write_frame(&mut input, &Message::Request(create)).unwrap();
        write_frame(&mut input, &Message::Request(count)).unwrap();
        let mut output = Vec::new();
        serve_connection(
            &mut Cursor::new(input),
            &mut output,
            &database,
            &authenticator(),
            &FrameAdmissionBudget::maximum(),
            || 1_000,
        )
        .unwrap();
        let mut reader = Cursor::new(output);
        let _ack = read_frame(&mut reader).unwrap();
        let Message::Response(created) = read_frame(&mut reader).unwrap() else {
            panic!("create response missing");
        };
        assert_eq!(created.status, 0);
        let Message::Response(counted) = read_frame(&mut reader).unwrap() else {
            panic!("count response missing");
        };
        assert_eq!(
            counted.status,
            0,
            "count failed: {}",
            String::from_utf8_lossy(&counted.payload)
        );
        let count_payload: serde_json::Value = serde_json::from_slice(&counted.payload).unwrap();
        assert_eq!(count_payload["count"], 1);

        // Now the decisive property: a replica built under a brief hold
        // executes its whole query while the authority stays locked.
        let session = authenticator().authenticate(&hello()).unwrap();
        let mut replica = {
            let authority = database.lock().unwrap();
            authority.build_read_replica().unwrap()
        };
        let authority_guard = database.lock().unwrap();
        let count = request("conversation.count", 11, json!({"user_id": 11}));
        let admitted = admit_request(&count, session.negotiated(), scope(), 1_000).unwrap();
        let response = session.dispatch_admitted(
            &mut replica,
            admitted,
            1_000,
            AuthorityGarbageCollectionBudget::conservative(),
        );
        assert_eq!(response.status, 0);
        let payload: serde_json::Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(payload["count"], 1);
        drop(authority_guard);
    }

    #[test]
    fn reader_capture_crosses_retired_count_quota_and_preserves_close_and_deadline() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let admission = crate::authority_admission::AuthorityAdmission::default();
        let replicas: Vec<_> = (0..512)
            .map(|_| database.lock().unwrap().build_read_replica().unwrap()).collect();
        assert!(matches!(build_read_replica_until(&database, 0, &mut || true, &admission).unwrap(), ReadReplicaAcquisition::DeadlineElapsed));
        assert!(matches!(build_read_replica_until(&database, 1000, &mut || false, &admission).unwrap(), ReadReplicaAcquisition::ConnectionClosed));
        let ReadReplicaAcquisition::Built(replica) = build_read_replica_until(&database, 1000, &mut || true, &admission).unwrap() else {
            panic!("reader was refused while old readers remained live");
        };
        database.lock().unwrap().begin(7, 11).unwrap();
        drop(replica);
        drop(replicas);
    }

    #[test]
    fn preparation_capture_crosses_retired_count_quota_without_waiting_for_release() {
        let directory = tempfile::tempdir().unwrap();
        let database = Mutex::new(AuthorityDatabase::initialize(directory.path()).unwrap());
        let admission = crate::authority_admission::AuthorityAdmission::default();
        let replicas: Vec<_> = (0..512)
            .map(|_| database.lock().unwrap().build_read_replica().unwrap()).collect();
        {
            let mut authority = database.lock().unwrap();
            let mut transaction = authority.begin(7, 11).unwrap();
            authority.entity_put(&mut transaction,
                crate::entity::EntityKey::new(7, 11, "preparation", b"progress").unwrap(),
                b"committed".to_vec()).unwrap();
            authority.commit(transaction).unwrap();
        }
        let ReplicaBuildOutcome::Built(replica) = build_replica_until(&database, 1000, &mut || true,
            &admission, ReplicaPurpose::Preparation, None).unwrap() else { panic!("preparation refused") };
        assert!(replica.is_preparing_transaction());
        let mut transaction = replica.begin(7, 11).unwrap();
        assert_eq!(replica.entity_get(&mut transaction,
            &crate::entity::EntityKey::new(7, 11, "preparation", b"progress").unwrap()).unwrap(), Some(b"committed".to_vec()));
        drop(transaction);
        drop(replica);
        drop(replicas);
    }
}

#[cfg(test)]
mod published_query_tests {
    use super::*;
    use std::sync::{Arc, mpsc};
    fn writer() -> (tempfile::TempDir, Mutex<AuthorityDatabase>, crate::authority::PublishedQueryView) {
        let directory = tempfile::tempdir().unwrap();
        let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut tx = authority.begin(7, 11).unwrap();
        authority.entity_put(&mut tx, crate::entity::EntityKey::new(7, 11, "published", b"k").unwrap(), b"old".to_vec()).unwrap();
        authority.commit(tx).unwrap(); let view = authority.enable_published_queries().unwrap();
        (directory, Mutex::new(authority), view)
    }
    #[test]
    fn published_query_acquires_while_writer_fifo_and_authority_are_held() {
        let (_directory, database, view) = writer();
        let gate = crate::authority_admission::AuthorityAdmission::default();
        let crate::authority_admission::Admission::Entered(_permit) = gate.acquire(Instant::now()+Duration::from_secs(5), &mut || true).unwrap() else { panic!() };
        let _writer_guard = database.lock().unwrap();
        thread::scope(|scope| {
            let (tx, rx) = mpsc::channel();
            let database = &database; let gate = &gate; let view = &view;
            scope.spawn(move || {
                let result = build_query_replica_until(database, 100, &mut || true, gate, Some(view)).unwrap();
                tx.send(matches!(result, ReadReplicaAcquisition::Built(_))).unwrap();
            });
            assert!(rx.recv_timeout(Duration::from_secs(2)).unwrap());
        });
    }
    #[test]
    fn published_queries_start_with_all_old_readers_and_writer_guards_retained() {
        let (_directory, database, view) = writer();
        let gate = crate::authority_admission::AuthorityAdmission::default();
        let readers: Vec<_> = (0..512).map(|_| view.try_build().unwrap().unwrap()).collect();
        assert!(matches!(build_query_replica_until(&database, 0, &mut || true, &gate, Some(&view)).unwrap(), ReadReplicaAcquisition::DeadlineElapsed));
        assert!(matches!(build_query_replica_until(&database, 1000, &mut || false, &gate, Some(&view)).unwrap(), ReadReplicaAcquisition::ConnectionClosed));
        let crate::authority_admission::Admission::Entered(_permit) = gate.acquire(Instant::now()+Duration::from_secs(5), &mut || true).unwrap() else { panic!() };
        let _authority = database.lock().unwrap();
        thread::scope(|scope| {
            let (sender, receiver) = mpsc::channel();
            for _ in 0..32 {
                let sender = sender.clone(); let database = &database; let gate = &gate; let view = &view;
                scope.spawn(move || {
                    let result = build_query_replica_until(database, 1000, &mut || true, gate, Some(view)).unwrap();
                    sender.send(matches!(result, ReadReplicaAcquisition::Built(_))).unwrap();
                });
            }
            for _ in 0..32 { assert!(receiver.recv_timeout(Duration::from_secs(5)).unwrap()); }
        });
        drop(_authority);
        drop(readers);
    }
    #[test]
    fn published_query_does_not_bypass_poisoned_authority() {
        let (_directory, database, view) = writer(); let database = Arc::new(database);
        let other = Arc::clone(&database);
        assert!(thread::spawn(move || { let _guard = other.lock().unwrap(); panic!("authority poison"); }).join().is_err());
        let gate = crate::authority_admission::AuthorityAdmission::default();
        assert_eq!(build_query_replica_until(&database, 100, &mut || true, &gate, Some(&view)).err().unwrap().to_string(), "storage.v2 authority mutex is poisoned");
    }
}


#[cfg(test)]
mod snapshot_capture_deadline_tests {
    use super::*;
    #[test]
    fn authority_capture_preserves_deadline_and_connection_close_without_leaking_roots() {
        for connection_closes in [true, false] {
            let directory = tempfile::tempdir().unwrap();
            let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
            let view = authority.enable_published_queries().unwrap();
            let database = Mutex::new(authority);
            let held = database.lock().unwrap();
            let admission = crate::authority_admission::AuthorityAdmission::default();
            let mut probes = 0;
            let started = Instant::now();
            let outcome = build_replica_until(&database, 20, &mut || {
                probes += 1;
                !connection_closes || probes == 1
            }, &admission, ReplicaPurpose::Preparation, Some(&view)).unwrap();
            if connection_closes {
                assert!(matches!(outcome, ReplicaBuildOutcome::ConnectionClosed));
            } else {
                assert!(matches!(outcome, ReplicaBuildOutcome::DeadlineElapsed));
            }
            assert!(started.elapsed() < Duration::from_secs(1));
            drop(held);
            assert_eq!(database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0);
            assert!(view.try_build().unwrap().is_ok());
        }
    }
}


#[cfg(test)]
mod poisoned_snapshot_capture_tests {
    use super::*;
    use std::sync::Arc;
    #[test]
    fn snapshot_capture_keeps_authority_poison_visible_with_many_live_readers() {
        let directory = tempfile::tempdir().unwrap();
        let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        let view = authority.enable_published_queries().unwrap();
        let readers: Vec<_> = (0..512).map(|_| view.try_build().unwrap().unwrap()).collect();
        let database = Arc::new(Mutex::new(authority));
        let poison = Arc::clone(&database);
        assert!(std::thread::spawn(move || {
            let _guard = poison.lock().unwrap();
            panic!("intentional authority poison");
        }).join().is_err());
        let admission = crate::authority_admission::AuthorityAdmission::default();
        for purpose in [ReplicaPurpose::Preparation, ReplicaPurpose::Query] {
            let result = build_replica_until(&database, 1000, &mut || true,
                &admission, purpose, Some(&view));
            let error = match result { Err(error) => error, Ok(_) => panic!("authority poison was hidden") };
            assert_eq!(error.kind(), io::ErrorKind::Other);
            assert_eq!(error.to_string(), "storage.v2 authority mutex is poisoned");
        }
        drop(readers);
    }
}
