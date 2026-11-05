//! Executable contract for metadata-only turn.get, through semantic admission.

use super::*;
use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
use crate::semantic::{admit_request, AuthenticatedScope};
use crate::semantic_executor::{execute_admitted_request, SemanticExecutionError};

fn request(operation: &str, owner: u64, payload: Value, command_id: Option<&str>) -> Request {
    Request {
        correlation_id: [7; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: owner,
        tenant_id: Some(7),
        command_id: command_id.map(str::to_owned),
        schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        operation: operation.to_owned(),
        payload: serde_json::to_vec(&payload).unwrap(),
    }
}

fn execute_bytes(
    database: &mut AuthorityDatabase,
    request: &Request,
) -> Result<Vec<u8>, SemanticExecutionError> {
    let admitted = admit_request(
        request,
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        },
        AuthenticatedScope {
            owner_id: request.owner_id,
            tenant_id: request.tenant_id,
        },
        1_000,
    )
    .unwrap();
    execute_admitted_request(database, admitted, 1_000)
}

fn execute(
    database: &mut AuthorityDatabase,
    request: &Request,
) -> Result<Value, SemanticExecutionError> {
    execute_bytes(database, request).map(|bytes| serde_json::from_slice(&bytes).unwrap())
}

fn seed(database: &mut AuthorityDatabase) {
    execute(database, &request("turn.append_settled", 11, json!({
        "conversation_id": "metadata", "user_id": 11, "command_id": "seed",
        "actor": "assistant", "kind": "reply", "status": "completed",
        "projection": {"content": "must not be read as metadata"}, "settlement": {},
        "turn_id": "turn", "conversation_defaults": {"allowCreate": true, "title": "Metadata"}
    }), Some("metadata-seed"))).unwrap();
}

fn query(owner: u64, flag: Option<Value>) -> Request {
    let mut payload = json!({"conversation_id": "metadata", "turn_id": "turn", "user_id": owner});
    if let Some(flag) = flag {
        payload["include_projection"] = flag;
    }
    request("turn.get", owner, payload, None)
}

#[test]
fn false_omits_projection_and_private_locators() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);

    let turn = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert!(
        turn.get("projection").is_none(),
        "metadata-only turn.get materialized projection: {turn}"
    );
    assert!(turn.get("_projectionHead").is_none());
    assert!(turn.get("_executionEpoch").is_none());
    assert_eq!(turn["turnId"], "turn");
    assert_eq!(turn["status"], "completed");
}

fn without_projection(mut turn: Value) -> Value {
    let object = turn.as_object_mut().unwrap();
    object.remove("projection");
    object.remove("_projectionHead");
    turn
}

#[test]
fn default_and_true_preserve_full_bytes_and_false_preserves_public_metadata() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let default = execute_bytes(&mut database, &query(11, None)).unwrap();
    let explicit = execute_bytes(&mut database, &query(11, Some(json!(true)))).unwrap();
    assert_eq!(explicit, default);
    let full: Value = serde_json::from_slice(&default).unwrap();
    assert_eq!(
        full["projection"]["content"],
        "must not be read as metadata"
    );
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        without_projection(full)
    );
}

#[test]
fn false_retains_absent_null_and_authenticated_owner_scope() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let own = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert_eq!(own["parentTurnId"], Value::Null);
    for flag in [false, true] {
        assert_eq!(
            execute(&mut database, &query(12, Some(json!(flag)))).unwrap(),
            Value::Null
        );
        for (conversation, turn) in [("absent", "turn"), ("metadata", "absent")] {
            let missing = request(
                "turn.get",
                11,
                json!({
                    "conversation_id": conversation, "turn_id": turn, "user_id": 11,
                    "include_projection": flag,
                }),
                None,
            );
            assert_eq!(execute(&mut database, &missing).unwrap(), Value::Null);
        }
        let mut foreign_tenant = query(11, Some(json!(flag)));
        foreign_tenant.tenant_id = Some(8);
        assert_eq!(
            execute(&mut database, &foreign_tenant).unwrap(),
            Value::Null
        );
        let mismatch = request(
            "turn.get",
            12,
            json!({
                "conversation_id": "metadata", "turn_id": "turn", "user_id": 11,
                "include_projection": flag,
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &mismatch).unwrap_err(),
            SemanticExecutionError::AuthorityScopeMismatch
        );
    }
}

#[test]
fn non_boolean_projection_flags_refuse_before_absent_turn_reads() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    for flag in [
        Value::Null,
        json!(0),
        json!(1),
        json!("false"),
        json!([]),
        json!({}),
    ] {
        let error = execute(&mut database, &query(11, Some(flag.clone()))).unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload, "flag {flag}");
        assert_eq!(error.code(), "database_protocol_error");
    }
}

fn physical(database: &AuthorityDatabase) -> Map<String, Value> {
    let mut transaction = database.begin(7, 11).unwrap();
    let key = turn_key(&transaction, "metadata", "turn").unwrap();
    let stored = database
        .entity_get(&mut transaction, &key)
        .unwrap()
        .unwrap();
    let (_, value, _) = crate::versioned_document::materialize_stored_document_value_bounded(
        database,
        7,
        11,
        &stored,
        DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )
    .unwrap();
    value.as_object().unwrap().clone()
}

fn replace_in_private_transaction(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &Map<String, Value>,
    logical_key: &str,
) {
    let key = turn_key(transaction, "metadata", "turn").unwrap();
    let stored = database.entity_get(transaction, &key).unwrap().unwrap();
    let version = crate::versioned_document::stored_document_version(
        &stored,
        DOCUMENT_IDENTITY,
        "metadata\u{1f}turn",
    )
    .unwrap();
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key,
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: logical_key.to_owned(),
            value_json: serde_json::to_vec(document).unwrap(),
            expected_version: Some(version),
            updated_at_ms: 2_000,
        },
        11,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )
    .unwrap();
}

#[test]
fn malformed_owned_metadata_fails_closed_and_private_unknown_fields_never_escape() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let original = physical(&database);
    for (field, value) in [
        ("turnId", json!("another-turn")),
        ("conversationId", json!("another-conversation")),
        ("presentationId", Value::Null),
        ("laneId", json!(3)),
        ("actor", json!([])),
        ("kind", Value::Null),
        ("runId", json!(false)),
        ("status", Value::Null),
        ("parentTurnId", json!({})),
        ("currentAttemptId", json!(9)),
        ("ordinal", json!(-1)),
        ("projectionRevision", json!("1")),
        ("modelContextRevision", json!(false)),
        ("createdAt", json!(-1)),
        ("updatedAt", json!("later")),
        ("settlement", Value::Null),
        ("settlement", json!([])),
        ("_executionEpoch", json!("0")),
        ("_modelContextDisplayOffset", json!(2)),
    ] {
        let mut document = original.clone();
        document.insert(field.to_owned(), value);
        let mut transaction = database.begin(7, 11).unwrap();
        replace_in_private_transaction(
            &database,
            &mut transaction,
            &document,
            "metadata\u{1f}turn",
        );
        let error = get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap_err();
        assert_eq!(
            error.kind(),
            io::ErrorKind::InvalidData,
            "field {field}: {error}"
        );
    }
    for field in [
        "laneId",
        "ordinal",
        "status",
        "settlement",
        "projectionRevision",
        "createdAt",
        "updatedAt",
    ] {
        let mut document = original.clone();
        document.remove(field);
        let mut transaction = database.begin(7, 11).unwrap();
        replace_in_private_transaction(
            &database,
            &mut transaction,
            &document,
            "metadata\u{1f}turn",
        );
        assert_eq!(
            get_with_projection(&database, &mut transaction, "metadata", "turn", false)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData,
            "missing {field}"
        );
    }
    let mut transaction = database.begin(7, 11).unwrap();
    replace_in_private_transaction(
        &database,
        &mut transaction,
        &original,
        "metadata\u{1f}wrong",
    );
    assert_eq!(
        get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidData
    );
    drop(transaction);

    let mut document = original.clone();
    document.remove("_executionEpoch"); // Legacy documents default to epoch zero.
    document.remove("presentationId");
    document.remove("currentAttemptId");
    document.insert(
        "_projectionHead".to_owned(),
        json!({"private": "not a public locator"}),
    );
    document.insert(
        "_unknownPrivateLocator".to_owned(),
        json!({"blob": "private"}),
    );
    document.insert(
        "unknownLegacyBody".to_owned(),
        json!([{"content": "ignored"}]),
    );
    let mut expected = document.clone();
    for field in [
        "projection",
        "_projectionHead",
        "_unknownPrivateLocator",
        "unknownLegacyBody",
    ] {
        expected.remove(field);
    }
    let epoch = {
        let mut transaction = database.begin(7, 11).unwrap();
        crate::conversation_header::execution_epoch(&database, &mut transaction, "metadata")
            .unwrap()
    };
    let expected = public_turn(expected, epoch).unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    replace_in_private_transaction(&database, &mut transaction, &document, "metadata\u{1f}turn");
    let actual: Value = serde_json::from_slice(
        &get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(actual, expected);
    drop(transaction);
    assert_eq!(physical(&database), original);
}

/// Direct corrupt-inline fixture for the existing physical codec, not a writer
/// or second storage format. All uses are dropped private transactions.
fn corrupt_inline_envelope(value_json: &[u8]) -> Vec<u8> {
    let namespace = DOCUMENT_IDENTITY.as_bytes();
    let key = b"metadata\x1fturn";
    let mut stored = Vec::new();
    stored.extend_from_slice(b"TDBDOC01");
    stored.extend_from_slice(&1_u32.to_le_bytes());
    stored.extend_from_slice(&1_u64.to_le_bytes());
    stored.extend_from_slice(&1_000_u64.to_le_bytes());
    stored.extend_from_slice(&(namespace.len() as u16).to_le_bytes());
    stored.extend_from_slice(&(key.len() as u16).to_le_bytes());
    stored.extend_from_slice(&[0; 4]);
    stored.extend_from_slice(&(value_json.len() as u64).to_le_bytes());
    stored.extend_from_slice(namespace);
    stored.extend_from_slice(key);
    stored.extend_from_slice(value_json);
    stored
}

#[test]
fn malformed_legacy_projection_json_is_validated_even_when_the_body_is_skipped() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let original = physical(&database);
    let mut metadata = original.clone();
    metadata.remove("projection");
    let mut prefix = serde_json::to_vec(&metadata).unwrap();
    prefix.pop();
    prefix.extend_from_slice(b",\"projection\":");
    let mut bodies = vec![
        b"{\"content\":\"\\uD800\"}".to_vec(),
        b"{\"content\":\"\xff\"}".to_vec(),
        b"{\"number\":1e400}".to_vec(),
        b"{\"content\":\"unterminated}".to_vec(),
        b"[1,]".to_vec(),
    ];
    let mut deep = vec![b'['; 128];
    deep.extend_from_slice(b"null");
    deep.extend(std::iter::repeat_n(b']', 128));
    bodies.push(deep);
    for body in bodies {
        let mut bytes = prefix.clone();
        bytes.extend_from_slice(&body);
        bytes.push(b'}');
        assert!(serde_json::from_slice::<Value>(&bytes).is_err());
        let mut transaction = database.begin(7, 11).unwrap();
        let key = turn_key(&transaction, "metadata", "turn").unwrap();
        database
            .entity_put(&mut transaction, key, corrupt_inline_envelope(&bytes))
            .unwrap();
        let error = get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }
    for bytes in [b"null".as_slice(), b"[]".as_slice()] {
        let mut transaction = database.begin(7, 11).unwrap();
        let key = turn_key(&transaction, "metadata", "turn").unwrap();
        database
            .entity_put(&mut transaction, key, corrupt_inline_envelope(bytes))
            .unwrap();
        assert_eq!(
            get_with_projection(&database, &mut transaction, "metadata", "turn", false)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }
    // Whitespace, escaped field names and duplicate keys keep Value's final
    // occurrence semantics. Skipping a valid noncanonical projection is safe.
    let mut bytes = serde_json::to_vec(&original).unwrap();
    bytes.pop();
    bytes.extend_from_slice(br#", "status": null, "sta\u0074us": "superseded", "settlement": {"outcome":"interrupted"}, "projection": {"content":"\u6c49\n"}}"#);
    let full: Value = serde_json::from_slice(&bytes).unwrap();
    let epoch = {
        let mut transaction = database.begin(7, 11).unwrap();
        crate::conversation_header::execution_epoch(&database, &mut transaction, "metadata")
            .unwrap()
    };
    let expected =
        without_projection(public_turn(full.as_object().unwrap().clone(), epoch).unwrap());
    let mut transaction = database.begin(7, 11).unwrap();
    let key = turn_key(&transaction, "metadata", "turn").unwrap();
    database
        .entity_put(&mut transaction, key, corrupt_inline_envelope(&bytes))
        .unwrap();
    let actual: Value = serde_json::from_slice(
        &get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(actual, expected);
    assert_eq!(actual["status"], "interrupted");
    drop(transaction);
    assert_eq!(physical(&database), original);
}

fn seed_live_head(database: &mut AuthorityDatabase) -> u64 {
    let projection = json!({"content": "seed", "thinking": "", "segments": [],
        "toolRounds": [{"id": "stable", "toolContent": "x".repeat(256 * 1024)}]});
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(
        database,
        &mut transaction,
        &AppendSettledRequest {
            conversation_id: "metadata".to_owned(),
            actor: "assistant".to_owned(),
            status: "pending".to_owned(),
            projection_json: serde_json::to_vec(&projection).unwrap(),
            projection_timestamp: None,
            settlement_json: b"{}".to_vec(),
            lane_id: "main".to_owned(),
            command_id: "metadata-live-seed".to_owned(),
            kind: "reply".to_owned(),
            run_id: "old-run".to_owned(),
            turn_id: "turn".to_owned(),
            attempt_id: Some("metadata-attempt".to_owned()),
            created_at_ms: 1_000,
            committed_at_ms: 1_000,
            defaults: TurnDefaults {
                allow_create: true,
                title: "Metadata live".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1_000,
            },
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    attempt_bind(
        database,
        &mut transaction,
        "metadata-attempt",
        "metadata-task",
        "",
        1_100,
    )
    .unwrap();
    database.commit(transaction).unwrap();
    let revision = physical(database)["projectionRevision"].as_u64().unwrap();
    let revision = publish_patch(
        database,
        revision,
        json!([
            {"op": "set", "path": ["model"], "value": "live-model"}
        ]),
    );
    publish_patch(
        database,
        revision,
        json!([
            {"op": "append_text", "path": ["content"], "value": " text suffix"}
        ]),
    )
}

fn publish_patch(database: &mut AuthorityDatabase, revision: u64, operations: Value) -> u64 {
    let result = execute(
        database,
        &request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": "metadata-attempt", "task_id": "metadata-task", "user_id": 11,
                "status": "running", "event_type": "projection_updated", "event_payload": {},
                "projection_patch": {"version": 1, "baseRevision": revision,
                    "targetRevision": revision + 1, "operations": operations}
            }),
            Some(&format!("metadata-patch-{revision}")),
        ),
    )
    .unwrap();
    assert_eq!(result["applied"], true);
    result["projection_revision"].as_u64().unwrap()
}

#[test]
fn live_head_metadata_does_not_read_checkpoint_patch_or_text_sources_after_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision = seed_live_head(&mut database);
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    let document = physical(&database);
    assert!(serde_json::to_vec(&document).unwrap().len() < 4_096);
    let head = projection_head_from_document(&document).unwrap().unwrap();
    assert!(head.patch_count > 0);
    assert!(head.text_tail.as_ref().unwrap().page_count() > 0);
    let full_bytes = execute_bytes(&mut database, &query(11, None)).unwrap();
    assert_eq!(
        execute_bytes(&mut database, &query(11, Some(json!(true)))).unwrap(),
        full_bytes
    );
    let full: Value = serde_json::from_slice(&full_bytes).unwrap();
    assert_eq!(full["projection"]["content"], "seed text suffix");
    assert_eq!(full["projection"]["model"], "live-model");
    let expected = without_projection(full);
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        expected
    );
    assert_eq!(expected["projectionRevision"], revision);

    // The seam is transaction-local: remove one source of each physical class.
    // A full read must exercise every armed fault, while metadata reads succeed
    // without reading those sources. Nothing corrupt is committed or cached.
    let mut scan = database.begin(7, 11).unwrap();
    let prefix = projection_head_prefix("metadata", "turn", &head.head_id).unwrap();
    let (start, end) = projection_head_range(&scan, "metadata", "turn", &head.head_id).unwrap();
    let rows = database.entity_scan(&mut scan, &start, &end, 100).unwrap();
    let classes = BTreeSet::from([0_u8, 1, 2]);
    let mut observed = BTreeSet::new();
    for (key, _) in rows {
        let class = key.key_bytes()[prefix.len()];
        if !classes.contains(&class) || !observed.insert(class) {
            continue;
        }
        let mut fault = database.begin(7, 11).unwrap();
        database.entity_delete(&mut fault, key).unwrap();
        let actual: Value = serde_json::from_slice(
            &get_with_projection(&database, &mut fault, "metadata", "turn", false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(actual, expected);
        assert!(actual.get("projection").is_none());
        assert!(actual.get("_projectionHead").is_none());
        let error = get(&database, &mut fault, "metadata", "turn").unwrap_err();
        let fence = match class {
            0 => "Turn projection checkpoint is missing",
            1 => "Turn projection patch is missing",
            2 => "Turn text page is missing",
            _ => unreachable!(),
        };
        assert_eq!(error.to_string(), fence);
    }
    assert_eq!(observed, classes);
    drop(scan);
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        expected
    );
}

#[test]
fn restored_execution_epoch_fences_metadata_and_pinned_old_readers_independently() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed_live_head(&mut database);
    let original = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert_eq!(original["status"], "running");
    assert_eq!(original["runId"], "old-run");
    let old_epoch = physical(&database)["_executionEpoch"].as_u64().unwrap();
    let old_reader = database.build_read_replica().unwrap();
    execute(
        &mut database,
        &request(
            "conversation.delete",
            11,
            json!({"conv_id": "metadata", "user_id": 11}),
            Some("metadata-delete"),
        ),
    )
    .unwrap();
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        Value::Null
    );
    execute(
        &mut database,
        &request(
            "conversation.restore",
            11,
            json!({"conv_id": "metadata", "user_id": 11}),
            Some("metadata-restore"),
        ),
    )
    .unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    let current_epoch =
        crate::conversation_header::execution_epoch(&database, &mut transaction, "metadata")
            .unwrap();
    assert!(current_epoch > old_epoch);
    drop(transaction);
    let restored = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert_eq!(restored["status"], "interrupted");
    assert_eq!(restored["currentAttemptId"], Value::Null);
    assert_eq!(restored["runId"], "");
    assert_eq!(restored["settlement"]["cause"], "conversation_deleted");
    assert_eq!(
        restored,
        without_projection(execute(&mut database, &query(11, None)).unwrap())
    );
    assert!(restored.get("_executionEpoch").is_none());
    assert!(restored.get("_projectionHead").is_none());
    let mut old_transaction = old_reader.begin(7, 11).unwrap();
    let old: Value = serde_json::from_slice(
        &get_with_projection(&old_reader, &mut old_transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(old, original);
    drop(old_transaction);
    drop(old_reader);
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        restored
    );
}

#[test]
fn context_revision_offsets_and_nullable_pointers_use_shared_public_normalization() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let original = physical(&database);
    let mut document = original.clone();
    document.insert("projectionRevision".to_owned(), json!(7));
    document.insert("modelContextRevision".to_owned(), json!(1));
    document.insert("_modelContextDisplayOffset".to_owned(), json!(2));
    document.insert("currentAttemptId".to_owned(), Value::Null);
    document.insert("parentTurnId".to_owned(), Value::Null);
    document.insert("status".to_owned(), json!("superseded"));
    document.insert("settlement".to_owned(), json!({"outcome": "interrupted"}));
    let mut transaction = database.begin(7, 11).unwrap();
    replace_in_private_transaction(&database, &mut transaction, &document, "metadata\u{1f}turn");
    let full: Value = serde_json::from_slice(
        &get(&database, &mut transaction, "metadata", "turn")
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    let metadata: Value = serde_json::from_slice(
        &get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(metadata, without_projection(full));
    assert_eq!(metadata["modelContextRevision"], 5);
    assert_eq!(metadata["projectionRevision"], 7);
    assert_eq!(metadata["currentAttemptId"], Value::Null);
    assert_eq!(metadata["parentTurnId"], Value::Null);
    assert_eq!(metadata["status"], "interrupted");
    assert!(metadata.get("_modelContextDisplayOffset").is_none());
    drop(transaction);
    assert_eq!(physical(&database), original);
}

/// Physical read cost of one metadata get over a deterministic VFS. Every
/// handle and positioned reader is wrapped, so the count covers the entity
/// tree, WAL/history and blob chunk reads the engine actually performs.
mod read_cost {
    use super::*;
    use crate::vfs::{
        BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, FileKind,
        FilesystemCapacity, OpenRequest, Vfs, VfsFile, VfsPositionedReader,
    };
    use std::io::IoSlice;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Arc;

    #[derive(Default)]
    struct ReadCounter {
        bytes: AtomicU64,
    }

    impl ReadCounter {
        fn note(&self, bytes: usize) {
            self.bytes.fetch_add(bytes as u64, Ordering::Relaxed);
        }

        fn take(&self) -> u64 {
            self.bytes.swap(0, Ordering::Relaxed)
        }
    }

    struct CountingVfs {
        inner: Arc<DeterministicVfs>,
        counter: Arc<ReadCounter>,
    }

    struct CountingFile {
        inner: Box<dyn VfsFile>,
        counter: Arc<ReadCounter>,
    }

    struct CountingReader {
        inner: Arc<dyn VfsPositionedReader>,
        counter: Arc<ReadCounter>,
    }

    impl VfsPositionedReader for CountingReader {
        fn len(&self) -> io::Result<u64> {
            self.inner.len()
        }

        fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
            self.inner.read_exact_at(offset, bytes)?;
            self.counter.note(bytes.len());
            Ok(())
        }
    }

    impl VfsFile for CountingFile {
        fn len(&self) -> io::Result<u64> {
            self.inner.len()
        }

        fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>> {
            let bytes = self.inner.read_all(maximum_bytes)?;
            self.counter.note(bytes.len());
            Ok(bytes)
        }

        fn read_all_with_observed_length(
            &mut self,
            maximum_bytes: usize,
            observed_length: u64,
        ) -> io::Result<Vec<u8>> {
            let bytes = self
                .inner
                .read_all_with_observed_length(maximum_bytes, observed_length)?;
            self.counter.note(bytes.len());
            Ok(bytes)
        }

        fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
            self.inner.read_exact_at(offset, bytes)?;
            self.counter.note(bytes.len());
            Ok(())
        }

        fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
            self.inner.write_all_at(offset, bytes)
        }

        fn write_all_vectored_at(
            &mut self,
            offset: u64,
            buffers: &mut [IoSlice<'_>],
        ) -> io::Result<()> {
            self.inner.write_all_vectored_at(offset, buffers)
        }

        fn set_len(&mut self, length: u64) -> io::Result<()> {
            self.inner.set_len(length)
        }

        fn sync_data(&mut self) -> io::Result<()> {
            self.inner.sync_data()
        }

        fn sync_all(&mut self) -> io::Result<()> {
            self.inner.sync_all()
        }

        fn try_lock_exclusive(&mut self) -> io::Result<()> {
            self.inner.try_lock_exclusive()
        }
    }

    impl Vfs for CountingVfs {
        fn supports_buffered_wal_writes(&self) -> bool {
            self.inner.supports_buffered_wal_writes()
        }

        fn filesystem_capacity(&self, path: &Path) -> io::Result<FilesystemCapacity> {
            self.inner.filesystem_capacity(path)
        }

        fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
            Ok(Box::new(CountingFile {
                inner: self.inner.open(path, request)?,
                counter: Arc::clone(&self.counter),
            }))
        }

        fn open_positioned_reader(
            &self,
            path: &Path,
        ) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
            Ok(self.inner.open_positioned_reader(path)?.map(|inner| {
                Arc::new(CountingReader {
                    inner,
                    counter: Arc::clone(&self.counter),
                }) as Arc<dyn VfsPositionedReader>
            }))
        }

        fn create_dir(&self, path: &Path) -> io::Result<()> {
            self.inner.create_dir(path)
        }

        fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
            self.inner.rename(source, destination)
        }

        fn remove_file(&self, path: &Path) -> io::Result<()> {
            self.inner.remove_file(path)
        }

        fn metadata(&self, path: &Path) -> io::Result<FileKind> {
            self.inner.metadata(path)
        }

        fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
            self.inner.read_directory(path)
        }

        fn read_directory_bounded(
            &self,
            path: &Path,
            maximum_entries: usize,
        ) -> io::Result<BoundedDirectoryEntries> {
            self.inner.read_directory_bounded(path, maximum_entries)
        }

        fn match_directory_prefix_bounded(
            &self,
            path: &Path,
            name_prefix: &str,
            maximum_entries_scanned: usize,
            maximum_matches: usize,
        ) -> io::Result<BoundedDirectoryMatches> {
            self.inner.match_directory_prefix_bounded(
                path,
                name_prefix,
                maximum_entries_scanned,
                maximum_matches,
            )
        }

        fn sync_directory(&self, path: &Path) -> io::Result<()> {
            self.inner.sync_directory(path)
        }
    }

    /// Deterministic text the blob codec cannot shrink below ~6 bits/byte.
    fn incompressible_text(bytes: usize, seed: u64) -> String {
        const ALPHABET: &[u8; 64] =
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut state = seed | 1;
        (0..bytes)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                ALPHABET[(state >> 58) as usize] as char
            })
            .collect()
    }

    /// One settled blob-carried Turn, checkpointed so that every value is read
    /// from authenticated block files rather than recovered WAL memory. Each
    /// read uses a freshly reopened authority with cold caches.
    struct Fixture {
        counter: Arc<ReadCounter>,
        vfs: Arc<dyn Vfs>,
        summary_bytes: usize,
    }

    impl Fixture {
        fn read(&self, include_projection: Option<bool>) -> (Vec<u8>, u64) {
            let mut database =
                AuthorityDatabase::open_with_vfs(Path::new("/data"), Arc::clone(&self.vfs)).unwrap();
            self.counter.take();
            let response =
                execute_bytes(&mut database, &query(11, include_projection.map(Value::from)))
                    .unwrap();
            (response, self.counter.take())
        }
    }

    fn settled_turn_fixture(projection_bytes: usize, summarized: bool) -> Fixture {
        let inner = Arc::new(DeterministicVfs::new(None));
        inner.create_dir(Path::new("/data")).unwrap();
        inner.sync_directory(Path::new("/")).unwrap();
        let counter = Arc::new(ReadCounter::default());
        let vfs: Arc<dyn Vfs> = Arc::new(CountingVfs {
            inner,
            counter: Arc::clone(&counter),
        });
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), Arc::clone(&vfs)).unwrap();
        let projection = json!({
            "content": incompressible_text(projection_bytes, projection_bytes as u64),
            "thinking": "", "segments": [], "toolRounds": []
        });
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut transaction,
            &AppendSettledRequest {
                conversation_id: "metadata".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                projection_timestamp: None,
                settlement_json: br#"{"outcome":"completed"}"#.to_vec(),
                lane_id: "main".to_owned(),
                command_id: "metadata-cost-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: "run".to_owned(),
                turn_id: "turn".to_owned(),
                attempt_id: None,
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Metadata cost".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let summary_key =
            metadata_summary::key(&turn_key(&transaction, "metadata", "turn").unwrap()).unwrap();
        let summary_bytes =
            database.entity_get(&mut transaction, &summary_key).unwrap().unwrap().len();
        if summarized {
            drop(transaction);
        } else {
            // A row written before summaries existed: the whole-document path.
            database.entity_delete(&mut transaction, summary_key).unwrap();
            database.commit(transaction).unwrap();
        }
        database.checkpoint_for_test().unwrap();
        drop(database);
        Fixture { counter, vfs, summary_bytes }
    }

    #[test]
    fn summarized_metadata_get_reads_do_not_grow_with_the_projection() {
        let mut summarized_reads = Vec::new();
        for megabytes in [1_usize, 2, 4] {
            let projection_bytes = megabytes * 1024 * 1024;
            let summarized = settled_turn_fixture(projection_bytes, true);
            let legacy = settled_turn_fixture(projection_bytes, false);
            let (metadata, metadata_read) = summarized.read(Some(false));
            let (legacy_metadata, legacy_read) = legacy.read(Some(false));
            let (full, full_read) = summarized.read(None);
            let full: Value = serde_json::from_slice(&full).unwrap();
            let metadata: Value = serde_json::from_slice(&metadata).unwrap();
            assert_eq!(metadata, without_projection(full));
            assert_eq!(serde_json::from_slice::<Value>(&legacy_metadata).unwrap(), metadata);
            // The projection text carries 6 bits per byte, so any read of its
            // blob is at least three quarters of its logical size.
            let blob_floor = (projection_bytes * 3 / 4) as u64;
            assert!(full_read >= blob_floor, "{megabytes} MiB full read {full_read}");
            assert!(legacy_read >= blob_floor, "{megabytes} MiB legacy read {legacy_read}");
            assert!(metadata_read + blob_floor <= legacy_read);
            eprintln!(
                "metadata-read-cost projection={megabytes}MiB summarized={metadata_read} \
                 legacy={legacy_read} full={full_read} summary_row={}",
                summarized.summary_bytes
            );
            summarized_reads.push(metadata_read);
        }
        assert!(
            summarized_reads.windows(2).all(|pair| pair[0] == pair[1]),
            "summarized metadata reads changed with projection size: {summarized_reads:?}"
        );
    }
}

/// Header-bound summaries: publication, exact binding and fallback.
mod summary {
    use super::*;

    /// Large enough that every Turn document in this module is blob-carried.
    fn large_projection() -> Value {
        json!({"content": "x".repeat(32 * 1024), "thinking": "", "segments": [], "toolRounds": []})
    }

    fn seed_large(database: &mut AuthorityDatabase) {
        execute(database, &request("turn.append_settled", 11, json!({
            "conversation_id": "metadata", "user_id": 11, "command_id": "seed-large",
            "actor": "assistant", "kind": "reply", "status": "completed",
            "projection": large_projection(), "settlement": {"outcome": "completed"},
            "turn_id": "turn", "conversation_defaults": {"allowCreate": true, "title": "Metadata"}
        }), Some("metadata-seed-large"))).unwrap();
    }

    fn summary_key(transaction: &AuthorityTransaction) -> EntityKey {
        metadata_summary::key(&turn_key(transaction, "metadata", "turn").unwrap()).unwrap()
    }

    fn stored(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction) -> Vec<u8> {
        let key = turn_key(transaction, "metadata", "turn").unwrap();
        database.entity_get(transaction, &key).unwrap().unwrap()
    }

    fn summary_row(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
    ) -> Option<Vec<u8>> {
        let key = summary_key(transaction);
        database.entity_get(transaction, &key).unwrap()
    }

    fn bound(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction) -> bool {
        let key = turn_key(transaction, "metadata", "turn").unwrap();
        let stored = stored(database, transaction);
        metadata_summary::bound(database, transaction, &key, &stored, "metadata\u{1f}turn")
            .unwrap()
            .is_some()
    }

    fn metadata(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
    ) -> Result<Vec<u8>, String> {
        get_with_projection(database, transaction, "metadata", "turn", false)
            .map(Option::unwrap)
            .map_err(|error| format!("{:?}: {error}", error.kind()))
    }

    /// The same request with the summary removed is the whole-document reader.
    fn whole_document_metadata(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
    ) -> Result<Vec<u8>, String> {
        let key = summary_key(transaction);
        database.entity_delete(transaction, key).unwrap();
        metadata(database, transaction)
    }

    fn put_bytes(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        value_json: Vec<u8>,
    ) -> io::Result<(Vec<u8>, Vec<u8>)> {
        let stored = stored(database, transaction);
        let version = crate::versioned_document::stored_document_version(
            &stored,
            DOCUMENT_IDENTITY,
            "metadata\u{1f}turn",
        )
        .unwrap();
        metadata_summary::put_document(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: turn_key(transaction, "metadata", "turn").unwrap(),
                namespace: DOCUMENT_IDENTITY.to_owned(),
                logical_key: "metadata\u{1f}turn".to_owned(),
                value_json,
                expected_version: Some(version),
                updated_at_ms: 2_000,
            },
            crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
        )
    }

    #[test]
    fn blob_turn_writes_publish_a_bound_summary_and_inline_turns_publish_none() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed(&mut database);
        let mut transaction = database.begin(7, 11).unwrap();
        assert!(crate::versioned_document::stored_blob_reference(&stored(&database, &mut transaction))
            .unwrap()
            .is_none());
        assert!(summary_row(&database, &mut transaction).is_none());
        drop(transaction);

        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_large(&mut database);
        let mut transaction = database.begin(7, 11).unwrap();
        assert!(crate::versioned_document::stored_blob_reference(&stored(&database, &mut transaction))
            .unwrap()
            .is_some());
        assert!(bound(&database, &mut transaction));
        drop(transaction);
        let full = execute(&mut database, &query(11, None)).unwrap();
        assert_eq!(full["projection"]["content"], large_projection()["content"]);
        assert_eq!(
            execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
            without_projection(full)
        );
        drop(database);
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert!(bound(&database, &mut transaction));
    }

    #[test]
    fn summarized_metadata_reads_never_touch_the_projection_blob() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_large(&mut database);
        let expected = execute_bytes(&mut database, &query(11, Some(json!(false)))).unwrap();
        // Dropped private transaction: redirect the envelope to an absent blob
        // and rebind the unchanged summary to it. A full read must fail on the
        // blob while the metadata read still answers from the summary alone.
        let mut transaction = database.begin(7, 11).unwrap();
        let mut dangling = stored(&database, &mut transaction);
        let reference = dangling.len() - 64;
        dangling[reference..].fill(0xa5);
        let summary = summary_row(&database, &mut transaction).unwrap();
        let mut rebound = summary[..8].to_vec();
        rebound.extend_from_slice(&Sha256::digest(&dangling));
        rebound.extend_from_slice(&summary[40..]);
        let key = turn_key(&transaction, "metadata", "turn").unwrap();
        database.entity_put(&mut transaction, key, dangling).unwrap();
        let key = summary_key(&transaction);
        database.entity_put(&mut transaction, key, rebound).unwrap();
        assert_eq!(metadata(&database, &mut transaction), Ok(expected.clone()));
        assert!(get(&database, &mut transaction, "metadata", "turn").is_err());
        assert!(whole_document_metadata(&database, &mut transaction).is_err());
        drop(transaction);
        assert_eq!(
            execute_bytes(&mut database, &query(11, Some(json!(false)))).unwrap(),
            expected
        );
    }

    #[test]
    fn another_envelopes_or_malformed_summary_falls_back_to_the_whole_document() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_large(&mut database);
        let original = physical(&database);
        let expected = execute_bytes(&mut database, &query(11, Some(json!(false)))).unwrap();

        // A writer that bypasses publication leaves the summary of the former
        // envelope; it must never answer for the current one.
        let mut changed = original.clone();
        changed.insert("status".to_owned(), json!("failed"));
        changed.insert("settlement".to_owned(), json!({"outcome": "failed", "cause": "x"}));
        let mut transaction = database.begin(7, 11).unwrap();
        let previous = summary_row(&database, &mut transaction).unwrap();
        replace_in_private_transaction(&database, &mut transaction, &changed, "metadata\u{1f}turn");
        assert_eq!(summary_row(&database, &mut transaction), Some(previous.clone()));
        assert!(!bound(&database, &mut transaction));
        let current: Value = serde_json::from_slice(&metadata(&database, &mut transaction).unwrap())
            .unwrap();
        assert_eq!(current["status"], "failed");
        let full: Value = serde_json::from_slice(
            &get(&database, &mut transaction, "metadata", "turn").unwrap().unwrap(),
        )
        .unwrap();
        assert_eq!(current, without_projection(full));
        drop(transaction);

        let envelope_digest = previous[8..40].to_vec();
        let metadata_json = previous[40..].to_vec();
        for corrupt in [
            Vec::new(),
            b"TDBTMS0".to_vec(),
            [b"XDBTMS01".as_slice(), &envelope_digest, &metadata_json].concat(),
            [b"TDBTMS01".as_slice(), &envelope_digest[..31]].concat(),
            [b"TDBTMS01".as_slice(), &envelope_digest, b"{\"turnId\":"].concat(),
            [b"TDBTMS01".as_slice(), &envelope_digest, b"[]"].concat(),
        ] {
            // The last two keep the exact digest: an undecodable bound body is
            // treated as absent rather than trusted or reported.
            let mut transaction = database.begin(7, 11).unwrap();
            let key = summary_key(&transaction);
            database.entity_put(&mut transaction, key, corrupt).unwrap();
            assert_eq!(metadata(&database, &mut transaction), Ok(expected.clone()));
        }
        assert_eq!(physical(&database), original);
    }

    #[test]
    fn summarized_metadata_matches_the_whole_document_reader_exactly() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_large(&mut database);
        let original = physical(&database);
        let canonical = serde_json::to_vec(&original).unwrap();
        let with_suffix = |suffix: &str| {
            let mut bytes = canonical.clone();
            bytes.pop();
            bytes.extend_from_slice(suffix.as_bytes());
            bytes.push(b'}');
            bytes
        };
        let mut legacy = original.clone();
        for field in ["presentationId", "currentAttemptId", "_executionEpoch"] {
            legacy.remove(field);
        }
        legacy.insert("_unknownPrivateLocator".to_owned(), json!({"blob": "private"}));
        legacy.insert("projectionRevision".to_owned(), json!(7));
        legacy.insert("_modelContextDisplayOffset".to_owned(), json!(2));
        let documents = [
            canonical.clone(),
            // Duplicates keep Value's final occurrence; escaped key spellings,
            // whitespace and escaped values must decode identically.
            with_suffix(concat!(
                r#" , "status" : null, "sta\u0074us": "superseded","#,
                r#" "settlement": {"outcome": "interrupted", "n": 1.5e-7},"#,
                r#" "ru\u006eId" : "\u0072un\n", "turnId" : "turn""#,
            )),
            serde_json::to_vec(&legacy).unwrap(),
            // Value interprets this marker only as the first key of an object.
            with_suffix(r#","settlement":{"$serde_json::private::RawValue":"{\"outcome\":\"completed\"}"}"#),
            with_suffix(r#","settlement":{"$serde_json::private::RawValue":"[1]"}"#),
            with_suffix(r#","ordinal":-1"#),
            with_suffix(r#","conversationId":"elsewhere""#),
            with_suffix(r#","settlement":"none""#),
        ];
        for document in documents {
            let mut transaction = database.begin(7, 11).unwrap();
            put_bytes(&database, &mut transaction, document.clone()).unwrap();
            assert!(
                bound(&database, &mut transaction),
                "{}",
                String::from_utf8_lossy(&document[document.len() - 160..])
            );
            let summarized = metadata(&database, &mut transaction);
            assert_eq!(
                summarized,
                whole_document_metadata(&database, &mut transaction),
                "{}",
                String::from_utf8_lossy(&document[document.len() - 160..])
            );
        }
        let mut transaction = database.begin(7, 11).unwrap();
        assert!(put_bytes(&database, &mut transaction, with_suffix(r#","x":1e400"#)).is_err());
        assert!(bound(&database, &mut transaction));
        drop(transaction);
        assert_eq!(physical(&database), original);
    }

    #[test]
    fn a_summary_beyond_the_entity_value_bound_is_omitted_without_refusing_the_write() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_large(&mut database);
        let mut document = physical(&database);
        document.insert(
            "settlement".to_owned(),
            json!({"outcome": "failed", "error": "e".repeat(9 * 1024)}),
        );
        document.insert("status".to_owned(), json!("failed"));
        let mut transaction = database.begin(7, 11).unwrap();
        let previous = summary_row(&database, &mut transaction);
        put_bytes(&database, &mut transaction, serde_json::to_vec(&document).unwrap()).unwrap();
        assert_eq!(summary_row(&database, &mut transaction), previous);
        assert!(!bound(&database, &mut transaction));
        let current: Value =
            serde_json::from_slice(&metadata(&database, &mut transaction).unwrap()).unwrap();
        let full: Value = serde_json::from_slice(
            &get(&database, &mut transaction, "metadata", "turn").unwrap().unwrap(),
        )
        .unwrap();
        assert_eq!(current, without_projection(full));
        assert_eq!(current["settlement"]["error"].as_str().unwrap().len(), 9 * 1024);
    }

    fn owner_summary_rows(database: &AuthorityDatabase, owner: u64) -> usize {
        let mut transaction = database.begin(7, owner).unwrap();
        let (start, end) =
            EntityKey::prefix_range(7, owner, TURN_METADATA_SUMMARY_NAMESPACE, b"").unwrap();
        database.entity_scan(&mut transaction, &start, &end, 16).unwrap().len()
    }

    #[test]
    fn summaries_follow_their_turns_through_delete_trash_restore_and_purge() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_large(&mut database);
        assert_eq!(owner_summary_rows(&database, 11), 1);
        assert_eq!(owner_summary_rows(&database, 12), 0);
        assert_eq!(execute(&mut database, &query(12, Some(json!(false)))).unwrap(), Value::Null);
        execute(&mut database, &request("turn.delete", 11, json!({
            "conversation_id": "metadata", "turn_ids": ["turn"], "user_id": 11
        }), Some("metadata-turn-delete"))).unwrap();
        assert_eq!(owner_summary_rows(&database, 11), 0);
        assert_eq!(execute(&mut database, &query(11, Some(json!(false)))).unwrap(), Value::Null);

        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_large(&mut database);
        execute(&mut database, &request("conversation.delete", 11,
            json!({"conv_id": "metadata", "user_id": 11}), Some("metadata-delete"))).unwrap();
        assert_eq!(owner_summary_rows(&database, 11), 0);
        execute(&mut database, &request("conversation.restore", 11,
            json!({"conv_id": "metadata", "user_id": 11}), Some("metadata-restore"))).unwrap();
        assert_eq!(owner_summary_rows(&database, 11), 1);
        let mut transaction = database.begin(7, 11).unwrap();
        assert!(bound(&database, &mut transaction));
        drop(transaction);
        let restored = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
        assert_eq!(restored["runId"], "");
        assert_eq!(
            restored,
            without_projection(execute(&mut database, &query(11, None)).unwrap())
        );
        execute(&mut database, &request("conversation.purge", 11,
            json!({"conv_id": "metadata", "user_id": 11}), Some("metadata-purge"))).unwrap();
        assert_eq!(owner_summary_rows(&database, 11), 0);
        drop(database);
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(owner_summary_rows(&database, 11), 0);
    }
}
