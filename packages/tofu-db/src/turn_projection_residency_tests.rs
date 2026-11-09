//! Reused live projections publish exactly what cold materialization does.
//!
//! The control arm forgets every resident projection before each event, so
//! each event materializes the durable head (resume from the per-event
//! checkpoint chain). Responses, every public surface and the reopened state
//! must be identical, across text tails, structural revisions behind them,
//! frame-witness candidates, the 64-patch checkpoint boundary and settlement.
use super::event_load_tests::*;
use super::reuse_specs::{assert_public_projection, commit, live_turn};
use super::*;
use crate::transaction_ir::{prepare_transaction_ir_with_deadline, TransactionIrStep};
use std::time::Duration;

#[test]
fn reused_and_cold_live_heads_publish_identical_state() {
    let (candidate_directory, mut candidate, mut candidate_producer) = live_turn();
    let (control_directory, mut control, mut control_producer) = live_turn();
    let mut schedule = Vec::new();
    for _ in 0..3 {
        schedule.extend([Class::Phase, Class::Text, Class::Text, Class::Fold, Class::Text,
            Class::ToolStart, Class::ToolProgress, Class::ToolResult, Class::Phase]);
    }
    // A text-free burst crosses the 64-patch boundary into a new checkpoint.
    schedule.extend((0..34).flat_map(|_| [Class::ToolStart, Class::ToolResult]));
    schedule.extend([Class::Phase, Class::Phase, Class::Text, Class::ToolStart, Class::ToolResult]);
    for (index, class) in schedule.into_iter().enumerate() {
        control.resident_projections().forget_all();
        let expected = commit(&mut control, control_producer.request(class));
        assert_eq!(commit(&mut candidate, candidate_producer.request(class)), expected, "{index} {class:?}");
        assert_eq!(current_head(&candidate), current_head(&control), "{index} {class:?}");
    }
    assert_public_projection(&candidate, &candidate_producer, "live");
    assert!(public_state(&candidate) == public_state(&control), "live public state");
    control.resident_projections().forget_all();
    let expected = commit(&mut control, terminal_request(&mut control_producer));
    assert_eq!(commit(&mut candidate, terminal_request(&mut candidate_producer)), expected, "terminal");
    assert_eq!(candidate.resident_projections().resident_keys(), 0, "settlement retires the residency");
    assert!(public_state(&candidate) == public_state(&control), "settled public state");
    drop((candidate, control));
    assert!(public_state(&AuthorityDatabase::open(candidate_directory.path()).unwrap())
        == public_state(&AuthorityDatabase::open(control_directory.path()).unwrap()), "reopened state");
}

#[test]
fn only_a_committed_event_leaves_its_projection_resident() {
    let (directory, mut database, mut producer) = live_turn();
    commit(&mut database, producer.request(Class::ToolStart));
    assert_eq!(database.resident_projections().resident_keys(), 1, "a committed head stays resident");

    // A prepared event that never commits consumes the entry and publishes nothing.
    let mut abandoned = Producer { committed: producer.committed.clone(), ..producer };
    let prepared = prepare_transaction_ir_with_deadline(&mut database,
        &single_event_ir(abandoned.request(Class::ToolResult)), Duration::MAX).unwrap();
    drop(prepared);
    assert_eq!(database.resident_projections().resident_keys(), 0);
    commit(&mut database, producer.request(Class::ToolResult));
    assert_public_projection(&database, &producer, "after a rolled back preparation");

    // A later step that is not an event drops the publication of that commit.
    let request = producer.request(Class::ToolStart);
    let mut ir = single_event_ir(request);
    let unrelated = EntityKey::new(7, 11, "conversation", b"unrelated").unwrap();
    ir.steps.push(TransactionIrStep::EntityPut { key: unrelated, value: b"v".to_vec(),
        condition: crate::transaction_ir::EntityWriteCondition::Always });
    let prepared = prepare_transaction_ir_with_deadline(&mut database, &ir, Duration::MAX).unwrap();
    database.commit(prepared.transaction.unwrap()).unwrap();
    assert_eq!(database.resident_projections().resident_keys(), 0);
    commit(&mut database, producer.request(Class::ToolResult));
    assert_public_projection(&database, &producer, "after a mixed transaction");
    drop(database);
    assert_public_projection(&AuthorityDatabase::open(directory.path()).unwrap(), &producer, "reopen");
}
