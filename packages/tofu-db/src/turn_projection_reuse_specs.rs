//! Failing-first specifications for live projection head reuse.
//!
//! A burst of structural events on one live Turn decodes its head once, not
//! once per event, and a structural revision behind an open text tail keeps
//! the head instead of writing a new full checkpoint. Each case checks the
//! public projection against the producer's own after every event and after a
//! cold reopen. Only the production event owner and the event-load harness
//! fixtures are used, so this file also runs against the previous engine.
use super::event_load_tests::*;
use super::*;
use crate::transaction_ir::prepare_transaction_ir_with_deadline;
use std::time::Duration;

pub(super) fn commit(database: &mut AuthorityDatabase, request: EventRecordRequest) -> Value {
    let prepared = prepare_transaction_ir_with_deadline(database, &single_event_ir(request), Duration::MAX)
        .unwrap();
    database.commit(prepared.transaction.expect("an applied event commits")).unwrap();
    serde_json::from_slice(&prepared.response.into_bytes()).unwrap()
}

/// A production-sized live head, reopened so nothing derived survives.
pub(super) fn live_turn() -> (tempfile::TempDir, AuthorityDatabase, Producer) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let projection = production_projection();
    let revision = seed(&mut database, &projection);
    drop(database);
    let database = AuthorityDatabase::open(directory.path()).unwrap();
    let producer = Producer { committed: projection, revision, sequence: 10, now_ms: 5000,
        rounds: HISTORY_ROUNDS };
    (directory, database, producer)
}

pub(super) fn assert_public_projection(database: &AuthorityDatabase, producer: &Producer, step: &str) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let turn: Value = serde_json::from_slice(&get(database, &mut tx, CONVERSATION, TURN).unwrap().unwrap())
        .unwrap();
    assert_eq!(turn["projectionRevision"], producer.revision, "{step}");
    assert!(turn["projection"] == Value::Object(producer.committed.clone()), "{step}: public projection");
}

#[test]
#[ignore = "process-wide materialization counters; run alone with --test-threads=1"]
fn a_structural_burst_decodes_its_live_head_once() {
    let (directory, mut database, mut producer) = live_turn();
    let mut decoded = Vec::new();
    for class in (0..8).flat_map(|_| [Class::ToolStart, Class::ToolResult]) {
        let before = crate::fsync_stats::counters().projection_checkpoint_bytes;
        assert_eq!(commit(&mut database, producer.request(class))["applied"], true, "{class:?}");
        decoded.push(crate::fsync_stats::counters().projection_checkpoint_bytes - before);
        assert_public_projection(&database, &producer, &format!("{class:?}"));
    }
    assert!(decoded[0] > 3_000_000, "the first event after reopen materializes the head: {decoded:?}");
    assert!(decoded[1..].iter().all(|bytes| *bytes == 0),
        "every later event of the burst reuses that head: {decoded:?}");
    drop(database);
    assert_public_projection(&AuthorityDatabase::open(directory.path()).unwrap(), &producer, "reopen");
}

#[test]
fn a_structural_revision_closes_an_open_text_tail_into_the_same_head() {
    let (directory, mut database, mut producer) = live_turn();
    let checkpoint = current_head(&database).unwrap();
    assert!(checkpoint.text_tail.is_none() && checkpoint.patch_count == 0);
    for round in 0..3 {
        for class in [Class::Text, Class::ToolProgress, Class::Text] {
            assert_eq!(commit(&mut database, producer.request(class))["applied"], true, "{round} {class:?}");
            assert!(current_head(&database).unwrap().text_tail.is_some(), "text appends open a tail");
            assert_public_projection(&database, &producer, &format!("{round} {class:?}"));
        }
        for class in [Class::Fold, Class::ToolStart, Class::Phase, Class::ToolResult] {
            assert_eq!(commit(&mut database, producer.request(class))["applied"], true, "{round} {class:?}");
            let head = current_head(&database).unwrap();
            assert_eq!(head.head_id, checkpoint.head_id, "{round} {class:?} wrote a new checkpoint");
            assert!(head.text_tail.is_none(), "{round} {class:?} left the tail open");
            assert_eq!(head.base_revision + head.patch_count as u64, producer.revision,
                "{round} {class:?}: one chain patch per revision");
            assert_public_projection(&database, &producer, &format!("{round} {class:?}"));
        }
    }
    drop(database);
    assert_public_projection(&AuthorityDatabase::open(directory.path()).unwrap(), &producer, "reopen");
}
