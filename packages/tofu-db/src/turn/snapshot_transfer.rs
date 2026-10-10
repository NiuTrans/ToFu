//! One bounded private-pipe transfer buffer. Upload bytes are hash-checked before
//! atomic Turn restore; read snapshots are pinned in memory until the last chunk.
//! Shared session admission excludes event/frozen uploads. EOF drops and zeroizes.
use super::*;
use crate::generated_tofudb_ir::{
    MAX_TURN_SNAPSHOT_BYTES, MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES,
};
use sha2::{Digest, Sha256};

#[derive(Default)]
pub(crate) struct TransferState {
    pending: Option<Pending>,
}
struct Pending {
    read: bool,
    identity: Value,
    size: usize,
    digest: String,
    bytes: zeroize::Zeroizing<Vec<u8>>,
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn number(payload: &Value, key: &str) -> io::Result<usize> {
    payload
        .get(key)
        .and_then(Value::as_u64)
        .and_then(|x| usize::try_from(x).ok())
        .ok_or_else(|| invalid_input("invalid transfer offset or size"))
}
impl TransferState {
    pub(crate) fn has_pending(&self) -> bool {
        self.pending.is_some()
    }
    pub(crate) fn execute(
        &mut self,
        db: &mut AuthorityDatabase,
        tenant: u64,
        owner: u64,
        op: &str,
        payload: Value,
    ) -> io::Result<Value> {
        match op {
            "migration.turn.transfer.abort" | "migration.turn.transfer.read.abort" => {
                self.pending = None;
                Ok(json!({"aborted":true}))
            }
            "migration.turn.transfer.begin" => {
                let size = number(&payload, "bytes")?;
                let digest = payload
                    .get("sha256")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                if size == 0
                    || size > MAX_TURN_SNAPSHOT_BYTES
                    || digest.len() != 64
                    || !digest
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                {
                    return Err(invalid_input("Turn transfer size or digest is invalid"));
                }
                if let Some(p) = &self.pending {
                    if p.read || p.size != size || p.digest != digest {
                        return Err(snapshot_conflict());
                    }
                    return Ok(json!({"offset":p.bytes.len()}));
                }
                let mut bytes = Vec::new();
                bytes.try_reserve_exact(size).map_err(|_| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "Turn transfer allocation refused",
                    )
                })?;
                self.pending = Some(Pending {
                    read: false,
                    identity: Value::Null,
                    size,
                    digest,
                    bytes: zeroize::Zeroizing::new(bytes),
                });
                Ok(json!({"offset":0}))
            }
            "migration.turn.transfer.append" => {
                let offset = number(&payload, "offset")?;
                let text = payload
                    .get("hex")
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_input("missing chunk bytes"))?;
                if text.is_empty()
                    || text.len() % 2 != 0
                    || text.len() > MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES * 2
                {
                    return Err(invalid_input("invalid chunk bound"));
                }
                let chunk = text
                    .as_bytes()
                    .chunks_exact(2)
                    .map(|pair| {
                        let s = std::str::from_utf8(pair)
                            .map_err(|_| invalid_input("invalid chunk hex"))?;
                        u8::from_str_radix(s, 16).map_err(|_| invalid_input("invalid chunk hex"))
                    })
                    .collect::<io::Result<Vec<_>>>()?;
                let p = self
                    .pending
                    .as_mut()
                    .filter(|p| !p.read)
                    .ok_or_else(|| invalid_input("no Turn upload"))?;
                let end = offset
                    .checked_add(chunk.len())
                    .filter(|end| *end <= p.size)
                    .ok_or_else(|| invalid_input("chunk exceeds declared size"))?;
                if offset == p.bytes.len() {
                    p.bytes.extend_from_slice(&chunk);
                } else if p.bytes.get(offset..end) != Some(chunk.as_slice()) {
                    return Err(snapshot_conflict());
                }
                Ok(json!({"offset":p.bytes.len()}))
            }
            "migration.turn.transfer.finish" => {
                let p = self
                    .pending
                    .take()
                    .filter(|p| !p.read)
                    .ok_or_else(|| invalid_input("no Turn upload"))?;
                if p.bytes.len() != p.size || hash(&p.bytes) != p.digest {
                    return Err(invalid_input("Turn upload length or digest differs"));
                }
                let value = serde_json::from_slice(&p.bytes)
                    .map_err(|_| invalid_input("Turn upload is not JSON"))?;
                drop(p);
                super::execute(db, tenant, owner, "migration.turn.restore", value)
            }
            "migration.turn.transfer.read.begin" => {
                if let Some(p) = &self.pending {
                    if !p.read || p.identity != payload {
                        return Err(snapshot_conflict());
                    }
                    return Ok(json!({"bytes":p.size,"sha256":p.digest}));
                }
                let mut lookup = payload.clone();
                let trash = lookup.as_object_mut().and_then(|map| map.remove("trash"))
                    == Some(Value::Bool(true));
                let value = if trash {
                    crate::conversation_header::trash_migration::execute(
                        db,
                        tenant,
                        owner,
                        "migration.trash.turn.get",
                        lookup,
                    )?
                } else {
                    super::execute(db, tenant, owner, "migration.turn.get", lookup)?
                };
                let bytes = serde_json::to_vec(&value)
                    .map_err(|_| invalid_data("Turn snapshot encoding failed"))?;
                if bytes.len() > MAX_TURN_SNAPSHOT_BYTES {
                    return Err(invalid_input("Turn snapshot exceeds transfer bound"));
                }
                let size = bytes.len();
                let digest = hash(&bytes);
                self.pending = Some(Pending {
                    read: true,
                    identity: payload,
                    size,
                    digest: digest.clone(),
                    bytes: zeroize::Zeroizing::new(bytes),
                });
                Ok(json!({"bytes":size,"sha256":digest}))
            }
            "migration.turn.transfer.read.chunk" => {
                let offset = number(&payload, "offset")?;
                let p = self
                    .pending
                    .as_ref()
                    .filter(|p| p.read)
                    .ok_or_else(|| invalid_input("no Turn read snapshot"))?;
                if offset >= p.size {
                    return Err(invalid_input("read offset exceeds snapshot"));
                }
                let end = (offset + MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES).min(p.size);
                let hex: String = p.bytes[offset..end]
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect();
                let final_chunk = end == p.size;
                let result = json!({"offset":offset,"hex":hex,"final":final_chunk});
                if final_chunk {
                    self.pending = None;
                }
                Ok(result)
            }
            _ => Err(invalid_input("unknown Turn transfer operation")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::{path::Path, sync::Arc};
    #[test]
    fn turn_transfer_rejects_bad_chunks_and_releases_failed_finish() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        let mut transfer = TransferState::default();
        let data = b"{\"turn_id\":\"no-parent\"}";
        let begin = json!({"bytes":data.len(),"sha256":hash(data)});
        assert_eq!(
            transfer
                .execute(
                    &mut db,
                    7,
                    11,
                    "migration.turn.transfer.begin",
                    begin.clone()
                )
                .unwrap()["offset"],
            0
        );
        let hex: String = data.iter().map(|b| format!("{b:02x}")).collect();
        assert!(transfer
            .execute(
                &mut db,
                7,
                11,
                "migration.turn.transfer.append",
                json!({"offset":1,"hex":hex})
            )
            .is_err());
        let chunk = json!({"offset":0,"hex":hex});
        transfer
            .execute(
                &mut db,
                7,
                11,
                "migration.turn.transfer.append",
                chunk.clone(),
            )
            .unwrap();
        transfer
            .execute(&mut db, 7, 11, "migration.turn.transfer.append", chunk)
            .unwrap();
        assert!(transfer
            .execute(
                &mut db,
                7,
                11,
                "migration.turn.transfer.append",
                json!({"offset":0,"hex":"00"})
            )
            .is_err());
        assert!(transfer
            .execute(&mut db, 7, 11, "migration.turn.transfer.finish", json!({}))
            .is_err());
        assert!(!transfer.has_pending());
        transfer
            .execute(&mut db, 7, 11, "migration.turn.transfer.begin", begin)
            .unwrap();
        assert!(transfer
            .execute(&mut db, 7, 11, "migration.turn.transfer.finish", json!({}))
            .is_err());
        assert!(!transfer.has_pending());
        assert!(transfer
            .execute(
                &mut db,
                7,
                11,
                "migration.turn.transfer.begin",
                json!({"bytes":MAX_TURN_SNAPSHOT_BYTES+1,"sha256":hash(data)})
            )
            .is_err());
    }
}
