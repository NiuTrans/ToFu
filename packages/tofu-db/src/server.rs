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
use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use crate::generated_storage_v2::{
    MAX_FRAME_BODY_BYTES, MAX_IN_FLIGHT_FRAMES, MAX_IN_FLIGHT_FRAME_BYTES,
    MAX_STREAMED_REQUEST_CHUNKS, MAX_STREAMED_REQUEST_PAYLOAD_BYTES, MAX_STREAMED_RESPONSE_CHUNKS,
    MAX_STREAMED_RESPONSE_PAYLOAD_BYTES, STATUS_BAD_REQUEST, STATUS_CONFLICT,
    STATUS_DEADLINE_ELAPSED, STATUS_FORBIDDEN, STATUS_INTEGRITY_FAILURE, STATUS_NOT_IMPLEMENTED,
    STATUS_RESOURCE_EXHAUSTED, STATUS_UNAVAILABLE, STREAMED_RESPONSE_CHUNK_BYTES,
};
use crate::protocol::{
    read_frame_or_eof_with_admission, write_frame_with_admission, Hello, Message,
    NegotiatedProtocol, Request, Response, ResponseChunk,
};
use crate::semantic::{admit_request, admit_streamed_request, AdmissionError, AuthenticatedScope};
use crate::semantic_executor::{
    execute_admitted_request_with_gc_budget, execute_search_admitted_request,
    SemanticExecutionError,
};
use crate::turn_search_projection::TurnSearchProjection;

#[derive(Clone, Debug)]
pub struct FrameAdmissionBudget {
    shared: Arc<Mutex<FrameAdmissionState>>,
    maximum_frames: usize,
    maximum_bytes: usize,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FrameAdmissionMetrics {
    pub in_flight_frames: usize,
    pub in_flight_bytes: usize,
    pub peak_in_flight_frames: usize,
    pub peak_in_flight_bytes: usize,
}

#[derive(Debug, Default)]
struct FrameAdmissionState {
    metrics: FrameAdmissionMetrics,
}

#[derive(Debug)]
pub struct FrameAdmissionGuard {
    shared: Arc<Mutex<FrameAdmissionState>>,
    bytes: usize,
}

impl FrameAdmissionBudget {
    pub fn new(maximum_frames: usize, maximum_bytes: usize) -> io::Result<Self> {
        if maximum_frames == 0
            || maximum_frames > MAX_IN_FLIGHT_FRAMES
            || !(MAX_FRAME_BODY_BYTES..=MAX_IN_FLIGHT_FRAME_BYTES).contains(&maximum_bytes)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid storage.v2 aggregate frame budget",
            ));
        }
        Ok(Self {
            shared: Arc::default(),
            maximum_frames,
            maximum_bytes,
        })
    }

    pub fn maximum() -> Self {
        Self::new(MAX_IN_FLIGHT_FRAMES, MAX_IN_FLIGHT_FRAME_BYTES)
            .expect("generated storage.v2 frame limits are valid")
    }

    pub fn admit(&self, bytes: usize) -> io::Result<FrameAdmissionGuard> {
        if bytes == 0 || bytes > MAX_FRAME_BODY_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage.v2 frame reservation is invalid",
            ));
        }
        self.admit_counted(bytes)
    }

    fn admit_streamed_payload(&self, bytes: usize) -> io::Result<FrameAdmissionGuard> {
        if bytes == 0 || bytes > MAX_STREAMED_REQUEST_PAYLOAD_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "storage.v2 streamed payload reservation is invalid",
            ));
        }
        self.admit_counted(bytes)
    }

    fn admit_counted(&self, bytes: usize) -> io::Result<FrameAdmissionGuard> {
        let mut state = self
            .shared
            .lock()
            .map_err(|_| io::Error::other("storage.v2 frame budget is poisoned"))?;
        let next_frames = state.metrics.in_flight_frames + 1;
        let next_bytes = state
            .metrics
            .in_flight_bytes
            .checked_add(bytes)
            .filter(|next| *next <= self.maximum_bytes);
        if next_frames > self.maximum_frames || next_bytes.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "storage.v2 aggregate frame budget is exhausted",
            ));
        }
        state.metrics.in_flight_frames = next_frames;
        state.metrics.in_flight_bytes = next_bytes.unwrap();
        state.metrics.peak_in_flight_frames = state.metrics.peak_in_flight_frames.max(next_frames);
        state.metrics.peak_in_flight_bytes = state
            .metrics
            .peak_in_flight_bytes
            .max(state.metrics.in_flight_bytes);
        drop(state);
        Ok(FrameAdmissionGuard {
            shared: Arc::clone(&self.shared),
            bytes,
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
        write_frame_with_admission(writer, &Message::Response(response), |bytes| {
            frame_budget.admit(bytes)
        })?;
        return Ok(1);
    }
    if response.payload.len() > MAX_STREAMED_RESPONSE_PAYLOAD_BYTES {
        response.payload.clear();
        response.status = STATUS_RESOURCE_EXHAUSTED;
        response.error_code = Some("response_too_large".to_owned());
        response.error_message = Some("response_too_large".to_owned());
        response.retryable = false;
        write_frame_with_admission(writer, &Message::Response(response), |bytes| {
            frame_budget.admit(bytes)
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
        write_frame_with_admission(writer, &chunk, |bytes| frame_budget.admit(bytes))?;
    }
    response.payload.clear();
    write_frame_with_admission(writer, &Message::Response(response), |bytes| {
        frame_budget.admit(bytes)
    })?;
    u64::try_from(chunk_count + 1)
        .map_err(|_| io::Error::other("storage.v2 response metric overflow"))
}

struct PendingStreamedRequest {
    correlation_id: [u8; 16],
    deadline_unix_ms: u64,
    owner_id: u64,
    tenant_id: Option<u64>,
    command_id: String,
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
            || request.command_id.as_deref() != Some(self.command_id.as_str())
            || request.schema_id != self.schema_id
            || !matches!(
                request.operation.as_str(),
                "artifact.create" | "task_results.checkpoint" | "tool_result_artifact.put"
            )
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

#[derive(Clone, Copy)]
pub(crate) struct StorageV2Stores<'stores> {
    authority: &'stores Mutex<AuthorityDatabase>,
    search_projection: Option<&'stores Mutex<TurnSearchProjection>>,
    authority_gc_budget: AuthorityGarbageCollectionBudget,
}

impl<'stores> StorageV2Stores<'stores> {
    pub(crate) const fn new(
        authority: &'stores Mutex<AuthorityDatabase>,
        search_projection: Option<&'stores Mutex<TurnSearchProjection>>,
    ) -> Self {
        Self {
            authority,
            search_projection,
            authority_gc_budget: AuthorityGarbageCollectionBudget::conservative(),
        }
    }

    pub(crate) const fn with_authority_gc_budget(
        mut self,
        authority_gc_budget: AuthorityGarbageCollectionBudget,
    ) -> Self {
        self.authority_gc_budget = authority_gc_budget;
        self
    }
}

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
        )
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
    ) -> Response {
        let request = admitted.request;
        let result = execute_admitted_request_with_gc_budget(
            database,
            admitted,
            now_unix_ms,
            authority_gc_budget,
        )
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
    Acquired(MutexGuard<'database, AuthorityDatabase>),
    DeadlineElapsed,
    ConnectionClosed,
}

fn acquire_authority_until<'database>(
    database: &'database Mutex<AuthorityDatabase>,
    remaining_millis: u64,
    connection_alive: &mut impl FnMut() -> bool,
) -> io::Result<AuthorityAcquisition<'database>> {
    let started_at = Instant::now();
    let wait_budget = Duration::from_millis(remaining_millis);
    loop {
        match database.try_lock() {
            Ok(authority) => return Ok(AuthorityAcquisition::Acquired(authority)),
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
    mut now_unix_ms: impl FnMut() -> u64,
    mut connection_alive: impl FnMut() -> bool,
) -> io::Result<ConnectionMetrics> {
    let first = read_frame_or_eof_with_admission(reader, |bytes| frame_budget.admit(bytes))?
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
    write_frame_with_admission(writer, &acknowledgement, |bytes| frame_budget.admit(bytes))?;

    let mut metrics = ConnectionMetrics {
        request_frames: 0,
        response_frames: 1,
    };
    let mut pending_streamed_request = None;
    loop {
        if !connection_alive() {
            return Ok(metrics);
        }
        let message =
            match read_frame_or_eof_with_admission(reader, |bytes| frame_budget.admit(bytes)) {
                Ok(Some(message)) => message,
                Ok(None) => return Ok(metrics),
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
        let admission = if streamed {
            session.admit_streamed(&request, admitted_at_ms)
        } else {
            session.admit(&request, admitted_at_ms)
        };
        let response = match admission {
            Err(response) => response,
            Ok(admitted) if admitted.operation.name == "conversation.search" => {
                match stores.search_projection {
                    None => error_response(
                        &request,
                        session.negotiated.schema_id,
                        SessionDispatchError::Execution(SemanticExecutionError::StorageUnavailable),
                    ),
                    Some(projection) => match acquire_search_projection_until(
                        projection,
                        admitted.remaining_millis,
                        &mut connection_alive,
                    )? {
                        SearchProjectionAcquisition::Acquired(projection) => {
                            session.dispatch_search_admitted(&projection, admitted, now_unix_ms())
                        }
                        SearchProjectionAcquisition::DeadlineElapsed => error_response(
                            &request,
                            session.negotiated.schema_id,
                            SessionDispatchError::Admission(AdmissionError::DeadlineElapsed),
                        ),
                        SearchProjectionAcquisition::ConnectionClosed => return Ok(metrics),
                    },
                }
            }
            Ok(admitted) => match acquire_authority_until(
                stores.authority,
                admitted.remaining_millis,
                &mut connection_alive,
            )? {
                AuthorityAcquisition::Acquired(mut authority) => session.dispatch_admitted(
                    &mut authority,
                    admitted,
                    now_unix_ms(),
                    stores.authority_gc_budget,
                ),
                AuthorityAcquisition::DeadlineElapsed => error_response(
                    &request,
                    session.negotiated.schema_id,
                    SessionDispatchError::Admission(AdmissionError::DeadlineElapsed),
                ),
                AuthorityAcquisition::ConnectionClosed => return Ok(metrics),
            },
        };
        metrics.request_frames = metrics
            .request_frames
            .checked_add(1)
            .ok_or_else(|| io::Error::other("storage.v2 request metric overflow"))?;
        drop(_stream_reservations);
        let written_response_frames = write_response_frames(writer, response, frame_budget)?;
        metrics.response_frames = metrics
            .response_frames
            .checked_add(written_response_frames)
            .ok_or_else(|| io::Error::other("storage.v2 response metric overflow"))?;
    }
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SessionDispatchError {
    Admission(AdmissionError),
    Execution(SemanticExecutionError),
}

fn error_response(
    request: &Request,
    response_schema_id: u32,
    error: SessionDispatchError,
) -> Response {
    let (status, code, retryable) = match error {
        SessionDispatchError::Admission(admission) => {
            let status = match admission {
                AdmissionError::AuthorityScopeMismatch => STATUS_FORBIDDEN,
                AdmissionError::DeadlineElapsed => STATUS_DEADLINE_ELAPSED,
                _ => STATUS_BAD_REQUEST,
            };
            (status, admission.code(), admission.retryable())
        }
        SessionDispatchError::Execution(execution) => {
            let status = match execution {
                SemanticExecutionError::InvalidPayload => STATUS_BAD_REQUEST,
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
                SemanticExecutionError::Integrity => STATUS_INTEGRITY_FAILURE,
                SemanticExecutionError::StorageUnavailable => STATUS_UNAVAILABLE,
                SemanticExecutionError::PayloadTooLarge
                | SemanticExecutionError::ResourceExhausted => STATUS_RESOURCE_EXHAUSTED,
            };
            (status, execution.code(), execution.retryable())
        }
    };
    Response {
        correlation_id: request.correlation_id,
        schema_id: response_schema_id,
        payload: Vec::new(),
        status,
        error_code: Some(code.to_owned()),
        error_message: Some(code.to_owned()),
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
                command_id: command_id.clone(),
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
    fn streamed_request_rejects_gaps_and_reserves_declared_total_once() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let budget = FrameAdmissionBudget::maximum();
        let make_chunk =
            |chunk_index, final_chunk, total_payload_bytes| crate::protocol::BlobChunk {
                correlation_id: [7; 16],
                deadline_unix_ms: 10_000,
                owner_id: 11,
                tenant_id: Some(7),
                command_id: "stream-budget".to_owned(),
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
    fn maximum_streamed_request_is_budgeted_once_and_pressure_fails_before_allocation() {
        let session = authenticator().authenticate(&hello()).unwrap();
        let chunk = || crate::protocol::BlobChunk {
            correlation_id: [7; 16],
            deadline_unix_ms: 10_000,
            owner_id: 11,
            tenant_id: Some(7),
            command_id: "maximum-stream-budget".to_owned(),
            schema_id: STORAGE_SCHEMA_VERSION,
            payload: vec![1],
            stream_id: [8; 16],
            chunk_index: 0,
            final_chunk: false,
            total_payload_bytes: MAX_STREAMED_REQUEST_PAYLOAD_BYTES as u64,
        };

        let constrained = FrameAdmissionBudget::new(1, MAX_FRAME_BODY_BYTES).unwrap();
        let mut rejected = None;
        assert_eq!(
            PendingStreamedRequest::append(&mut rejected, chunk(), &session, &constrained, 1_000,)
                .unwrap_err()
                .kind(),
            io::ErrorKind::OutOfMemory
        );
        assert!(rejected.is_none());
        assert_eq!(
            constrained.metrics().unwrap(),
            FrameAdmissionMetrics::default()
        );

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
            json!([{"id": "conversation-1", "snippet": "…independent projection lane…"}])
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

        let outcome = acquire_authority_until(&database, 60_000, &mut connection_alive).unwrap();
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
    fn aggregate_frame_budget_backpressures_and_releases_with_guard_lifetime() {
        let budget = FrameAdmissionBudget::new(1, MAX_FRAME_BODY_BYTES).unwrap();
        let guard = budget.admit(MAX_FRAME_BODY_BYTES).unwrap();
        assert_eq!(
            budget.admit(1).unwrap_err().kind(),
            io::ErrorKind::OutOfMemory
        );
        assert_eq!(
            budget.metrics().unwrap(),
            FrameAdmissionMetrics {
                in_flight_frames: 1,
                in_flight_bytes: MAX_FRAME_BODY_BYTES,
                peak_in_flight_frames: 1,
                peak_in_flight_bytes: MAX_FRAME_BODY_BYTES,
            }
        );
        drop(guard);
        assert_eq!(budget.metrics().unwrap().in_flight_frames, 0);
        assert_eq!(budget.metrics().unwrap().in_flight_bytes, 0);
    }
}
