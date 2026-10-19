//! Only the precise immutable IR shape gains a reusable result certificate.
use super::*;
fn eligible()->TransactionIr {
    TransactionIr {tenant_id:7,owner_user_id:11,operation:"record.get".into(),operation_kind:StorageOperationKind::Query,
        steps:vec![TransactionIrStep::VersionedDocumentGet{key:EntityKey::new(7,11,"test",b"k").unwrap(),namespace:"test".into(),logical_key:"k".into(),slot:0}],
        result:TransactionIrResult::SlotOrLiteral{slot:0,missing:b"null".to_vec()},command_effects:None}
}
#[test]
fn query_result_cache_certificate_is_ir_proven_and_null_proof_is_exact() {
    let good=eligible();let mut response=IrResponse::unverified(b"null".to_vec());
    response.certify_snapshot_stable(&good);assert!(response.snapshot_stable());assert!(response.json_proven());
    for bytes in [b" null".as_slice(),b"{\"v\":1}".as_slice(),b"broken".as_slice()] {
        let mut response=IrResponse::unverified(bytes.to_vec());response.certify_snapshot_stable(&good);
        assert!(response.snapshot_stable());assert!(!response.json_proven(),"shape alone never certifies arbitrary bytes");
    }
    for variant in 0..6 {
        let mut ir=eligible();
        match variant {
            0=>ir.steps.clear(),
            1=>ir.steps.push(ir.steps[0].clone()),
            2=>ir.result=TransactionIrResult::SlotOrLiteral{slot:1,missing:b"null".to_vec()},
            3=>ir.result=TransactionIrResult::SlotOrLiteral{slot:0,missing:b"false".to_vec()},
            4=>ir.operation_kind=StorageOperationKind::Command,
            _=>ir.operation="future.clock_selected_document".into(),
        }
        let mut response=IrResponse::unverified(b"null".to_vec());response.certify_snapshot_stable(&ir);
        assert!(!response.snapshot_stable());assert!(!response.json_proven());
    }
}

#[test]
fn query_result_cache_snapshot_requires_execution_completion_and_exact_selected_ir() {
    let mut good = eligible();
    good.operation = "turn.sync.snapshot".into();
    good.steps = vec![TransactionIrStep::TurnSyncSnapshot {
        conversation_id: "conversation".into(), turn_limit: 20,
        max_projection_bytes: 65536, include_artifact_hint: true, slot: 0,
    }];
    let mut unexecuted = IrResponse::unverified(b"null".to_vec());
    unexecuted.certify_snapshot_stable(&good);
    assert!(!unexecuted.snapshot_stable(), "IR shape alone cannot prove a complete snapshot");
    for variant in 0..7 {
        let mut ir = good.clone();
        match variant {
            0 => {},
            1 => ir.steps.push(ir.steps[0].clone()),
            2 => ir.result = TransactionIrResult::SlotOrLiteral { slot: 1, missing: b"null".to_vec() },
            3 => ir.result = TransactionIrResult::Literal(b"null".to_vec()),
            4 => ir.operation_kind = StorageOperationKind::Command,
            5 => ir.operation = "turn.sync.page".into(),
            _ => ir.result = TransactionIrResult::SlotOrLiteral { slot: 0, missing: b"false".to_vec() },
        }
        let mut completed = IrResponse::unverified(b"null".to_vec());
        completed.snapshot_read_complete = true;
        completed.certify_snapshot_stable(&ir);
        assert_eq!(completed.snapshot_stable(), variant == 0);
        assert_eq!(completed.json_proven(), variant == 0);
    }
    let mut truncated = IrResponse::unverified(b"{\"turns\":[]}".to_vec());
    truncated.certify_snapshot_stable(&good);
    assert!(!truncated.snapshot_stable());
    assert!(!truncated.json_proven());
}
