//! Header-bound active-work summaries for restart recovery.
//! Project mutations publish the summary in their existing transaction. Legacy
//! rows fall back to the projection until the existing maintenance worker fills
//! them from a pinned replica; publication rechecks the exact source header.

use super::*;
use crate::generated_tofudb_ir::PROJECT_BRAIN_RECOVERY_NAMESPACE;
use crate::maintenance::MaintenanceScope;
use std::sync::{Arc, Mutex, TryLockError};

const MAGIC: &[u8; 7] = b"TDBPR01";
const CURSOR_KEY: &[u8] = b"backfill";
const SCAN_ROWS: usize = 1000;

fn key(transaction: &AuthorityTransaction, project: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_RECOVERY_NAMESPACE,
        &project_digest(project),
    )
}

fn cursor_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_RECOVERY_NAMESPACE,
        CURSOR_KEY,
    )
}

fn source_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project: &str,
) -> io::Result<Option<Vec<u8>>> {
    let raw = database.entity_get(transaction, &document_key(transaction, project)?)?;
    if let Some(raw) = &raw {
        versioned_document::stored_document_version(raw, LOGICAL_NAMESPACE, project)?;
    }
    Ok(raw)
}

fn encode(header: &[u8], projection: &Map<String, Value>) -> io::Result<Vec<u8>> {
    let active = !public_work_items(projection, true)?.is_empty();
    let mut summary = MAGIC.to_vec();
    summary.extend_from_slice(&Sha256::digest(header));
    summary.push(u8::from(active));
    Ok(summary)
}

fn active(summary: &[u8], header: &[u8]) -> Option<bool> {
    if summary.len() != 40 || &summary[..7] != MAGIC || summary[7..39] != Sha256::digest(header)[..]
    {
        return None;
    }
    match summary[39] {
        0 => Some(false),
        1 => Some(true),
        _ => None,
    }
}

pub(super) fn save_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project: &str,
    projection: &Map<String, Value>,
) -> io::Result<()> {
    let header = source_header(database, transaction, project)?
        .ok_or_else(|| invalid_data("Project Brain summary has no source document"))?;
    database.entity_put(
        transaction,
        key(transaction, project)?,
        encode(&header, projection)?,
    )
}

pub(super) fn can_skip(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project: &str,
) -> io::Result<bool> {
    let Some(header) = source_header(database, transaction, project)? else {
        return Ok(true);
    };
    Ok(database
        .entity_get(transaction, &key(transaction, project)?)?
        .and_then(|summary| active(&summary, &header))
        == Some(false))
}

pub(super) fn remove_summary(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project: &str,
) -> io::Result<()> {
    database.entity_delete(transaction, key(transaction, project)?)
}

struct Prepared {
    old_cursor: Option<Vec<u8>>,
    next_cursor: Vec<u8>,
    summary: Option<(String, Vec<u8>, Vec<u8>)>,
}

fn prepare(
    database: &AuthorityDatabase,
    scope: MaintenanceScope,
    stopped: &dyn Fn() -> bool,
) -> io::Result<Option<Prepared>> {
    let mut transaction = database.begin(scope.tenant_id, scope.owner_user_id)?;
    let cursor = cursor_key(&transaction)?;
    let old_cursor = database.entity_get(&mut transaction, &cursor)?;
    if old_cursor.as_deref() == Some(&[1]) {
        return Ok(None);
    }
    let (mut start, end) = updated_index_range(&transaction)?;
    if let Some(cursor) = &old_cursor {
        if cursor.first() != Some(&0) {
            return Err(invalid_data("Project recovery cursor is invalid"));
        }
        start = EntityKey::new(
            scope.tenant_id,
            scope.owner_user_id,
            PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE,
            &cursor[1..],
        )?;
    }
    let rows = database.entity_scan(&mut transaction, &start, &end, SCAN_ROWS)?;
    if rows.is_empty() && old_cursor.is_none() {
        return Ok(None);
    }
    let mut summary = None;
    let mut consumed = 0;
    for (_, raw_project) in &rows {
        if stopped() {
            return Ok(None);
        }
        consumed += 1;
        let project = std::str::from_utf8(raw_project)
            .map_err(|_| invalid_data("Project Brain updated index is malformed"))?;
        let Some(header) = source_header(database, &mut transaction, project)? else {
            continue;
        };
        let summary_key = key(&transaction, project)?;
        if database
            .entity_get(&mut transaction, &summary_key)?
            .and_then(|summary| active(&summary, &header))
            .is_some()
        {
            continue;
        }
        // At most one full historical projection per round, off the writer.
        let projection = load_projection(database, &mut transaction, project)?;
        summary = Some((
            project.to_owned(),
            header.clone(),
            encode(&header, &projection)?,
        ));
        break;
    }
    let next_cursor = if consumed == rows.len() && rows.len() < SCAN_ROWS {
        vec![1]
    } else {
        let mut next = vec![0];
        next.extend_from_slice(rows[consumed - 1].0.key_bytes());
        next.push(0);
        next
    };
    Ok(Some(Prepared {
        old_cursor,
        next_cursor,
        summary,
    }))
}

/// One bounded round on the existing worker. No full projection is read while
/// holding the foreground writer; stale preparations never advance the cursor.
pub(crate) fn backfill(
    database: &Arc<Mutex<AuthorityDatabase>>,
    scope: MaintenanceScope,
    stopped: &dyn Fn() -> bool,
) -> io::Result<bool> {
    if stopped() {
        return Ok(false);
    }
    let replica = match database.try_lock() {
        Ok(database) => database.build_read_replica()?,
        Err(TryLockError::WouldBlock) => return Ok(false),
        Err(_) => return Err(io::Error::other("project recovery authority is poisoned")),
    };
    let Some(prepared) = prepare(&replica, scope, stopped)? else {
        return Ok(false);
    };
    drop(replica);
    if stopped() {
        return Ok(false);
    }
    let mut database = match database.try_lock() {
        Ok(database) => database,
        Err(TryLockError::WouldBlock) => return Ok(false),
        Err(_) => return Err(io::Error::other("project recovery authority is poisoned")),
    };
    publish(&mut database, scope, prepared, stopped)
}

fn publish(
    database: &mut AuthorityDatabase,
    scope: MaintenanceScope,
    prepared: Prepared,
    stopped: &dyn Fn() -> bool,
) -> io::Result<bool> {
    let mut transaction = database.begin(scope.tenant_id, scope.owner_user_id)?;
    let cursor = cursor_key(&transaction)?;
    if database.entity_get(&mut transaction, &cursor)? != prepared.old_cursor {
        return Ok(false);
    }
    if let Some((project, header, summary)) = prepared.summary {
        if source_header(&database, &mut transaction, &project)?.as_ref() != Some(&header) {
            return Ok(false);
        }
        let summary_key = key(&transaction, &project)?;
        database.maintenance_entity_put(&mut transaction, summary_key, summary)?;
    }
    if stopped() {
        return Ok(false);
    }
    database.maintenance_entity_put(&mut transaction, cursor, prepared.next_cursor)?;
    database.commit(transaction)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Operation, Vfs};
    use std::path::Path;

    fn write(database: &mut AuthorityDatabase, project: &str, status: &str, legacy: bool) {
        let mut transaction = database.begin(7, 11).unwrap();
        let mut projection = empty_projection(11, project);
        projection.insert(
            "workItems".into(),
            json!([{"id":"work", "taskId":"task", "status":status}]),
        );
        projection.insert(
            "narratives".into(),
            json!([{"text":"historical payload ".repeat(32768)}]),
        );
        save_projection(database, &mut transaction, project, &projection, 2000).unwrap();
        if legacy {
            remove_summary(database, &mut transaction, project).unwrap();
        }
        database.commit(transaction).unwrap();
    }

    fn snapshot(database: &AuthorityDatabase) -> Value {
        let mut transaction = database.begin(7, 11).unwrap();
        serde_json::from_slice(&recovery_snapshot(database, &mut transaction, None).unwrap())
            .unwrap()
    }

    #[test]
    fn matching_negative_summary_avoids_historical_blob_reads_after_reopen() {
        let seed = Arc::new(DeterministicVfs::new(None));
        seed.create_dir(Path::new("/data")).unwrap();
        seed.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), seed.clone()).unwrap();
        for i in 0..24 {
            write(&mut database, &format!("/project-{i}"), "completed", false);
        }
        database.checkpoint_for_test().unwrap();
        drop(database);
        let mut measurements = Vec::new();
        for legacy in [true, false] {
            let vfs = Arc::new(seed.fork_durable().unwrap());
            if legacy {
                let mut database =
                    AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
                let mut transaction = database.begin(7, 11).unwrap();
                for i in 0..24 {
                    remove_summary(&database, &mut transaction, &format!("/project-{i}")).unwrap();
                }
                database.commit(transaction).unwrap();
                database.checkpoint_for_test().unwrap();
            }
            let database =
                AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(None).unwrap();
            let started = std::time::Instant::now();
            assert_eq!(snapshot(&database), json!({"projects":[], "capped":false}));
            let elapsed = started.elapsed().as_micros();
            let trace = vfs.trace().unwrap();
            let opens = trace.iter().filter(|op| **op == Operation::Open).count();
            let reads = trace.iter().filter(|op| **op == Operation::Read).count();
            eprintln!("project_recovery legacy={legacy} projects=24 history_bytes={} elapsed_us={elapsed} opens={opens} reads={reads}",
                24 * 32768 * 19);
            measurements.push(reads);
        }
        // Assert the eliminated I/O, not a timing threshold sensitive to CI load.
        assert!(
            measurements[1] < measurements[0],
            "summary did not avoid historical reads: {measurements:?}"
        );
    }

    #[test]
    fn stale_corrupt_and_foreign_summaries_never_hide_active_work() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        write(&mut database, "/project", "completed", false);
        let mut read = database.begin(7, 11).unwrap();
        let summary_key = key(&read, "/project").unwrap();
        let old = database
            .entity_get(&mut read, &summary_key)
            .unwrap()
            .unwrap();
        drop(read);
        write(&mut database, "/project", "active", false);
        for bytes in [old, b"corrupt negative summary".to_vec()] {
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(&mut transaction, summary_key.clone(), bytes)
                .unwrap();
            database.commit(transaction).unwrap();
            assert_eq!(
                snapshot(&database)["projects"][0]["workItems"][0]["taskId"],
                "task"
            );
        }
        let mut foreign = database.begin(7, 12).unwrap();
        assert!(can_skip(&database, &mut foreign, "/project").unwrap());
        assert_eq!(
            serde_json::from_slice::<Value>(
                &recovery_snapshot(&database, &mut foreign, None).unwrap()
            )
            .unwrap(),
            json!({"projects":[], "capped":false})
        );
    }

    #[test]
    fn backfill_is_source_fenced_cancellable_and_outbox_neutral() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        write(&mut database, "/project", "completed", true);
        let scope = MaintenanceScope::new(7, 11).unwrap();
        let replica = database.build_read_replica().unwrap();
        let prepared = prepare(&replica, scope, &|| false).unwrap().unwrap();
        // A writer creates active work after preparation: the old false bit
        // cannot overwrite its summary or advance the legacy cursor.
        write(&mut database, "/project", "active", false);
        assert!(!publish(&mut database, scope, prepared, &|| false).unwrap());
        assert_eq!(snapshot(&database)["projects"].as_array().unwrap().len(), 1);
        drop(replica);
        let mut transaction = database.begin(7, 11).unwrap();
        remove_summary(&database, &mut transaction, "/project").unwrap();
        database.commit(transaction).unwrap();
        database
            .configure_logical_outbox(&[42; 32], 1 << 20, 1 << 17)
            .unwrap();
        let database = Arc::new(Mutex::new(database));
        assert!(!backfill(&database, scope, &|| true).unwrap());
        assert!(backfill(&database, scope, &|| false).unwrap());
        assert!(!backfill(&database, scope, &|| false).unwrap());
        let guard = database.lock().unwrap();
        assert_eq!(guard.logical_outbox_status(7, 11).unwrap().last_sequence, 0);
        assert_eq!(snapshot(&guard)["projects"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn slow_legacy_hydration_does_not_hold_writer() {
        let vfs = Arc::new(crate::test_stop_vfs::StopVfs::new());
        vfs.create_dir(Path::new("/data")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        for i in 0..2 {
            write(&mut database, &format!("/project-{i}"), "completed", true);
        }
        database.checkpoint_for_test().unwrap();
        drop(database);
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap(),
        ));
        database
            .lock()
            .unwrap()
            .configure_hot_data_caches(0, 0)
            .unwrap();
        let (entered, release) = vfs.block_next_read();
        let source = Arc::clone(&database);
        let worker = std::thread::spawn(move || {
            backfill(&source, MaintenanceScope::new(7, 11).unwrap(), &|| false)
        });
        entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        {
            let writer = database
                .try_lock()
                .expect("legacy hydration held the foreground writer");
            assert_ne!(writer.authority_uuid(), uuid::Uuid::nil());
        }
        release.send(()).unwrap();
        assert!(worker.join().unwrap().unwrap());
        assert_eq!(
            snapshot(&database.lock().unwrap())["projects"]
                .as_array()
                .unwrap()
                .len(),
            0
        );
    }

    #[test]
    fn backfill_faults_publish_summary_and_cursor_atomically_without_losing_work() {
        use crate::vfs::{FaultAction, FaultRule};
        let seed = Arc::new(DeterministicVfs::new(None));
        seed.create_dir(Path::new("/data")).unwrap();
        seed.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), seed.clone()).unwrap();
        write(&mut database, "/project", "active", true);
        database.checkpoint_for_test().unwrap();
        drop(database);
        let scope = MaintenanceScope::new(7, 11).unwrap();
        let setup = || {
            let vfs = Arc::new(seed.fork_durable().unwrap());
            let database =
                AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let prepared = prepare(&database, scope, &|| false).unwrap().unwrap();
            vfs.arm_fault(None).unwrap();
            (vfs, database, prepared)
        };
        let (probe, mut database, prepared) = setup();
        assert!(publish(&mut database, scope, prepared, &|| false).unwrap());
        let trace = probe.trace().unwrap();
        for (index, operation) in trace.iter().enumerate() {
            let mut faults = vec![FaultAction::ErrorBefore(io::ErrorKind::StorageFull)];
            if *operation == Operation::Write {
                faults.push(FaultAction::ShortWrite(1));
            }
            if matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                faults.push(FaultAction::DropSync);
            }
            for action in faults {
                let (vfs, mut database, prepared) = setup();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let _ = publish(&mut database, scope, prepared, &|| false);
                // Capture power-loss state before destructor checkpointing.
                let crash = Arc::new(vfs.fork_durable().unwrap());
                let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), crash).unwrap();
                assert_eq!(snapshot(&reopened)["projects"].as_array().unwrap().len(), 1);
                let mut read = reopened.begin(7, 11).unwrap();
                let header = source_header(&reopened, &mut read, "/project")
                    .unwrap()
                    .unwrap();
                let summary_key = key(&read, "/project").unwrap();
                let summary = reopened.entity_get(&mut read, &summary_key).unwrap();
                let cursor = cursor_key(&read).unwrap();
                let cursor = reopened.entity_get(&mut read, &cursor).unwrap();
                assert_eq!(
                    cursor.is_some(),
                    summary.as_ref().and_then(|s| active(s, &header)) == Some(true),
                    "fault {index} {action:?}"
                );
            }
        }
    }
}
