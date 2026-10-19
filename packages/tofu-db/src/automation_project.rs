//! Atomic owned automation project registration, immutable root binding and CAS.
//! Entry points: Request/execute/require_active. Uses the existing versioned
//! record authority; registered paths are metadata and confer no file-write grant.
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::automation_run::{conflict, digest, encode, integrity, invalid, put, read};
use crate::generated_tofudb_ir::{
    AUTOMATION_PROJECT_CONTRACT, AUTOMATION_TARGET_NAMESPACE,
    MAX_AUTOMATION_PROJECT_LABEL_CHARACTERS, MAX_AUTOMATION_PROJECT_ROOT_CHARACTERS,
    MAX_AUTOMATION_SAFE_INTEGER,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    user_id: u64,
    tenant_id: String,
    project_key: String,
    expected_version: u64,
    request_id: String,
    actor_subject_id: String,
    root: String,
    label: String,
    active: bool,
    now_ms: u64,
}

fn prefix(owner: u64, tenant: &str) -> String {
    format!("{owner}:{}:", &digest(tenant.as_bytes())[..32])
}

fn tenant_matches(value: &Value, tenant: &str) -> bool {
    if tenant.is_empty() {
        value.is_null()
    } else {
        value.as_str() == Some(tenant)
    }
}

impl Request {
    fn identity(&self) -> String {
        format!("project_{}", digest(self.root.as_bytes()))
    }
    pub(crate) fn validate(&self, owner: u64, now: u64) -> io::Result<()> {
        let request = self.request_id.as_bytes();
        if self.user_id == 0
            || self.user_id != owner
            || self.user_id > MAX_AUTOMATION_SAFE_INTEGER
            || self.expected_version > MAX_AUTOMATION_SAFE_INTEGER
            || self.tenant_id.chars().count() > 256
            || self.root.is_empty()
            || self.root.chars().count() > MAX_AUTOMATION_PROJECT_ROOT_CHARACTERS
            || self.root.contains('\0')
            || self.label.is_empty()
            || self.label.chars().count() > MAX_AUTOMATION_PROJECT_LABEL_CHARACTERS
            || self.actor_subject_id.is_empty()
            || self.actor_subject_id.chars().count() > 256
            || request.is_empty()
            || request.len() > 128
            || !request[0].is_ascii_alphanumeric()
            || request
                .iter()
                .any(|c| !c.is_ascii_alphanumeric() && !b"_.-".contains(c))
            || self.project_key != format!("{}{}", prefix(owner, &self.tenant_id), self.identity())
            || self.now_ms == 0
            || self.now_ms > MAX_AUTOMATION_SAFE_INTEGER
            || self.now_ms.saturating_sub(now)
                > crate::generated_tofudb_ir::MAX_AUTOMATION_EVENT_CLOCK_SKEW_MS
        {
            return Err(invalid("invalid owned project registration"));
        }
        Ok(())
    }
    pub(crate) fn literal_bytes(&self) -> io::Result<usize> {
        serde_json::to_vec(self)
            .map(|value| value.len())
            .map_err(|_| invalid("project request encoding"))
    }
    fn request_digest(&self) -> io::Result<String> {
        let mut value =
            serde_json::to_value(self).map_err(|_| invalid("project request encoding"))?;
        value.as_object_mut().unwrap().remove("now_ms");
        Ok(digest(&encode(&value)?))
    }
}

pub(crate) fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    req: &Request,
) -> io::Result<Vec<u8>> {
    req.validate(tx.owner_user_id(), u64::MAX)?;
    let current = read(db, tx, AUTOMATION_TARGET_NAMESPACE, &req.project_key)?;
    let digest = req.request_digest()?;
    let created = if let Some(row) = &current {
        let old = &row["value"];
        if old["contract"] != AUTOMATION_PROJECT_CONTRACT
            || old["id"] != req.identity()
            || old["ownerId"].as_u64() != Some(req.user_id)
            || !tenant_matches(&old["tenantId"], &req.tenant_id)
            || old["root"] != req.root
            || old["createdAt"].as_u64().is_none()
        {
            return Err(integrity("stored project identity differs"));
        }
        let command = &old["command"];
        if command["id"] == req.request_id && command["digest"] != digest {
            return Err(conflict("project registration request changed"));
        }
        if row["version"].as_u64() == Some(req.expected_version + 1)
            && command["id"] == req.request_id
            && command["digest"] == digest
        {
            return encode(row);
        }
        old["createdAt"].as_u64().unwrap()
    } else {
        req.now_ms
    };
    let tenant = if req.tenant_id.is_empty() {
        Value::Null
    } else {
        json!(req.tenant_id)
    };
    let value = json!({"contract":AUTOMATION_PROJECT_CONTRACT,"id":req.identity(),
        "ownerId":req.user_id,"tenantId":tenant,"root":req.root,"label":req.label,"active":req.active,
        "createdAt":created,"updatedAt":req.now_ms,
        "command":{"id":req.request_id,"digest":digest,"actorSubjectId":req.actor_subject_id}});
    let mut saved = put(
        db,
        tx,
        AUTOMATION_TARGET_NAMESPACE,
        &req.project_key,
        &value,
        req.expected_version,
        req.now_ms,
    )?;
    // Record writes carry an internal key; public registration and its replay
    // share the read envelope, as does the selected SQL authority.
    saved.as_object_mut().unwrap().remove("key");
    encode(&saved)
}

pub(crate) fn require_active(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    tenant: &str,
    identity: Option<&str>,
) -> io::Result<()> {
    let Some(identity) = identity else {
        return Ok(());
    };
    let key = format!("{}{}", prefix(tx.owner_user_id(), tenant), identity);
    let row = read(db, tx, AUTOMATION_TARGET_NAMESPACE, &key)?;
    let Some(row) = row else {
        return Err(conflict("owned automation project is unavailable"));
    };
    let value = &row["value"];
    if value["contract"] != AUTOMATION_PROJECT_CONTRACT
        || value["id"] != identity
        || value["ownerId"].as_u64() != Some(tx.owner_user_id())
        || !tenant_matches(&value["tenantId"], tenant)
        || value["active"] != true
    {
        return Err(conflict("owned automation project is unavailable"));
    }
    if value["root"]
        .as_str()
        .is_none_or(|root| format!("project_{}", digest(root.as_bytes())) != identity)
    {
        return Err(integrity("stored project root identity differs"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registration_replay_preserves_public_envelope() {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        let root = "/project/replay";
        let mut req: Request = serde_json::from_value(json!({
            "user_id": 11, "tenant_id": "owned", "project_key":
                format!("{}project_{}", prefix(11, "owned"), digest(root.as_bytes())),
            "expected_version": 0, "request_id": "register", "actor_subject_id": "owner",
            "root": root, "label": "Replay", "active": true, "now_ms": 1000,
        }))
        .unwrap();
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let first: Value = serde_json::from_slice(&execute(&db, &mut tx, &req).unwrap()).unwrap();
        assert_eq!(first.as_object().unwrap().len(), 3);
        assert_eq!(first["version"], 1);
        assert!(first.get("key").is_none());
        db.commit(tx).unwrap();
        drop(db);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        let mut replay_tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        req.now_ms = 2000;
        let replay: Value =
            serde_json::from_slice(&execute(&db, &mut replay_tx, &req).unwrap()).unwrap();
        assert_eq!(first, replay);
    }
}
