//! Executable diagnostic contract. Oversized values are seeded through the
//! retained blob codec, not the ordinary 8 MiB mutation ingress.
use super::*;
use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use crate::protocol::{NegotiatedProtocol, Request as WireRequest, PROTOCOL_VERSION};
use crate::semantic::{admit_request, AuthenticatedScope};
use crate::versioned_document::{self, PutRequest};

fn restore(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: EntityKey,
    namespace: &str,
    logical: &str,
    value: Value,
) -> Vec<u8> {
    versioned_document::put_with_blob_owner_bounded(
        db,
        tx,
        PutRequest {
            key: key.clone(),
            namespace: namespace.into(),
            logical_key: logical.into(),
            value_json: serde_json::to_vec(&value).unwrap(),
            expected_version: None,
            updated_at_ms: 100,
        },
        11,
        64 * 1024 * 1024,
    )
    .unwrap();
    db.entity_get(tx, &key).unwrap().unwrap()
}
fn header(db: &AuthorityDatabase, tx: &mut AuthorityTransaction, messages: Value, count: u64) {
    let key = EntityKey::new(
        7,
        11,
        crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
        b"conv",
    )
    .unwrap();
    restore(
        db,
        tx,
        key,
        "conversations",
        "conv",
        json!({
            "id":"conv", "user_id":11, "rev":12, "title":"Diagnostic", "settings":{},
            "created_at":100,"updated_at":100,"msg_count":count,"messages":messages,
            "execution_epoch":0, "search_text":""
        }),
    );
}
fn seed(db: &mut AuthorityDatabase, projection: Value, patched: bool) -> Value {
    let mut tx = db.begin(7, 11).unwrap();
    header(db, &mut tx, json!([]), 2);
    // The first surviving ordinal is 3, the next 19. index is rank, not ordinal.
    let mut result = Value::Null;
    for (id, ordinal, body) in [
        ("small", 3, json!({"content":"small"})),
        ("large", 19, projection),
    ] {
        let mut turn = json!({"turnId":id,"presentationId":id,"conversationId":"conv","laneId":"main",
            "ordinal":ordinal,"actor":"assistant","kind":"reply","status":"completed",
            "projection":body,"projectionRevision":1,"currentAttemptId":null,"runId":"", "settlement":{"outcome":"completed"},
            "createdAt":100,"updatedAt":100,"_executionEpoch":0});
        if patched && id == "large" {
            let patch = json!({"version":1,"baseRevision":1,"targetRevision":2,"operations":[
                {"op":"append_text","path":["content"],"value":" patched汉字"},
                {"op":"set","path":["nested"],"value":{"ok":true}}
            ]});
            let head = ProjectionHeadDescriptor {
                head_id: "a".repeat(64),
                attempt_id: "attempt".into(),
                base_revision: 1,
                patch_count: 1,
                patch_bytes: serde_json::to_vec(&patch).unwrap().len(),
                text_tail: None, frame_witness: None,
            };
            let checkpoint_key = projection_checkpoint_key(&tx, "conv", id, &head.head_id).unwrap();
            restore(
                db,
                &mut tx,
                checkpoint_key,
                PROJECTION_HEAD_DOCUMENT_IDENTITY,
                &format!("{}\u{1f}checkpoint", head.head_id),
                json!({"attemptId":"attempt","revision":1,"projection":turn["projection"]}),
            );
            store_projection_head_patch(
                db,
                &mut tx,
                "conv",
                id,
                &head,
                patch.as_object().unwrap(),
                100,
            )
            .unwrap();
            turn["_projectionHead"] = projection_head_value(&head);

            turn["status"] = json!("running");
            turn["currentAttemptId"] = json!("attempt");
            turn["projectionRevision"] = json!(2);
            turn["projection"] = json!({});
        }
        let key = turn_key(&tx, "conv", id).unwrap();
        let stored = restore(
            db,
            &mut tx,
            key,
            DOCUMENT_IDENTITY,
            &turn_logical_key("conv", id),
            turn.clone(),
        );
        let key = lane_index_key(&tx, "conv", "main", ordinal).unwrap();
        db.entity_put(&mut tx, key, stored).unwrap();
        result = turn;
    }
    let key = lane_count_key(&tx, "conv", "main").unwrap();
    db.entity_put(&mut tx, key, 2_u64.to_le_bytes().to_vec())
        .unwrap();
    db.commit(tx).unwrap();
    result
}
fn query(
    db: &mut AuthorityDatabase,
    owner: u64,
    payload: Value,
) -> Result<Value, SemanticExecutionError> {
    let request = WireRequest {
        correlation_id: [9; 16],
        deadline_unix_ms: 100000,
        owner_id: owner,
        tenant_id: Some(7),
        command_id: None,
        schema_id: STORAGE_SCHEMA_VERSION,
        operation: "conversation.diagnostic".into(),
        payload: serde_json::to_vec(&payload).unwrap(),
    };
    let admitted = admit_request(
        &request,
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        },
        AuthenticatedScope {
            owner_id: owner,
            tenant_id: Some(7),
        },
        100,
    )
    .unwrap();
    let bytes = crate::semantic_executor::execute_admitted_request(db, admitted, 100)?;
    Ok(serde_json::from_slice(&bytes).unwrap())
}
fn payload(kind: &str) -> Value {
    let mut value = json!({"conv_id":"conv","user_id":11,"kind":kind});
    if kind != "header" {
        value["revision"] = json!(12);
        value["index"] = json!(1);
    }
    if kind == "chunk" {
        value["offset"] = json!(0);
    }
    value
}
fn chunk_payload(descriptor: &Value) -> Value {
    let mut request = payload("chunk");
    request["revision"] = descriptor["revision"].clone();
    request["index"] = descriptor["index"].clone();
    request["source_token"] = descriptor["source"]["token"].clone();
    request
}

fn chunk_bytes(chunk: &Value) -> Vec<u8> {
    base64::engine::general_purpose::STANDARD
        .decode(chunk["data"].as_str().unwrap())
        .unwrap()
}

fn source_bytes(db: &mut AuthorityDatabase, descriptor: &Value) -> Vec<u8> {
    let total = descriptor["source"]["totalBytes"].as_u64().unwrap();
    let mut assembled = Vec::new();
    // Include a tiny read crossing a multibyte sequence in the later suffix;
    // consumers decode UTF8 only after concatenating every raw chunk.
    loop {
        let mut req = chunk_payload(descriptor);
        req["offset"] = json!(assembled.len());
        let chunk = query(db, 11, req).unwrap();
        let bytes = chunk_bytes(&chunk);
        assert!(bytes.len() <= MAX_CHUNK_BYTES);
        assert_eq!(chunk["sourceToken"], descriptor["source"]["token"]);
        assert_eq!(chunk["totalBytes"], total);
        assembled.extend_from_slice(&bytes);
        if chunk["nextOffset"].is_null() {
            break;
        }
        assert_eq!(chunk["nextOffset"], assembled.len());
    }
    assert_eq!(assembled.len() as u64, total);
    assembled
}

fn reconstruct(db: &mut AuthorityDatabase) -> (Value, Value) {
    let turn = query(db, 11, payload("turn")).unwrap();
    assert!(turn["turn"].get("projection").is_none());
    assert!(turn["turn"].get("_projectionHead").is_none());
    assert_eq!(turn["turn"]["ordinal"], 19);
    assert!(serde_json::to_vec(&turn).unwrap().len() < 8192);
    let total = turn["source"]["totalBytes"].as_u64().unwrap();
    let mut base: Value = serde_json::from_slice(&source_bytes(db, &turn)).unwrap();
    if turn["source"]["format"] == "turn" {
        base = base["projection"].take();
    }
    for patch in turn["patches"].as_array().unwrap() {
        base = Value::Object(
            crate::turn_projection_patch::apply_projection_patch(
                base.as_object(),
                patch["projectionPatch"].as_object().unwrap(),
            )
            .unwrap(),
        );
    }
    let mut eof = chunk_payload(&turn);
    eof["offset"] = json!(total);

    match turn.get("projectionTransform").and_then(Value::as_str) {
        None => {}
        Some("terminalize_restored") => terminalize_restored_projection(&mut base),
        Some(other) => panic!("unknown diagnostic projection transform {other}"),
    }
    let eof_result = query(db, 11, eof.clone()).unwrap();
    assert_eq!(eof_result["data"], "");
    assert!(eof_result["nextOffset"].is_null());
    eof["offset"] = json!(total + 1);
    assert_eq!(
        query(db, 11, eof).unwrap_err().code(),
        "database_invalid_payload"
    );
    (turn, base)
}

#[test]
fn diagnostic_large_inline_turn_chunks_reconstruct_on_read_replica_without_mutation() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let projection = json!({"content":format!("{}汉字\\\"", "x".repeat(9*1024*1024)),"toolRounds":[{"status":"completed"}]});
    seed(&mut db, projection.clone(), false);
    let mut replica = db.build_read_replica().unwrap();
    let header = query(&mut replica, 11, payload("header")).unwrap();
    assert_eq!(header["revision"], 12);
    assert_eq!(header["totalCount"], 2);
    assert_eq!(header["document"]["messages"], json!([]));

    assert_eq!(header["document"]["source"], "sidecar");
    assert_eq!(header["document"]["metadata"]["id"], "conv");
    assert_eq!(header["document"]["metadata"]["msg_count"], 2);
    assert_eq!(header["document"]["metadata"]["rev"], 12);
    let (turn, reconstructed) = reconstruct(&mut replica);
    assert_eq!(turn["source"]["format"], "turn");

    assert!(turn["source"]["totalBytes"].as_u64().unwrap() > 8 * 1024 * 1024);
    assert!(turn.get("projectionTransform").is_none());
    assert_eq!(reconstructed, projection);
    let tx = replica.begin(7, 11).unwrap();
    assert!(!tx.has_business_mutation());
}
#[test]
fn diagnostic_large_checkpoint_chunks_and_patch_head_reconstruct_exactly() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let projection = json!({"content":"x".repeat(9*1024*1024)});
    seed(&mut db, projection.clone(), true);
    let (turn, mut reconstructed) = reconstruct(&mut db);
    assert_eq!(turn["source"]["format"], "projection");

    assert!(turn["source"]["totalBytes"].as_u64().unwrap() > 8 * 1024 * 1024);
    assert!(turn.get("projectionTransform").is_none());
    assert_eq!(turn["patches"].as_array().unwrap().len(), 1);
    assert_eq!(
        reconstructed["content"].as_str().unwrap().len(),
        9 * 1024 * 1024 + " patched汉字".len()
    );
    reconstructed["content"] = projection["content"].clone();
    reconstructed.as_object_mut().unwrap().remove("nested");
    assert_eq!(reconstructed, projection);
    source_plan::clear();
    let mut cold = chunk_payload(&turn);
    cold["offset"] = json!(9 * 1024 * 1024);
    cold["limit"] = json!(3);
    let chunk = query(&mut db, 11, cold).unwrap();
    assert_eq!(
        base64::engine::general_purpose::STANDARD
            .decode(chunk["data"].as_str().unwrap())
            .unwrap()
            .len(),
        3
    );
}
#[test]
fn diagnostic_payload_owner_revision_and_legacy_fences() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"ok"}), false);

    let descriptor = query(&mut db, 11, payload("turn")).unwrap();
    for (field, bad) in [
        ("kind", json!("bad")),
        ("revision", json!(true)),
        ("index", json!(false)),
        ("offset", json!(-1)),
        ("offset", json!(true)),
        ("limit", json!(0)),
        ("limit", json!(65537)),
        ("limit", json!(1.5)),
        ("index", json!(2)),
        ("conv_id", json!("")),
    ] {
        let mut req = chunk_payload(&descriptor);
        req[field] = bad;
        assert_eq!(
            query(&mut db, 11, req).unwrap_err().code(),
            "database_invalid_payload",
            "field={field}"
        );
    }
    for revision in [None, Some(11)] {
        let mut req = payload("turn");
        req.as_object_mut().unwrap().remove("revision");
        if let Some(r) = revision {
            req["revision"] = json!(r);
        }
        assert_eq!(
            query(&mut db, 11, req).unwrap_err().code(),
            if revision.is_none() {
                "database_invalid_payload"
            } else {
                "database_conflict"
            }
        );
    }
    let mut req = payload("header");
    req["user_id"] = json!(12);
    assert_eq!(query(&mut db, 12, req).unwrap(), Value::Null);
    assert_eq!(
        query(&mut db, 12, payload("header")).unwrap_err().code(),
        "authority_scope_mismatch"
    );
    let mut req = payload("turn");
    req["conv_id"] = json!("absent");
    assert_eq!(query(&mut db, 11, req).unwrap(), Value::Null);
    let mut tx = db.begin(7, 11).unwrap();
    let req = Request {
        conversation_id: "conv".into(),
        kind: Kind::Chunk,
        revision: Some(12),
        index: Some(1),
        offset: 0,
        limit: 1,

        source_token: Some(descriptor["source"]["token"].as_str().unwrap().into()),
    };
    read(&db, &mut tx, &req, &mut || false).unwrap();
    assert!(!tx.has_business_mutation());
    drop(tx);
    let dir2 = tempfile::tempdir().unwrap();
    let mut legacy = AuthorityDatabase::initialize(dir2.path()).unwrap();
    let mut tx = legacy.begin(7, 11).unwrap();
    header(&legacy, &mut tx, json!([{"content":"legacy"}]), 1);
    legacy.commit(tx).unwrap();
    assert_eq!(
        query(&mut legacy, 11, payload("header"))
            .unwrap_err()
            .code(),
        "database_unsupported"
    );
}

fn replace_large(db: &mut AuthorityDatabase, mutate: impl FnOnce(&mut Value)) {
    let mut tx = db.begin(7, 11).unwrap();
    let key = turn_key(&tx, "conv", "large").unwrap();
    let mut value: Value = serde_json::from_slice(
        &versioned_document::get_value_with_version(
            db,
            &mut tx,
            &key,
            DOCUMENT_IDENTITY,
            &turn_logical_key("conv", "large"),
        )
        .unwrap()
        .unwrap()
        .0,
    )
    .unwrap();
    mutate(&mut value);
    let stored = restore(
        db,
        &mut tx,
        key,
        DOCUMENT_IDENTITY,
        &turn_logical_key("conv", "large"),
        value,
    );
    let lane_key = lane_index_key(&tx, "conv", "main", 19).unwrap();
    db.entity_put(&mut tx, lane_key, stored).unwrap();
    db.commit(tx).unwrap();
}

#[test]
fn diagnostic_projection_revision_and_head_budgets_fail_closed() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"base"}), true);
    for (field, bad) in [
        ("patchCount", json!(65)),
        ("patchBytes", json!(1_048_577)),
        ("baseRevision", json!(u64::MAX)),
    ] {
        replace_large(&mut db, |turn| turn["_projectionHead"][field] = bad);
        assert_eq!(
            query(&mut db, 11, payload("turn")).unwrap_err().code(),
            "database_integrity"
        );
        replace_large(&mut db, |turn| {
            turn["_projectionHead"]["patchCount"] = json!(1);
            turn["_projectionHead"]["patchBytes"] = json!(1);
            turn["_projectionHead"]["baseRevision"] = json!(1);
        });
    }
    replace_large(&mut db, |turn| turn["projectionRevision"] = json!(3));
    assert_eq!(
        query(&mut db, 11, payload("turn")).unwrap_err().code(),
        "database_integrity"
    );
    // Inline projections also have to be objects, not an arbitrary JSON scalar.
    replace_large(&mut db, |turn| {
        turn.as_object_mut().unwrap().remove("_projectionHead");
        turn["projection"] = Value::Null;
    });
    assert_eq!(
        query(&mut db, 11, payload("turn")).unwrap_err().code(),
        "database_integrity"
    );
}

#[test]
fn diagnostic_source_hint_rechecks_snapshot_and_turn_envelope() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"before"}), false);
    let before = query(&mut db, 11, payload("turn")).unwrap();
    let mut pinned = db.build_read_replica().unwrap();
    replace_large(&mut db, |turn| {
        turn["projection"] = json!({"content":"after-with-more-bytes"})
    });
    // Even an out-of-band restore preserving logical revision must invalidate
    // the old descriptor, not silently serve a different source on cache miss.
    assert_eq!(
        query(&mut db, 11, chunk_payload(&before))
            .unwrap_err()
            .code(),
        "database_conflict"
    );
    let descriptor = query(&mut db, 11, payload("turn")).unwrap();
    let after = query(&mut db, 11, chunk_payload(&descriptor)).unwrap();
    let raw = chunk_bytes(&after);
    assert_eq!(
        serde_json::from_slice::<Value>(&raw).unwrap()["projection"]["content"],
        "after-with-more-bytes"
    );
    assert_ne!(after["totalBytes"], before["source"]["totalBytes"]);
    let mut tx = db.begin(7, 11).unwrap();
    let key = EntityKey::new(
        7,
        11,
        crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
        b"conv",
    )
    .unwrap();
    let mut header: Value = serde_json::from_slice(
        &versioned_document::get_value_with_version(&db, &mut tx, &key, "conversations", "conv")
            .unwrap()
            .unwrap()
            .0,
    )
    .unwrap();
    header["rev"] = json!(13);
    restore(&db, &mut tx, key, "conversations", "conv", header);
    db.commit(tx).unwrap();
    assert_eq!(
        query(&mut db, 11, chunk_payload(&descriptor))
            .unwrap_err()
            .code(),
        "database_conflict"
    );
    // The immutable replica still answers the old exact snapshot, not the new
    // revision or an in-thread hint belonging to the writer.
    let old = query(&mut pinned, 11, chunk_payload(&before)).unwrap();
    let raw = base64::engine::general_purpose::STANDARD
        .decode(old["data"].as_str().unwrap())
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&raw).unwrap()["projection"]["content"],
        "before"
    );
}

fn compile_raw_payload(payload: Vec<u8>) -> Result<TransactionIr, SemanticExecutionError> {
    let wire = WireRequest {
        correlation_id: [9; 16],
        deadline_unix_ms: 100000,
        owner_id: 11,
        tenant_id: Some(7),
        command_id: None,
        schema_id: STORAGE_SCHEMA_VERSION,
        operation: "conversation.diagnostic".into(),
        payload,
    };
    let admitted = admit_request(
        &wire,
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        },
        AuthenticatedScope {
            owner_id: 11,
            tenant_id: Some(7),
        },
        100,
    )
    .unwrap();
    compile(admitted)
}

#[test]
fn diagnostic_native_wire_rejects_unknown_duplicate_and_wrong_kind_fields() {
    for kind in ["header", "turn", "chunk"] {
        let mut valid = payload(kind);
        if kind == "chunk" {
            valid["source_token"] = json!("a".repeat(64));
            // Both omitted and explicit default limits are valid wire requests.
            compile_raw_payload(serde_json::to_vec(&valid).unwrap()).unwrap();
            valid["limit"] = json!(MAX_CHUNK_BYTES);
        }
        let raw = serde_json::to_string(&valid).unwrap();
        compile_raw_payload(raw.as_bytes().to_vec())
            .unwrap()
            .validate()
            .unwrap();
        let mut unknown = valid.clone();
        unknown["unrecognized"] = json!(1);
        assert_eq!(
            compile_raw_payload(serde_json::to_vec(&unknown).unwrap())
                .unwrap_err()
                .code(),
            "database_invalid_payload",
            "unknown field in {kind}"
        );
        for field in [
            "conv_id",
            "user_id",
            "kind",
            "revision",
            "index",
            "offset",
            "limit",
            "source_token",
        ] {
            let mut null = valid.clone();
            null[field] = Value::Null;
            assert_eq!(
                compile_raw_payload(serde_json::to_vec(&null).unwrap())
                    .unwrap_err()
                    .code(),
                "database_invalid_payload",
                "null {field} in {kind}"
            );
        }
        for (field, value) in valid.as_object().unwrap() {
            // Construct raw JSON: Value would erase the duplicate before ingress.
            let duplicate = format!("{},\"{}\":{}}}", &raw[..raw.len() - 1], field, value);
            assert_eq!(
                compile_raw_payload(duplicate.into_bytes())
                    .unwrap_err()
                    .code(),
                "database_invalid_payload",
                "duplicate {field} in {kind}"
            );
        }
    }
    for (kind, field, value) in [
        ("header", "revision", json!(12)),
        ("header", "index", json!(0)),
        ("header", "offset", json!(0)),
        ("header", "limit", json!(MAX_CHUNK_BYTES)),
        ("header", "source_token", json!("a".repeat(64))),
        ("turn", "offset", json!(0)),
        ("turn", "limit", json!(MAX_CHUNK_BYTES)),
        ("turn", "source_token", json!("a".repeat(64))),
    ] {
        let mut request = payload(kind);
        request[field] = value;
        assert_eq!(
            compile_raw_payload(serde_json::to_vec(&request).unwrap())
                .unwrap_err()
                .code(),
            "database_invalid_payload",
            "wrong-kind {field} in {kind}"
        );
    }
}

#[test]
fn diagnostic_native_wire_requires_kind_specific_fields_and_preserves_owner_fence() {
    for kind in ["turn", "chunk"] {
        let mut valid = payload(kind);
        if kind == "chunk" {
            valid["source_token"] = json!("a".repeat(64));
        }
        for field in if kind == "chunk" {
            vec!["revision", "index", "offset", "source_token"]
        } else {
            vec!["revision", "index"]
        } {
            let mut missing = valid.clone();
            missing.as_object_mut().unwrap().remove(field);
            assert_eq!(
                compile_raw_payload(serde_json::to_vec(&missing).unwrap())
                    .unwrap_err()
                    .code(),
                "database_invalid_payload",
                "missing {field} in {kind}"
            );
        }
    }
    let mut foreign = payload("header");
    foreign["user_id"] = json!(12);
    assert_eq!(
        compile_raw_payload(serde_json::to_vec(&foreign).unwrap())
            .unwrap_err()
            .code(),
        "authority_scope_mismatch"
    );
    foreign["user_id"] = json!(0);
    assert_eq!(
        compile_raw_payload(serde_json::to_vec(&foreign).unwrap())
            .unwrap_err()
            .code(),
        "database_invalid_payload"
    );
}

#[test]
fn diagnostic_ir_is_query_only_and_validates_before_execution() {
    use crate::generated_storage_operations::{storage_operation, StorageOperationKind};
    let operation = storage_operation("conversation.diagnostic").unwrap();
    assert_eq!(operation.kind, StorageOperationKind::Query);
    assert!(!operation.receipt_required);
    let request = Request {
        conversation_id: "conv".into(),
        kind: Kind::Turn,
        revision: Some(12),
        index: Some(1),
        offset: 0,
        limit: MAX_CHUNK_BYTES,
        source_token: None,
    };
    let mut ir = TransactionIr {
        tenant_id: 7,
        owner_user_id: 11,
        operation: "conversation.diagnostic".into(),
        operation_kind: StorageOperationKind::Query,
        steps: vec![TransactionIrStep::ConversationDiagnostic {
            request: request.clone(),
            slot: 0,
        }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects: None,
    };
    ir.validate().unwrap();
    assert!(matches!(
        ir.steps[0],
        TransactionIrStep::ConversationDiagnostic { .. }
    ));
    for invalid in [
        Request {
            offset: 1,
            ..request.clone()
        },
        Request {
            limit: 1,
            ..request.clone()
        },
        Request {
            revision: None,
            ..request.clone()
        },
        Request {
            kind: Kind::Header,
            ..request.clone()
        },
    ] {
        ir.steps[0] = TransactionIrStep::ConversationDiagnostic {
            request: invalid,
            slot: 0,
        };
        assert!(ir.validate().is_err());
    }
    ir.steps[0] = TransactionIrStep::ConversationDiagnostic {
        request: Request {
            limit: 0,
            ..request
        },
        slot: 0,
    };
    assert!(ir.validate().is_err());
    ir.steps = vec![TransactionIrStep::EntityPut {
        key: EntityKey::new(7, 11, "test", b"write").unwrap(),
        value: vec![1],
        condition: crate::transaction_ir::EntityWriteCondition::Always,
    }];
    assert!(ir.validate().is_err());
}

#[test]
fn diagnostic_respects_zero_budget_and_interrupts_stream_scanning() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content": "x".repeat(3 * 65_536)}), false);
    let request = Request {
        conversation_id: "conv".into(),
        kind: Kind::Turn,
        revision: Some(12),
        index: Some(1),
        offset: 0,
        limit: MAX_CHUNK_BYTES,
        source_token: None,
    };
    let ir = TransactionIr {
        tenant_id: 7,
        owner_user_id: 11,
        operation: "conversation.diagnostic".into(),
        operation_kind: crate::generated_storage_operations::StorageOperationKind::Query,
        steps: vec![TransactionIrStep::ConversationDiagnostic {
            request: request.clone(),
            slot: 0,
        }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects: None,
    };
    let error = crate::transaction_ir::execute_transaction_ir_with_deadline(
        &mut db,
        &ir,
        &mut Vec::new(),
        std::time::Duration::ZERO,
    )
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    let mut tx = db.begin(7, 11).unwrap();
    let mut checks = 0;
    let error = read(&db, &mut tx, &request, &mut || {
        checks += 1;
        checks > 4
    })
    .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    assert!(query(&mut db, 11, payload("turn")).is_ok());
}

#[test]
fn diagnostic_retained_lane_without_counter_never_fakes_empty_history() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"retained"}), false);
    let mut tx = db.begin(7, 11).unwrap();
    let count_key = lane_count_key(&tx, "conv", "main").unwrap();
    db.entity_delete(&mut tx, count_key).unwrap();
    let key = EntityKey::new(
        7,
        11,
        crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
        b"conv",
    )
    .unwrap();
    let mut header: Value = serde_json::from_slice(
        &versioned_document::get_value_with_version(&db, &mut tx, &key, "conversations", "conv")
            .unwrap()
            .unwrap()
            .0,
    )
    .unwrap();
    header["msg_count"] = json!(0);
    restore(&db, &mut tx, key, "conversations", "conv", header);
    db.commit(tx).unwrap();
    assert_eq!(
        query(
            &mut db,
            11,
            json!({"conv_id":"conv","user_id":11,"kind":"header"})
        )
        .unwrap()["totalCount"],
        2
    );
    assert_eq!(
        query(&mut db, 11, payload("turn")).unwrap()["turn"]["turnId"],
        "large"
    );
}

#[test]
fn diagnostic_cached_source_cannot_alias_another_owned_conversation() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"private-source"}), false);
    let descriptor = query(&mut db, 11, payload("turn")).unwrap();
    let mut tx = db.begin(7, 11).unwrap();
    let key = turn_key(&tx, "conv", "large").unwrap();
    let stored = db.entity_get(&mut tx, &key).unwrap().unwrap();
    let header = EntityKey::new(
        7,
        11,
        crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
        b"alias",
    )
    .unwrap();
    restore(
        &db,
        &mut tx,
        header,
        "conversations",
        "alias",
        json!({"id":"alias","user_id":11,"rev":12,"msg_count":1,"messages":[]}),
    );
    let key = lane_count_key(&tx, "alias", "main").unwrap();
    db.entity_put(&mut tx, key, 1_u64.to_le_bytes().to_vec())
        .unwrap();
    let key = lane_index_key(&tx, "alias", "main", 19).unwrap();
    db.entity_put(&mut tx, key, stored).unwrap();
    db.commit(tx).unwrap();
    let mut request = chunk_payload(&descriptor);
    request["conv_id"] = json!("alias");
    request["index"] = json!(0);
    assert_eq!(
        query(&mut db, 11, request).unwrap_err().code(),
        "database_integrity"
    );
}

fn public_large(db: &AuthorityDatabase) -> Value {
    let mut tx = db.begin(7, 11).unwrap();
    let key = turn_key(&tx, "conv", "large").unwrap();
    let stored = db.entity_get(&mut tx, &key).unwrap().unwrap();
    let epoch = crate::conversation_header::execution_epoch(db, &mut tx, "conv").unwrap();
    public_turn(materialize_turn(db, &mut tx, &stored).unwrap(), epoch).unwrap()
}

fn advance_epoch(db: &mut AuthorityDatabase) {
    let mut tx = db.begin(7, 11).unwrap();
    let key = EntityKey::new(
        7,
        11,
        crate::generated_tofudb_ir::CONVERSATION_EXECUTION_EPOCH_NAMESPACE,
        b"conv",
    )
    .unwrap();
    db.entity_put(&mut tx, key, 1_u64.to_le_bytes().to_vec())
        .unwrap();
    db.commit(tx).unwrap();
}

#[test]
fn diagnostic_restored_completed_turn_marks_public_projection_transform() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let projection = json!({
        "content":"restored".repeat(1_200_000), "_streaming":true,
        "toolRounds":[{"status":"running","isExecuting":true,"results":{"a":{"_pending":true}}}],
        "thinking": {"_live":true,"data":{"_streaming":true}},
        "timingTrace":{"segments":[{"status":"running"}]},
        "assistantBlocks":[{"type":"tool_use","status":"pending","results":{}}]
    });
    seed(&mut db, projection.clone(), false);
    let mut tx = db.begin(7, 11).unwrap();
    let epoch = EntityKey::new(
        7,
        11,
        crate::generated_tofudb_ir::CONVERSATION_EXECUTION_EPOCH_NAMESPACE,
        b"conv",
    )
    .unwrap();
    db.entity_put(&mut tx, epoch, 1_u64.to_le_bytes().to_vec())
        .unwrap();
    db.commit(tx).unwrap();
    let (descriptor, actual) = reconstruct(&mut db);
    let expected = public_large(&db);
    let mut actual_turn = descriptor["turn"].clone();
    actual_turn["projection"] = actual.clone();
    assert_eq!(descriptor["projectionTransform"], "terminalize_restored");
    assert_eq!(descriptor["turn"]["status"], "completed");
    assert_eq!(actual_turn, expected);
    // Chunks remain the retained raw source. The transform is a final public
    // projection step, never a rewrite of durable or diagnostic source bytes.
    let raw: Value = serde_json::from_slice(&source_bytes(&mut db, &descriptor)).unwrap();
    assert_eq!(raw["projection"], projection);
    assert_eq!(actual["toolRounds"][0]["status"], "aborted");
    assert!(actual.get("_streaming").is_none());
}

#[test]
fn diagnostic_chunk_requires_canonical_source_token_in_compiler_and_ir() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"token-bound"}), false);
    let descriptor = query(&mut db, 11, payload("turn")).unwrap();
    let token = descriptor["source"]["token"].as_str().unwrap();
    assert_eq!(token.len(), 64);
    assert!(token
        .bytes()
        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)));
    for bad in [
        None,
        Some(Value::Null),
        Some(json!(true)),
        Some(json!(7)),
        Some(json!("")),
        Some(json!("a".repeat(63))),
        Some(json!("a".repeat(65))),
        Some(json!("A".repeat(64))),
        Some(json!("g".repeat(64))),
    ] {
        let mut request = payload("chunk");
        if let Some(bad) = bad {
            request["source_token"] = bad;
        }
        assert_eq!(
            query(&mut db, 11, request).unwrap_err().code(),
            "database_invalid_payload"
        );
    }
    for kind in ["header", "turn"] {
        let mut request = payload(kind);
        request["source_token"] = json!(token);
        assert_eq!(
            query(&mut db, 11, request).unwrap_err().code(),
            "database_invalid_payload"
        );
    }
    let mut wrong = chunk_payload(&descriptor);
    wrong["source_token"] = json!("0".repeat(64));
    assert_eq!(
        query(&mut db, 11, wrong).unwrap_err().code(),
        "database_conflict"
    );

    let wire = WireRequest {
        correlation_id: [9; 16],
        deadline_unix_ms: 100000,
        owner_id: 11,
        tenant_id: Some(7),
        command_id: None,
        schema_id: STORAGE_SCHEMA_VERSION,
        operation: "conversation.diagnostic".into(),
        payload: serde_json::to_vec(&chunk_payload(&descriptor)).unwrap(),
    };
    let admitted = admit_request(
        &wire,
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        },
        AuthenticatedScope {
            owner_id: 11,
            tenant_id: Some(7),
        },
        100,
    )
    .unwrap();
    let mut ir = compile(admitted).unwrap();
    ir.validate().unwrap();
    assert_eq!(ir.tenant_id, 7);
    assert_eq!(ir.owner_user_id, 11);
    let TransactionIrStep::ConversationDiagnostic { request, slot } = &mut ir.steps[0] else {
        panic!("compiler must emit the read-only diagnostic step")
    };
    assert_eq!(*slot, 0);
    assert_eq!(request.source_token.as_deref(), Some(token));
    // The semantic payload is snake_case, while the typed IR is camelCase.
    assert_eq!(
        serde_json::to_value(&*request).unwrap()["sourceToken"],
        token
    );
    request.source_token = None;
    assert!(ir.validate().is_err());
}

fn replace_checkpoint(db: &mut AuthorityDatabase, mutate: impl FnOnce(&mut Value)) {
    let mut tx = db.begin(7, 11).unwrap();
    let head_id = "a".repeat(64);
    let key = projection_checkpoint_key(&tx, "conv", "large", &head_id).unwrap();
    let logical = format!("{head_id}\u{1f}checkpoint");
    let mut value: Value = serde_json::from_slice(
        &versioned_document::get_value_with_version(
            db,
            &mut tx,
            &key,
            PROJECTION_HEAD_DOCUMENT_IDENTITY,
            &logical,
        )
        .unwrap()
        .unwrap()
        .0,
    )
    .unwrap();
    mutate(&mut value);
    restore(
        db,
        &mut tx,
        key,
        PROJECTION_HEAD_DOCUMENT_IDENTITY,
        &logical,
        value,
    );
    db.commit(tx).unwrap();
}

#[test]
fn diagnostic_same_length_turn_and_checkpoint_replacement_after_first_chunk_is_fenced() {
    // Cover both physical document forms as well as both diagnostic codecs.
    for patched in [false, true] {
        for blob_backed in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
            let repeats = if blob_backed { 20_000 } else { 1 };
            seed(
                &mut db,
                json!({"content":"before".repeat(repeats)}),
                patched,
            );
            let before = query(&mut db, 11, payload("turn")).unwrap();

            let mut tx = db.begin(7, 11).unwrap();
            let key = if patched {
                projection_checkpoint_key(&tx, "conv", "large", &"a".repeat(64)).unwrap()
            } else {
                lane_index_key(&tx, "conv", "main", 19).unwrap()
            };
            let stored = db.entity_get(&mut tx, &key).unwrap().unwrap();
            assert_eq!(
                versioned_document::stored_blob_reference(&stored)
                    .unwrap()
                    .is_some(),
                blob_backed
            );
            let mut first_request = chunk_payload(&before);
            first_request["limit"] = json!(13);
            let first = query(&mut db, 11, first_request).unwrap();
            let first_bytes = chunk_bytes(&first);
            assert_eq!(first_bytes.len(), 13);
            let mut next = chunk_payload(&before);
            next["offset"] = first["nextOffset"].clone();
            assert_eq!(next["offset"], 13);
            let mut pinned = db.build_read_replica().unwrap();
            if patched {
                replace_checkpoint(&mut db, |checkpoint| {
                    checkpoint["projection"]["content"] = json!("AFTER!".repeat(repeats));
                });
            } else {
                replace_large(&mut db, |turn| {
                    turn["projection"]["content"] = json!("AFTER!".repeat(repeats));
                });
            }
            // A warm plan must compare current envelopes, and a cold worker
            // must recompute the hash rather than trust the descriptor length.
            for cold in [false, true] {
                if cold {
                    source_plan::clear();
                }
                assert_eq!(
                    query(&mut db, 11, next.clone()).unwrap_err().code(),
                    "database_conflict",
                    "patched={patched}, blob_backed={blob_backed}, cold={cold}"
                );
            }
            let after = query(&mut db, 11, payload("turn")).unwrap();
            assert_eq!(after["revision"], before["revision"]);
            assert_eq!(
                after["source"]["totalBytes"],
                before["source"]["totalBytes"]
            );
            assert_eq!(after["source"]["format"], before["source"]["format"]);
            assert_ne!(after["source"]["token"], before["source"]["token"]);
            let raw: Value = serde_json::from_slice(&source_bytes(&mut db, &after)).unwrap();
            let projection = if patched { &raw } else { &raw["projection"] };
            assert_eq!(projection["content"], "AFTER!".repeat(repeats));
            assert_eq!(
                query(&mut db, 11, next.clone()).unwrap_err().code(),
                "database_conflict"
            );

            // An immutable replica still accepts the old descriptor and only
            // those old bytes can be joined to the first pre-replacement chunk.
            let old = source_bytes(&mut pinned, &before);
            assert_eq!(&old[..13], &first_bytes);
            let rest = query(&mut pinned, 11, next).unwrap();
            assert_eq!(rest["sourceToken"], before["source"]["token"]);
            let rest_bytes = chunk_bytes(&rest);
            assert_eq!(rest_bytes, old[13..13 + rest_bytes.len()]);
            let raw: Value = serde_json::from_slice(&old).unwrap();
            let projection = if patched { &raw } else { &raw["projection"] };
            assert_eq!(projection["content"], "before".repeat(repeats));
            assert_eq!(
                query(&mut pinned, 11, chunk_payload(&after))
                    .unwrap_err()
                    .code(),
                "database_conflict"
            );
        }
    }
}

#[test]
fn diagnostic_epoch_change_fences_source_even_when_raw_bytes_are_identical() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(
        &mut db,
        json!({"content":"retained", "_streaming":true}),
        false,
    );
    let before = query(&mut db, 11, payload("turn")).unwrap();
    let raw_before = source_bytes(&mut db, &before);
    advance_epoch(&mut db);
    for cold in [false, true] {
        if cold {
            source_plan::clear();
        }
        assert_eq!(
            query(&mut db, 11, chunk_payload(&before))
                .unwrap_err()
                .code(),
            "database_conflict"
        );
    }
    let after = query(&mut db, 11, payload("turn")).unwrap();
    assert_eq!(after["revision"], before["revision"]);
    assert_eq!(
        after["source"]["totalBytes"],
        before["source"]["totalBytes"]
    );
    assert_ne!(after["source"]["token"], before["source"]["token"]);
    assert_eq!(source_bytes(&mut db, &after), raw_before);
    assert_eq!(after["projectionTransform"], "terminalize_restored");
}

#[test]
fn diagnostic_sparse_ordinals_are_ranks_but_cached_lane_alias_is_corruption() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"large"}), false);
    for (rank, ordinal, id) in [(0, 3, "small"), (1, 19, "large")] {
        let mut request = payload("turn");
        request["index"] = json!(rank);
        let descriptor = query(&mut db, 11, request).unwrap();
        assert_eq!(descriptor["index"], rank);
        assert_eq!(descriptor["turn"]["ordinal"], ordinal);
        let raw: Value = serde_json::from_slice(&source_bytes(&mut db, &descriptor)).unwrap();
        assert_eq!(raw["ordinal"], ordinal);
        assert_eq!(raw["turnId"], id);
    }
    // Populate the valid ordinal-19 hint, then alias that exact envelope under
    // ordinal 3. Checking only the envelope would bypass ordinal validation.
    let descriptor = query(&mut db, 11, payload("turn")).unwrap();
    let mut tx = db.begin(7, 11).unwrap();
    let key = lane_index_key(&tx, "conv", "main", 19).unwrap();
    let stored = db.entity_get(&mut tx, &key).unwrap().unwrap();
    let alias_key = lane_index_key(&tx, "conv", "main", 3).unwrap();
    db.entity_put(&mut tx, alias_key, stored).unwrap();
    db.commit(tx).unwrap();
    for cold in [false, true] {
        if cold {
            source_plan::clear();
        }
        let mut request = chunk_payload(&descriptor);
        request["index"] = json!(0);
        assert_eq!(
            query(&mut db, 11, request).unwrap_err().code(),
            "database_integrity"
        );
    }
    let mut request = payload("turn");
    request["index"] = json!(0);
    assert_eq!(
        query(&mut db, 11, request).unwrap_err().code(),
        "database_integrity"
    );
    assert!(query(&mut db, 11, chunk_payload(&descriptor)).is_ok());
}

#[test]
fn diagnostic_lane_ordinal_must_be_present_unsigned_and_match_its_key() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"large"}), false);
    let descriptor = query(&mut db, 11, payload("turn")).unwrap();
    for bad in [
        None,
        Some(Value::Null),
        Some(json!(-1)),
        Some(json!(true)),
        Some(json!("19")),
        Some(json!(3)),
    ] {
        replace_large(&mut db, |turn| {
            turn.as_object_mut().unwrap().remove("ordinal");
            if let Some(bad) = bad {
                turn["ordinal"] = bad;
            }
        });
        assert_eq!(
            query(&mut db, 11, payload("turn")).unwrap_err().code(),
            "database_integrity"
        );
        assert_eq!(
            query(&mut db, 11, chunk_payload(&descriptor))
                .unwrap_err()
                .code(),
            "database_integrity"
        );
    }
}

#[test]
fn diagnostic_restored_checkpoint_terminalizes_after_patch_replay_like_public_turn() {
    for status in ["pending", "running"] {
        let dir = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
        seed(&mut db, json!({"content":"base", "_streaming":true}), true);
        let patch = json!({"version":1,"baseRevision":1,"targetRevision":2,"operations":[
            {"op":"append_text","path":["content"],"value":" patched"},
            {"op":"set","path":["toolRounds"],"value":[{"status":"running","runId":"patch-run"},{"status":"completed"}]},
            {"op":"set","path":["timingTrace"],"value":{"status":"running","running":true}},
            {"op":"set","path":["assistantBlocks"],"value":[{"type":"tool_use","result":{"status":"pending","_streaming":true}}]},
            {"op":"set","path":["nested"],"value":{"_streaming":true,"children":[{"attemptId":"old","isStreaming":true}]}}
        ]});
        replace_large(&mut db, |turn| {
            turn["status"] = json!(status);
            turn["runId"] = json!("old-run");
            turn["_projectionHead"]["patchBytes"] =
                json!(serde_json::to_vec(&patch).unwrap().len());
        });
        let mut tx = db.begin(7, 11).unwrap();
        let key = projection_patch_key(&tx, "conv", "large", &"a".repeat(64), 2).unwrap();
        restore(
            &db,
            &mut tx,
            key,
            PROJECTION_HEAD_DOCUMENT_IDENTITY,
            &format!("{}\u{1f}{:020}", "a".repeat(64), 2),
            json!({"attemptId":"attempt", "patch":patch}),
        );
        db.commit(tx).unwrap();
        advance_epoch(&mut db);
        let (descriptor, projection) = reconstruct(&mut db);
        let mut actual = descriptor["turn"].clone();
        actual["projection"] = projection.clone();
        assert_eq!(actual, public_large(&db));
        assert_eq!(descriptor["source"]["format"], "projection");
        assert_eq!(descriptor["projectionTransform"], "terminalize_restored");
        assert_eq!(descriptor["turn"]["status"], "interrupted");
        assert!(descriptor["turn"]["currentAttemptId"].is_null());
        assert_eq!(descriptor["turn"]["runId"], "");
        assert_eq!(projection["content"], "base patched");
        assert_eq!(projection["toolRounds"][0]["status"], "aborted");
        assert!(projection["toolRounds"][0].get("runId").is_none());
        assert_eq!(projection["toolRounds"][1]["status"], "completed");
        assert_eq!(projection["timingTrace"]["running"], false);
        assert_eq!(
            projection["assistantBlocks"][0]["result"]["status"],
            "aborted"
        );
        assert!(projection["nested"].get("_streaming").is_none());
        assert_eq!(projection["nested"]["children"][0], json!({}));
        // Patch-introduced live fields prove the transform follows replay.
        assert_eq!(descriptor["patches"][0]["projectionPatch"], patch);
        let raw: Value = serde_json::from_slice(&source_bytes(&mut db, &descriptor)).unwrap();
        assert_eq!(raw, json!({"content":"base", "_streaming":true}));
    }
}

#[test]
fn diagnostic_execution_epoch_validation_matches_public_turn() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    seed(&mut db, json!({"content":"retained"}), false);
    for epoch in [
        None,
        Some(json!(0)),
        Some(Value::Null),
        Some(json!(-1)),
        Some(json!("0")),
    ] {
        let valid = epoch.is_none() || epoch == Some(json!(0));
        replace_large(&mut db, |turn| {
            turn.as_object_mut().unwrap().remove("_executionEpoch");
            if let Some(epoch) = epoch {
                turn["_executionEpoch"] = epoch;
            }
        });
        let mut tx = db.begin(7, 11).unwrap();
        let public = crate::turn::get(&db, &mut tx, "conv", "large");
        let diagnostic = query(&mut db, 11, payload("turn"));
        if valid {
            let public: Value = serde_json::from_slice(&public.unwrap().unwrap()).unwrap();
            let mut actual = diagnostic.unwrap()["turn"].clone();
            actual["projection"] = json!({"content":"retained"});
            assert_eq!(actual, public);
        } else {
            assert_eq!(public.unwrap_err().kind(), io::ErrorKind::InvalidData);
            assert_eq!(diagnostic.unwrap_err().code(), "database_integrity");
        }
    }
}
