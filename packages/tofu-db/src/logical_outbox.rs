//! Authenticated native logical-outbox records; persistence is coordinated elsewhere.

use std::io;

use aes_gcm::aead::{Aead, Payload};
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};

use crate::blob::{BlobId, BlobReference};
use crate::block::BlockId;
use crate::protocol::MAX_OPERATION_BYTES;
use crate::receipt::MAX_RECEIPT_COMMAND_ID_BYTES;

const MAGIC: &[u8; 8] = b"TDBOUT01";
const STORED_MAGIC: &[u8; 8] = b"TDBOST01";
const FAMILY_MAGIC: &[u8; 8] = b"TDBOFR01";
const VERSION: u32 = 1;
const NONCE_BYTES: usize = 12;
const KEY_ID_BYTES: usize = 8;
const TAG_BYTES: usize = 16;
const FIXED_BYTES: usize = 8 + 4 + 8 + 8 + 8 + 4 + 4 + 2 + 2 + 2 + 2 + 32 + 8 + 8 + 12 + 4;
const AAD_DOMAIN: &[u8] = b"tofu-db:logical-outbox:aes-256-gcm:v1\0";
pub const MAX_LOGICAL_PAYLOAD_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_REQUEST_ID_BYTES: usize = 128;
pub const MAX_INLINE_LOGICAL_OUTBOX_BYTES: usize = 4 * 1024;
pub const MAX_ENCODED_LOGICAL_OUTBOX_BYTES: usize = MAX_LOGICAL_PAYLOAD_BYTES
    + FIXED_BYTES
    + MAX_OPERATION_BYTES
    + MAX_REQUEST_ID_BYTES
    + MAX_RECEIPT_COMMAND_ID_BYTES
    + TAG_BYTES;
const STORED_HEADER_BYTES: usize = 8 + 4 + 1 + 3 + 4;
const BLOB_REFERENCE_BYTES: usize = 32 + 32 + 8;
const FAMILY_HEADER_BYTES: usize = 8 + 4 + 8 + 8 + 8 + 32 + 4;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LogicalOutboxIdentity {
    pub sequence: u64,
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub schema_version: u32,
    pub registry_version: u32,
    pub operation: String,
    pub request_id: String,
    pub request_digest: [u8; 32],
    pub command_id: Option<String>,
    pub committed_at_ms: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LogicalOutboxCapture {
    pub schema_version: u32,
    pub registry_version: u32,
    pub operation: String,
    pub request_id: String,
    pub request_digest: [u8; 32],
    pub command_id: Option<String>,
    pub committed_at_ms: u64,
    pub clear_payload: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SealedLogicalOutboxRecord {
    pub identity: LogicalOutboxIdentity,
    pub event_id: [u8; 32],
    pub encryption_key_id: [u8; KEY_ID_BYTES],
    pub nonce: [u8; NONCE_BYTES],
    pub ciphertext: Vec<u8>,
}

pub struct LogicalOutboxCipher {
    cipher: Aes256Gcm,
    key_id: [u8; KEY_ID_BYTES],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StoredLogicalOutboxRecord {
    Inline(Vec<u8>),
    Blob(BlobReference),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LogicalOutboxFamilyRecord {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub sequence: u64,
    pub event_id: [u8; 32],
    pub stored: StoredLogicalOutboxRecord,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn validate_text(value: &str, maximum: usize) -> bool {
    !value.is_empty() && value.len() <= maximum && !value.as_bytes().contains(&0)
}

impl LogicalOutboxIdentity {
    pub fn validate(&self) -> io::Result<()> {
        if self.sequence == 0
            || self.tenant_id == 0
            || self.owner_user_id == 0
            || self.schema_version == 0
            || self.registry_version == 0
            || self.committed_at_ms == 0
            || !validate_text(&self.operation, MAX_OPERATION_BYTES)
            || !validate_text(&self.request_id, MAX_REQUEST_ID_BYTES)
            || self
                .command_id
                .as_ref()
                .is_some_and(|command_id| !validate_text(command_id, MAX_RECEIPT_COMMAND_ID_BYTES))
        {
            return Err(invalid_input("invalid logical outbox identity"));
        }
        Ok(())
    }

    pub fn event_id(&self) -> io::Result<[u8; 32]> {
        self.validate()?;
        let request_digest = hexadecimal(&self.request_digest);
        let identity = match &self.command_id {
            Some(command_id) => format!("command\0{}\0{command_id}", self.operation),
            None => format!(
                "request\0{}\0{}\0{request_digest}",
                self.operation, self.request_id
            ),
        };
        Ok(Sha256::digest(identity.as_bytes()).into())
    }

    fn aad(&self, event_id: &[u8; 32], key_id: &[u8; KEY_ID_BYTES]) -> io::Result<Vec<u8>> {
        self.validate()?;
        let operation_length = u16::try_from(self.operation.len())
            .map_err(|_| invalid_input("logical outbox operation length overflow"))?;
        let request_id_length = u16::try_from(self.request_id.len())
            .map_err(|_| invalid_input("logical outbox request ID length overflow"))?;
        let command_length = self
            .command_id
            .as_ref()
            .map(|command_id| u16::try_from(command_id.len()))
            .transpose()
            .map_err(|_| invalid_input("logical outbox command ID length overflow"))?
            .unwrap_or(0);
        let mut aad = Vec::with_capacity(
            AAD_DOMAIN.len()
                + 8 * 4
                + 4 * 2
                + 2 * 3
                + 32 * 2
                + KEY_ID_BYTES
                + self.operation.len()
                + self.request_id.len()
                + command_length as usize,
        );
        aad.extend_from_slice(AAD_DOMAIN);
        aad.extend_from_slice(&self.sequence.to_le_bytes());
        aad.extend_from_slice(&self.tenant_id.to_le_bytes());
        aad.extend_from_slice(&self.owner_user_id.to_le_bytes());
        aad.extend_from_slice(&self.schema_version.to_le_bytes());
        aad.extend_from_slice(&self.registry_version.to_le_bytes());
        aad.extend_from_slice(&operation_length.to_le_bytes());
        aad.extend_from_slice(&request_id_length.to_le_bytes());
        aad.extend_from_slice(&command_length.to_le_bytes());
        aad.extend_from_slice(&self.request_digest);
        aad.extend_from_slice(event_id);
        aad.extend_from_slice(key_id);
        aad.extend_from_slice(&self.committed_at_ms.to_le_bytes());
        aad.extend_from_slice(self.operation.as_bytes());
        aad.extend_from_slice(self.request_id.as_bytes());
        if let Some(command_id) = &self.command_id {
            aad.extend_from_slice(command_id.as_bytes());
        }
        Ok(aad)
    }
}

impl LogicalOutboxCapture {
    pub fn into_identity(
        self,
        sequence: u64,
        tenant_id: u64,
        owner_user_id: u64,
    ) -> io::Result<(LogicalOutboxIdentity, Vec<u8>)> {
        if self.clear_payload.is_empty() || self.clear_payload.len() > MAX_LOGICAL_PAYLOAD_BYTES {
            return Err(invalid_input(
                "logical outbox payload is empty or unbounded",
            ));
        }
        let identity = LogicalOutboxIdentity {
            sequence,
            tenant_id,
            owner_user_id,
            schema_version: self.schema_version,
            registry_version: self.registry_version,
            operation: self.operation,
            request_id: self.request_id,
            request_digest: self.request_digest,
            command_id: self.command_id,
            committed_at_ms: self.committed_at_ms,
        };
        identity.validate()?;
        Ok((identity, self.clear_payload))
    }
}

fn hexadecimal(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        encoded.push(DIGITS[(byte >> 4) as usize] as char);
        encoded.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    encoded
}

impl LogicalOutboxCipher {
    pub fn new(key: &[u8; 32]) -> Self {
        let key_hash = Sha256::digest(key);
        let mut key_id = [0; KEY_ID_BYTES];
        key_id.copy_from_slice(&key_hash[..KEY_ID_BYTES]);
        Self {
            cipher: Aes256Gcm::new_from_slice(key).expect("AES-256 key length is fixed"),
            key_id,
        }
    }

    pub fn key_id(&self) -> [u8; KEY_ID_BYTES] {
        self.key_id
    }

    pub fn seal(
        &self,
        identity: LogicalOutboxIdentity,
        clear_payload: &[u8],
    ) -> io::Result<SealedLogicalOutboxRecord> {
        let mut nonce = [0; NONCE_BYTES];
        OsRng.fill_bytes(&mut nonce);
        self.seal_with_nonce(identity, clear_payload, nonce)
    }

    fn seal_with_nonce(
        &self,
        identity: LogicalOutboxIdentity,
        clear_payload: &[u8],
        nonce: [u8; NONCE_BYTES],
    ) -> io::Result<SealedLogicalOutboxRecord> {
        if clear_payload.is_empty() || clear_payload.len() > MAX_LOGICAL_PAYLOAD_BYTES {
            return Err(invalid_input(
                "logical outbox payload is empty or unbounded",
            ));
        }
        let event_id = identity.event_id()?;
        let aad = identity.aad(&event_id, &self.key_id)?;
        let ciphertext = self
            .cipher
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: clear_payload,
                    aad: &aad,
                },
            )
            .map_err(|_| invalid_data("logical outbox encryption failed"))?;
        Ok(SealedLogicalOutboxRecord {
            identity,
            event_id,
            encryption_key_id: self.key_id,
            nonce,
            ciphertext,
        })
    }

    pub fn open(&self, record: &SealedLogicalOutboxRecord) -> io::Result<Vec<u8>> {
        record.validate()?;
        if record.encryption_key_id != self.key_id {
            return Err(invalid_data("logical outbox encryption key differs"));
        }
        let aad = record
            .identity
            .aad(&record.event_id, &record.encryption_key_id)?;
        let cleartext = self
            .cipher
            .decrypt(
                Nonce::from_slice(&record.nonce),
                Payload {
                    msg: &record.ciphertext,
                    aad: &aad,
                },
            )
            .map_err(|_| invalid_data("logical outbox authentication failed"))?;
        if cleartext.is_empty() || cleartext.len() > MAX_LOGICAL_PAYLOAD_BYTES {
            return Err(invalid_data("logical outbox clear payload is unbounded"));
        }
        Ok(cleartext)
    }
}

impl SealedLogicalOutboxRecord {
    pub fn validate(&self) -> io::Result<()> {
        self.identity
            .validate()
            .map_err(|_| invalid_data("invalid logical outbox identity"))?;
        if self.event_id
            != self
                .identity
                .event_id()
                .map_err(|_| invalid_data("invalid logical outbox event identity"))?
            || self.ciphertext.len() <= TAG_BYTES
            || self.ciphertext.len() > MAX_LOGICAL_PAYLOAD_BYTES + TAG_BYTES
        {
            return Err(invalid_data("invalid logical outbox sealed record"));
        }
        Ok(())
    }

    pub fn encode(&self) -> io::Result<Vec<u8>> {
        self.validate()?;
        let operation = self.identity.operation.as_bytes();
        let request_id = self.identity.request_id.as_bytes();
        let command_id = self.identity.command_id.as_deref().unwrap_or("").as_bytes();
        let mut encoded = Vec::with_capacity(self.encoded_len()?);
        encoded.extend_from_slice(MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&self.identity.sequence.to_le_bytes());
        encoded.extend_from_slice(&self.identity.tenant_id.to_le_bytes());
        encoded.extend_from_slice(&self.identity.owner_user_id.to_le_bytes());
        encoded.extend_from_slice(&self.identity.schema_version.to_le_bytes());
        encoded.extend_from_slice(&self.identity.registry_version.to_le_bytes());
        encoded.extend_from_slice(&(operation.len() as u16).to_le_bytes());
        encoded.extend_from_slice(&(request_id.len() as u16).to_le_bytes());
        encoded.extend_from_slice(&(command_id.len() as u16).to_le_bytes());
        encoded.extend_from_slice(&0_u16.to_le_bytes());
        encoded.extend_from_slice(&self.identity.request_digest);
        encoded.extend_from_slice(&self.identity.committed_at_ms.to_le_bytes());
        encoded.extend_from_slice(&self.encryption_key_id);
        encoded.extend_from_slice(&self.nonce);
        encoded.extend_from_slice(&(self.ciphertext.len() as u32).to_le_bytes());
        encoded.extend_from_slice(operation);
        encoded.extend_from_slice(request_id);
        encoded.extend_from_slice(command_id);
        encoded.extend_from_slice(&self.ciphertext);
        Ok(encoded)
    }

    pub fn encoded_len(&self) -> io::Result<usize> {
        self.validate()?;
        FIXED_BYTES
            .checked_add(self.identity.operation.len())
            .and_then(|length| length.checked_add(self.identity.request_id.len()))
            .and_then(|length| {
                length.checked_add(self.identity.command_id.as_ref().map_or(0, String::len))
            })
            .and_then(|length| length.checked_add(self.ciphertext.len()))
            .filter(|length| *length <= MAX_ENCODED_LOGICAL_OUTBOX_BYTES)
            .ok_or_else(|| invalid_data("logical outbox encoded length overflow"))
    }

    pub fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < FIXED_BYTES || !encoded.starts_with(MAGIC) {
            return Err(invalid_data("invalid logical outbox record"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let sequence = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
        let tenant_id = u64::from_le_bytes(encoded[20..28].try_into().unwrap());
        let owner_user_id = u64::from_le_bytes(encoded[28..36].try_into().unwrap());
        let schema_version = u32::from_le_bytes(encoded[36..40].try_into().unwrap());
        let registry_version = u32::from_le_bytes(encoded[40..44].try_into().unwrap());
        let operation_length = u16::from_le_bytes(encoded[44..46].try_into().unwrap()) as usize;
        let request_id_length = u16::from_le_bytes(encoded[46..48].try_into().unwrap()) as usize;
        let command_id_length = u16::from_le_bytes(encoded[48..50].try_into().unwrap()) as usize;
        let reserved = u16::from_le_bytes(encoded[50..52].try_into().unwrap());
        let request_digest = encoded[52..84].try_into().unwrap();
        let committed_at_ms = u64::from_le_bytes(encoded[84..92].try_into().unwrap());
        let encryption_key_id = encoded[92..100].try_into().unwrap();
        let nonce = encoded[100..112].try_into().unwrap();
        let ciphertext_length = u32::from_le_bytes(encoded[112..116].try_into().unwrap()) as usize;
        let expected = FIXED_BYTES
            .checked_add(operation_length)
            .and_then(|length| length.checked_add(request_id_length))
            .and_then(|length| length.checked_add(command_id_length))
            .and_then(|length| length.checked_add(ciphertext_length))
            .ok_or_else(|| invalid_data("logical outbox record length overflow"))?;
        if version != VERSION
            || reserved != 0
            || operation_length == 0
            || operation_length > MAX_OPERATION_BYTES
            || request_id_length == 0
            || request_id_length > MAX_REQUEST_ID_BYTES
            || command_id_length > MAX_RECEIPT_COMMAND_ID_BYTES
            || ciphertext_length <= TAG_BYTES
            || ciphertext_length > MAX_LOGICAL_PAYLOAD_BYTES + TAG_BYTES
            || expected != encoded.len()
        {
            return Err(invalid_data("invalid logical outbox record header"));
        }
        let mut offset = FIXED_BYTES;
        let operation_end = offset + operation_length;
        let operation = std::str::from_utf8(&encoded[offset..operation_end])
            .map_err(|_| invalid_data("logical outbox operation is not UTF-8"))?
            .to_owned();
        offset = operation_end;
        let request_id_end = offset + request_id_length;
        let request_id = std::str::from_utf8(&encoded[offset..request_id_end])
            .map_err(|_| invalid_data("logical outbox request ID is not UTF-8"))?
            .to_owned();
        offset = request_id_end;
        let command_id_end = offset + command_id_length;
        let command_id = if command_id_length == 0 {
            None
        } else {
            Some(
                std::str::from_utf8(&encoded[offset..command_id_end])
                    .map_err(|_| invalid_data("logical outbox command ID is not UTF-8"))?
                    .to_owned(),
            )
        };
        offset = command_id_end;
        let identity = LogicalOutboxIdentity {
            sequence,
            tenant_id,
            owner_user_id,
            schema_version,
            registry_version,
            operation,
            request_id,
            request_digest,
            command_id,
            committed_at_ms,
        };
        let record = Self {
            event_id: identity
                .event_id()
                .map_err(|_| invalid_data("invalid logical outbox event identity"))?,
            identity,
            encryption_key_id,
            nonce,
            ciphertext: encoded[offset..].to_vec(),
        };
        record.validate()?;
        Ok(record)
    }
}

impl StoredLogicalOutboxRecord {
    pub fn encode(&self) -> io::Result<Vec<u8>> {
        let (kind, logical_bytes, body): (u8, usize, Vec<u8>) = match self {
            Self::Inline(encoded) => {
                SealedLogicalOutboxRecord::decode(encoded)?;
                if encoded.len() > MAX_INLINE_LOGICAL_OUTBOX_BYTES {
                    return Err(invalid_input("inline logical outbox record is too large"));
                }
                (0, encoded.len(), encoded.clone())
            }
            Self::Blob(reference) => {
                if reference.logical_bytes == 0
                    || reference.logical_bytes as usize > MAX_ENCODED_LOGICAL_OUTBOX_BYTES
                {
                    return Err(invalid_input("logical outbox blob reference is invalid"));
                }
                let mut encoded = Vec::with_capacity(BLOB_REFERENCE_BYTES);
                encoded.extend_from_slice(&reference.blob_id.0);
                encoded.extend_from_slice(&reference.manifest_block_id.0);
                encoded.extend_from_slice(&reference.logical_bytes.to_le_bytes());
                (1, reference.logical_bytes as usize, encoded)
            }
        };
        let logical_bytes = u32::try_from(logical_bytes)
            .map_err(|_| invalid_input("logical outbox stored length overflow"))?;
        let mut encoded = Vec::with_capacity(STORED_HEADER_BYTES + body.len());
        encoded.extend_from_slice(STORED_MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.push(kind);
        encoded.extend_from_slice(&[0; 3]);
        encoded.extend_from_slice(&logical_bytes.to_le_bytes());
        encoded.extend_from_slice(&body);
        Ok(encoded)
    }

    pub fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < STORED_HEADER_BYTES || !encoded.starts_with(STORED_MAGIC) {
            return Err(invalid_data("invalid stored logical outbox record"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let kind = encoded[12];
        let reserved = &encoded[13..16];
        let logical_bytes = u32::from_le_bytes(encoded[16..20].try_into().unwrap()) as usize;
        if version != VERSION
            || reserved != [0, 0, 0]
            || logical_bytes == 0
            || logical_bytes > MAX_ENCODED_LOGICAL_OUTBOX_BYTES
        {
            return Err(invalid_data("invalid stored logical outbox header"));
        }
        let body = &encoded[STORED_HEADER_BYTES..];
        match kind {
            0 if body.len() == logical_bytes && body.len() <= MAX_INLINE_LOGICAL_OUTBOX_BYTES => {
                SealedLogicalOutboxRecord::decode(body)?;
                Ok(Self::Inline(body.to_vec()))
            }
            1 if body.len() == BLOB_REFERENCE_BYTES => {
                let logical_bytes_u64 = u64::try_from(logical_bytes)
                    .map_err(|_| invalid_data("logical outbox blob length overflow"))?;
                Ok(Self::Blob(BlobReference {
                    blob_id: BlobId(body[..32].try_into().unwrap()),
                    manifest_block_id: BlockId(body[32..64].try_into().unwrap()),
                    logical_bytes: logical_bytes_u64,
                }))
            }
            _ => Err(invalid_data("invalid stored logical outbox body")),
        }
    }

    pub fn logical_bytes(&self) -> usize {
        match self {
            Self::Inline(encoded) => encoded.len(),
            Self::Blob(reference) => reference.logical_bytes as usize,
        }
    }
}

pub(crate) fn stored_blob_reference(stored: &[u8]) -> io::Result<Option<BlobReference>> {
    if !stored.starts_with(STORED_MAGIC) {
        return Ok(None);
    }
    match StoredLogicalOutboxRecord::decode(stored)? {
        StoredLogicalOutboxRecord::Inline(_) => Ok(None),
        StoredLogicalOutboxRecord::Blob(reference) => Ok(Some(reference)),
    }
}

pub fn encode_logical_outbox_family_record(
    tenant_id: u64,
    owner_user_id: u64,
    sequence: u64,
    event_id: [u8; 32],
    stored: &StoredLogicalOutboxRecord,
) -> io::Result<Vec<u8>> {
    if tenant_id == 0 || owner_user_id == 0 || sequence == 0 {
        return Err(invalid_input("invalid logical outbox family scope"));
    }
    let stored = stored.encode()?;
    let mut encoded = Vec::with_capacity(FAMILY_HEADER_BYTES + stored.len());
    encoded.extend_from_slice(FAMILY_MAGIC);
    encoded.extend_from_slice(&VERSION.to_le_bytes());
    encoded.extend_from_slice(&tenant_id.to_le_bytes());
    encoded.extend_from_slice(&owner_user_id.to_le_bytes());
    encoded.extend_from_slice(&sequence.to_le_bytes());
    encoded.extend_from_slice(&event_id);
    encoded.extend_from_slice(&(stored.len() as u32).to_le_bytes());
    encoded.extend_from_slice(&stored);
    Ok(encoded)
}

pub fn decode_logical_outbox_family_record(
    encoded: &[u8],
) -> io::Result<LogicalOutboxFamilyRecord> {
    if encoded.len() < FAMILY_HEADER_BYTES || !encoded.starts_with(FAMILY_MAGIC) {
        return Err(invalid_data("invalid logical outbox family record"));
    }
    let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
    let tenant_id = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
    let owner_user_id = u64::from_le_bytes(encoded[20..28].try_into().unwrap());
    let sequence = u64::from_le_bytes(encoded[28..36].try_into().unwrap());
    let event_id = encoded[36..68].try_into().unwrap();
    let stored_bytes = u32::from_le_bytes(encoded[68..72].try_into().unwrap()) as usize;
    if version != VERSION
        || tenant_id == 0
        || owner_user_id == 0
        || sequence == 0
        || FAMILY_HEADER_BYTES.checked_add(stored_bytes) != Some(encoded.len())
    {
        return Err(invalid_data("invalid logical outbox family header"));
    }
    let family = LogicalOutboxFamilyRecord {
        tenant_id,
        owner_user_id,
        sequence,
        event_id,
        stored: StoredLogicalOutboxRecord::decode(&encoded[FAMILY_HEADER_BYTES..])?,
    };
    if let StoredLogicalOutboxRecord::Inline(inline) = &family.stored {
        let sealed = SealedLogicalOutboxRecord::decode(inline)?;
        if sealed.identity.tenant_id != family.tenant_id
            || sealed.identity.owner_user_id != family.owner_user_id
            || sealed.identity.sequence != family.sequence
            || sealed.event_id != family.event_id
        {
            return Err(invalid_data("logical outbox family identity differs"));
        }
    }
    Ok(family)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> LogicalOutboxIdentity {
        LogicalOutboxIdentity {
            sequence: 7,
            tenant_id: 3,
            owner_user_id: 9,
            schema_version: 57,
            registry_version: 37,
            operation: "artifact.create".to_owned(),
            request_id: "00000000-0000-0000-0000-000000000001".to_owned(),
            request_digest: [5; 32],
            command_id: Some("command-1".to_owned()),
            committed_at_ms: 1_000,
        }
    }

    #[test]
    fn aes_gcm_round_trip_binds_every_routing_field() {
        let cipher = LogicalOutboxCipher::new(&[7; 32]);
        let record = cipher
            .seal_with_nonce(identity(), b"canonical transaction IR", [3; 12])
            .unwrap();
        assert_eq!(cipher.open(&record).unwrap(), b"canonical transaction IR");
        for mutate in [
            |record: &mut SealedLogicalOutboxRecord| record.identity.owner_user_id += 1,
            |record: &mut SealedLogicalOutboxRecord| record.identity.sequence += 1,
            |record: &mut SealedLogicalOutboxRecord| record.identity.schema_version += 1,
        ] {
            let mut tampered = record.clone();
            mutate(&mut tampered);
            assert!(cipher.open(&tampered).is_err());
        }
        let mut tampered = record;
        tampered.ciphertext[0] ^= 1;
        assert!(cipher.open(&tampered).is_err());
    }

    #[test]
    fn event_identity_matches_the_existing_command_rule() {
        let expected: [u8; 32] = Sha256::digest(b"command\0artifact.create\0command-1").into();
        assert_eq!(identity().event_id().unwrap(), expected);
    }

    #[test]
    fn record_encoding_contains_only_ciphertext_and_bounded_metadata() {
        let cipher = LogicalOutboxCipher::new(&[7; 32]);
        let secret = b"private provider credential";
        let record = cipher.seal_with_nonce(identity(), secret, [3; 12]).unwrap();
        let encoded = record.encode().unwrap();
        let decoded = SealedLogicalOutboxRecord::decode(&encoded).unwrap();
        assert_eq!(decoded, record);
        assert_eq!(cipher.open(&decoded).unwrap(), secret);
        assert!(encoded.starts_with(MAGIC));
        assert!(!encoded.windows(secret.len()).any(|window| window == secret));
        assert!(cipher
            .seal_with_nonce(identity(), &vec![0; MAX_LOGICAL_PAYLOAD_BYTES + 1], [3; 12])
            .is_err());
        for length in 0..encoded.len() {
            assert!(SealedLogicalOutboxRecord::decode(&encoded[..length]).is_err());
        }
        let mut trailing = encoded;
        trailing.push(0);
        assert!(SealedLogicalOutboxRecord::decode(&trailing).is_err());
    }

    #[test]
    fn wrong_key_and_invalid_identity_fail_closed() {
        let cipher = LogicalOutboxCipher::new(&[7; 32]);
        let record = cipher
            .seal_with_nonce(identity(), b"payload", [3; 12])
            .unwrap();
        assert!(LogicalOutboxCipher::new(&[8; 32]).open(&record).is_err());
        let mut invalid = identity();
        invalid.tenant_id = 0;
        assert!(cipher
            .seal_with_nonce(invalid, b"payload", [3; 12])
            .is_err());

        let first = cipher.seal(identity(), b"payload").unwrap();
        let second = cipher.seal(identity(), b"payload").unwrap();
        assert_ne!(first.nonce, second.nonce);
    }

    #[test]
    fn stored_and_family_records_round_trip_inline_and_blob_references() {
        let cipher = LogicalOutboxCipher::new(&[7; 32]);
        let sealed = cipher
            .seal_with_nonce(identity(), b"payload", [3; 12])
            .unwrap();
        let inline = StoredLogicalOutboxRecord::Inline(sealed.encode().unwrap());
        let encoded_inline = inline.encode().unwrap();
        assert_eq!(
            StoredLogicalOutboxRecord::decode(&encoded_inline).unwrap(),
            inline
        );
        let family =
            encode_logical_outbox_family_record(3, 9, 7, sealed.event_id, &inline).unwrap();
        assert_eq!(
            decode_logical_outbox_family_record(&family).unwrap(),
            LogicalOutboxFamilyRecord {
                tenant_id: 3,
                owner_user_id: 9,
                sequence: 7,
                event_id: sealed.event_id,
                stored: inline,
            }
        );

        let blob = StoredLogicalOutboxRecord::Blob(BlobReference {
            blob_id: BlobId([5; 32]),
            manifest_block_id: BlockId([6; 32]),
            logical_bytes: 10_000,
        });
        assert_eq!(
            StoredLogicalOutboxRecord::decode(&blob.encode().unwrap()).unwrap(),
            blob
        );
    }

    #[test]
    fn stored_and_family_records_reject_truncation_and_identity_transplant() {
        let cipher = LogicalOutboxCipher::new(&[7; 32]);
        let sealed = cipher
            .seal_with_nonce(identity(), b"payload", [3; 12])
            .unwrap();
        let stored = StoredLogicalOutboxRecord::Inline(sealed.encode().unwrap());
        let encoded = stored.encode().unwrap();
        for length in 0..encoded.len() {
            assert!(StoredLogicalOutboxRecord::decode(&encoded[..length]).is_err());
        }
        let family =
            encode_logical_outbox_family_record(3, 9, 7, sealed.event_id, &stored).unwrap();
        for length in 0..family.len() {
            assert!(decode_logical_outbox_family_record(&family[..length]).is_err());
        }
        let mut transplanted = family;
        transplanted[28..36].copy_from_slice(&8_u64.to_le_bytes());
        assert!(decode_logical_outbox_family_record(&transplanted).is_err());

        let mut oversized_blob = StoredLogicalOutboxRecord::Blob(BlobReference {
            blob_id: BlobId([5; 32]),
            manifest_block_id: BlockId([6; 32]),
            logical_bytes: MAX_ENCODED_LOGICAL_OUTBOX_BYTES as u64 + 1,
        });
        assert!(oversized_blob.encode().is_err());
        if let StoredLogicalOutboxRecord::Blob(reference) = &mut oversized_blob {
            reference.logical_bytes = 1;
        }
        let mut trailing = oversized_blob.encode().unwrap();
        trailing.push(0);
        assert!(StoredLogicalOutboxRecord::decode(&trailing).is_err());
    }
}
