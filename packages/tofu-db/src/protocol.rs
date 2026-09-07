//! Strict bounded storage.v2 MessagePack framing; no semantic dispatch lives here.

use std::fmt;
use std::io::{self, Read, Write};

pub use crate::generated_storage_v2::{
    AUTH_TOKEN_BYTES, MAX_BLOB_CHUNK_BYTES, MAX_COMMAND_ID_BYTES, MAX_ERROR_CODE_BYTES,
    MAX_ERROR_MESSAGE_BYTES, MAX_FRAME_BODY_BYTES, MAX_OPERATION_BYTES, MAX_PAYLOAD_BYTES,
    MAX_SCHEMA_IDS, MAX_STREAMED_REQUEST_PAYLOAD_BYTES, PROTOCOL_VERSION,
};
use crate::generated_storage_v2::{
    FIELD_AUTH_TOKEN, FIELD_CHUNK_INDEX, FIELD_COMMAND_ID, FIELD_CORRELATION_ID,
    FIELD_DEADLINE_UNIX_MS, FIELD_ERROR_CODE, FIELD_ERROR_MESSAGE, FIELD_FINAL, FIELD_KIND,
    FIELD_MAXIMUM_VERSION, FIELD_MINIMUM_VERSION, FIELD_OPERATION, FIELD_OWNER_ID, FIELD_PAYLOAD,
    FIELD_PROTOCOL_VERSION, FIELD_RETRYABLE, FIELD_SCHEMA_ID, FIELD_SCHEMA_IDS, FIELD_STATUS,
    FIELD_STREAM_ID, FIELD_TENANT_ID, FIELD_TOTAL_PAYLOAD_BYTES, KIND_BLOB_CHUNK, KIND_HELLO,
    KIND_REQUEST, KIND_RESPONSE, KIND_RESPONSE_CHUNK, REQUIRED_BLOB_CHUNK_FIELDS,
    REQUIRED_HELLO_FIELDS, REQUIRED_REQUEST_FIELDS, REQUIRED_RESPONSE_CHUNK_FIELDS,
    REQUIRED_RESPONSE_FIELDS,
};
use zeroize::{Zeroize, Zeroizing};

#[derive(Clone, Eq, PartialEq)]
pub struct Hello {
    pub correlation_id: [u8; 16],
    pub minimum_version: u16,
    pub maximum_version: u16,
    pub schema_ids: Vec<u32>,
    pub auth_token: [u8; AUTH_TOKEN_BYTES],
}

impl fmt::Debug for Hello {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Hello")
            .field("correlation_id", &self.correlation_id)
            .field("minimum_version", &self.minimum_version)
            .field("maximum_version", &self.maximum_version)
            .field("schema_ids", &self.schema_ids)
            .field("auth_token", &"<redacted>")
            .finish()
    }
}

impl Drop for Hello {
    fn drop(&mut self) {
        self.auth_token.zeroize();
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct NegotiatedProtocol {
    pub protocol_version: u16,
    pub schema_id: u32,
}

impl Hello {
    pub fn negotiate(&self, local_schema_ids: &[u32]) -> io::Result<NegotiatedProtocol> {
        Message::Hello(self.clone()).validate()?;
        if local_schema_ids.is_empty()
            || local_schema_ids.len() > MAX_SCHEMA_IDS
            || local_schema_ids.contains(&0)
            || !local_schema_ids.windows(2).all(|pair| pair[0] < pair[1])
        {
            return Err(invalid_input("invalid local storage.v2 schema set"));
        }
        let schema_id = self
            .schema_ids
            .iter()
            .rev()
            .find(|schema| local_schema_ids.binary_search(schema).is_ok())
            .copied()
            .ok_or_else(|| invalid("storage.v2 has no mutually supported schema"))?;
        Ok(NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Request {
    pub correlation_id: [u8; 16],
    pub deadline_unix_ms: u64,
    pub owner_id: u64,
    pub tenant_id: Option<u64>,
    pub command_id: Option<String>,
    pub schema_id: u32,
    pub operation: String,
    pub payload: Vec<u8>,
}

impl Request {
    pub fn validate(&self) -> io::Result<()> {
        validate_identifier(&self.correlation_id, "correlation ID")?;
        validate_identity(self.owner_id, self.tenant_id, self.schema_id)?;
        if self.deadline_unix_ms == 0 || self.payload.len() > MAX_PAYLOAD_BYTES {
            return Err(invalid_input("invalid storage.v2 request bound"));
        }
        validate_text(&self.operation, MAX_OPERATION_BYTES, "operation")?;
        if let Some(command_id) = &self.command_id {
            validate_text(command_id, MAX_COMMAND_ID_BYTES, "command ID")?;
        }
        Ok(())
    }

    pub fn remaining_at(&self, now_unix_ms: u64) -> io::Result<std::time::Duration> {
        self.deadline_unix_ms
            .checked_sub(now_unix_ms)
            .filter(|remaining| *remaining > 0)
            .map(std::time::Duration::from_millis)
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "storage.v2 deadline elapsed"))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Response {
    pub correlation_id: [u8; 16],
    pub schema_id: u32,
    pub payload: Vec<u8>,
    pub status: u16,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub retryable: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlobChunk {
    pub correlation_id: [u8; 16],
    pub deadline_unix_ms: u64,
    pub owner_id: u64,
    pub tenant_id: Option<u64>,
    pub command_id: String,
    pub schema_id: u32,
    pub payload: Vec<u8>,
    pub stream_id: [u8; 16],
    pub chunk_index: u32,
    pub final_chunk: bool,
    pub total_payload_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResponseChunk {
    pub correlation_id: [u8; 16],
    pub schema_id: u32,
    pub payload: Vec<u8>,
    pub stream_id: [u8; 16],
    pub chunk_index: u32,
    pub final_chunk: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Message {
    Hello(Hello),
    Request(Request),
    Response(Response),
    BlobChunk(BlobChunk),
    ResponseChunk(ResponseChunk),
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn invalid_input(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn validate_text(value: &str, maximum: usize, name: &str) -> io::Result<()> {
    if value.is_empty() || value.len() > maximum || value.as_bytes().contains(&0) {
        return Err(invalid_input(format!("invalid storage.v2 {name}")));
    }
    Ok(())
}

fn validate_identity(owner_id: u64, tenant_id: Option<u64>, schema_id: u32) -> io::Result<()> {
    if owner_id == 0 || tenant_id == Some(0) || schema_id == 0 {
        return Err(invalid_input(
            "storage.v2 owner, tenant, and schema IDs must be positive",
        ));
    }
    Ok(())
}

fn validate_identifier(identifier: &[u8; 16], name: &str) -> io::Result<()> {
    if identifier.iter().all(|byte| *byte == 0) {
        return Err(invalid_input(format!("storage.v2 {name} may not be zero")));
    }
    Ok(())
}

impl Message {
    fn validate(&self) -> io::Result<()> {
        match self {
            Self::Hello(message) => {
                validate_identifier(&message.correlation_id, "correlation ID")?;
                if message.minimum_version == 0
                    || message.minimum_version > PROTOCOL_VERSION
                    || message.maximum_version < PROTOCOL_VERSION
                    || message.minimum_version > message.maximum_version
                    || message.schema_ids.is_empty()
                    || message.schema_ids.len() > MAX_SCHEMA_IDS
                    || message.schema_ids.contains(&0)
                    || !message.schema_ids.windows(2).all(|pair| pair[0] < pair[1])
                    || message.auth_token.iter().all(|byte| *byte == 0)
                {
                    return Err(invalid_input("invalid storage.v2 hello negotiation"));
                }
            }
            Self::Request(message) => {
                message.validate()?;
            }
            Self::Response(message) => {
                validate_identifier(&message.correlation_id, "correlation ID")?;
                if message.schema_id == 0 || message.payload.len() > MAX_PAYLOAD_BYTES {
                    return Err(invalid_input("invalid storage.v2 response bound"));
                }
                match message.status {
                    0 if message.error_code.is_none()
                        && message.error_message.is_none()
                        && !message.retryable => {}
                    0 => return Err(invalid_input("successful response carries error fields")),
                    _ if message.payload.is_empty()
                        && message.error_code.is_some()
                        && message.error_message.is_some() =>
                    {
                        validate_text(
                            message.error_code.as_deref().unwrap(),
                            MAX_ERROR_CODE_BYTES,
                            "error code",
                        )?;
                        validate_text(
                            message.error_message.as_deref().unwrap(),
                            MAX_ERROR_MESSAGE_BYTES,
                            "error message",
                        )?;
                    }
                    _ => return Err(invalid_input("invalid failed response envelope")),
                }
            }
            Self::BlobChunk(message) => {
                validate_identifier(&message.correlation_id, "correlation ID")?;
                validate_identifier(&message.stream_id, "stream ID")?;
                validate_identity(message.owner_id, message.tenant_id, message.schema_id)?;
                if message.deadline_unix_ms == 0
                    || message.payload.is_empty()
                    || message.payload.len() > MAX_BLOB_CHUNK_BYTES
                    || message.total_payload_bytes == 0
                    || message.total_payload_bytes > MAX_STREAMED_REQUEST_PAYLOAD_BYTES as u64
                    || message.payload.len() as u64 > message.total_payload_bytes
                {
                    return Err(invalid_input("invalid storage.v2 blob chunk bound"));
                }
                validate_text(&message.command_id, MAX_COMMAND_ID_BYTES, "command ID")?;
            }
            Self::ResponseChunk(message) => {
                validate_identifier(&message.correlation_id, "correlation ID")?;
                validate_identifier(&message.stream_id, "stream ID")?;
                if message.schema_id == 0
                    || message.payload.is_empty()
                    || message.payload.len() > MAX_BLOB_CHUNK_BYTES
                {
                    return Err(invalid_input("invalid storage.v2 response chunk bound"));
                }
            }
        }
        Ok(())
    }

    pub fn encode_body(&self) -> io::Result<Vec<u8>> {
        self.validate()?;
        let mut bytes = Vec::new();
        match self {
            Self::Hello(message) => {
                put_map(&mut bytes, 6);
                put_field_uint(&mut bytes, FIELD_KIND, KIND_HELLO);
                put_field_binary(&mut bytes, FIELD_CORRELATION_ID, &message.correlation_id);
                put_field_uint(
                    &mut bytes,
                    FIELD_MINIMUM_VERSION,
                    message.minimum_version as u64,
                );
                put_field_uint(
                    &mut bytes,
                    FIELD_MAXIMUM_VERSION,
                    message.maximum_version as u64,
                );
                put_uint(&mut bytes, FIELD_SCHEMA_IDS);
                put_array(&mut bytes, message.schema_ids.len());
                for schema in &message.schema_ids {
                    put_uint(&mut bytes, *schema as u64);
                }
                put_field_binary(&mut bytes, FIELD_AUTH_TOKEN, &message.auth_token);
            }
            Self::Request(message) => {
                put_map(&mut bytes, 10);
                put_field_uint(&mut bytes, FIELD_KIND, KIND_REQUEST);
                put_field_uint(&mut bytes, FIELD_PROTOCOL_VERSION, PROTOCOL_VERSION as u64);
                put_field_binary(&mut bytes, FIELD_CORRELATION_ID, &message.correlation_id);
                put_field_uint(&mut bytes, FIELD_DEADLINE_UNIX_MS, message.deadline_unix_ms);
                put_field_uint(&mut bytes, FIELD_OWNER_ID, message.owner_id);
                put_uint(&mut bytes, FIELD_TENANT_ID);
                put_optional_uint(&mut bytes, message.tenant_id);
                put_uint(&mut bytes, FIELD_COMMAND_ID);
                put_optional_string(&mut bytes, message.command_id.as_deref());
                put_field_uint(&mut bytes, FIELD_SCHEMA_ID, message.schema_id as u64);
                put_field_string(&mut bytes, FIELD_OPERATION, &message.operation);
                put_field_binary(&mut bytes, FIELD_PAYLOAD, &message.payload);
            }
            Self::Response(message) => {
                put_map(&mut bytes, 9);
                put_field_uint(&mut bytes, FIELD_KIND, KIND_RESPONSE);
                put_field_uint(&mut bytes, FIELD_PROTOCOL_VERSION, PROTOCOL_VERSION as u64);
                put_field_binary(&mut bytes, FIELD_CORRELATION_ID, &message.correlation_id);
                put_field_uint(&mut bytes, FIELD_SCHEMA_ID, message.schema_id as u64);
                put_field_binary(&mut bytes, FIELD_PAYLOAD, &message.payload);
                put_field_uint(&mut bytes, FIELD_STATUS, message.status as u64);
                put_uint(&mut bytes, FIELD_ERROR_CODE);
                put_optional_string(&mut bytes, message.error_code.as_deref());
                put_uint(&mut bytes, FIELD_ERROR_MESSAGE);
                put_optional_string(&mut bytes, message.error_message.as_deref());
                put_uint(&mut bytes, FIELD_RETRYABLE);
                put_bool(&mut bytes, message.retryable);
            }
            Self::BlobChunk(message) => {
                put_map(&mut bytes, 13);
                put_field_uint(&mut bytes, FIELD_KIND, KIND_BLOB_CHUNK);
                put_field_uint(&mut bytes, FIELD_PROTOCOL_VERSION, PROTOCOL_VERSION as u64);
                put_field_binary(&mut bytes, FIELD_CORRELATION_ID, &message.correlation_id);
                put_field_uint(&mut bytes, FIELD_DEADLINE_UNIX_MS, message.deadline_unix_ms);
                put_field_uint(&mut bytes, FIELD_OWNER_ID, message.owner_id);
                put_uint(&mut bytes, FIELD_TENANT_ID);
                put_optional_uint(&mut bytes, message.tenant_id);
                put_field_string(&mut bytes, FIELD_COMMAND_ID, &message.command_id);
                put_field_uint(&mut bytes, FIELD_SCHEMA_ID, message.schema_id as u64);
                put_field_binary(&mut bytes, FIELD_PAYLOAD, &message.payload);
                put_field_binary(&mut bytes, FIELD_STREAM_ID, &message.stream_id);
                put_field_uint(&mut bytes, FIELD_CHUNK_INDEX, message.chunk_index as u64);
                put_uint(&mut bytes, FIELD_FINAL);
                put_bool(&mut bytes, message.final_chunk);
                put_field_uint(
                    &mut bytes,
                    FIELD_TOTAL_PAYLOAD_BYTES,
                    message.total_payload_bytes,
                );
            }
            Self::ResponseChunk(message) => {
                put_map(&mut bytes, 8);
                put_field_uint(&mut bytes, FIELD_KIND, KIND_RESPONSE_CHUNK);
                put_field_uint(&mut bytes, FIELD_PROTOCOL_VERSION, PROTOCOL_VERSION as u64);
                put_field_binary(&mut bytes, FIELD_CORRELATION_ID, &message.correlation_id);
                put_field_uint(&mut bytes, FIELD_SCHEMA_ID, message.schema_id as u64);
                put_field_binary(&mut bytes, FIELD_PAYLOAD, &message.payload);
                put_field_binary(&mut bytes, FIELD_STREAM_ID, &message.stream_id);
                put_field_uint(&mut bytes, FIELD_CHUNK_INDEX, message.chunk_index as u64);
                put_uint(&mut bytes, FIELD_FINAL);
                put_bool(&mut bytes, message.final_chunk);
            }
        }
        if bytes.len() > MAX_FRAME_BODY_BYTES {
            return Err(invalid_input("storage.v2 frame body exceeds 8 MiB"));
        }
        Ok(bytes)
    }

    pub fn decode_body(bytes: &[u8]) -> io::Result<Self> {
        if bytes.is_empty() || bytes.len() > MAX_FRAME_BODY_BYTES {
            return Err(invalid("storage.v2 frame body size is invalid"));
        }
        let mut decoder = Decoder::new(bytes);
        let fields = decoder.fields()?;
        if !decoder.is_finished() {
            return Err(invalid("storage.v2 frame has trailing bytes"));
        }
        let message = fields.into_message()?;
        message
            .validate()
            .map_err(|error| invalid(error.to_string()))?;
        Ok(message)
    }
}

pub fn write_frame(writer: &mut impl Write, message: &Message) -> io::Result<usize> {
    write_frame_with_admission(writer, message, |_| Ok(()))
}

pub fn write_frame_with_admission<G>(
    writer: &mut impl Write,
    message: &Message,
    admit: impl FnOnce(usize) -> io::Result<G>,
) -> io::Result<usize> {
    // The body does not exist yet, so reserve the hard maximum. The returned
    // guard remains live until encoding and all writes finish.
    let _admission_guard = admit(MAX_FRAME_BODY_BYTES)?;
    let body = Zeroizing::new(message.encode_body()?);
    let length = u32::try_from(body.len()).map_err(|_| invalid_input("frame length overflow"))?;
    writer.write_all(&length.to_be_bytes())?;
    writer.write_all(&body)?;
    writer.write_all(&crc32c::crc32c(&body).to_be_bytes())?;
    Ok(8 + body.len())
}

pub fn read_frame(reader: &mut impl Read) -> io::Result<Message> {
    read_frame_with_admission(reader, |_| Ok(()))
}

pub fn read_frame_with_admission<G>(
    reader: &mut impl Read,
    admit: impl FnOnce(usize) -> io::Result<G>,
) -> io::Result<Message> {
    read_frame_or_eof_with_admission(reader, admit)?.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "storage.v2 connection closed before a frame",
        )
    })
}

pub fn read_frame_or_eof_with_admission<G>(
    reader: &mut impl Read,
    admit: impl FnOnce(usize) -> io::Result<G>,
) -> io::Result<Option<Message>> {
    let mut length_bytes = [0_u8; 4];
    loop {
        match reader.read(&mut length_bytes[..1]) {
            Ok(0) => return Ok(None),
            Ok(1) => break,
            Ok(_) => unreachable!(),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
    reader.read_exact(&mut length_bytes[1..])?;
    let length = u32::from_be_bytes(length_bytes) as usize;
    if length == 0 || length > MAX_FRAME_BODY_BYTES {
        return Err(invalid("storage.v2 declared frame length is invalid"));
    }
    // Rejecting the prefix happens before admission and allocation. The guard
    // covers body allocation, checksum validation, and MessagePack decoding.
    let _admission_guard = admit(length)?;
    let mut body = Zeroizing::new(vec![0_u8; length]);
    reader.read_exact(&mut body)?;
    let mut checksum_bytes = [0_u8; 4];
    reader.read_exact(&mut checksum_bytes)?;
    if u32::from_be_bytes(checksum_bytes) != crc32c::crc32c(&body) {
        return Err(invalid("storage.v2 frame CRC32C mismatch"));
    }
    Message::decode_body(&body).map(Some)
}

fn put_map(bytes: &mut Vec<u8>, length: usize) {
    bytes.push(0x80 | length as u8);
}

fn put_array(bytes: &mut Vec<u8>, length: usize) {
    if length <= 15 {
        bytes.push(0x90 | length as u8);
    } else {
        bytes.push(0xdc);
        bytes.extend_from_slice(&(length as u16).to_be_bytes());
    }
}

fn put_uint(bytes: &mut Vec<u8>, value: u64) {
    match value {
        0..=0x7f => bytes.push(value as u8),
        0x80..=0xff => bytes.extend_from_slice(&[0xcc, value as u8]),
        0x100..=0xffff => {
            bytes.push(0xcd);
            bytes.extend_from_slice(&(value as u16).to_be_bytes());
        }
        0x1_0000..=0xffff_ffff => {
            bytes.push(0xce);
            bytes.extend_from_slice(&(value as u32).to_be_bytes());
        }
        _ => {
            bytes.push(0xcf);
            bytes.extend_from_slice(&value.to_be_bytes());
        }
    }
}

fn put_binary(bytes: &mut Vec<u8>, value: &[u8]) {
    if value.len() <= u8::MAX as usize {
        bytes.extend_from_slice(&[0xc4, value.len() as u8]);
    } else if value.len() <= u16::MAX as usize {
        bytes.push(0xc5);
        bytes.extend_from_slice(&(value.len() as u16).to_be_bytes());
    } else {
        bytes.push(0xc6);
        bytes.extend_from_slice(&(value.len() as u32).to_be_bytes());
    }
    bytes.extend_from_slice(value);
}

fn put_string(bytes: &mut Vec<u8>, value: &str) {
    let length = value.len();
    if length <= 31 {
        bytes.push(0xa0 | length as u8);
    } else if length <= u8::MAX as usize {
        bytes.extend_from_slice(&[0xd9, length as u8]);
    } else if length <= u16::MAX as usize {
        bytes.push(0xda);
        bytes.extend_from_slice(&(length as u16).to_be_bytes());
    } else {
        bytes.push(0xdb);
        bytes.extend_from_slice(&(length as u32).to_be_bytes());
    }
    bytes.extend_from_slice(value.as_bytes());
}

fn put_bool(bytes: &mut Vec<u8>, value: bool) {
    bytes.push(if value { 0xc3 } else { 0xc2 });
}

fn put_optional_uint(bytes: &mut Vec<u8>, value: Option<u64>) {
    match value {
        Some(value) => put_uint(bytes, value),
        None => bytes.push(0xc0),
    }
}

fn put_optional_string(bytes: &mut Vec<u8>, value: Option<&str>) {
    match value {
        Some(value) => put_string(bytes, value),
        None => bytes.push(0xc0),
    }
}

fn put_field_uint(bytes: &mut Vec<u8>, field: u64, value: u64) {
    put_uint(bytes, field);
    put_uint(bytes, value);
}

fn put_field_binary(bytes: &mut Vec<u8>, field: u64, value: &[u8]) {
    put_uint(bytes, field);
    put_binary(bytes, value);
}

fn put_field_string(bytes: &mut Vec<u8>, field: u64, value: &str) {
    put_uint(bytes, field);
    put_string(bytes, value);
}

#[derive(Default)]
struct Fields {
    seen: u32,
    kind: Option<u64>,
    version: Option<u64>,
    correlation: Option<Vec<u8>>,
    deadline: Option<u64>,
    owner: Option<u64>,
    tenant: Option<Option<u64>>,
    command: Option<Option<String>>,
    schema: Option<u64>,
    operation: Option<String>,
    payload: Option<Vec<u8>>,
    status: Option<u64>,
    error_code: Option<Option<String>>,
    error_message: Option<Option<String>>,
    retryable: Option<bool>,
    stream: Option<Vec<u8>>,
    chunk: Option<u64>,
    final_chunk: Option<bool>,
    total_payload_bytes: Option<u64>,
    minimum_version: Option<u64>,
    maximum_version: Option<u64>,
    schema_ids: Option<Vec<u32>>,
    auth_token: Option<Vec<u8>>,
}

impl Drop for Fields {
    fn drop(&mut self) {
        if let Some(auth_token) = &mut self.auth_token {
            auth_token.zeroize();
        }
    }
}

impl Fields {
    fn exact(&self, expected: &[u8]) -> io::Result<()> {
        let mask = expected
            .iter()
            .fold(0_u32, |mask, field| mask | (1 << field));
        if self.seen != mask {
            return Err(invalid("storage.v2 message field set mismatch"));
        }
        Ok(())
    }

    fn correlation(&mut self) -> io::Result<[u8; 16]> {
        let value = self
            .correlation
            .take()
            .ok_or_else(|| invalid("missing correlation ID"))?;
        value
            .try_into()
            .map_err(|_| invalid("correlation ID must contain 16 bytes"))
    }

    fn protocol_version(&self) -> io::Result<()> {
        if self.version != Some(PROTOCOL_VERSION as u64) {
            return Err(invalid("unsupported storage.v2 protocol version"));
        }
        Ok(())
    }

    fn authentication_token(&mut self) -> io::Result<[u8; AUTH_TOKEN_BYTES]> {
        let mut value = self
            .auth_token
            .take()
            .ok_or_else(|| invalid("missing authentication token"))?;
        if value.len() != AUTH_TOKEN_BYTES {
            value.zeroize();
            return Err(invalid("authentication token must contain 32 bytes"));
        }
        let mut token = [0_u8; AUTH_TOKEN_BYTES];
        token.copy_from_slice(&value);
        value.zeroize();
        Ok(token)
    }

    fn into_message(mut self) -> io::Result<Message> {
        match self.kind {
            Some(KIND_HELLO) => {
                self.exact(REQUIRED_HELLO_FIELDS)?;
                Ok(Message::Hello(Hello {
                    correlation_id: self.correlation()?,
                    minimum_version: narrow_u16(self.minimum_version, "minimum version")?,
                    maximum_version: narrow_u16(self.maximum_version, "maximum version")?,
                    schema_ids: self
                        .schema_ids
                        .take()
                        .ok_or_else(|| invalid("missing schemas"))?,
                    auth_token: self.authentication_token()?,
                }))
            }
            Some(KIND_REQUEST) => {
                self.exact(REQUIRED_REQUEST_FIELDS)?;
                self.protocol_version()?;
                Ok(Message::Request(Request {
                    correlation_id: self.correlation()?,
                    deadline_unix_ms: required(self.deadline, "deadline")?,
                    owner_id: required(self.owner, "owner")?,
                    tenant_id: self.tenant.ok_or_else(|| invalid("missing tenant"))?,
                    command_id: self
                        .command
                        .take()
                        .ok_or_else(|| invalid("missing command"))?,
                    schema_id: narrow_u32(self.schema, "schema")?,
                    operation: self
                        .operation
                        .take()
                        .ok_or_else(|| invalid("missing operation"))?,
                    payload: self
                        .payload
                        .take()
                        .ok_or_else(|| invalid("missing payload"))?,
                }))
            }
            Some(KIND_RESPONSE) => {
                self.exact(REQUIRED_RESPONSE_FIELDS)?;
                self.protocol_version()?;
                Ok(Message::Response(Response {
                    correlation_id: self.correlation()?,
                    schema_id: narrow_u32(self.schema, "schema")?,
                    payload: self
                        .payload
                        .take()
                        .ok_or_else(|| invalid("missing payload"))?,
                    status: narrow_u16(self.status, "status")?,
                    error_code: self
                        .error_code
                        .take()
                        .ok_or_else(|| invalid("missing error code"))?,
                    error_message: self
                        .error_message
                        .take()
                        .ok_or_else(|| invalid("missing error message"))?,
                    retryable: self.retryable.ok_or_else(|| invalid("missing retryable"))?,
                }))
            }
            Some(KIND_BLOB_CHUNK) => {
                self.exact(REQUIRED_BLOB_CHUNK_FIELDS)?;
                self.protocol_version()?;
                Ok(Message::BlobChunk(BlobChunk {
                    correlation_id: self.correlation()?,
                    deadline_unix_ms: required(self.deadline, "deadline")?,
                    owner_id: required(self.owner, "owner")?,
                    tenant_id: self.tenant.ok_or_else(|| invalid("missing tenant"))?,
                    command_id: self
                        .command
                        .take()
                        .flatten()
                        .ok_or_else(|| invalid("blob chunk requires command ID"))?,
                    schema_id: narrow_u32(self.schema, "schema")?,
                    payload: self
                        .payload
                        .take()
                        .ok_or_else(|| invalid("missing payload"))?,
                    stream_id: self
                        .stream
                        .take()
                        .ok_or_else(|| invalid("missing stream ID"))?
                        .try_into()
                        .map_err(|_| invalid("stream ID must contain 16 bytes"))?,
                    chunk_index: narrow_u32(self.chunk, "chunk index")?,
                    final_chunk: self
                        .final_chunk
                        .ok_or_else(|| invalid("missing final flag"))?,
                    total_payload_bytes: required(self.total_payload_bytes, "total payload bytes")?,
                }))
            }
            Some(KIND_RESPONSE_CHUNK) => {
                self.exact(REQUIRED_RESPONSE_CHUNK_FIELDS)?;
                self.protocol_version()?;
                Ok(Message::ResponseChunk(ResponseChunk {
                    correlation_id: self.correlation()?,
                    schema_id: narrow_u32(self.schema, "schema")?,
                    payload: self
                        .payload
                        .take()
                        .ok_or_else(|| invalid("missing payload"))?,
                    stream_id: self
                        .stream
                        .take()
                        .ok_or_else(|| invalid("missing stream ID"))?
                        .try_into()
                        .map_err(|_| invalid("stream ID must contain 16 bytes"))?,
                    chunk_index: narrow_u32(self.chunk, "chunk index")?,
                    final_chunk: self
                        .final_chunk
                        .ok_or_else(|| invalid("missing final flag"))?,
                }))
            }
            _ => Err(invalid("unknown storage.v2 message kind")),
        }
    }
}

fn required(value: Option<u64>, name: &str) -> io::Result<u64> {
    value.ok_or_else(|| invalid(format!("missing storage.v2 {name}")))
}

fn narrow_u32(value: Option<u64>, name: &str) -> io::Result<u32> {
    u32::try_from(required(value, name)?).map_err(|_| invalid(format!("{name} exceeds u32")))
}

fn narrow_u16(value: Option<u64>, name: &str) -> io::Result<u16> {
    u16::try_from(required(value, name)?).map_err(|_| invalid(format!("{name} exceeds u16")))
}

struct Decoder<'a> {
    bytes: &'a [u8],
    offset: usize,
}

impl<'a> Decoder<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, offset: 0 }
    }

    fn is_finished(&self) -> bool {
        self.offset == self.bytes.len()
    }

    fn byte(&mut self) -> io::Result<u8> {
        let byte = *self
            .bytes
            .get(self.offset)
            .ok_or_else(|| invalid("truncated storage.v2 MessagePack"))?;
        self.offset += 1;
        Ok(byte)
    }

    fn take(&mut self, length: usize) -> io::Result<&'a [u8]> {
        let end = self
            .offset
            .checked_add(length)
            .ok_or_else(|| invalid("MessagePack length overflow"))?;
        let value = self
            .bytes
            .get(self.offset..end)
            .ok_or_else(|| invalid("truncated storage.v2 MessagePack"))?;
        self.offset = end;
        Ok(value)
    }

    fn uint(&mut self) -> io::Result<u64> {
        let marker = self.byte()?;
        match marker {
            0x00..=0x7f => Ok(marker as u64),
            0xcc => {
                let value = self.byte()? as u64;
                (value >= 0x80)
                    .then_some(value)
                    .ok_or_else(|| invalid("non-canonical MessagePack integer"))
            }
            0xcd => {
                let value = u16::from_be_bytes(self.take(2)?.try_into().unwrap()) as u64;
                (value > u8::MAX as u64)
                    .then_some(value)
                    .ok_or_else(|| invalid("non-canonical MessagePack integer"))
            }
            0xce => {
                let value = u32::from_be_bytes(self.take(4)?.try_into().unwrap()) as u64;
                (value > u16::MAX as u64)
                    .then_some(value)
                    .ok_or_else(|| invalid("non-canonical MessagePack integer"))
            }
            0xcf => {
                let value = u64::from_be_bytes(self.take(8)?.try_into().unwrap());
                (value > u32::MAX as u64)
                    .then_some(value)
                    .ok_or_else(|| invalid("non-canonical MessagePack integer"))
            }
            _ => Err(invalid("expected unsigned MessagePack integer")),
        }
    }

    fn map_length(&mut self) -> io::Result<usize> {
        let marker = self.byte()?;
        if (0x80..=0x8f).contains(&marker) {
            return Ok((marker & 0x0f) as usize);
        }
        Err(invalid(
            "storage.v2 requires a canonical flat MessagePack map",
        ))
    }

    fn array_u32(&mut self, maximum: usize) -> io::Result<Vec<u32>> {
        let marker = self.byte()?;
        let length = if (0x90..=0x9f).contains(&marker) {
            (marker & 0x0f) as usize
        } else if marker == 0xdc {
            let length = u16::from_be_bytes(self.take(2)?.try_into().unwrap()) as usize;
            if length <= 15 {
                return Err(invalid("non-canonical MessagePack array"));
            }
            length
        } else {
            return Err(invalid("expected MessagePack array"));
        };
        if length > maximum {
            return Err(invalid("MessagePack array exceeds protocol bound"));
        }
        (0..length)
            .map(|_| u32::try_from(self.uint()?).map_err(|_| invalid("schema ID exceeds u32")))
            .collect()
    }

    fn binary(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        let marker = self.byte()?;
        let length = match marker {
            0xc4 => self.byte()? as usize,
            0xc5 => {
                let value = u16::from_be_bytes(self.take(2)?.try_into().unwrap()) as usize;
                if value <= u8::MAX as usize {
                    return Err(invalid("non-canonical MessagePack binary"));
                }
                value
            }
            0xc6 => {
                let value = u32::from_be_bytes(self.take(4)?.try_into().unwrap()) as usize;
                if value <= u16::MAX as usize {
                    return Err(invalid("non-canonical MessagePack binary"));
                }
                value
            }
            _ => return Err(invalid("expected MessagePack binary")),
        };
        if length > maximum {
            return Err(invalid("MessagePack binary exceeds protocol bound"));
        }
        Ok(self.take(length)?.to_vec())
    }

    fn string(&mut self, maximum: usize) -> io::Result<String> {
        let marker = self.byte()?;
        let length = if (0xa0..=0xbf).contains(&marker) {
            (marker & 0x1f) as usize
        } else {
            match marker {
                0xd9 => {
                    let value = self.byte()? as usize;
                    if value <= 31 {
                        return Err(invalid("non-canonical MessagePack string"));
                    }
                    value
                }
                0xda => {
                    let value = u16::from_be_bytes(self.take(2)?.try_into().unwrap()) as usize;
                    if value <= u8::MAX as usize {
                        return Err(invalid("non-canonical MessagePack string"));
                    }
                    value
                }
                0xdb => {
                    let value = u32::from_be_bytes(self.take(4)?.try_into().unwrap()) as usize;
                    if value <= u16::MAX as usize {
                        return Err(invalid("non-canonical MessagePack string"));
                    }
                    value
                }
                _ => return Err(invalid("expected MessagePack string")),
            }
        };
        if length > maximum {
            return Err(invalid("MessagePack string exceeds protocol bound"));
        }
        String::from_utf8(self.take(length)?.to_vec())
            .map_err(|_| invalid("MessagePack string is not UTF-8"))
    }

    fn optional_uint(&mut self) -> io::Result<Option<u64>> {
        if self.bytes.get(self.offset) == Some(&0xc0) {
            self.offset += 1;
            Ok(None)
        } else {
            self.uint().map(Some)
        }
    }

    fn optional_string(&mut self, maximum: usize) -> io::Result<Option<String>> {
        if self.bytes.get(self.offset) == Some(&0xc0) {
            self.offset += 1;
            Ok(None)
        } else {
            self.string(maximum).map(Some)
        }
    }

    fn boolean(&mut self) -> io::Result<bool> {
        match self.byte()? {
            0xc2 => Ok(false),
            0xc3 => Ok(true),
            _ => Err(invalid("expected MessagePack boolean")),
        }
    }

    fn fields(&mut self) -> io::Result<Fields> {
        let count = self.map_length()?;
        let mut fields = Fields::default();
        let mut previous = None;
        for index in 0..count {
            let field = self.uint()?;
            if (index == 0 && field != FIELD_KIND)
                || field > FIELD_TOTAL_PAYLOAD_BYTES
                || previous.is_some_and(|previous| field <= previous)
            {
                return Err(invalid("unknown, duplicate, or unordered storage.v2 field"));
            }
            previous = Some(field);
            fields.seen |= 1_u32 << field;
            match field {
                FIELD_KIND => fields.kind = Some(self.uint()?),
                FIELD_PROTOCOL_VERSION => fields.version = Some(self.uint()?),
                FIELD_CORRELATION_ID => fields.correlation = Some(self.binary(16)?),
                FIELD_DEADLINE_UNIX_MS => fields.deadline = Some(self.uint()?),
                FIELD_OWNER_ID => fields.owner = Some(self.uint()?),
                FIELD_TENANT_ID => fields.tenant = Some(self.optional_uint()?),
                FIELD_COMMAND_ID => {
                    fields.command = Some(self.optional_string(MAX_COMMAND_ID_BYTES)?)
                }
                FIELD_SCHEMA_ID => fields.schema = Some(self.uint()?),
                FIELD_OPERATION => fields.operation = Some(self.string(MAX_OPERATION_BYTES)?),
                FIELD_PAYLOAD => {
                    let maximum =
                        if matches!(fields.kind, Some(KIND_BLOB_CHUNK | KIND_RESPONSE_CHUNK)) {
                            MAX_BLOB_CHUNK_BYTES
                        } else {
                            MAX_PAYLOAD_BYTES
                        };
                    fields.payload = Some(self.binary(maximum)?);
                }
                FIELD_STATUS => fields.status = Some(self.uint()?),
                FIELD_ERROR_CODE => {
                    fields.error_code = Some(self.optional_string(MAX_ERROR_CODE_BYTES)?)
                }
                FIELD_ERROR_MESSAGE => {
                    fields.error_message = Some(self.optional_string(MAX_ERROR_MESSAGE_BYTES)?)
                }
                FIELD_RETRYABLE => fields.retryable = Some(self.boolean()?),
                FIELD_STREAM_ID => fields.stream = Some(self.binary(16)?),
                FIELD_CHUNK_INDEX => fields.chunk = Some(self.uint()?),
                FIELD_FINAL => fields.final_chunk = Some(self.boolean()?),
                FIELD_MINIMUM_VERSION => fields.minimum_version = Some(self.uint()?),
                FIELD_MAXIMUM_VERSION => fields.maximum_version = Some(self.uint()?),
                FIELD_SCHEMA_IDS => fields.schema_ids = Some(self.array_u32(MAX_SCHEMA_IDS)?),
                FIELD_AUTH_TOKEN => fields.auth_token = Some(self.binary(AUTH_TOKEN_BYTES)?),
                FIELD_TOTAL_PAYLOAD_BYTES => fields.total_payload_bytes = Some(self.uint()?),
                _ => unreachable!(),
            }
        }
        Ok(fields)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    fn request() -> Message {
        Message::Request(Request {
            correlation_id: [7; 16],
            deadline_unix_ms: 9_000,
            owner_id: 41,
            tenant_id: Some(3),
            command_id: Some("command-1".to_owned()),
            schema_id: 57,
            operation: "conversation.turn.append".to_owned(),
            payload: b"payload".to_vec(),
        })
    }

    fn messages() -> Vec<Message> {
        vec![
            Message::Hello(Hello {
                correlation_id: [1; 16],
                minimum_version: 1,
                maximum_version: 2,
                schema_ids: vec![56, 57],
                auth_token: [9; AUTH_TOKEN_BYTES],
            }),
            request(),
            Message::Response(Response {
                correlation_id: [7; 16],
                schema_id: 57,
                payload: b"result".to_vec(),
                status: 0,
                error_code: None,
                error_message: None,
                retryable: false,
            }),
            Message::Response(Response {
                correlation_id: [8; 16],
                schema_id: 57,
                payload: Vec::new(),
                status: 409,
                error_code: Some("serialization_conflict".to_owned()),
                error_message: Some("range witness changed".to_owned()),
                retryable: true,
            }),
            Message::BlobChunk(BlobChunk {
                correlation_id: [9; 16],
                deadline_unix_ms: 10_000,
                owner_id: 41,
                tenant_id: None,
                command_id: "command-blob".to_owned(),
                schema_id: 57,
                payload: vec![5; 1024],
                stream_id: [4; 16],
                chunk_index: 12,
                final_chunk: true,
                total_payload_bytes: 1024,
            }),
            Message::ResponseChunk(ResponseChunk {
                correlation_id: [10; 16],
                schema_id: 57,
                payload: vec![6; 1024],
                stream_id: [5; 16],
                chunk_index: 3,
                final_chunk: false,
            }),
        ]
    }

    struct ChunkedReader {
        bytes: Vec<u8>,
        offset: usize,
        maximum_chunk: usize,
    }

    impl Read for ChunkedReader {
        fn read(&mut self, target: &mut [u8]) -> io::Result<usize> {
            if self.offset == self.bytes.len() {
                return Ok(0);
            }
            let length = target
                .len()
                .min(self.maximum_chunk)
                .min(self.bytes.len() - self.offset);
            target[..length].copy_from_slice(&self.bytes[self.offset..self.offset + length]);
            self.offset += length;
            Ok(length)
        }
    }

    #[test]
    fn every_message_round_trips_through_fragmented_crc_frame() {
        for message in messages() {
            let body = message.encode_body().unwrap();
            assert_eq!(Message::decode_body(&body).unwrap(), message);
            let mut frame = Vec::new();
            assert_eq!(write_frame(&mut frame, &message).unwrap(), frame.len());
            let mut reader = ChunkedReader {
                bytes: frame,
                offset: 0,
                maximum_chunk: 3,
            };
            assert_eq!(read_frame(&mut reader).unwrap(), message);
        }
    }

    #[test]
    fn every_truncated_prefix_and_corrupt_checksum_fails_closed() {
        let mut frame = Vec::new();
        write_frame(&mut frame, &request()).unwrap();
        for length in 0..frame.len() {
            assert!(read_frame(&mut Cursor::new(&frame[..length])).is_err());
        }
        let mut corrupt = frame.clone();
        let body_offset = 4;
        corrupt[body_offset + 5] ^= 0x40;
        assert_eq!(
            read_frame(&mut Cursor::new(corrupt)).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn oversized_declared_length_is_rejected_before_payload_read() {
        let prefix = ((MAX_FRAME_BODY_BYTES + 1) as u32).to_be_bytes();
        assert_eq!(
            read_frame(&mut Cursor::new(prefix)).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn decoder_rejects_noncanonical_and_nonflat_messagepack() {
        assert!(Decoder::new(&[0xcc, 2]).uint().is_err());
        assert!(Decoder::new(&[0xcd, 0, 255]).uint().is_err());
        assert!(Decoder::new(&[0xde, 0, 1]).map_length().is_err());

        let mut duplicate = Vec::new();
        put_map(&mut duplicate, 2);
        put_field_uint(&mut duplicate, 0, KIND_REQUEST);
        put_field_uint(&mut duplicate, 0, KIND_REQUEST);
        assert!(Message::decode_body(&duplicate).is_err());

        let mut unknown = Vec::new();
        put_map(&mut unknown, 2);
        put_field_uint(&mut unknown, 0, KIND_REQUEST);
        put_field_uint(&mut unknown, 21, 1);
        assert!(Message::decode_body(&unknown).is_err());
    }

    #[test]
    fn response_success_and_failure_shapes_are_mutually_exclusive() {
        let invalid = [
            Message::Response(Response {
                correlation_id: [1; 16],
                schema_id: 57,
                payload: Vec::new(),
                status: 0,
                error_code: Some("error".to_owned()),
                error_message: Some("message".to_owned()),
                retryable: false,
            }),
            Message::Response(Response {
                correlation_id: [1; 16],
                schema_id: 57,
                payload: b"must-be-empty".to_vec(),
                status: 500,
                error_code: Some("error".to_owned()),
                error_message: Some("message".to_owned()),
                retryable: false,
            }),
        ];
        assert!(invalid
            .into_iter()
            .all(|message| message.encode_body().is_err()));
    }

    #[test]
    fn identity_text_blob_and_negotiation_bounds_are_enforced() {
        let invalid = [
            Message::BlobChunk(BlobChunk {
                correlation_id: [1; 16],
                deadline_unix_ms: 1,
                owner_id: 1,
                tenant_id: None,
                command_id: "command".to_owned(),
                schema_id: 57,
                payload: vec![0; MAX_BLOB_CHUNK_BYTES + 1],
                stream_id: [2; 16],
                chunk_index: 0,
                final_chunk: false,
                total_payload_bytes: (MAX_BLOB_CHUNK_BYTES + 1) as u64,
            }),
            Message::BlobChunk(BlobChunk {
                correlation_id: [1; 16],
                deadline_unix_ms: 1,
                owner_id: 1,
                tenant_id: None,
                command_id: "command".to_owned(),
                schema_id: 57,
                payload: vec![1],
                stream_id: [2; 16],
                chunk_index: 0,
                final_chunk: false,
                total_payload_bytes: MAX_STREAMED_REQUEST_PAYLOAD_BYTES as u64 + 1,
            }),
            Message::Hello(Hello {
                correlation_id: [1; 16],
                minimum_version: 2,
                maximum_version: 2,
                schema_ids: vec![57, 57],
                auth_token: [9; AUTH_TOKEN_BYTES],
            }),
            Message::Hello(Hello {
                correlation_id: [1; 16],
                minimum_version: 2,
                maximum_version: 2,
                schema_ids: vec![57],
                auth_token: [0; AUTH_TOKEN_BYTES],
            }),
            Message::Request(Request {
                correlation_id: [0; 16],
                deadline_unix_ms: 1,
                owner_id: 1,
                tenant_id: None,
                command_id: None,
                schema_id: 57,
                operation: "query".to_owned(),
                payload: Vec::new(),
            }),
            Message::Request(Request {
                correlation_id: [1; 16],
                deadline_unix_ms: 1,
                owner_id: 0,
                tenant_id: None,
                command_id: None,
                schema_id: 57,
                operation: "query".to_owned(),
                payload: Vec::new(),
            }),
        ];
        assert!(invalid
            .into_iter()
            .all(|message| message.encode_body().is_err()));
    }

    #[test]
    fn deadline_converts_to_a_bounded_remaining_duration() {
        let Message::Request(request) = request() else {
            unreachable!()
        };
        assert_eq!(
            request.remaining_at(8_750).unwrap(),
            std::time::Duration::from_millis(250)
        );
        assert_eq!(
            request.remaining_at(9_000).unwrap_err().kind(),
            io::ErrorKind::TimedOut
        );
        assert_eq!(
            request.remaining_at(10_000).unwrap_err().kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn hello_selects_the_highest_common_schema_or_rejects() {
        let hello = Hello {
            correlation_id: [1; 16],
            minimum_version: 1,
            maximum_version: 3,
            schema_ids: vec![55, 57, 60],
            auth_token: [9; AUTH_TOKEN_BYTES],
        };
        assert_eq!(
            hello.negotiate(&[54, 57, 59]).unwrap(),
            NegotiatedProtocol {
                protocol_version: 2,
                schema_id: 57,
            }
        );
        assert_eq!(
            hello.negotiate(&[54, 56]).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(
            hello.negotiate(&[57, 57]).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let rendered = format!("{hello:?}");
        assert!(rendered.contains("<redacted>"));
        assert!(!rendered.contains("9, 9"));
    }

    struct AdmissionGuard(Arc<AtomicUsize>);

    impl Drop for AdmissionGuard {
        fn drop(&mut self) {
            self.0.fetch_sub(1, Ordering::SeqCst);
        }
    }

    #[test]
    fn admission_precedes_frame_allocation_and_covers_codec_lifetime() {
        let mut frame = Vec::new();
        let active = Arc::new(AtomicUsize::new(0));
        let write_active = Arc::clone(&active);
        write_frame_with_admission(&mut frame, &request(), |reserved| {
            assert_eq!(reserved, MAX_FRAME_BODY_BYTES);
            write_active.fetch_add(1, Ordering::SeqCst);
            Ok(AdmissionGuard(Arc::clone(&write_active)))
        })
        .unwrap();
        assert_eq!(active.load(Ordering::SeqCst), 0);

        let declared = u32::from_be_bytes(frame[..4].try_into().unwrap()) as usize;
        let read_active = Arc::clone(&active);
        let decoded = read_frame_with_admission(&mut Cursor::new(frame), |reserved| {
            assert_eq!(reserved, declared);
            read_active.fetch_add(1, Ordering::SeqCst);
            Ok(AdmissionGuard(Arc::clone(&read_active)))
        })
        .unwrap();
        assert_eq!(decoded, request());
        assert_eq!(active.load(Ordering::SeqCst), 0);

        let calls = AtomicUsize::new(0);
        let prefix = ((MAX_FRAME_BODY_BYTES + 1) as u32).to_be_bytes();
        assert!(read_frame_with_admission(&mut Cursor::new(prefix), |_| {
            calls.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })
        .is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
}
