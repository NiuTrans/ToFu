//! Bounded, owner-scoped command receipt identity and storage encoding.

use std::io::{self, Cursor, Read, Write};

use flate2::bufread::ZlibDecoder;
use flate2::write::ZlibEncoder;
use flate2::Compression;
use sha2::{Digest, Sha256};

use crate::blob::{BlobId, BlobReference};
use crate::block::BlockId;
use crate::entity::EntityKey;
use crate::protocol::MAX_OPERATION_BYTES;

const ENTRY_MAGIC: &[u8; 8] = b"TDBRCP01";
const ENTRY_VERSION: u32 = 1;
const ENTRY_FIXED_BYTES: usize = 8 + 4 + 2 + 1 + 1 + 32 + 8 + 4;
const RESPONSE_INLINE: u8 = 1;
const RESPONSE_BLOB: u8 = 2;
const BLOB_REFERENCE_BYTES: usize = 32 + 32 + 8;
const COMMAND_KEY_DOMAIN: &[u8] = b"tofu.command-receipt.command.v2\0";
const RECEIPT_CODEC_PREFIX: &[u8] = b"tofu.receipt.";
pub const COMPRESSED_RECEIPT_MAGIC: &[u8] = b"tofu.receipt.zlib.v1\0";
pub const MAX_STORED_RECEIPT_BYTES: usize = 64 * 1024;
pub const MAX_DECODED_RECEIPT_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_INLINE_RECEIPT_BYTES: usize = 7 * 1024;
pub const MAX_RECEIPT_COMMAND_ID_BYTES: usize = 200;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StoredReceiptResponse {
    Inline(Vec<u8>),
    Blob(BlobReference),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandReceipt {
    pub operation: String,
    pub request_digest: [u8; 32],
    pub committed_at_ms: u64,
    pub response: StoredReceiptResponse,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiptFamilyRecord {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub command_key: [u8; 32],
    pub receipt: CommandReceipt,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn validate_text(value: &str, maximum: usize, name: &str) -> io::Result<()> {
    if value.is_empty() || value.len() > maximum || value.as_bytes().contains(&0) {
        return Err(invalid_input(name));
    }
    Ok(())
}

pub fn command_receipt_key(command_id: &str) -> io::Result<[u8; 32]> {
    validate_text(
        command_id,
        MAX_RECEIPT_COMMAND_ID_BYTES,
        "invalid command receipt command ID",
    )?;
    let mut hasher = Sha256::new();
    hasher.update(COMMAND_KEY_DOMAIN);
    hasher.update(command_id.as_bytes());
    Ok(hasher.finalize().into())
}

pub fn command_receipt_entity_key(
    tenant_id: u64,
    owner_user_id: u64,
    command_id: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        tenant_id,
        owner_user_id,
        "command_receipt",
        &command_receipt_key(command_id)?,
    )
}

pub fn validate_receipt_identity(operation: &str, committed_at_ms: u64) -> io::Result<()> {
    validate_text(
        operation,
        MAX_OPERATION_BYTES,
        "invalid command receipt operation",
    )?;
    if committed_at_ms == 0 {
        return Err(invalid_input("invalid command receipt commit time"));
    }
    Ok(())
}

pub fn encode_receipt_response(raw: &[u8]) -> io::Result<Vec<u8>> {
    if raw.is_empty() {
        return Err(invalid_input("command receipt response is empty"));
    }
    if raw.len() <= MAX_STORED_RECEIPT_BYTES {
        return Ok(raw.to_vec());
    }
    if raw.len() > MAX_DECODED_RECEIPT_BYTES {
        return Err(invalid_input(
            "command response exceeds decoded receipt budget",
        ));
    }
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::new(1));
    encoder.write_all(raw)?;
    let compressed = encoder.finish()?;
    let mut encoded = Vec::with_capacity(COMPRESSED_RECEIPT_MAGIC.len() + 4 + compressed.len());
    encoded.extend_from_slice(COMPRESSED_RECEIPT_MAGIC);
    encoded.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    encoded.extend_from_slice(&compressed);
    if encoded.len() > MAX_STORED_RECEIPT_BYTES {
        return Err(invalid_input("command response is too large for a receipt"));
    }
    Ok(encoded)
}

pub fn decode_receipt_response(encoded: &[u8]) -> io::Result<Vec<u8>> {
    if encoded.len() > MAX_STORED_RECEIPT_BYTES {
        return Err(invalid_data(
            "stored command receipt exceeds its byte budget",
        ));
    }
    if !encoded.starts_with(COMPRESSED_RECEIPT_MAGIC) {
        if encoded.starts_with(RECEIPT_CODEC_PREFIX) {
            return Err(invalid_data(
                "stored command receipt uses an unsupported codec",
            ));
        }
        return Ok(encoded.to_vec());
    }
    let payload_start = COMPRESSED_RECEIPT_MAGIC.len() + 4;
    if encoded.len() <= payload_start {
        return Err(invalid_data("stored compressed receipt is truncated"));
    }
    let decoded_length = u32::from_be_bytes(
        encoded[COMPRESSED_RECEIPT_MAGIC.len()..payload_start]
            .try_into()
            .unwrap(),
    ) as usize;
    if decoded_length <= MAX_STORED_RECEIPT_BYTES || decoded_length > MAX_DECODED_RECEIPT_BYTES {
        return Err(invalid_data("stored compressed receipt length is invalid"));
    }
    let compressed = &encoded[payload_start..];
    let mut decoder = ZlibDecoder::new(Cursor::new(compressed));
    let mut raw = Vec::with_capacity(decoded_length);
    decoder
        .by_ref()
        .take(decoded_length as u64 + 1)
        .read_to_end(&mut raw)?;
    if raw.len() != decoded_length
        || decoder.total_out() as usize != decoded_length
        || decoder.total_in() as usize != compressed.len()
    {
        return Err(invalid_data("stored compressed receipt length mismatched"));
    }
    Ok(raw)
}

impl CommandReceipt {
    pub fn encode(&self) -> io::Result<Vec<u8>> {
        validate_receipt_identity(&self.operation, self.committed_at_ms)?;
        let (response_kind, response_bytes): (u8, Vec<u8>) = match &self.response {
            StoredReceiptResponse::Inline(response) => {
                if response.len() > MAX_INLINE_RECEIPT_BYTES {
                    return Err(invalid_input("inline command receipt is too large"));
                }
                (RESPONSE_INLINE, response.clone())
            }
            StoredReceiptResponse::Blob(reference) => {
                if reference.logical_bytes == 0
                    || reference.logical_bytes > MAX_STORED_RECEIPT_BYTES as u64
                {
                    return Err(invalid_input("receipt blob reference is invalid"));
                }
                let mut encoded = Vec::with_capacity(BLOB_REFERENCE_BYTES);
                encoded.extend_from_slice(&reference.blob_id.0);
                encoded.extend_from_slice(&reference.manifest_block_id.0);
                encoded.extend_from_slice(&reference.logical_bytes.to_le_bytes());
                (RESPONSE_BLOB, encoded)
            }
        };
        let total = ENTRY_FIXED_BYTES
            .checked_add(self.operation.len())
            .and_then(|length| length.checked_add(response_bytes.len()))
            .ok_or_else(|| invalid_input("command receipt length overflow"))?;
        let mut encoded = Vec::with_capacity(total);
        encoded.extend_from_slice(ENTRY_MAGIC);
        encoded.extend_from_slice(&ENTRY_VERSION.to_le_bytes());
        encoded.extend_from_slice(&(self.operation.len() as u16).to_le_bytes());
        encoded.push(response_kind);
        encoded.push(0);
        encoded.extend_from_slice(&self.request_digest);
        encoded.extend_from_slice(&self.committed_at_ms.to_le_bytes());
        encoded.extend_from_slice(&(response_bytes.len() as u32).to_le_bytes());
        encoded.extend_from_slice(self.operation.as_bytes());
        encoded.extend_from_slice(&response_bytes);
        Ok(encoded)
    }

    pub fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < ENTRY_FIXED_BYTES || !encoded.starts_with(ENTRY_MAGIC) {
            return Err(invalid_data("invalid command receipt entry"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let operation_length = u16::from_le_bytes(encoded[12..14].try_into().unwrap()) as usize;
        let response_kind = encoded[14];
        let reserved = encoded[15];
        let request_digest = encoded[16..48].try_into().unwrap();
        let committed_at_ms = u64::from_le_bytes(encoded[48..56].try_into().unwrap());
        let response_length = u32::from_le_bytes(encoded[56..60].try_into().unwrap()) as usize;
        let expected = ENTRY_FIXED_BYTES
            .checked_add(operation_length)
            .and_then(|length| length.checked_add(response_length))
            .ok_or_else(|| invalid_data("command receipt length overflow"))?;
        if version != ENTRY_VERSION
            || reserved != 0
            || committed_at_ms == 0
            || operation_length == 0
            || operation_length > MAX_OPERATION_BYTES
            || expected != encoded.len()
        {
            return Err(invalid_data("invalid command receipt header"));
        }
        let operation = std::str::from_utf8(&encoded[60..60 + operation_length])
            .map_err(|_| invalid_data("command receipt operation is not UTF-8"))?;
        validate_text(
            operation,
            MAX_OPERATION_BYTES,
            "invalid command receipt operation",
        )
        .map_err(|_| invalid_data("invalid command receipt operation"))?;
        let response_bytes = &encoded[60 + operation_length..];
        let response = match response_kind {
            RESPONSE_INLINE
                if !response_bytes.is_empty()
                    && response_bytes.len() <= MAX_INLINE_RECEIPT_BYTES =>
            {
                StoredReceiptResponse::Inline(response_bytes.to_vec())
            }
            RESPONSE_BLOB if response_bytes.len() == BLOB_REFERENCE_BYTES => {
                let logical_bytes = u64::from_le_bytes(response_bytes[64..72].try_into().unwrap());
                if logical_bytes == 0 || logical_bytes > MAX_STORED_RECEIPT_BYTES as u64 {
                    return Err(invalid_data("receipt blob reference is invalid"));
                }
                StoredReceiptResponse::Blob(BlobReference {
                    blob_id: BlobId(response_bytes[..32].try_into().unwrap()),
                    manifest_block_id: BlockId(response_bytes[32..64].try_into().unwrap()),
                    logical_bytes,
                })
            }
            _ => return Err(invalid_data("invalid command receipt response storage")),
        };
        Ok(Self {
            operation: operation.to_owned(),
            request_digest,
            committed_at_ms,
            response,
        })
    }
}

pub(crate) fn stored_blob_reference(stored: &[u8]) -> io::Result<Option<BlobReference>> {
    if !stored.starts_with(ENTRY_MAGIC) {
        return Ok(None);
    }
    match CommandReceipt::decode(stored)?.response {
        StoredReceiptResponse::Inline(_) => Ok(None),
        StoredReceiptResponse::Blob(reference) => Ok(Some(reference)),
    }
}

pub fn encode_receipt_family_record(
    tenant_id: u64,
    owner_user_id: u64,
    command_key: [u8; 32],
    entry: &[u8],
) -> io::Result<Vec<u8>> {
    if tenant_id == 0 || owner_user_id == 0 {
        return Err(invalid_input("invalid command receipt owner scope"));
    }
    CommandReceipt::decode(entry)?;
    let mut record = Vec::with_capacity(16 + 32 + entry.len());
    record.extend_from_slice(&tenant_id.to_le_bytes());
    record.extend_from_slice(&owner_user_id.to_le_bytes());
    record.extend_from_slice(&command_key);
    record.extend_from_slice(entry);
    Ok(record)
}

pub fn decode_receipt_family_record(record: &[u8]) -> io::Result<ReceiptFamilyRecord> {
    if record.len() < 16 + 32 + ENTRY_FIXED_BYTES {
        return Err(invalid_data("truncated command receipt family record"));
    }
    let tenant_id = u64::from_le_bytes(record[..8].try_into().unwrap());
    let owner_user_id = u64::from_le_bytes(record[8..16].try_into().unwrap());
    if tenant_id == 0 || owner_user_id == 0 {
        return Err(invalid_data("invalid command receipt owner scope"));
    }
    Ok(ReceiptFamilyRecord {
        tenant_id,
        owner_user_id,
        command_key: record[16..48].try_into().unwrap(),
        receipt: CommandReceipt::decode(&record[48..])?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_key_matches_the_existing_storage_v2_authority() {
        assert_eq!(
            command_receipt_key("command-1").unwrap(),
            [
                0xe7, 0x0d, 0x50, 0xa3, 0xef, 0xcb, 0x6d, 0x9c, 0x66, 0x93, 0x92, 0xc1, 0x13, 0xcd,
                0x0c, 0xc0, 0x73, 0x4b, 0x5f, 0xfc, 0xaa, 0xb8, 0xb2, 0xa7, 0x2b, 0x13, 0xba, 0xc0,
                0x64, 0x8e, 0x76, 0x08,
            ]
        );
        assert!(command_receipt_key(&"x".repeat(MAX_RECEIPT_COMMAND_ID_BYTES)).is_ok());
        assert!(command_receipt_key(&"x".repeat(MAX_RECEIPT_COMMAND_ID_BYTES + 1)).is_err());
    }

    #[test]
    fn response_codec_round_trips_raw_and_bounded_compressed_bytes() {
        let raw = br#"{"ok":true}"#;
        assert_eq!(
            decode_receipt_response(&encode_receipt_response(raw).unwrap()).unwrap(),
            raw
        );
        let large = vec![b'x'; 100_000];
        let encoded = encode_receipt_response(&large).unwrap();
        assert!(encoded.starts_with(COMPRESSED_RECEIPT_MAGIC));
        assert!(encoded.len() <= MAX_STORED_RECEIPT_BYTES);
        assert_eq!(decode_receipt_response(&encoded).unwrap(), large);
        assert!(encode_receipt_response(&vec![1; MAX_DECODED_RECEIPT_BYTES + 1]).is_err());
    }

    #[test]
    fn receipt_entry_round_trips_inline_and_blob_storage() {
        let inline = CommandReceipt {
            operation: "artifact.create".to_owned(),
            request_digest: [3; 32],
            committed_at_ms: 42,
            response: StoredReceiptResponse::Inline(b"response".to_vec()),
        };
        assert_eq!(
            CommandReceipt::decode(&inline.encode().unwrap()).unwrap(),
            inline
        );
        let blob = CommandReceipt {
            operation: "artifact.create".to_owned(),
            request_digest: [7; 32],
            committed_at_ms: 43,
            response: StoredReceiptResponse::Blob(BlobReference {
                blob_id: BlobId([8; 32]),
                manifest_block_id: BlockId([9; 32]),
                logical_bytes: 60_000,
            }),
        };
        assert_eq!(
            CommandReceipt::decode(&blob.encode().unwrap()).unwrap(),
            blob
        );
        let entry = inline.encode().unwrap();
        let family = encode_receipt_family_record(7, 11, [5; 32], &entry).unwrap();
        assert_eq!(
            decode_receipt_family_record(&family).unwrap(),
            ReceiptFamilyRecord {
                tenant_id: 7,
                owner_user_id: 11,
                command_key: [5; 32],
                receipt: inline,
            }
        );
    }

    #[test]
    fn response_decoder_rejects_unknown_trailing_and_forged_compression() {
        assert!(decode_receipt_response(b"tofu.receipt.future\0bad").is_err());
        let large = vec![b'x'; 100_000];
        let mut encoded = encode_receipt_response(&large).unwrap();
        encoded.extend_from_slice(b"trailing");
        assert!(decode_receipt_response(&encoded).is_err());
        let length_offset = COMPRESSED_RECEIPT_MAGIC.len();
        encoded.truncate(encoded.len() - b"trailing".len());
        encoded[length_offset..length_offset + 4].copy_from_slice(&100_001_u32.to_be_bytes());
        assert!(decode_receipt_response(&encoded).is_err());
    }
}
