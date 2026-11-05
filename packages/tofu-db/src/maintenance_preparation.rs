//! Logical maintenance on the shared command-preparation snapshot lane.
//!
//! Mount materialization and retention scans/decoding run without the writer.
//! Each existing bounded transaction captures a fresh pinned view, then the
//! single writer validates its original OCC witnesses before publication.
//! Dependencies: AuthorityDatabase preparation/commit, artifact and Turn owners.
use super::*;
use crate::authority::AuthorityTransaction;

pub(super) fn run_transaction<T>(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    prepare: impl FnOnce(&AuthorityDatabase) -> io::Result<(Option<AuthorityTransaction>, T)>,
) -> io::Result<(T, bool)> {
    let replica = {
        let database = authority.try_lock().map_err(lock_error)?;
        let _hold = crate::authority_watch::AuthorityHoldTimer::probe("maintenance.round");
        database.build_preparation_replica()?
    };
    let (transaction, result) = prepare(&replica)?;
    // The prepared transaction retains its own pin and private blob lease.
    // No physical page or logical mutation is published during preparation.
    drop(replica);
    let committed = transaction.is_some();
    if let Some(transaction) = transaction {
        let mut database = authority.try_lock().map_err(lock_error)?;
        let _hold = crate::authority_watch::AuthorityHoldTimer::probe("maintenance.round");
        database.commit(transaction)?;
    }
    Ok((result, committed))
}

fn lock_error<T>(error: TryLockError<T>) -> io::Error {
    match error {
        TryLockError::WouldBlock => {
            io::Error::new(io::ErrorKind::WouldBlock, "maintenance authority is busy")
        }
        TryLockError::Poisoned(_) => io::Error::other("maintenance authority mutex is poisoned"),
    }
}

pub(super) fn run(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    scope: MaintenanceScope,
    now_ms: u64,
) -> RoundOutcome {
    let mut mount = None;
    let mut tool = crate::artifact::ToolPruneProgress {
        deleted: 0,
        has_more: false,
    };
    let mut prune = ScheduledPruneOutcome::default();
    let mut committed = false;
    let result: io::Result<()> = (|| {
        let ((mount_result, tool_result), changed) = run_transaction(authority, |database| {
            let mut transaction = database.begin(scope.tenant_id, scope.owner_user_id)?;
            let mount = database.consolidate_one_entity_range_mount(&mut transaction)?;
            if mount.is_some() {
                return Ok((Some(transaction), (mount, None)));
            }
            let tool = crate::artifact::tool_prune(
                database,
                &mut transaction,
                now_ms,
                TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND,
            )?;
            Ok((
                (tool.deleted > 0).then_some(transaction),
                (None, Some(tool)),
            ))
        })?;
        committed |= changed;
        mount = mount_result;
        if mount.is_some() {
            return Ok(());
        }
        if let Some(result) = tool_result {
            tool = result;
        }

        // A fresh snapshot per transaction preserves the existing ordering:
        // sync-protected attempt events remain protected until a later round.
        let ((events, attempts, has_more), changed) = run_transaction(authority, |database| {
            let mut transaction =
                database.begin_with_identity_claim_scopes(scope.tenant_id, scope.owner_user_id)?;
            let outcome = crate::turn::events_prune(
                database,
                &mut transaction,
                &crate::turn::EventPruneRequest {
                    settled_before_ms: now_ms.saturating_sub(PRUNE_RETENTION_MS),
                    max_attempts: 64,
                    max_rows: MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_ROUND,
                },
            );
            let outcome = match outcome {
                Ok(bytes) => bytes,
                Err(error) if empty_mutation(&error) => {
                    br#"{"deleted_rows":0,"deleted_attempts":0,"remaining":0}"#.to_vec()
                }
                Err(error) => return Err(error),
            };
            // Cursor-only progress and stale-index removal must also commit.
            Ok((
                transaction.has_business_mutation().then_some(transaction),
                parse_prune_outcome(&outcome),
            ))
        })?;
        committed |= changed;
        prune.events = events;
        prune.attempts = attempts;
        prune.events_has_more = has_more;

        let ((deleted, has_more), changed) = run_transaction(authority, |database| {
            let mut transaction =
                database.begin_with_identity_claim_scopes(scope.tenant_id, scope.owner_user_id)?;
            let outcome = crate::turn::sync_prune(
                database,
                &mut transaction,
                &crate::turn::SyncPruneRequest {
                    created_before_ms: now_ms.saturating_sub(PRUNE_RETENTION_MS),
                    max_rows: MAX_SYNC_PRUNE_ROWS_PER_ROUND,
                },
            );
            let outcome = match outcome {
                Ok(bytes) => bytes,
                Err(error) if empty_mutation(&error) => {
                    br#"{"deletedRows":0,"remaining":false}"#.to_vec()
                }
                Err(error) => return Err(error),
            };
            let value = serde_json::from_slice::<serde_json::Value>(&outcome).ok();
            let deleted = value
                .as_ref()
                .and_then(|value| value.get("deletedRows")?.as_u64())
                .unwrap_or(0) as usize;
            let remaining = value
                .as_ref()
                .and_then(|value| value.get("remaining")?.as_bool())
                .unwrap_or(false);
            Ok(((deleted > 0).then_some(transaction), (deleted, remaining)))
        })?;
        committed |= changed;
        prune.sync_rows = deleted;
        prune.sync_has_more = has_more;
        Ok(())
    })();
    // Earlier transactions are already durable logical commits. If a later
    // stage conflicts, keep their counters and defer only unfinished work;
    // the existing scheduler retries from the next fresh snapshot.
    let deferred = match result {
        Ok(()) if !committed => return RoundOutcome::NoWork,
        Ok(()) => false,
        Err(error) => match round_engine_error(error) {
            RoundOutcome::Conflict if committed => true,
            outcome => return outcome,
        },
    };
    let (rows, bytes, mount_completed) = mount.map_or((0, 0, false), |progress| {
        (
            progress.rows_materialized,
            progress.materialized_bytes,
            progress.mount_completed,
        )
    });
    RoundOutcome::Committed {
        rows,
        bytes,
        mount_completed,
        tool_result_artifacts_pruned: tool.deleted,
        tool_result_prune_has_more: tool.has_more,
        attempt_events_pruned: prune.events + prune.sync_rows,
        attempt_event_prune_attempts: prune.attempts,
        attempt_event_prune_has_more: prune.events_has_more || prune.sync_has_more,
        deferred,
    }
}

fn empty_mutation(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::InvalidInput
        && error.to_string().contains("no durable semantic mutation")
}
