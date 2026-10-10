//! Explicit offline import primitives; never a normal startup or wire path.
//!
//! The operator binds an immutable source and mapping plan to the opened
//! authority UUID. Domain adapters supply canonical logical documents and
//! rebuild all their indexes in the same caller-owned AuthorityTransaction.
//! Exact target enumeration is required before external progress publication.

use std::io;

use uuid::Uuid;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
pub use crate::versioned_document::{SnapshotDocument, SnapshotRestoreOutcome};

pub fn restore_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    expected_authority_uuid: Uuid,
    document: SnapshotDocument,
) -> io::Result<SnapshotRestoreOutcome> {
    if database.authority_uuid() != expected_authority_uuid {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "migration target authority identity differs",
        ));
    }
    crate::versioned_document::restore_snapshot(database, transaction, document)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use std::sync::Arc;

    use crate::entity::EntityKey;
    use crate::versioned_document::{self, PutRequest};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, database)
    }

    fn key() -> EntityKey {
        EntityKey::new(7, 11, "migration_test", b"row").unwrap()
    }

    fn marker_key() -> EntityKey {
        EntityKey::new(7, 11, "migration_test", b"verified-prefix").unwrap()
    }

    fn document(bytes: usize) -> SnapshotDocument {
        SnapshotDocument {
            key: key(),
            namespace: "logical_records".into(),
            logical_key: "row".into(),
            version: 1042,
            updated_at_ms: 123_456_789,
            value_json: serde_json::to_vec(&serde_json::json!({"payload": "x".repeat(bytes)}))
                .unwrap(),
            blob_owner_user_id: 11,
        }
    }

    fn projected(database: &AuthorityDatabase) -> Option<serde_json::Value> {
        let mut transaction = database.begin(7, 11).unwrap();
        versioned_document::get(database, &mut transaction, &key(), "logical_records", "row")
            .unwrap()
            .map(|bytes| serde_json::from_slice(&bytes).unwrap())
    }

    #[test]
    fn import_preserves_inline_and_blob_metadata_and_normal_updates_continue() {
        for bytes in [16, 32 * 1024] {
            let (vfs, mut database) = prepared();
            let uuid = database.authority_uuid();
            let mut transaction = database.begin(7, 11).unwrap();
            assert_eq!(
                restore_document(&database, &mut transaction, uuid, document(bytes)).unwrap(),
                SnapshotRestoreOutcome::Inserted
            );
            database.commit(transaction).unwrap();
            drop(database);
            vfs.crash().unwrap();
            let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let projection = projected(&database).unwrap();
            assert_eq!(projection["version"], 1042);
            assert_eq!(projection["updated_at_ms"], 123_456_789);
            assert_eq!(projection["value"]["payload"], "x".repeat(bytes));
            let mut transaction = database.begin(7, 11).unwrap();
            assert_eq!(
                restore_document(&database, &mut transaction, uuid, document(bytes)).unwrap(),
                SnapshotRestoreOutcome::AlreadyMatches
            );
            drop(transaction);
            let mut transaction = database.begin(7, 11).unwrap();
            let response = versioned_document::put(
                &database,
                &mut transaction,
                PutRequest {
                    key: key(),
                    namespace: "logical_records".into(),
                    logical_key: "row".into(),
                    value_json: br#"{"updated":true}"#.to_vec(),
                    expected_version: Some(1042),
                    updated_at_ms: 123_456_790,
                },
            )
            .unwrap();
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&response).unwrap()["version"],
                1043
            );
            database.commit(transaction).unwrap();
            assert_eq!(projected(&database).unwrap()["value"]["updated"], true);
        }
    }

    #[test]
    fn import_refuses_other_authorities_owners_and_divergent_existing_documents() {
        let (vfs, mut database) = prepared();
        let uuid = database.authority_uuid();
        let mut transaction = database.begin(7, 11).unwrap();
        let error = restore_document(&database, &mut transaction, Uuid::new_v4(), document(16))
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
        assert!(vfs.trace().unwrap().is_empty());
        let mut foreign = document(16);
        foreign.key = EntityKey::new(7, 12, "migration_test", b"row").unwrap();
        assert_eq!(
            restore_document(&database, &mut transaction, uuid, foreign)
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        restore_document(&database, &mut transaction, uuid, document(16)).unwrap();
        database.commit(transaction).unwrap();
        let before = projected(&database).unwrap();
        for variant in 0..3 {
            let mut transaction = database.begin(7, 11).unwrap();
            let mut changed = document(16);
            match variant {
                0 => changed.version += 1,
                1 => changed.updated_at_ms += 1,
                _ => changed.value_json = br#"{"different":true}"#.to_vec(),
            }
            assert_eq!(
                restore_document(&database, &mut transaction, uuid, changed)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::WouldBlock
            );
            drop(transaction);
            assert_eq!(projected(&database).unwrap(), before);
        }
    }

    fn import_and_mark(database: &mut AuthorityDatabase) -> io::Result<()> {
        let mut transaction = database.begin(7, 11)?;
        restore_document(
            database,
            &mut transaction,
            database.authority_uuid(),
            document(32 * 1024),
        )?;
        database.entity_put(
            &mut transaction,
            marker_key(),
            b"verified-prefix-1".to_vec(),
        )?;
        database.commit(transaction)?;
        Ok(())
    }

    #[test]
    fn import_rejects_invalid_metadata_json_and_bytes_before_storage_io() {
        let (vfs, database) = prepared();
        for variant in 0..5 {
            let mut request = document(16);
            match variant {
                0 => request.version = 0,
                1 => request.updated_at_ms = 0,
                2 => request.value_json = b"{".to_vec(),
                3 => {
                    request.value_json =
                        vec![b' '; crate::generated_tofudb_ir::MAX_MIGRATION_DOCUMENT_BYTES + 1]
                }
                _ => request.blob_owner_user_id = 12,
            }
            let mut transaction = database.begin(7, 11).unwrap();
            assert!(restore_document(
                &database,
                &mut transaction,
                database.authority_uuid(),
                request
            )
            .is_err());
            assert!(vfs.trace().unwrap().is_empty());
        }
        assert!(projected(&database).is_none());
    }

    #[test]
    fn import_document_and_progress_recover_together_under_every_commit_fault() {
        let (baseline, mut database) = prepared();
        import_and_mark(&mut database).unwrap();
        let trace = baseline.trace().unwrap();
        assert!(!trace.is_empty());
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::StorageFull)];
            if operation == &Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            if matches!(
                operation,
                Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
            ) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let (vfs, mut database) = prepared();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = import_and_mark(&mut database).is_ok();
                drop(database);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut database =
                    AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
                let projection = projected(&database);
                let mut transaction = database.begin(7, 11).unwrap();
                let marker = database
                    .entity_get(&mut transaction, &marker_key())
                    .unwrap();
                drop(transaction);
                assert_eq!(
                    projection.is_some(),
                    marker.is_some(),
                    "{index}: {action:?}"
                );
                assert!(
                    !acknowledged || projection.is_some(),
                    "lost acknowledgement at {index}: {action:?}"
                );
                if let Some(projection) = projection {
                    assert_eq!(projection["version"], 1042);
                    assert_eq!(projection["updated_at_ms"], 123_456_789);
                    assert_eq!(projection["value"]["payload"], "x".repeat(32 * 1024));
                    assert_eq!(marker.unwrap(), b"verified-prefix-1");
                }
                import_and_mark(&mut database).unwrap();
                assert!(projected(&database).is_some());
            }
        }
    }
}
