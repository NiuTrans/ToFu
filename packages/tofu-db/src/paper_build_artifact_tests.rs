// Included by semantic_executor::tests to exercise the real admission/IR path.
fn retained_fixture(owner: u64, build: char, data: &[u8]) -> (Value, Value) {
    let proof = json!({"user_id":owner,"document_id":"d".repeat(64),"build_id":build.to_string().repeat(32),"upload_id":"a".repeat(32)});
    let receipt = json!({"schema_version":1,"owner_user_id":owner,"document_id":proof["document_id"],"build_id":proof["build_id"],
        "target":"host","source_digest":"e".repeat(64),"version_verified":true,"main_file":"main.tex","engine":"pdflatex",
        "toolchain":"fixture","status":"success","started_at":1,"completed_at":2,"diagnostics":[],
        "artifacts":[{"name":"document.pdf","sha256":format!("{:x}",Sha256::digest(data)),"size_bytes":data.len()}]});
    (proof, receipt)
}
fn retained_call(
    db: &mut AuthorityDatabase,
    op: &str,
    payload: Value,
    now: u64,
) -> Result<Value, SemanticExecutionError> {
    let owner = payload["user_id"].as_u64().unwrap_or(11);
    let command = uuid::Uuid::new_v4().to_string();
    execute(db, &request(op, owner, payload, Some(&command)), now)
}
fn retained_upload(db: &mut AuthorityDatabase, proof: &Value, receipt: &Value, data: &[u8]) {
    use base64::Engine as _;
    let mut payload = proof.clone();
    payload["receipt"] = receipt.clone();
    assert_eq!(
        retained_call(db, "paper.artifact.prepare", payload, 1000).unwrap(),
        json!({"next_sequence":0,"state":"uploading"})
    );
    for (sequence, chunk) in data
        .chunks(crate::generated_paper_build::RETAINED_CHUNK_BYTES)
        .enumerate()
    {
        let mut payload = proof.clone();
        payload["sequence"] = json!(sequence);
        payload["data"] = json!(base64::engine::general_purpose::STANDARD.encode(chunk));
        assert_eq!(
            retained_call(db, "paper.artifact.append", payload, 1001).unwrap(),
            json!({"next_sequence":sequence+1})
        );
    }
    retained_call(db, "paper.artifact.seal", proof.clone(), 1002).unwrap();
}
fn retained_workspace(proof: &Value, receipt: &Value, revision: u64) -> Value {
    json!({"user_id":proof["user_id"],"paper_hash":"retained-paper","lang":"en","updated_at":2,"expected_revision":revision,
        "artifact_proof":proof,"workspace":{"compilation":{"mode":"paper_build","status":"passing","build":{
            "document_id":proof["document_id"],"build_id":proof["build_id"],"manifest_digest":receipt["source_digest"],
            "pdf_sha256":receipt["artifacts"][0]["sha256"]}}}})
}
fn retained_read(db: &mut AuthorityDatabase, proof: &Value, now: u64) -> Value {
    let mut payload = proof.clone();
    payload["lease_id"] = json!("b".repeat(32));
    payload["operation"] = json!("retain");
    retained_call(db, "paper.artifact.lease", payload.clone(), now).unwrap();
    payload["sequence"] = json!(0);
    retained_call(db, "paper.artifact.read", payload, now).unwrap()
}

#[test]
fn retained_receipt_schema_rejects_invalid_shapes_and_accepts_json_schema_integers() {
    let (_, valid) = retained_fixture(11, '1', b"pdf");
    let validate = crate::paper_build_receipt::validate;
    assert!(validate(&valid).is_ok());
    for (pointer, value) in [
        ("/schema_version", json!(1.0)),
        ("/schema_version", json!(true)),
        ("/document_id", json!("d".repeat(32))),
        ("/artifacts/0/size_bytes", json!(0)),
        ("/version_verified", Value::Null),
        ("/source_digest", Value::Null),
        ("/started_at", json!(1.1)),
        ("/artifacts/0/sha256", json!("G".repeat(64))),
        ("/status", json!("failed")),
    ] {
        let mut bad = valid.clone();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(validate(&bad).is_err(), "{pointer}");
    }
    let mut extra = valid.clone();
    extra["unregistered"] = json!(1);
    assert!(validate(&extra).is_err());
    let mut duplicate = valid.clone();
    duplicate["artifacts"]
        .as_array_mut()
        .unwrap()
        .push(valid["artifacts"][0].clone());
    assert!(validate(&duplicate).is_err());
    let mut numeric = valid.clone();
    numeric["owner_user_id"] = json!(11.0);
    numeric["started_at"] = json!(1e20);
    numeric["artifacts"][0]["size_bytes"] = json!(3.0);
    assert!(validate(&numeric).is_ok());
    numeric["main_file"] = json!("界".repeat(512));
    assert!(validate(&numeric).is_ok());
    numeric["main_file"] = json!("界".repeat(513));
    assert!(validate(&numeric).is_err());
    let mut error = valid.clone();
    error["diagnostics"] =
        json!([{"severity":"error","code":"bad","message":"bad","file":"","line":null}]);
    assert!(validate(&error).is_err());
    let mut oversized = valid.clone();
    oversized["unregistered"] = json!("x".repeat(262145));
    assert!(validate(&oversized).is_err());
    assert!(!crate::paper_build_receipt::equal(
        &json!(9007199254740993_u64),
        &json!(9007199254740992.0)
    ));
    assert!(crate::paper_build_receipt::equal(
        &json!({"x":[11]}),
        &json!({"x":[11.0]})
    ));
}

#[test]
fn retained_upload_retries_integrity_and_owner_fences() {
    use base64::Engine as _;
    let vfs = prepared_vfs();
    let mut db = simulated_database(vfs.clone());
    let (proof, receipt) = retained_fixture(11, '1', b"pdf");
    let mut payload = proof.clone();
    payload["receipt"] = receipt.clone();
    retained_call(&mut db, "paper.artifact.prepare", payload.clone(), 1000).unwrap();
    assert_eq!(
        retained_call(&mut db, "paper.artifact.seal", proof.clone(), 1001).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    let mut append = proof.clone();
    append["sequence"] = json!(0);
    append["data"] = json!(base64::engine::general_purpose::STANDARD.encode(b"pdf"));
    assert_eq!(
        retained_call(&mut db, "paper.artifact.append", append.clone(), 1002).unwrap(),
        json!({"next_sequence":1})
    );
    assert_eq!(
        retained_call(&mut db, "paper.artifact.append", append.clone(), 1003).unwrap(),
        json!({"next_sequence":1})
    );
    let mut different = append.clone();
    different["data"] = json!("YmFk");
    assert_eq!(
        retained_call(&mut db, "paper.artifact.append", different, 1003).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    retained_call(&mut db, "paper.artifact.seal", proof.clone(), 1004).unwrap();
    retained_call(&mut db, "paper.artifact.append", append, 1005).unwrap();
    payload["receipt"]["started_at"] = json!(1.0);
    retained_call(&mut db, "paper.artifact.prepare", payload.clone(), 1005).unwrap();
    payload["receipt"]["toolchain"] = json!("changed");
    assert_eq!(
        retained_call(&mut db, "paper.artifact.prepare", payload, 1005).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    let mut foreign = proof.clone();
    foreign["user_id"] = json!(12);
    assert_eq!(
        retained_call(&mut db, "paper.artifact.seal", foreign, 1005).unwrap_err(),
        SemanticExecutionError::NotFound
    );
    let mut invalid = proof.clone();
    invalid["sequence"] = json!(0);
    invalid["data"] = json!("x".repeat(349529));
    vfs.arm_fault(None).unwrap();
    assert_eq!(
        retained_call(&mut db, "paper.artifact.append", invalid, 1005).unwrap_err(),
        SemanticExecutionError::InvalidPayload
    );
    assert!(vfs.trace().unwrap().is_empty());
    // record.put reaches the same document codec, retaining corruption injection.
    let chunk_key = format!(
        "11:{}:{}:0",
        proof["document_id"].as_str().unwrap(),
        proof["build_id"].as_str().unwrap()
    );
    retained_call(&mut db,"record.put",json!({"namespace":"paper.build.chunk.v1","key":chunk_key,"value":{"data":"YmFk","sha256":"0".repeat(64)}}),1006).unwrap();
    retained_call(
        &mut db,
        "research.workspace.put",
        retained_workspace(&proof, &receipt, 0),
        1007,
    )
    .unwrap();
    let mut reader = proof.clone();
    reader["lease_id"] = json!("b".repeat(32));
    reader["operation"] = json!("retain");
    retained_call(&mut db, "paper.artifact.lease", reader.clone(), 1008).unwrap();
    reader["sequence"] = json!(0);
    assert!(matches!(
        retained_call(&mut db, "paper.artifact.read", reader, 1009).unwrap_err(),
        SemanticExecutionError::Integrity(_)
    ));
}

#[test]
fn retained_publication_cas_lost_ack_discard_and_reader_lifecycle() {
    let mut db = simulated_database(prepared_vfs());
    let (first, r1) = retained_fixture(11, '1', b"old");
    let (second, r2) = retained_fixture(11, '2', b"new");
    retained_upload(&mut db, &first, &r1, b"old");
    retained_upload(&mut db, &second, &r2, b"new");
    retained_call(
        &mut db,
        "research.workspace.put",
        retained_workspace(&first, &r1, 0),
        2000,
    )
    .unwrap();
    retained_call(&mut db, "paper.artifact.discard", first.clone(), 2001).unwrap();
    assert_eq!(
        retained_call(
            &mut db,
            "research.workspace.put",
            retained_workspace(&second, &r2, 0),
            2002
        )
        .unwrap_err(),
        SemanticExecutionError::Conflict
    );
    assert_eq!(
        retained_call(&mut db, "paper.artifact.latest", first.clone(), 2003).unwrap(),
        json!({"build_id":first["build_id"]})
    );
    let mut invalid_proof = retained_workspace(&second, &r2, 0);
    invalid_proof["artifact_proof"] = json!("malformed");
    assert_eq!(
        retained_call(&mut db, "research.workspace.put", invalid_proof, 2003).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    let mut forged = retained_workspace(&second, &r2, 1);
    forged.as_object_mut().unwrap().remove("artifact_proof");
    assert_eq!(
        retained_call(&mut db, "research.workspace.put", forged, 2003).unwrap_err(),
        SemanticExecutionError::InvalidPayload
    );
    assert_eq!(retained_read(&mut db, &first, 2004), json!({"data":"b2xk"}));
    retained_call(
        &mut db,
        "research.workspace.put",
        retained_workspace(&second, &r2, 1),
        2005,
    )
    .unwrap();
    let mut lease = first.clone();
    lease["lease_id"] = json!("b".repeat(32));
    lease["operation"] = json!("renew");
    retained_call(&mut db, "paper.artifact.lease", lease.clone(), 242003).unwrap();
    retained_call(&mut db, "paper.artifact.lease", lease.clone(), 242006).unwrap();
    lease["lease_id"] = json!("c".repeat(32));
    lease["operation"] = json!("retain");
    // A live viewer permits later Range requests on its retired manuscript.
    assert_eq!(
        retained_call(&mut db, "paper.artifact.lease", lease, 242006).unwrap()["retained"],
        json!(true)
    );
    assert_eq!(
        retained_call(
            &mut db,
            "paper.artifact.prune",
            json!({"user_id":11}),
            242007
        )
        .unwrap(),
        json!({"progress":false,"chunks_deleted":0})
    );
    assert_eq!(
        retained_call(
            &mut db,
            "paper.artifact.prune",
            json!({"user_id":11}),
            482007
        )
        .unwrap(),
        json!({"progress":true,"chunks_deleted":1})
    );
    assert_eq!(
        retained_read(&mut db, &second, 482008),
        json!({"data":"bmV3"})
    );
}

#[test]
fn retained_count_and_total_bytes_do_not_refuse_work_and_prune_stays_owned() {
    let mut db = simulated_database(prepared_vfs());
    let (p1, r1) = retained_fixture(11, '1', b"x");
    let (p2, r2) = retained_fixture(12, '2', b"x");
    let prepare = |p: &Value, r: &Value| {
        let mut v = p.clone();
        v["receipt"] = r.clone();
        v
    };
    retained_call(&mut db, "paper.artifact.prepare", prepare(&p1, &r1), 1000).unwrap();
    retained_call(&mut db, "paper.artifact.prepare", prepare(&p2, &r2), 1000).unwrap();
    assert_eq!(
        retained_call(
            &mut db,
            "paper.artifact.prune",
            json!({"user_id":13}),
            301001
        )
        .unwrap()["progress"],
        false
    );
    assert_eq!(
        retained_call(
            &mut db,
            "paper.artifact.prune",
            json!({"user_id":11}),
            301001
        )
        .unwrap()["progress"],
        true
    );
    retained_call(&mut db, "paper.artifact.prune", json!({"user_id":12}), 301002).unwrap();
    retained_call(&mut db, "paper.artifact.prepare", prepare(&p2, &r2), 301002).unwrap();
    let mut other_tenant = request(
        "paper.artifact.prepare",
        11,
        prepare(&p1, &r1),
        Some("other-tenant"),
    );
    other_tenant.tenant_id = Some(8);
    execute(&mut db, &other_tenant, 301003).unwrap();
}

#[test]
fn retained_readers_have_no_count_quota_and_upload_deadline_is_owned() {
    let mut db = simulated_database(prepared_vfs());
    let (proof, receipt) = retained_fixture(11, '1', b"pdf");
    retained_upload(&mut db, &proof, &receipt, b"pdf");
    retained_call(
        &mut db,
        "research.workspace.put",
        retained_workspace(&proof, &receipt, 0),
        2000,
    )
    .unwrap();
    for i in 0..256 {
        let mut p = proof.clone();
        p["lease_id"] = json!(format!("{i:032x}"));
        p["operation"] = json!("retain");
        retained_call(&mut db, "paper.artifact.lease", p, 2001).unwrap();
    }
    let mut p = proof.clone();
    p["lease_id"] = json!("f".repeat(32));
    p["operation"] = json!("retain");
    retained_call(&mut db, "paper.artifact.lease", p.clone(), 2002).unwrap();
    retained_call(&mut db, "paper.artifact.lease", p, 242002).unwrap();
    let (expired, r) = retained_fixture(11, '2', b"pdf");
    let mut p = expired.clone();
    p["receipt"] = r;
    retained_call(&mut db, "paper.artifact.prepare", p, 1000).unwrap();
    assert_eq!(
        retained_call(&mut db, "paper.artifact.seal", expired, 301000).unwrap_err(),
        SemanticExecutionError::Conflict
    );
}

#[test]
fn retained_prune_deletes_at_most_32_chunks_and_resumes_after_reopen() {
    let vfs = prepared_vfs();
    let mut db = simulated_database(vfs.clone());
    let data = vec![b'x'; 33 * crate::generated_paper_build::RETAINED_CHUNK_BYTES];
    let (proof, receipt) = retained_fixture(11, '1', &data);
    retained_upload(&mut db, &proof, &receipt, &data);
    retained_call(&mut db, "paper.artifact.discard", proof.clone(), 2000).unwrap();
    assert_eq!(
        retained_call(&mut db, "paper.artifact.prune", json!({"user_id":11}), 2001).unwrap(),
        json!({"progress":true,"chunks_deleted":32})
    );
    assert_eq!(
        retained_call(&mut db, "paper.artifact.seal", proof, 2002).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    drop(db);
    vfs.crash().unwrap();
    vfs.arm_fault(None).unwrap();
    let mut db = AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
    assert_eq!(
        retained_call(&mut db, "paper.artifact.prune", json!({"user_id":11}), 2003).unwrap(),
        json!({"progress":true,"chunks_deleted":1})
    );
    assert_eq!(
        retained_call(&mut db, "paper.artifact.prune", json!({"user_id":11}), 2004).unwrap(),
        json!({"progress":false,"chunks_deleted":0})
    );
}

fn retained_fault_seed(
    vfs: std::sync::Arc<DeterministicVfs>,
) -> (AuthorityDatabase, Value, Value, Request) {
    let mut db = simulated_database(vfs.clone());
    let (p1, r1) = retained_fixture(11, '1', b"old");
    let (p2, r2) = retained_fixture(11, '2', b"new");
    retained_upload(&mut db, &p1, &r1, b"old");
    retained_upload(&mut db, &p2, &r2, b"new");
    retained_call(
        &mut db,
        "research.workspace.put",
        retained_workspace(&p1, &r1, 0),
        2000,
    )
    .unwrap();
    let publish = request(
        "research.workspace.put",
        11,
        retained_workspace(&p2, &r2, 1),
        Some("retained-publish"),
    );
    vfs.arm_fault(None).unwrap();
    (db, p1, p2, publish)
}
#[test]
fn retained_every_publication_io_fault_recovers_workspace_pin_and_old_pdf_atomically() {
    let vfs = prepared_vfs();
    let (mut db, _, _, publish) = retained_fault_seed(vfs.clone());
    execute(&mut db, &publish, 2100).unwrap();
    let trace = vfs.trace().unwrap();
    drop(db);
    let mut faults = Vec::new();
    for (index, op) in trace.iter().enumerate() {
        faults.push((
            index as u64 + 1,
            FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        ));
        if *op == Operation::Write {
            faults.push((index as u64 + 1, FaultAction::ShortWrite(7)));
        }
        if matches!(
            op,
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        ) {
            faults.push((index as u64 + 1, FaultAction::DropSync));
        }
    }
    for (operation_number, action) in faults {
        let vfs = prepared_vfs();
        let (mut db, p1, p2, publish) = retained_fault_seed(vfs.clone());
        vfs.arm_fault(Some(FaultRule {
            operation_number,
            action,
        }))
        .unwrap();
        let _ = execute(&mut db, &publish, 2100);
        drop(db);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let workspace = retained_call(
            &mut recovered,
            "research.workspace.get",
            json!({"user_id":11,"paper_hash":"retained-paper","lang":"en"}),
            3000,
        )
        .unwrap();
        let latest =
            retained_call(&mut recovered, "paper.artifact.latest", p1.clone(), 3000).unwrap();
        assert_eq!(
            workspace["workspace"]["compilation"]["build"]["build_id"], latest["build_id"],
            "fault {operation_number}"
        );
        assert_eq!(
            retained_read(&mut recovered, &p1, 3000),
            json!({"data":"b2xk"})
        );
        execute(&mut recovered, &publish, 3001).unwrap();
        retained_call(&mut recovered, "paper.artifact.discard", p2.clone(), 3002).unwrap();
        assert_eq!(
            retained_read(&mut recovered, &p2, 3003),
            json!({"data":"bmV3"})
        );
    }
}

fn retained_upload_fault_seed(
    vfs: std::sync::Arc<DeterministicVfs>,
    stage: &str,
) -> (AuthorityDatabase, Value, Request) {
    use base64::Engine as _;
    let mut db = simulated_database(vfs.clone());
    let data = vec![b'z'; 65536];
    let (proof, receipt) = retained_fixture(11, '3', &data);
    let mut prepare = proof.clone();
    prepare["receipt"] = receipt;
    let mut append = proof.clone();
    append["sequence"] = json!(0);
    append["data"] = json!(base64::engine::general_purpose::STANDARD.encode(&data));
    if stage != "prepare" {
        retained_call(&mut db, "paper.artifact.prepare", prepare.clone(), 1000).unwrap();
    }
    if matches!(stage, "seal" | "prune") {
        retained_call(&mut db, "paper.artifact.append", append.clone(), 1001).unwrap();
    }
    if stage == "prune" {
        retained_call(&mut db, "paper.artifact.discard", proof.clone(), 1002).unwrap();
    }
    let payload = match stage {
        "prepare" => prepare,
        "append" => append,
        _ => proof.clone(),
    };
    let command = request(
        &format!("paper.artifact.{stage}"),
        11,
        payload,
        Some("retained-natural-retry"),
    );
    vfs.arm_fault(None).unwrap();
    (db, proof, command)
}
#[test]
fn retained_every_upload_and_reclamation_io_fault_retries_without_partial_state() {
    for stage in ["prepare", "append", "seal", "prune"] {
        let vfs = prepared_vfs();
        let (mut db, _, command) = retained_upload_fault_seed(vfs.clone(), stage);
        execute(&mut db, &command, 2000).unwrap();
        let trace = vfs.trace().unwrap();
        drop(db);
        let mut faults = Vec::new();
        for (index, op) in trace.iter().enumerate() {
            faults.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if *op == Operation::Write {
                faults.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
            if matches!(
                op,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                faults.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in faults {
            let vfs = prepared_vfs();
            let (mut db, proof, command) = retained_upload_fault_seed(vfs.clone(), stage);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = execute(&mut db, &command, 2000);
            drop(db);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut db =
                AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
            db.configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
                .unwrap();
            execute(&mut db, &command, 3000).unwrap();
            let raw=retained_call(&mut db,"record.get",json!({"namespace":"paper.build.artifact.v1","key":format!("11:{}:{}",proof["document_id"].as_str().unwrap(),proof["build_id"].as_str().unwrap())}),3001).unwrap();
            if stage == "prune" {
                assert!(raw.is_null(), "{stage}:{operation_number}");
            } else {
                let expected = if stage == "prepare" { 0 } else { 1 };
                assert_eq!(
                    raw["value"]["next_sequence"], expected,
                    "{stage}:{operation_number}"
                );
                if stage == "seal" {
                    assert_eq!(raw["value"]["state"], "sealed");
                }
            }
        }
    }
}

#[test]
fn retained_validated_chunk_keeps_direct_ir_validation_and_payload_bounds() {
    use crate::paper_build_artifact::{
        Identity, Operation as ArtifactOperation, Request as ArtifactRequest, UploadWitness,
        ValidatedChunk,
    };
    use base64::Engine as _;
    let encoded = base64::engine::general_purpose::STANDARD.encode(
        vec![b'x'; crate::generated_paper_build::RETAINED_CHUNK_BYTES],
    );
    let chunk = ValidatedChunk::decode(&encoded).unwrap();
    for invalid in ["", "cGRm=", "cGRm====", "cGRm\n", "eA=", "eA== "] {
        assert!(ValidatedChunk::decode(invalid).is_err(), "{invalid:?}");
    }
    let too_large = base64::engine::general_purpose::STANDARD.encode(vec![
        b'x';
        crate::generated_paper_build::RETAINED_CHUNK_BYTES
            + 1
    ]);
    assert!(ValidatedChunk::decode(&too_large).is_err());
    let mut typed = ArtifactRequest {
        now_unix_ms: 1000,
        operation: ArtifactOperation::Append {
            upload: UploadWitness {
                identity: Identity {
                    document_id: "d".repeat(64),
                    build_id: "1".repeat(32),
                },
                upload_id: "a".repeat(32),
            },
            sequence: 0,
            data: chunk,
        },
    };
    let bytes = typed.validate().unwrap();
    assert!((encoded.len()..=encoded.len() + 512).contains(&bytes));
    if let ArtifactOperation::Append { upload, .. } = &mut typed.operation {
        upload.upload_id = "invalid".to_owned();
    }
    assert_eq!(
        typed.validate().unwrap_err().kind(),
        io::ErrorKind::InvalidInput
    );
}

#[test]
fn retained_streamed_replay_digest_matches_exact_canonical_json_and_bound() {
    for value in [
        json!("eB=="),
        json!("quotes\" and control\n and 界"),
        json!({"z":3,"a":[1.0,true,null]}),
        json!("x".repeat(349528)),
    ] {
        let encoded = canonical_json(&value).unwrap();
        let (digest, bytes) =
            crate::paper_build_receipt::digest_bounded(&value, encoded.len()).unwrap();
        assert_eq!(digest, <[u8; 32]>::from(Sha256::digest(&encoded)));
        assert_eq!(bytes, encoded.len());
        assert!(crate::paper_build_receipt::digest_bounded(&value, encoded.len() - 1).is_err());
    }
}

#[test]
fn retained_reclamation_advances_beyond_a_full_page_of_pinned_builds() {
    let mut db = simulated_database(prepared_vfs());
    for index in 0..129 {
        let (mut proof, mut receipt) = retained_fixture(11, '1', b"pdf");
        proof["document_id"] = json!(format!("{index:064x}"));
        receipt["document_id"] = proof["document_id"].clone();
        retained_upload(&mut db, &proof, &receipt, b"pdf");
        let mut workspace = retained_workspace(&proof, &receipt, 0);
        workspace["paper_hash"] = json!(format!("{index:064x}"));
        retained_call(&mut db,"research.workspace.put",workspace,2000).unwrap();
    }
    let (mut expired, mut receipt) = retained_fixture(11, '2', b"pdf");
    expired["document_id"] = json!("f".repeat(64));
    receipt["document_id"] = expired["document_id"].clone();
    let mut prepare = expired.clone();
    prepare["receipt"] = receipt;
    retained_call(&mut db,"paper.artifact.prepare",prepare,1000).unwrap();
    let first = retained_call(&mut db,"paper.artifact.prune",json!({"user_id":11}),301001).unwrap();
    assert_eq!(first,json!({"progress":true,"chunks_deleted":0}));
    let second = retained_call(&mut db,"paper.artifact.prune",json!({"user_id":11}),301002).unwrap();
    assert_eq!(second,json!({"progress":true,"chunks_deleted":0}));
    let mut retry = expired;
    retry["receipt"] = retained_fixture(11,'2',b"pdf").1;
    retry["receipt"]["document_id"] = json!("f".repeat(64));
    retained_call(&mut db,"paper.artifact.prepare",retry,301003).unwrap();
}
