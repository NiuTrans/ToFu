//! Owned provider settings CAS and semantic replay; plaintext secrets stay in vault.
//! Entry points: Request/execute. Dependencies: existing versioned record/MVCC
//! authority and generated source contract. Replay retains the first staged
//! credential reference, even when an identical retry stages a fresh reference.
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::automation_run::{conflict, digest, encode, integrity, invalid, put, read};
use crate::generated_tofudb_ir::{
    AUTOMATION_SKILLSOURCE_NAMESPACE, AUTOMATION_SKILL_SOURCE_CONTRACT,
    AUTOMATION_SKILL_SOURCE_CREDENTIAL_SCOPE_DOMAIN, AUTOMATION_SKILL_SOURCE_ENVIRONMENTS,
    AUTOMATION_SKILL_SOURCE_ID, MAX_AUTOMATION_SAFE_INTEGER,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    user_id: u64,
    tenant_id: String,
    source_key: String,
    expected_version: u64,
    request_id: String,
    actor_subject_id: String,
    environment: String,
    active: bool,
    credential_ref: Option<String>,
    credential_seal: Option<String>,
    now_ms: u64,
}

fn scope(owner: u64, tenant: &str) -> String {
    digest(
        format!("{AUTOMATION_SKILL_SOURCE_CREDENTIAL_SCOPE_DOMAIN}\n{owner}\n{tenant}").as_bytes(),
    )[..24]
        .to_owned()
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn credential_valid(
    owner: u64,
    tenant: &str,
    active: bool,
    reference: Option<&str>,
    seal: Option<&str>,
) -> bool {
    match (reference, seal) {
        (None, None) => !active,
        (Some(reference), Some(seal)) => {
            reference.split_once('_').is_some_and(|(prefix, version)| {
                prefix == scope(owner, tenant) && lower_hex(version, 32)
            }) && lower_hex(seal, 64)
        }
        _ => false,
    }
}

impl Request {
    pub(crate) fn validate(&self, owner: u64, now: u64) -> io::Result<()> {
        let nonce = self.request_id.as_bytes();
        if self.user_id == 0
            || self.user_id != owner
            || self.user_id > MAX_AUTOMATION_SAFE_INTEGER
            || self.expected_version > MAX_AUTOMATION_SAFE_INTEGER
            || self.tenant_id.chars().count() > 256
            || self.actor_subject_id.is_empty()
            || self.actor_subject_id.chars().count() > 256
            || nonce.is_empty()
            || nonce.len() > 128
            || !nonce[0].is_ascii_alphanumeric()
            || nonce
                .iter()
                .any(|byte| !byte.is_ascii_alphanumeric() && !b"_.-".contains(byte))
            || !AUTOMATION_SKILL_SOURCE_ENVIRONMENTS.contains(&self.environment.as_str())
            || self.source_key
                != format!(
                    "{owner}:{}:{AUTOMATION_SKILL_SOURCE_ID}",
                    &digest(self.tenant_id.as_bytes())[..32]
                )
            || !credential_valid(
                owner,
                &self.tenant_id,
                self.active,
                self.credential_ref.as_deref(),
                self.credential_seal.as_deref(),
            )
            || self.now_ms == 0
            || self.now_ms > MAX_AUTOMATION_SAFE_INTEGER
            || self.now_ms.saturating_sub(now)
                > crate::generated_tofudb_ir::MAX_AUTOMATION_EVENT_CLOCK_SKEW_MS
        {
            return Err(invalid("invalid owned skill source setting"));
        }
        Ok(())
    }

    pub(crate) fn literal_bytes(&self) -> io::Result<usize> {
        serde_json::to_vec(self)
            .map(|value| value.len())
            .map_err(|_| invalid("source request encoding"))
    }

    fn request_digest(&self) -> io::Result<String> {
        let mut value =
            serde_json::to_value(self).map_err(|_| invalid("source request encoding"))?;
        let object = value.as_object_mut().unwrap();
        object.remove("now_ms");
        object.remove("credential_ref");
        Ok(digest(&encode(&value)?))
    }
}

fn validate_stored(value: &Value, owner: u64, tenant: &str) -> io::Result<u64> {
    let fields = [
        "contract",
        "id",
        "ownerId",
        "tenantId",
        "environment",
        "active",
        "credentialRef",
        "credentialSeal",
        "createdAt",
        "updatedAt",
        "command",
    ];
    let object = value
        .as_object()
        .ok_or_else(|| integrity("stored source is not an object"))?;
    if object.len() != fields.len()
        || fields.iter().any(|field| !object.contains_key(*field))
        || value["contract"] != AUTOMATION_SKILL_SOURCE_CONTRACT
        || value["id"] != AUTOMATION_SKILL_SOURCE_ID
        || value["ownerId"].as_u64() != Some(owner)
        || (if tenant.is_empty() {
            !value["tenantId"].is_null()
        } else {
            value["tenantId"].as_str() != Some(tenant)
        })
        || value["environment"]
            .as_str()
            .is_none_or(|env| !AUTOMATION_SKILL_SOURCE_ENVIRONMENTS.contains(&env))
        || value["active"].as_bool().is_none()
        || (value["credentialRef"].as_str().is_none() && !value["credentialRef"].is_null())
        || (value["credentialSeal"].as_str().is_none() && !value["credentialSeal"].is_null())
        || !credential_valid(
            owner,
            tenant,
            value["active"] == true,
            value["credentialRef"].as_str(),
            value["credentialSeal"].as_str(),
        )
        || ["createdAt", "updatedAt"].iter().any(|field| {
            value[*field]
                .as_u64()
                .is_none_or(|time| time == 0 || time > MAX_AUTOMATION_SAFE_INTEGER)
        })
        || value["command"]
            .as_object()
            .is_none_or(|command| command.len() != 3)
        || value["command"]["digest"]
            .as_str()
            .is_none_or(|hash| !lower_hex(hash, 64))
    {
        return Err(integrity("stored source ownership or credential differs"));
    }
    let nonce = value["command"]["id"]
        .as_str()
        .ok_or_else(|| integrity("stored source request is missing"))?;
    let actor = value["command"]["actorSubjectId"]
        .as_str()
        .ok_or_else(|| integrity("stored source actor is missing"))?;
    if nonce.is_empty()
        || nonce.len() > 128
        || !nonce.as_bytes()[0].is_ascii_alphanumeric()
        || nonce
            .bytes()
            .any(|byte| !byte.is_ascii_alphanumeric() && !b"_.-".contains(&byte))
        || actor.is_empty()
        || actor.chars().count() > 256
    {
        return Err(integrity("stored source request is invalid"));
    }
    Ok(value["createdAt"].as_u64().unwrap())
}

pub(crate) fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Vec<u8>> {
    let current = read(
        db,
        tx,
        AUTOMATION_SKILLSOURCE_NAMESPACE,
        &request.source_key,
    )?;
    let hash = request.request_digest()?;
    let created = match &current {
        None => request.now_ms,
        Some(row) => {
            let created = validate_stored(&row["value"], tx.owner_user_id(), &request.tenant_id)?;
            let command = &row["value"]["command"];
            if command["id"] == request.request_id && command["digest"] != hash {
                return Err(conflict("source registration request changed"));
            }
            if row["version"].as_u64() == Some(request.expected_version + 1)
                && command["id"] == request.request_id
                && command["digest"] == hash
            {
                return encode(row);
            }
            created
        }
    };
    let value = json!({"contract":AUTOMATION_SKILL_SOURCE_CONTRACT,"id":AUTOMATION_SKILL_SOURCE_ID,
        "ownerId":request.user_id,"tenantId":if request.tenant_id.is_empty(){None}else{Some(&request.tenant_id)},
        "environment":request.environment,"active":request.active,"credentialRef":request.credential_ref,
        "credentialSeal":request.credential_seal,"createdAt":created,"updatedAt":request.now_ms,
        "command":{"id":request.request_id,"digest":hash,"actorSubjectId":request.actor_subject_id}});
    let mut row = put(
        db,
        tx,
        AUTOMATION_SKILLSOURCE_NAMESPACE,
        &request.source_key,
        &value,
        request.expected_version,
        request.now_ms,
    )?;
    row.as_object_mut()
        .ok_or_else(|| integrity("source result is not an object"))?
        .remove("key");
    encode(&row)
}
