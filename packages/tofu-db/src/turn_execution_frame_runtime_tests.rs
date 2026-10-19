//! Production single-event witness, public semantics and original checkpoint
//! boundaries. The unframed arm is the pre-existing execution owner, not a
//! separately implemented reducer. Every event remains its own transaction.
use super::*;

fn canonical_projection() -> Value {
    json!({"content":"before", "thinking":"", "largeStableEvidence":"s".repeat(256*1024),
        "toolRounds":[],"segments":[{"type":"text","blockId":"text:terminal",
            "text":"before","deliverable":true,"terminal":true}]})
}

fn head(database: &AuthorityDatabase) -> ProjectionHeadDescriptor {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    load_event_record_state(database, &mut tx, &request(1, 0), false)
        .unwrap()
        .unwrap()
        .1
        .projection_head
        .unwrap()
}

fn commit_step(
    database: &mut AuthorityDatabase,
    request: &EventRecordRequest,
    runtime: bool,
) -> io::Result<Vec<u8>> {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11)?;
    let response = event_record_mode(database, &mut tx, request, runtime)?;
    database.commit(tx)?;
    Ok(response)
}

#[test]
fn execution_frame_runtime_matches_original_before_at_and_after_checkpoint_threshold() {
    let a = tempfile::tempdir().unwrap();
    let b = tempfile::tempdir().unwrap();
    let mut control = AuthorityDatabase::initialize(a.path()).unwrap();
    let mut runtime = AuthorityDatabase::initialize(b.path()).unwrap();
    let revision = seed_projection(&mut control, canonical_projection(), false);
    assert_eq!(
        seed_projection(&mut runtime, canonical_projection(), true),
        revision
    );
    assert!(head(&runtime).frame_witness.is_none());
    assert!(head(&control).frame_witness.is_none());
    for index in 0..MAX_TURN_PROJECTION_HEAD_PATCHES + 2 {
        let mut command = request(revision + index as u64, index);
        command.projection_segments_stable = true;
        // Actual producer shapes: a phase with no projection change and a
        // model-fallback phase carrying one new top-level model field.
        command.projection_patch.as_mut().unwrap()["operations"] = if index % 2 == 0 {
            json!([])
        } else {
            json!([{"op":"set","path":["fallbackModel"],"value":format!("model-{index}")}])
        };
        assert_eq!(
            commit_step(&mut runtime, &command, true).unwrap(),
            commit_step(&mut control, &command, false).unwrap()
        );
        let left = head(&runtime);
        let right = head(&control);
        assert!(left.frame_witness.is_some());
        assert_eq!(left.head_id, right.head_id);
        assert_eq!(left.base_revision, right.base_revision);
        assert_eq!(left.patch_count, right.patch_count);
        assert_eq!(left.patch_bytes, right.patch_bytes);
        if [
            0,
            7,
            MAX_TURN_PROJECTION_HEAD_PATCHES - 2,
            MAX_TURN_PROJECTION_HEAD_PATCHES - 1,
            MAX_TURN_PROJECTION_HEAD_PATCHES,
            MAX_TURN_PROJECTION_HEAD_PATCHES + 1,
        ]
        .contains(&index)
        {
            assert_eq!(state(&runtime), state(&control), "boundary {index}");
        }
    }
    let expected = state(&control);
    drop(runtime);
    assert_eq!(state(&AuthorityDatabase::open(b.path()).unwrap()), expected);
}

#[test]
fn execution_frame_runtime_unsupported_and_invalid_patches_keep_original_outcomes() {
    let operations = [
        json!([{"op":"remove","path":["phase"]},{"op":"set","path":["phase"],"value":"prefix"},{"op":"append_text","path":["phase"],"value":"\n汉😀"}]),
        json!([{"op":"set","path":["phase"],"value":42},{"op":"set","path":["phase"],"value":"text"},{"op":"append_text","path":["phase"],"value":"suffix"}]),
        json!([{"op":"set","path":["phase"],"value":42},{"op":"append_text","path":["phase"],"value":"invalid"}]),
        json!([{"op":"set","path":["phase"],"value":{"escaped":"汉😀\n\\\""}}]),
        json!([{"op":"remove","path":["missing"]}]),
        json!([{"op":"append_text","path":["content"],"value":"suffix"},
            {"op":"set","path":["phase"],"value":null}]),
        json!([{"op":"append_text","path":["unknown"],"value":"bad"}]),
        json!([{"op":"append_text","path":["segments"],"value":"bad"}]),
        json!([{"op":"set","path":["segments",0,"text"],"value":"nested"}]),
        json!([{"op":"set","path":["toolRounds"],"value":[{"guidanceId":"g",
            "guidanceAnswerCommandId":"receipt","guidanceResponse":"answer","status":"running"}]}]),
        json!([{"op":"set","path":["phase"],"value":{"$serde_json::private::RawValue":"{}"}}]),
    ];
    for operations in operations {
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        let mut control = AuthorityDatabase::initialize(a.path()).unwrap();
        let mut runtime = AuthorityDatabase::initialize(b.path()).unwrap();
        let revision = seed_projection(&mut control, canonical_projection(), false);
        seed_projection(&mut runtime, canonical_projection(), true);
        let mut warm = request(revision, 0);
        warm.projection_segments_stable = true;
        warm.projection_patch.as_mut().unwrap()["operations"] = json!([]);
        commit_step(&mut runtime, &warm, true).unwrap();
        commit_step(&mut control, &warm, false).unwrap();
        assert!(head(&runtime).frame_witness.is_some());
        let mut command = request(revision + 1, 1);
        command.projection_segments_stable = true;
        command.projection_patch.as_mut().unwrap()["operations"] = operations;
        let left = commit_step(&mut runtime, &command, true);
        let right = commit_step(&mut control, &command, false);
        match (left, right) {
            (Ok(left), Ok(right)) => assert_eq!(left, right),
            (Err(left), Err(right)) => {
                assert_eq!(left.kind(), right.kind());
                assert_eq!(left.to_string(), right.to_string());
            }
            other => panic!("runtime/control acceptance differs: {other:?}"),
        }
        // Reserved serde RawValue objects are accepted by the legacy writer
        // but rejected during head readback; preserve that rejection as well.
        let read = |database: &AuthorityDatabase| {
            let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            get(database, &mut tx, CONVERSATION, TURN)
        };
        match (read(&runtime), read(&control)) {
            (Ok(left), Ok(right)) => {
                assert_eq!(left, right);
                assert_eq!(state(&runtime), state(&control));
            }
            (Err(left), Err(right)) => {
                assert_eq!(left.kind(), right.kind());
                assert_eq!(left.to_string(), right.to_string());
            }
            other => panic!("runtime/control readback differs: {other:?}"),
        }
    }
}

#[test]
fn execution_frame_runtime_text_tail_discards_metadata_and_returns_to_same_owner() {
    let a = tempfile::tempdir().unwrap();
    let mut runtime = AuthorityDatabase::initialize(a.path()).unwrap();
    let revision = seed_projection(&mut runtime, canonical_projection(), true);
    let mut command = request(revision, 0);
    command.projection_patch.as_mut().unwrap()["operations"]
        .as_array_mut()
        .unwrap()
        .truncate(1);
    commit_step(&mut runtime, &command, true).unwrap();
    assert!(head(&runtime).text_tail.is_some());
    assert!(head(&runtime).frame_witness.is_none());
    let expected = state(&runtime);
    drop(runtime);
    assert_eq!(state(&AuthorityDatabase::open(a.path()).unwrap()), expected);
}

fn producer_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../tests/fixtures/execution_frame_producer.json"
    ))
    .unwrap()
}

fn producer_projection(prefix: &str) -> Value {
    let mut projection = producer_fixture()["projection"].clone();
    projection["content"] = json!(prefix);
    projection["segments"][0]["text"] = json!(prefix);
    projection
}

fn semantic_request(call: &Value) -> crate::protocol::Request {
    crate::protocol::Request {
        correlation_id: [3; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: 11,
        tenant_id: Some(7),
        command_id: Some(call["command_id"].as_str().unwrap().into()),
        schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        operation: call["operation"].as_str().unwrap().into(),
        payload: serde_json::to_vec(&call["payload"]).unwrap(),
    }
}

fn semantic_execute(
    database: &mut AuthorityDatabase,
    command: &crate::protocol::Request,
) -> Result<Vec<u8>, crate::semantic_executor::SemanticExecutionError> {
    let admitted = crate::semantic::admit_request(
        command,
        crate::protocol::NegotiatedProtocol {
            protocol_version: crate::protocol::PROTOCOL_VERSION,
            schema_id: command.schema_id,
        },
        crate::semantic::AuthenticatedScope {
            owner_id: command.owner_id,
            tenant_id: command.tenant_id,
        },
        1000,
    )
    .unwrap();
    crate::semantic_executor::execute_admitted_request(database, admitted, 1000)
}

#[test]
fn execution_frame_runtime_real_producer_compiler_retry_outbox_and_reexecution() {
    let a = tempfile::tempdir().unwrap();
    let b = tempfile::tempdir().unwrap();
    let c = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(a.path()).unwrap();
    let mut replay = AuthorityDatabase::initialize(b.path()).unwrap();
    let mut control = AuthorityDatabase::initialize(c.path()).unwrap();
    for db in [&mut database, &mut replay, &mut control] {
        assert_eq!(
            seed_projection(db, producer_projection(&"s".repeat(256 * 1024)), false),
            3
        );
    }
    database
        .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
        .unwrap();
    replay
        .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
        .unwrap();
    let fixture = producer_fixture();
    for call in fixture["calls"].as_array().unwrap() {
        let command = semantic_request(call);
        let admitted = crate::semantic::admit_request(
            &command,
            crate::protocol::NegotiatedProtocol {
                protocol_version: crate::protocol::PROTOCOL_VERSION,
                schema_id: command.schema_id,
            },
            crate::semantic::AuthenticatedScope {
                owner_id: 11,
                tenant_id: Some(7),
            },
            1000,
        )
        .unwrap();
        let compiled = crate::semantic_executor::compile_parallel_transaction(admitted, 1000)
            .unwrap()
            .unwrap();
        let [TransactionIrStep::TurnEventRecord { request, .. }] = compiled.steps.as_slice() else {
            panic!("producer must use real single-event owner")
        };
        let expected = commit_step(&mut control, request, false).unwrap();
        let response = semantic_execute(&mut database, &command).unwrap();
        assert_eq!(response, expected);
        let wal = database.wal_len().unwrap();
        // This catalog operation deliberately has no command receipt. Keep
        // its typed stale-revision reconciliation contract on a lost ACK.
        assert!(matches!(
            semantic_execute(&mut database, &command),
            Err(crate::semantic_executor::SemanticExecutionError::TurnProjectionStale)
        ));
        assert!(
            matches!(commit_step(&mut control,request,false),Err(error) if error.kind()==io::ErrorKind::WouldBlock)
        );
        assert_eq!(
            database.wal_len().unwrap(),
            wal,
            "lost-ACK retry must not append"
        );
        assert!(head(&database).frame_witness.is_some());
        assert_eq!(state(&database), state(&control));
    }
    let expected = state(&database);
    let pending = database.logical_outbox_pending(7, 11, 10).unwrap();
    assert_eq!(pending.len(), 2);
    let cipher = crate::logical_outbox::LogicalOutboxCipher::new(&[72; 32]);
    for (record, call) in pending.iter().zip(fixture["calls"].as_array().unwrap()) {
        let captured: Value =
            serde_json::from_slice(&cipher.open(&record.record).unwrap()).unwrap();
        assert_eq!(
            captured,
            json!({"operation":call["operation"],"payload":call["payload"]})
        );
        let mut replay_call = captured;
        replay_call["command_id"] = call["command_id"].clone();
        semantic_execute(&mut replay, &semantic_request(&replay_call)).unwrap();
    }
    // This is same-compiler reexecution of original captured semantics. It is
    // deliberately not a claim about a generic native passive replay target.
    assert_eq!(state(&replay), expected);
    let mut foreign = semantic_request(&fixture["calls"][1]);
    foreign.owner_id = 12;
    let mut payload: Value = serde_json::from_slice(&foreign.payload).unwrap();
    payload["user_id"] = json!(12);
    foreign.payload = serde_json::to_vec(&payload).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&semantic_execute(&mut database, &foreign).unwrap())
            .unwrap()["applied"],
        false
    );
    let mut changed = semantic_request(&fixture["calls"][1]);
    let mut payload: Value = serde_json::from_slice(&changed.payload).unwrap();
    payload["event_payload"]["phase"] = json!("different");
    changed.payload = serde_json::to_vec(&payload).unwrap();
    assert!(semantic_execute(&mut database, &changed).is_err());
    assert_eq!(state(&database), expected);
    drop(database);
    let mut reopened = AuthorityDatabase::open(a.path()).unwrap();
    assert_eq!(state(&reopened), expected);
    assert!(head(&reopened).frame_witness.is_some());
    assert!(matches!(
        semantic_execute(&mut reopened, &semantic_request(&fixture["calls"][1])),
        Err(crate::semantic_executor::SemanticExecutionError::TurnProjectionStale)
    ));
}

fn seeded_runtime_vfs() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database =
        AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    assert_eq!(
        seed_projection(
            &mut database,
            producer_projection(&"s".repeat(256 * 1024)),
            false
        ),
        3
    );
    database
        .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
        .unwrap();
    semantic_execute(
        &mut database,
        &semantic_request(&producer_fixture()["calls"][0]),
    )
    .unwrap();
    assert!(head(&database).frame_witness.is_some());
    vfs.arm_fault(None).unwrap();
    (vfs, database)
}

#[test]
fn execution_frame_runtime_every_commit_fault_keeps_original_outbox_and_event_atomicity() {
    let command = semantic_request(&producer_fixture()["calls"][1]);
    let (vfs, mut baseline) = seeded_runtime_vfs();
    let before = state(&baseline);
    vfs.arm_fault(None).unwrap();
    let response = semantic_execute(&mut baseline, &command).unwrap();
    let trace = vfs.trace().unwrap();
    let after = state(&baseline);
    drop(baseline);
    for (index, operation) in trace.iter().enumerate() {
        if !matches!(
            operation,
            Operation::Write | Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        ) {
            continue;
        }
        for action in [
            FaultAction::ErrorBefore(io::ErrorKind::Other),
            FaultAction::ShortWrite(7),
            FaultAction::DropSync,
        ] {
            if !matches!(operation, Operation::Write)
                && matches!(action, FaultAction::ShortWrite(_))
            {
                continue;
            }
            if matches!(operation, Operation::Write) && matches!(action, FaultAction::DropSync) {
                continue;
            }
            let (vfs, mut database) = seeded_runtime_vfs();
            let occurrence = trace[..=index]
                .iter()
                .filter(|observed| *observed == operation)
                .count();
            vfs.arm_operation_fault(*operation, occurrence, action)
                .unwrap();
            let result = semantic_execute(&mut database, &command);
            assert!(vfs.operation_fault_fired().unwrap());
            let image = Arc::new(vfs.fork_durable().unwrap());
            drop(database);
            let mut reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), image).unwrap();
            let actual = state(&reopened);
            assert!(
                actual == before || actual == after,
                "partial event at {index}"
            );
            if result.is_ok() {
                assert_eq!(actual, after, "acknowledged event lost");
            }
            reopened
                .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
                .unwrap();
            assert_eq!(
                reopened.logical_outbox_pending(7, 11, 10).unwrap().len(),
                1 + usize::from(actual == after)
            );
            if actual == before {
                assert_eq!(semantic_execute(&mut reopened, &command).unwrap(), response);
            } else {
                assert!(matches!(
                    semantic_execute(&mut reopened, &command),
                    Err(crate::semantic_executor::SemanticExecutionError::TurnProjectionStale)
                ));
            }
            assert_eq!(state(&reopened), after);
            assert_eq!(reopened.logical_outbox_pending(7, 11, 10).unwrap().len(), 2);
        }
    }
}

#[test]
fn execution_frame_runtime_byte_rollover_and_repair_fallback_match_original() {
    let sequences=vec![
        (0..15).map(|i|json!([{"op":"set","path":["phase"],"value":format!("{i}{}","x".repeat(90*1024))}])).collect::<Vec<_>>(),
        vec![json!([{"op":"set","path":["content"],"value":"changed"}]),json!([]),json!([])],
        vec![json!([{"op":"set","path":["toolRounds"],"value":[{"guidanceId":"g","guidanceAnswerCommandId":"receipt","guidanceResponse":"answer","status":"running"}]}]),json!([]),json!([])],
        vec![json!([{"op":"set","path":["segments",0,"text"],"value":"nested"}]),json!([]),json!([])],
    ];
    for sequence in sequences {
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        let mut runtime = AuthorityDatabase::initialize(a.path()).unwrap();
        let mut control = AuthorityDatabase::initialize(b.path()).unwrap();
        let revision = seed_projection(&mut runtime, canonical_projection(), true);
        seed_projection(&mut control, canonical_projection(), false);
        let mut operations = vec![json!([])];
        operations.extend(sequence);
        for (index, operations) in operations.into_iter().enumerate() {
            let mut command = request(revision + index as u64, index);
            command.projection_segments_stable = true;
            command.projection_patch.as_mut().unwrap()["operations"] = operations;
            assert_eq!(
                commit_step(&mut runtime, &command, true).unwrap(),
                commit_step(&mut control, &command, false).unwrap()
            );
            let left = head(&runtime);
            let right = head(&control);
            assert_eq!(
                (
                    left.head_id,
                    left.base_revision,
                    left.patch_count,
                    left.patch_bytes
                ),
                (
                    right.head_id,
                    right.base_revision,
                    right.patch_count,
                    right.patch_bytes
                )
            );
            assert_eq!(state(&runtime), state(&control));
        }
    }
}

#[test]
fn execution_frame_runtime_witness_depth_corruption_and_legacy_version_fallback() {
    let projection = canonical_projection();
    let witness =
        ProjectionWitness::mint(projection.as_object().unwrap(), 7, "assistant", "running")
            .unwrap()
            .unwrap();
    let encoded = serde_json::to_value(&witness).unwrap();
    assert!(ProjectionWitness::decode(&encoded, 8).is_err());
    let mut future = encoded.clone();
    future["version"] = json!(99);
    assert!(ProjectionWitness::decode(&future, 7).unwrap().is_none());
    let mut corrupt = encoded.clone();
    corrupt["fields"]["content"]["depth"] = json!(usize::MAX);
    assert!(ProjectionWitness::decode(&corrupt, 7).is_err());
    let mut changed = projection.as_object().unwrap().clone();
    changed.insert("content".into(), json!("changed"));
    assert!(witness.verify(&changed, "assistant", "running").is_err());
    // Same encoded lengths still need the shared semantic no-op proof.
    let mut changed = projection.as_object().unwrap().clone();
    changed.insert("content".into(), json!("BEFORE"));
    assert!(witness.verify(&changed, "assistant", "running").is_err());
    let mut deep = json!(0);
    for _ in 0..crate::response_json::MAX_VALIDATED_CONTAINER_DEPTH {
        deep = json!([deep]);
    }
    let mut map = Map::new();
    map.insert("phase".into(), deep);
    assert!(
        ProjectionWitness::mint(&map, 1, "assistant", "running")
            .unwrap()
            .is_none()
    );
}

fn timing_prefix(fixture: &str) -> String {
    match fixture {
        "compressible" => "s".repeat(256 * 1024),
        "high_entropy" => {
            const SYMBOLS: &[u8; 64] =
                b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
            let mut prefix = String::with_capacity(256 * 1024);
            for counter in 0_u64..8192 {
                let mut digest = Sha256::new();
                digest.update(b"tofu.execution-frame.prefix.v1");
                digest.update(counter.to_le_bytes());
                for byte in digest.finalize() {
                    prefix.push(SYMBOLS[usize::from(byte & 63)] as char);
                }
            }
            prefix
        }
        _ => panic!("unknown prefix fixture"),
    }
}

fn producer_shaped_step(revision: u64, index: usize, case: &str) -> EventRecordRequest {
    let mut command = request(revision, index);
    command.projection_segments_stable = true;
    command.projection_patch.as_mut().unwrap()["operations"] = match case {
        "cold" | "hot" => {
            if index % 2 == 0 {
                json!([])
            } else {
                json!([{"op":"set","path":["fallbackModel"],"value":format!("model-{index}")}])
            }
        }
        "nested" => {
            json!([{"op":"set","path":["segments",0,"blockId"],"value":format!("block-{index}")}])
        }
        "tool" => {
            json!([{"op":"set","path":["toolRounds"],"value":[{"id":"timing","status":"running","progress":index}]}])
        }
        _ => panic!("unknown timing case"),
    };
    command
}

#[test]
#[ignore = "explicit work-count attribution; profile-on, never a timing result"]
fn execution_frame_runtime_work_counts() {
    assert!(crate::request_profile::enabled());
    for case in ["cold", "hot", "nested", "tool"] {
        for count in [1, 64] {
            let mut expected = None;
            for runtime in [false, true] {
                let directory = tempfile::tempdir().unwrap();
                let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
                let mut revision = seed_projection(
                    &mut database,
                    producer_projection(&timing_prefix("compressible")),
                    false,
                );
                let offset = usize::from(case != "cold");
                if offset > 0 {
                    commit_step(
                        &mut database,
                        &producer_shaped_step(revision, 0, "cold"),
                        runtime,
                    )
                    .unwrap();
                    revision += 1;
                }
                let before = crate::request_profile::snapshot();
                let wal_before = database.wal_len().unwrap();
                for index in 0..count {
                    let mut guard =
                        crate::request_profile::RequestGuard::start("turn.event.record").unwrap();
                    commit_step(
                        &mut database,
                        &producer_shaped_step(revision + index as u64, index + offset, case),
                        runtime,
                    )
                    .unwrap();
                    guard.finish("ok");
                }
                let aggregate = |value: Value| {
                    value["operations"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|v| v["operation"] == "turn.event.record")
                        .cloned()
                        .unwrap_or(json!({}))
                };
                let a = aggregate(before);
                let b = aggregate(crate::request_profile::snapshot());
                let mut work = Map::new();
                for (name, value) in b["work"].as_object().unwrap() {
                    work.insert(
                        name.clone(),
                        json!(value.as_u64().unwrap() - a["work"][name].as_u64().unwrap_or(0)),
                    );
                }
                let actual = state(&database);
                if let Some(expected) = &expected {
                    assert_eq!(&actual, expected);
                } else {
                    expected = Some(actual);
                }
                println!(
                    "FRAME_RUNTIME_WORK {}",
                    json!({"case":case,"events":count,"runtime":runtime,"work":work,"wal_bytes":database.wal_len().unwrap()-wal_before})
                );
            }
        }
    }
}

#[test]
#[cfg(target_os = "linux")]
#[ignore = "release/profile-off fresh process timing leg; requires coordinated quiet window"]
fn execution_frame_runtime_timing_leg() {
    assert!(!cfg!(debug_assertions));
    for variable in ["TOFU_DB_REQUEST_PROFILE_EVERY", "TOFU_DB_RECOVERY_PROFILE"] {
        assert_eq!(std::env::var(variable).as_deref(), Ok("0"));
    }
    assert!(!cfg!(feature = "recovery-profile"));
    assert!(!cfg!(feature = "allocator-diagnostics"));
    assert!(!crate::request_profile::enabled());
    let count: usize = std::env::var("TOFU_FRAME_EVENTS").unwrap().parse().unwrap();
    assert!([1, 8, 64].contains(&count));
    let runtime = match std::env::var("TOFU_FRAME_RUNTIME").unwrap().as_str() {
        "0" => false,
        "1" => true,
        _ => panic!("runtime must be 0 or 1"),
    };
    let fixture = std::env::var("TOFU_FRAME_FIXTURE").unwrap();
    let prefix = timing_prefix(&fixture);
    let case = std::env::var("TOFU_FRAME_CASE").unwrap();
    assert!(["cold", "hot", "nested", "tool"].contains(&case.as_str()));
    let cpu_ns = || {
        let mut t = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        assert_eq!(
            unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut t) },
            0
        );
        t.tv_sec as u128 * 1_000_000_000 + t.tv_nsec as u128
    };
    let mut samples = Vec::new();
    let mut signature = None;
    for _ in 0..12 {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .set_wal_durability(crate::wal::WalDurability::Strict)
            .unwrap();
        let mut revision = seed_projection(&mut database, producer_projection(&prefix), false);
        let offset = usize::from(case != "cold");
        if offset > 0 {
            commit_step(
                &mut database,
                &producer_shaped_step(revision, 0, "cold"),
                runtime,
            )
            .unwrap();
            revision += 1;
        }
        let commands: Vec<_> = (0..count)
            .map(|i| producer_shaped_step(revision + i as u64, i + offset, &case))
            .collect();
        let wal_before = database.wal_len().unwrap();
        let cpu_before = cpu_ns();
        let started = std::time::Instant::now();
        let responses: Vec<_> = commands
            .iter()
            .map(|command| commit_step(&mut database, command, runtime).unwrap())
            .collect();
        let elapsed_ns = started.elapsed().as_nanos();
        let process_cpu_ns = cpu_ns() - cpu_before;
        let wal_bytes = database.wal_len().unwrap() - wal_before;
        let actual = state(&database);
        assert_eq!(actual["raw"].as_array().unwrap().len(), count + offset);
        let layout = head(&database);
        drop(database);
        assert_eq!(
            state(&AuthorityDatabase::open(directory.path()).unwrap()),
            actual
        );
        let current = json!({"state_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&actual).unwrap())),
            "responses_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&responses).unwrap())),
            "head_id":layout.head_id,"base_revision":layout.base_revision,"patch_count":layout.patch_count,"patch_bytes":layout.patch_bytes});
        if let Some(expected) = &signature {
            assert_eq!(&current, expected);
        } else {
            signature = Some(current);
        }
        samples.push(
            json!({"elapsed_ns":elapsed_ns,"process_cpu_ns":process_cpu_ns,"wal_bytes":wal_bytes}),
        );
    }
    println!(
        "FRAME_RUNTIME_TIMING {}",
        json!({"events":count,"runtime":runtime,"fixture":fixture,"case":case,"samples":samples,"signature":signature,
        "prefix_sha256":format!("{:x}",Sha256::digest(prefix.as_bytes())),"prefix_bytes":prefix.len(),"strict":true,"measured":"N independent event prepares + N strict commits","reopened_each_sample":true})
    );
}
