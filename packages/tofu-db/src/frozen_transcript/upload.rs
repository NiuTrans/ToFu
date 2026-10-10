//! One volatile, bounded page upload on the exclusive private operator pipe.
//! Exact offset/replay and SHA256 precede atomic page publication; failed finish,
//! abort and EOF release the buffer. Ordinary protocol frames stay unchanged.
use super::*;
use zeroize::Zeroizing;

#[derive(Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct BeginUpload {
    conversation_id: String,
    page_index: usize,
    start: usize,
    bytes: usize,
    sha256: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Append {
    offset: usize,
    hex: String,
}
struct Pending {
    tenant: u64,
    owner: u64,
    metadata: BeginUpload,
    buffer: Zeroizing<Vec<u8>>,
}
#[derive(Default)]
pub(crate) struct UploadState {
    pending: Option<Pending>,
}
impl UploadState {
    pub(crate) fn has_pending(&self) -> bool {
        self.pending.is_some()
    }
    pub(crate) fn execute(
        &mut self,
        database: &mut AuthorityDatabase,
        tenant: u64,
        owner: u64,
        operation: &str,
        payload: Value,
    ) -> io::Result<Value> {
        if self
            .pending
            .as_ref()
            .is_some_and(|p| p.tenant != tenant || p.owner != owner)
        {
            return Err(invalid("frozen transcript upload scope differs"));
        }
        match operation {
            "migration.frozen.transcript.upload.begin" => {
                let p: BeginUpload = serde_json::from_value(payload)
                    .map_err(|_| invalid("invalid frozen transcript upload"))?;
                validate_id(&p.conversation_id)?;
                if p.bytes == 0
                    || p.bytes > MAX_FROZEN_TRANSCRIPT_PAGE_BYTES
                    || p.page_index >= MAX_FROZEN_TRANSCRIPT_PAGES
                    || p.start > MAX_FROZEN_TRANSCRIPT_MESSAGES
                    || !valid_digest(&p.sha256)
                {
                    return Err(invalid("frozen transcript upload exceeds bounds"));
                }
                if let Some(existing) = &self.pending {
                    if existing.metadata != p {
                        return Err(conflict("a different frozen page upload is active"));
                    }
                    return Ok(json!({"offset":existing.buffer.len()}));
                }
                let mut tx = database.begin_with_identity_claim_scopes(tenant, owner)?;
                crate::conversation_header::require_active(database, &mut tx, &p.conversation_id)?;
                let (m, _) = load(database, &mut tx, &p.conversation_id)?
                    .ok_or_else(|| invalid("frozen transcript begin missing"))?;
                if p.page_index > m.pages.len()
                    || (p.page_index == m.pages.len() && (m.complete || p.start != m.messages))
                {
                    return Err(conflict("frozen transcript upload page order differs"));
                }
                self.pending = Some(Pending {
                    tenant,
                    owner,
                    buffer: Zeroizing::new(Vec::with_capacity(p.bytes)),
                    metadata: p,
                });
                Ok(json!({"offset":0}))
            }
            "migration.frozen.transcript.upload.append" => {
                let p: Append = serde_json::from_value(payload)
                    .map_err(|_| invalid("invalid frozen transcript upload chunk"))?;
                if p.hex.is_empty()
                    || !p.hex.len().is_multiple_of(2)
                    || p.hex.len() > 2 * MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES
                {
                    return Err(invalid("frozen transcript upload chunk exceeds bound"));
                }
                let raw: Vec<u8> = p
                    .hex
                    .as_bytes()
                    .chunks_exact(2)
                    .map(|pair| {
                        let s = std::str::from_utf8(pair)
                            .map_err(|_| invalid("invalid frozen transcript chunk hex"))?;
                        u8::from_str_radix(s, 16)
                            .map_err(|_| invalid("invalid frozen transcript chunk hex"))
                    })
                    .collect::<io::Result<_>>()?;
                let pending = self
                    .pending
                    .as_mut()
                    .ok_or_else(|| invalid("frozen transcript upload missing"))?;
                let end = p
                    .offset
                    .checked_add(raw.len())
                    .ok_or_else(|| invalid("frozen transcript chunk offset overflow"))?;
                if p.offset < pending.buffer.len() {
                    if end > pending.buffer.len() || pending.buffer[p.offset..end] != raw {
                        return Err(conflict("frozen transcript chunk replay differs"));
                    }
                } else {
                    if p.offset != pending.buffer.len() || end > pending.metadata.bytes {
                        return Err(invalid("frozen transcript chunk offset exceeds upload"));
                    }
                    pending.buffer.extend_from_slice(&raw);
                }
                Ok(json!({"offset":pending.buffer.len()}))
            }
            "migration.frozen.transcript.upload.finish" => {
                if payload != json!({}) {
                    return Err(invalid("invalid frozen transcript upload finish"));
                }
                let mut p = self
                    .pending
                    .take()
                    .ok_or_else(|| invalid("frozen transcript upload missing"))?;
                if p.buffer.len() != p.metadata.bytes || digest(&p.buffer) != p.metadata.sha256 {
                    return Err(invalid("frozen transcript upload bytes or hash differ"));
                }
                let mut tx = database.begin_with_identity_claim_scopes(tenant, owner)?;
                let inserted = restore_page(
                    database,
                    &mut tx,
                    &p.metadata.conversation_id,
                    p.metadata.page_index,
                    p.metadata.start,
                    std::mem::take(&mut *p.buffer),
                )?;
                if inserted {
                    database.commit(tx)?;
                }
                Ok(json!({"inserted":inserted}))
            }
            "migration.frozen.transcript.upload.abort" => {
                if payload != json!({}) {
                    return Err(invalid("invalid frozen transcript upload abort"));
                }
                self.pending = None;
                Ok(json!({"aborted":true}))
            }
            _ => Err(invalid("unknown frozen transcript upload operation")),
        }
    }
}
