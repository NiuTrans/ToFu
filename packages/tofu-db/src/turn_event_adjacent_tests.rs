//! Production IR adjacency: owned projection reuse, invalidation and atomicity.
//! Uses the original ordinary event owner as the independent execution oracle.
use super::*;

fn nested_events(revision: u64, count: usize) -> TransactionIr {
    let mut command = ir(revision, count, false);
    for (index, step) in command.steps.iter_mut().enumerate() {
        if let TransactionIrStep::TurnEventRecord { request, .. } = step {
            request.projection_segments_stable = true;
            request.projection_patch.as_mut().unwrap()["operations"] = json!([
                {"op":"set", "path":["progress","ordinal"], "value":index + 1}
            ]);
        }
    }
    command
}

fn seed_adjacent(database: &mut AuthorityDatabase) -> u64 {
    let mut projection = canonical_projection();
    projection["progress"] = json!({"ordinal": 0});
    seed_projection(database, projection, true)
}

#[test]
#[ignore = "isolated process-wide materialization counters; run with --test-threads=1"]
fn adjacent_event_records_materialize_a_checkpoint_once() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision = seed_adjacent(&mut database);
    let command = |count| nested_events(revision, count);
    // Measure in-transaction adjacency alone, from the durable head.
    database.resident_projections().forget_all();
    let before = crate::fsync_stats::counters().projection_checkpoint_bytes;
    let single = prepare_transaction_ir_with_deadline(
        &mut database, &command(1), Duration::from_secs(30)).unwrap();
    let one_read = crate::fsync_stats::counters().projection_checkpoint_bytes - before;
    assert!(one_read > 0);
    drop(single); // Roll back; both observations start at the same durable revision.
    let before = crate::fsync_stats::counters().projection_checkpoint_bytes;
    let batch = prepare_transaction_ir_with_deadline(
        &mut database, &command(3), Duration::from_secs(30)).unwrap();
    let batch_reads = crate::fsync_stats::counters().projection_checkpoint_bytes - before;
    assert_eq!(batch_reads, one_read,
        "adjacent ordinary records must carry their current projection through the transaction");
    database.commit(batch.transaction.unwrap()).unwrap();
    let expected = state(&database);
    assert_eq!(expected["turn"]["projection"]["progress"]["ordinal"], 3);
    drop(database);
    assert_eq!(state(&AuthorityDatabase::open(directory.path()).unwrap()), expected);
}

#[test]
fn adjacent_events_match_original_owner_across_checkpoint_and_fast_path_boundaries() {
    for (count, terminal) in [(3, false), (65, false), (3, true)] {
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        let mut original = AuthorityDatabase::initialize(a.path()).unwrap();
        let mut candidate = AuthorityDatabase::initialize(b.path()).unwrap();
        let revision = seed_adjacent(&mut original);
        assert_eq!(seed_adjacent(&mut candidate), revision);
        let mut command = nested_events(revision, count);
        if let TransactionIrStep::TurnEventRecord { request, .. } = &mut command.steps[1] {
            request.projection_patch.as_mut().unwrap()["operations"] = json!([
                {"op":"set", "path":["phase"], "value":{"name":"working"}}
            ]);
        }
        if terminal {
            if let TransactionIrStep::TurnEventRecord { request, .. } = command.steps.last_mut().unwrap() {
                request.terminal = true;
                request.status = "completed".into();
            }
        }
        let mut transaction = original.begin_with_identity_claim_scopes(7, 11).unwrap();
        let mut expected_response = Vec::new();
        for step in &command.steps {
            if let TransactionIrStep::TurnEventRecord { request, .. } = step {
                expected_response = event_record(&original, &mut transaction, request).unwrap();
            }
        }
        original.commit(transaction).unwrap();
        assert_eq!(execute(&mut candidate, &command, false).unwrap(), expected_response);
        let expected = state(&original);
        assert_eq!(state(&candidate), expected);
        drop(candidate);
        assert_eq!(state(&AuthorityDatabase::open(b.path()).unwrap()), expected);
    }
}

#[test]
fn intervening_ir_mutation_cannot_hide_a_missing_checkpoint() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision = seed_adjacent(&mut database);
    let before = state(&database);
    let head = head(&database);
    let transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let key = projection_checkpoint_key(&transaction, CONVERSATION, TURN, &head.head_id).unwrap();
    drop(transaction);
    let mut command = nested_events(revision, 2);
    command.steps.insert(1, TransactionIrStep::EntityDelete {
        key, condition: crate::transaction_ir::EntityWriteCondition::Always,
    });
    let error = prepare_transaction_ir_with_deadline(&mut database, &command, Duration::MAX)
        .err().expect("the intervening deletion must invalidate the owned reconstruction");
    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    assert!(error.to_string().contains("checkpoint"), "{error}");
    assert_eq!(state(&database), before);
}

#[test]
fn adjacent_events_keep_revision_scope_and_whole_batch_rollback() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision = seed_adjacent(&mut database);
    let before = state(&database);
    for invalid in ["revision", "attempt", "terminal", "owner"] {
        let mut command = nested_events(revision, 3);
        if let TransactionIrStep::TurnEventRecord { request, .. } = &mut command.steps[1] {
            match invalid {
                "revision" => request.projection_patch.as_mut().unwrap()["baseRevision"] = json!(0),
                "attempt" => request.attempt_id = "different-attempt".into(),
                "terminal" => { request.terminal = true; request.status = "completed".into(); }
                "owner" => command.owner_user_id = 12,
                _ => unreachable!(),
            }
        }
        let result = prepare_transaction_ir_with_deadline(&mut database, &command, Duration::MAX);
        if invalid == "owner" {
            let prepared = result.unwrap();
            assert!(prepared.transaction.is_none());
            assert_eq!(serde_json::from_slice::<Value>(&prepared.response).unwrap()["applied"], false);
        } else {
            assert!(result.is_err(), "{invalid}");
        }
        assert_eq!(state(&database), before, "{invalid}");
        assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    }
}

#[test]
fn adjacent_preparation_retains_ordinary_commit_conflict_fences() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision = seed_adjacent(&mut database);
    let command = nested_events(revision, 3);
    let prepared = prepare_transaction_ir_with_deadline(&mut database, &command, Duration::MAX).unwrap();
    let mut competing = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    event_record(&database, &mut competing, &request(revision, 0)).unwrap();
    database.commit(competing).unwrap();
    let expected = state(&database);
    assert_eq!(database.commit(prepared.transaction.unwrap()).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    assert_eq!(state(&database), expected);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
}
