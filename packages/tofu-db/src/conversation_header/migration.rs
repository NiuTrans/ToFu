//! Exclusive offline header restore. Preserves the source logical revision and
//! timestamps; transcript adapters only reconstruct the derived message count.
//! Uses the ordinary owner claim, header codec and covering index transaction.

use super::*;
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    id: String,
    user_id: u64,
    title: String,
    settings: Value,
    created_at_ms: u64,
    updated_at_ms: u64,
    rev: u64,
}

pub(crate) fn restore(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    payload: Value,
) -> io::Result<Value> {
    let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
    let inserted = restore_in_transaction(database, &mut transaction, payload)?;
    if inserted {
        database.commit(transaction)?;
    }
    Ok(json!({"inserted": inserted}))
}

pub(crate) fn restore_in_transaction(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let tenant = transaction.tenant_id();
    let owner = transaction.owner_user_id();
    let source: Snapshot = serde_json::from_value(payload).map_err(|_| {
        io::Error::new(io::ErrorKind::InvalidInput, "invalid conversation snapshot")
    })?;
    if source.id.is_empty()
        || source.id.chars().count() > 256
        || source.user_id != owner
        || owner == 0
        || source.title.is_empty()
        || source.title.chars().count() > 500
        || !source.settings.is_object()
        || source.updated_at_ms < source.created_at_ms
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid conversation snapshot",
        ));
    }
    let expected = json!({"id": source.id, "user_id": owner, "title": source.title,
        "settings": source.settings, "created_at": source.created_at_ms,
        "updated_at": source.updated_at_ms, "rev": source.rev});
    if let Some((header, _)) = load_header(database, transaction, &source.id)? {
        if expected
            .as_object()
            .unwrap()
            .iter()
            .any(|(key, value)| header.get(key) != Some(value))
        {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "conversation snapshot differs",
            ));
        }
        return Ok(false);
    }
    create(
        database,
        transaction,
        &CreateRequest {
            conversation_id: source.id.clone(),
            title: source.title,
            settings_json: serde_json::to_vec(&source.settings)?,
            created_at_ms: source.created_at_ms,
            updated_at_ms: source.updated_at_ms,
            committed_at_ms: source.updated_at_ms.max(1),
        },
    )?;
    let (mut header, version) = load_header(database, transaction, &source.id)?
        .ok_or_else(|| invalid_data("imported conversation disappeared"))?;
    header.insert("rev".to_owned(), Value::from(source.rev));
    let envelope = store_header(
        database,
        transaction,
        &source.id,
        header,
        version,
        source.updated_at_ms.max(1),
    )?;
    let index = updated_index_key(tenant, owner, source.updated_at_ms, &source.id)?;
    database.entity_put(transaction, index, envelope)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Vfs};
    use std::{path::Path, sync::Arc};

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/headers")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/headers"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, database)
    }
    fn source() -> Value {
        json!({"id":"historical", "user_id":11, "title":"Historical", "settings":{"model":"saved"},
               "created_at_ms":500, "updated_at_ms":700, "rev":9183})
    }
    fn header(database: &AuthorityDatabase) -> Option<Map<String, Value>> {
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        load_header(database, &mut tx, "historical")
            .unwrap()
            .map(|(h, _)| h)
    }
    #[test]
    fn source_revision_and_clock_survive_turn_import_reopen_and_replay() {
        let (vfs, mut database) = prepared();
        restore(&mut database, 7, 11, source()).unwrap();
        crate::turn::migration::execute(&mut database, 7, 11, "migration.turn.restore", json!({
            "turn_id":"old-turn", "conversation_id":"historical", "presentation_id":"", "lane_id":"main",
            "parent_turn_id":null, "ordinal":1, "actor":"user", "kind":"reply", "run_id":"",
            "status":"completed", "current_attempt_id":null, "projection":{"content":"history"},
            "projection_revision":3, "settlement":{}, "created_at":600, "updated_at":900
        })).unwrap();
        let observed = header(&database).unwrap();
        assert_eq!(observed["rev"], 9183);
        assert_eq!(observed["updated_at"], 700);
        assert_eq!(observed["msg_count"], 1);
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/headers"), vfs).unwrap();
        assert_eq!(
            restore(&mut database, 7, 11, source()).unwrap()["inserted"],
            false
        );
        assert_eq!(header(&database).unwrap(), observed);
        let mut changed = source();
        changed["rev"] = json!(9184);
        assert!(restore(&mut database, 7, 11, changed).is_err());
        assert!(restore(&mut database, 7, 12, source()).is_err());
    }
    #[test]
    fn header_revision_and_covering_index_publish_atomically_under_faults() {
        let (baseline, mut database) = prepared();
        restore(&mut database, 7, 11, source()).unwrap();
        let trace = baseline.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == crate::vfs::Operation::Write {
                actions.push(FaultAction::ShortWrite(3));
            }
            for action in actions {
                let (vfs, mut database) = prepared();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = restore(&mut database, 7, 11, source()).is_ok();
                vfs.arm_fault(None).unwrap();
                drop(database);
                vfs.crash().unwrap();
                let mut database =
                    AuthorityDatabase::open_with_vfs(Path::new("/headers"), vfs).unwrap();
                if acknowledged {
                    assert!(header(&database).is_some());
                }
                if let Some(observed) = header(&database) {
                    assert_eq!(observed["rev"], 9183);
                    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
                    let index = updated_index_key(7, 11, 700, "historical").unwrap();
                    assert_eq!(
                        database.entity_get(&mut tx, &index).unwrap().unwrap(),
                        stored_document_envelope(&database, &mut tx, "historical").unwrap()
                    );
                }
                restore(&mut database, 7, 11, source()).unwrap();
            }
        }
    }
}
