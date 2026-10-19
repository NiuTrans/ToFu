// Included by semantic_executor::tests: exercise public admission and real IR commits.
fn saved_source(main: &str) -> Value {
    json!({"source":"local","project_root":"/owned/界😀","path":"chapter.tex","main_file":main,"label":"Paper"})
}
fn saved_register(db: &mut AuthorityDatabase, owner: u64, main: &str) -> Value {
    retained_call(
        db,
        "paper.document.register",
        json!({"user_id":owner,"source":saved_source(main)}),
        1000,
    )
    .unwrap()
}
fn saved_request(db: &mut AuthorityDatabase, id: &Value, key: &str, now: u64) -> Value {
    retained_call(
        db,
        "paper.document.request",
        json!({"user_id":11,"document_id":id,"conversation_id":"chat","request_key":key}),
        now,
    )
    .unwrap()
}
fn saved_fixture(id: &Value, build: char, data: &[u8]) -> (Value, Value) {
    let (mut proof, mut receipt) = retained_fixture(11, build, data);
    proof["document_id"] = id.clone();
    receipt["document_id"] = id.clone();
    (proof, receipt)
}
fn saved_finish(id: &Value, generation: &Value, receipt: &Value, proof: &Value) -> Value {
    json!({"user_id":11,"document_id":id,"generation":generation["generation"],"receipt":receipt,"proof":proof})
}
fn saved_get(db: &mut AuthorityDatabase, id: &Value, now: u64) -> Value {
    retained_call(
        db,
        "paper.document.get",
        json!({"user_id":11,"document_id":id}),
        now,
    )
    .unwrap()
}

#[test]
fn saved_document_research_adopts_retained_identity_and_preserves_publication_authority() {
    let vfs = prepared_vfs();
    let mut db = simulated_database(vfs.clone());
    let (proof, receipt) = retained_fixture(11, 'b', b"research pdf");
    retained_upload(&mut db, &proof, &receipt, b"research pdf");
    let mut workspace = retained_workspace(&proof, &receipt, 0);
    workspace["paper_hash"] = json!("a".repeat(32));
    retained_call(&mut db, "research.workspace.put", workspace, 1003).unwrap();
    let source = json!({"source":"research","paper_hash":"a".repeat(32),"lang":"en","document_id":proof["document_id"]});
    let register = json!({"user_id":11,"source":source});
    let record = retained_call(&mut db, "paper.document.register", register.clone(), 1004).unwrap();
    assert_eq!(record["document_id"], proof["document_id"]);
    assert_eq!(record["source"]["main_file"], "main.tex");
    assert_eq!(
        retained_call(&mut db, "paper.document.register", register.clone(), 1005).unwrap(),
        record
    );
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.register",
            json!({"user_id":12,"source":source}),
            1005
        )
        .unwrap_err(),
        SemanticExecutionError::NotFound
    );
    let id = &proof["document_id"];
    let state = json!({"build_id":proof["build_id"],"page":15,"fraction":0.3,"horizontal":0.5,"scale":1.25,"fit":false,"reading_mode":true});
    let save = json!({"user_id":11,"document_id":id,"state":state,"expected_revision":0,"writer_id":"a".repeat(32),"sequence":1});
    retained_call(&mut db, "paper.document.reading", save.clone(), 1006).unwrap();
    let mut other = save.clone();
    other["writer_id"] = json!("b".repeat(32));
    assert_eq!(
        retained_call(&mut db, "paper.document.reading", other, 1007).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    for (operation, payload) in [
        (
            "paper.document.request",
            json!({"user_id":11,"document_id":id,"conversation_id":"research-chat","request_key":"attempt"}),
        ),
        (
            "paper.document.finish",
            json!({"user_id":11,"document_id":id,"generation":1,"receipt":receipt,"proof":null}),
        ),
        (
            "paper.document.abandon",
            json!({"user_id":11,"document_id":id,"generation":1}),
        ),
    ] {
        assert_eq!(
            retained_call(&mut db, operation, payload, 1008).unwrap_err(),
            SemanticExecutionError::Conflict
        );
    }
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.current",
            json!({"user_id":11,"conversation_id":"research-chat"}),
            1009
        )
        .unwrap(),
        Value::Null
    );
    drop(db);
    vfs.crash().unwrap();
    vfs.arm_fault(None).unwrap();
    let mut db = AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
    db.configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
        .unwrap();
    let reopened = retained_call(&mut db, "paper.document.register", register, 1010).unwrap();
    assert_eq!(reopened["reading"], state);
    assert_eq!(reopened["reading_revision"], 1);
    assert_eq!(saved_get(&mut db, id, 1011)["receipt"], receipt);
}

#[test]
fn saved_document_identity_schema_and_owner_are_stable_without_source_access() {
    let mut db = simulated_database(prepared_vfs());
    let first = saved_register(&mut db, 11, "main.tex");
    let id = &first["document_id"];
    assert!(crate::paper_build_receipt::lower_hex(
        id.as_str().unwrap(),
        64,
        64
    ));
    let mut changed = saved_source("main.tex");
    changed["label"] = json!("renamed");
    changed["path"] = json!("missing.tex");
    let register = |source: Value| json!({"user_id":11,"source":source});
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.register",
            register(changed.clone()),
            1001
        )
        .unwrap(),
        first
    );
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.resolve",
            json!({"user_id":11,"sources":[changed.clone()]}),
            1001
        )
        .unwrap(),
        json!([id])
    );
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.resolve",
            json!({"user_id":12,"sources":[changed.clone()]}),
            1001
        )
        .unwrap(),
        json!([null])
    );
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.get",
            json!({"user_id":12,"document_id":id}),
            1001
        )
        .unwrap_err(),
        SemanticExecutionError::NotFound
    );
    changed["document_id"] = json!("a".repeat(64));
    assert_eq!(
        retained_call(&mut db, "paper.document.register", register(changed), 1001).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    let cloud = json!({"source":"overleaf","project_id":"a".repeat(24),"mcp_server":"cloud","main_file":null,"label":"Cloud"});
    assert!(retained_call(&mut db, "paper.document.register", register(cloud), 1001).is_ok());
    for source in [json!({}), json!({"source":"other"}), {
        let mut s = saved_source("main.tex");
        s["label"] = json!("x".repeat(1025));
        s
    }] {
        assert_eq!(
            retained_call(&mut db, "paper.document.register", register(source), 1001).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
    }
    let repeated_sources = retained_call(
        &mut db,
        "paper.document.resolve",
        json!({"user_id":11,"sources":vec![saved_source("x");65]}),
        1001
    ).unwrap();
    assert_eq!(repeated_sources.as_array().unwrap().len(), 65);
}

#[test]
fn saved_document_generations_fence_expired_abandoned_and_first_terminal_receipts() {
    let mut db = simulated_database(prepared_vfs());
    let row = saved_register(&mut db, 11, "main.tex");
    let id = &row["document_id"];
    let first = saved_request(&mut db, id, "first", 1001);
    assert_eq!(saved_request(&mut db, id, "first", 1002), first);
    let (proof, mut receipt) = saved_fixture(id, '1', b"pdf");
    receipt["status"] = json!("failed");
    receipt["artifacts"] = json!([]);
    let expire = 1001 + crate::generated_paper_build::DOCUMENT_REQUEST_SECONDS as u64 * 1000;
    assert_eq!(saved_get(&mut db, id, expire)["status"], "interrupted");
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.finish",
            saved_finish(id, &first, &receipt, &json!(false)),
            expire
        )
        .unwrap(),
        json!({"current":false,"published":false})
    );
    let second = saved_request(&mut db, id, "first", expire);
    assert_eq!(second["generation"], 2);
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.finish",
            saved_finish(id, &first, &receipt, &proof),
            expire + 1
        )
        .unwrap()["current"],
        false
    );
    let finish = saved_finish(id, &second, &receipt, &Value::Null);
    assert_eq!(
        retained_call(&mut db, "paper.document.finish", finish.clone(), expire + 1).unwrap(),
        json!({"current":true,"published":false})
    );
    assert_eq!(
        retained_call(&mut db, "paper.document.finish", finish, expire + 2).unwrap()["current"],
        true
    );
    receipt["toolchain"] = json!("changed");
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.finish",
            saved_finish(id, &second, &receipt, &Value::Null),
            expire + 3
        )
        .unwrap_err(),
        SemanticExecutionError::Conflict
    );
    receipt["build_id"] = json!("2".repeat(32));
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.finish",
            saved_finish(id, &second, &receipt, &Value::Null),
            expire + 3
        )
        .unwrap()["current"],
        false
    );
    let third = saved_request(&mut db, id, "third", expire + 4);
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.abandon",
            json!({"user_id":11,"document_id":id,"generation":third["generation"]}),
            expire + 5
        )
        .unwrap(),
        json!({"abandoned":true})
    );
    assert_eq!(
        saved_request(&mut db, id, "third", expire + 6)["generation"],
        4
    );
}

#[test]
fn saved_document_publication_keeps_verified_pdf_and_rolls_back_receipt_mismatch() {
    let mut db = simulated_database(prepared_vfs());
    let row = saved_register(&mut db, 11, "main.tex");
    let id = &row["document_id"];
    let (first, r1) = saved_fixture(id, '1', b"old");
    retained_upload(&mut db, &first, &r1, b"old");
    let generation = saved_request(&mut db, id, "first", 1100);
    let finish = saved_finish(id, &generation, &r1, &first);
    assert_eq!(
        retained_call(&mut db, "paper.document.finish", finish.clone(), 1101).unwrap()["published"],
        true
    );
    assert_eq!(
        retained_call(&mut db, "paper.document.finish", finish, 1102).unwrap()["published"],
        true
    );
    let (second, mut r2) = saved_fixture(id, '2', b"new");
    retained_upload(&mut db, &second, &r2, b"new");
    let generation = saved_request(&mut db, id, "second", 1200);
    r2["toolchain"] = json!("mismatch");
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.finish",
            saved_finish(id, &generation, &r2, &second),
            1201
        )
        .unwrap_err(),
        SemanticExecutionError::Conflict
    );
    assert_eq!(saved_get(&mut db, id, 1202)["receipt"], r1);
    assert_eq!(saved_get(&mut db, id, 1202)["status"], "pending");
    let (cloud, mut r3) = saved_fixture(id, '3', b"cloud");
    r3["target"] = json!("overleaf");
    r3["version_verified"] = json!(false);
    r3["source_digest"] = Value::Null;
    retained_upload(&mut db, &cloud, &r3, b"cloud");
    let generation = saved_request(&mut db, id, "cloud", 1300);
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.finish",
            saved_finish(id, &generation, &r3, &cloud),
            1301
        )
        .unwrap(),
        json!({"current":true,"published":false})
    );
    assert_eq!(saved_get(&mut db, id, 1302)["receipt"], r1);
    let other = saved_register(&mut db, 11, "other.tex");
    let other = &other["document_id"];
    let (cloud, mut r4) = saved_fixture(other, '4', b"cloud");
    r4["target"] = json!("overleaf");
    r4["version_verified"] = json!(false);
    r4["source_digest"] = Value::Null;
    retained_upload(&mut db, &cloud, &r4, b"cloud");
    let generation = saved_request(&mut db, other, "initial-cloud", 1300);
    assert_eq!(
        retained_call(
            &mut db,
            "paper.document.finish",
            saved_finish(other, &generation, &r4, &cloud),
            1301
        )
        .unwrap()["published"],
        true
    );
}

#[test]
fn saved_document_reading_sequences_reject_stale_windows_and_keep_latest_writer() {
    let mut db = simulated_database(prepared_vfs());
    let row = saved_register(&mut db, 11, "main.tex");
    let id = &row["document_id"];
    let (proof, receipt) = saved_fixture(id, '1', b"pdf");
    retained_upload(&mut db, &proof, &receipt, b"pdf");
    let generation = saved_request(&mut db, id, "first", 1100);
    retained_call(
        &mut db,
        "paper.document.finish",
        saved_finish(id, &generation, &receipt, &proof),
        1101,
    )
    .unwrap();
    let state = json!({"build_id":proof["build_id"],"page":12,"fraction":0.3,"horizontal":0.5,"scale":1.25,"fit":false,"reading_mode":true});
    let mut save = json!({"user_id":11,"document_id":id,"expected_revision":0,"writer_id":"a".repeat(32),"sequence":2,"state":state});
    let result = retained_call(&mut db, "paper.document.reading", save.clone(), 1200).unwrap();
    save["sequence"] = json!(1);
    save["state"]["page"] = json!(1);
    assert_eq!(
        retained_call(&mut db, "paper.document.reading", save.clone(), 1201).unwrap(),
        result
    );
    save["sequence"] = json!(2);
    assert_eq!(
        retained_call(&mut db, "paper.document.reading", save.clone(), 1202).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    save["writer_id"] = json!("b".repeat(32));
    save["expected_revision"] = json!(1);
    save["sequence"] = json!(1.0);
    assert_eq!(
        retained_call(&mut db, "paper.document.reading", save.clone(), 1203).unwrap()["revision"],
        2
    );
    save["writer_id"] = json!("a".repeat(32));
    save["expected_revision"] = json!(0);
    save["sequence"] = json!(3);
    assert_eq!(
        retained_call(&mut db, "paper.document.reading", save.clone(), 1204).unwrap_err(),
        SemanticExecutionError::Conflict
    );
    save["state"]["scale"] = json!(0.1);
    assert_eq!(
        retained_call(&mut db, "paper.document.reading", save, 1205).unwrap_err(),
        SemanticExecutionError::InvalidPayload
    );
}

#[test]
fn saved_document_and_association_counts_do_not_refuse_owned_work() {
    let mut db = simulated_database(prepared_vfs());
    let row = saved_register(&mut db, 11, "main.tex");
    let id = &row["document_id"];
    let missing = json!({"user_id":11,"document_id":"0".repeat(64),"conversation_id":"missing","request_key":"x"});
    assert_eq!(
        retained_call(&mut db, "paper.document.request", missing, 1100).unwrap_err(),
        SemanticExecutionError::NotFound
    );
    for i in 1..256 {
        saved_register(&mut db, 11 + (i % 2) as u64, &format!("{i}.tex"));
    }
    saved_register(&mut db, 12, "extra.tex");
    assert_eq!(saved_register(&mut db, 11, "main.tex")["document_id"], *id);
    for i in 0..1024 {
        retained_call(&mut db,"paper.document.request",json!({"user_id":11,"document_id":id,"conversation_id":format!("chat-{i}"),"request_key":"same"}),1100).unwrap();
    }
    let before = saved_get(&mut db, id, 1101);
    retained_call(&mut db,"paper.document.request",json!({"user_id":11,"document_id":id,"conversation_id":"extra","request_key":"next"}),1102).unwrap();
    assert!(saved_get(&mut db,id,1103)["generation"].as_u64().unwrap() > before["generation"].as_u64().unwrap());
    assert_eq!(retained_call(&mut db,"paper.document.current",json!({"user_id":11,"conversation_id":"extra"}),1104).unwrap()["document_id"], *id);

}

#[test]
fn saved_document_canonical_json_keeps_ascii_escapes_float_spelling_and_byte_bound() {
    let encode = crate::paper_document::python_json;
    let value = json!({"z":"界😀\u{7f}\n","a":[1.0,1e20,1e-6,1e-5,-0.0,1e16]});
    let expected = br#"{"a":[1.0,1e+20,1e-06,1e-05,-0.0,1e+16],"z":"\u754c\ud83d\ude00\u007f\n"}"#;
    assert_eq!(encode(&value, expected.len()).unwrap(), expected);
    assert!(encode(&value, expected.len() - 1).is_err());
}

#[test]
fn saved_document_ir_rejects_query_mutation_and_bounds_maximum_source() {
    let payload = json!({"user_id":11,"source":saved_source("main.tex")});
    let typed = crate::paper_document::Request::parse(
        "paper.document.register",
        payload.as_object().unwrap().clone(),
        1000,
    )
    .unwrap();
    let ir = TransactionIr {
        tenant_id: 7,
        owner_user_id: 11,
        operation: "paper.document.get".to_owned(),
        operation_kind: crate::generated_storage_operations::StorageOperationKind::Query,
        steps: vec![TransactionIrStep::PaperDocument {
            request: typed,
            slot: 0,
        }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects: None,
    };
    assert!(ir.validate().is_err());
    let mut db = simulated_database(prepared_vfs());
    let budget_key = crate::entity::EntityKey::new(
        7,
        crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
        crate::generated_paper_build::DOCUMENT_BUDGET_NAMESPACE,
        b"capacity",
    )
    .unwrap();
    let mut ordinary = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(
        db.entity_get(&mut ordinary, &budget_key)
            .unwrap_err()
            .kind(),
        io::ErrorKind::PermissionDenied
    );
    drop(ordinary);
    let maximal = json!({"source":"local","project_root":"😀".repeat(4096),
        "main_file":"😀".repeat(1024),"path":"\u{1}".repeat(1024),"label":"界".repeat(1024)});
    let row = retained_call(
        &mut db,
        "paper.document.register",
        json!({"user_id":11,"source":maximal}),
        1000,
    )
    .unwrap();
    assert_eq!(
        row["source"]["project_root"]
            .as_str()
            .unwrap()
            .chars()
            .count(),
        4096
    );
    assert!(
        serde_json::to_vec(&row).unwrap().len()
            < crate::generated_paper_build::DOCUMENT_MAX_JSON_BYTES
    );
}

#[test]
fn saved_document_registration_io_faults_keep_binding_record_and_capacity_atomic() {
    let probe = prepared_vfs();
    let mut db = simulated_database(probe.clone());
    probe.arm_fault(None).unwrap();
    saved_register(&mut db, 11, "main.tex");
    let trace = probe.trace().unwrap();
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
        let mut db = simulated_database(vfs.clone());
        vfs.arm_fault(Some(FaultRule {
            operation_number,
            action,
        }))
        .unwrap();
        let _ = retained_call(
            &mut db,
            "paper.document.register",
            json!({"user_id":11,"source":saved_source("main.tex")}),
            1000,
        );
        drop(db);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        db.configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let ids = retained_call(
            &mut db,
            "paper.document.resolve",
            json!({"user_id":11,"sources":[saved_source("main.tex")]}),
            1100,
        )
        .unwrap();
        if !ids[0].is_null() {
            saved_get(&mut db, &ids[0], 1101);
        }
        let row = saved_register(&mut db, 11, "main.tex");
        if !ids[0].is_null() {
            assert_eq!(row["document_id"], ids[0]);
        }
        let mut tx = db.begin_with_paper_document_scope(7, 11).unwrap();
        let key = crate::entity::EntityKey::new(
            7,
            crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            crate::generated_paper_build::DOCUMENT_BUDGET_NAMESPACE,
            b"capacity",
        )
        .unwrap();
        let raw = db.entity_get(&mut tx, &key).unwrap().unwrap();
        assert_eq!(
            u64::from_be_bytes(raw[..8].try_into().unwrap()),
            1,
            "fault {operation_number}"
        );
    }
}

fn saved_fault_seed(
    vfs: std::sync::Arc<DeterministicVfs>,
) -> (AuthorityDatabase, Value, Value, Request) {
    let mut db = simulated_database(vfs);
    let row = saved_register(&mut db, 11, "main.tex");
    let id = &row["document_id"];
    let (old, r1) = saved_fixture(id, '1', b"old");
    retained_upload(&mut db, &old, &r1, b"old");
    let generation = saved_request(&mut db, id, "first", 1100);
    retained_call(
        &mut db,
        "paper.document.finish",
        saved_finish(id, &generation, &r1, &old),
        1101,
    )
    .unwrap();
    let (next, r2) = saved_fixture(id, '2', b"new");
    retained_upload(&mut db, &next, &r2, b"new");
    let generation = saved_request(&mut db, id, "next", 1200);
    let command = uuid::Uuid::new_v4().to_string();
    let finish = request(
        "paper.document.finish",
        11,
        saved_finish(id, &generation, &r2, &next),
        Some(&command),
    );
    (db, old, next, finish)
}
#[test]
fn saved_document_every_publication_io_fault_recovers_terminal_record_and_pin_atomically() {
    let probe = prepared_vfs();
    let (mut db, _, _, finish) = saved_fault_seed(probe.clone());
    probe.arm_fault(None).unwrap();
    execute(&mut db, &finish, 1300).unwrap();
    let trace = probe.trace().unwrap();
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
        let (mut db, old, next, finish) = saved_fault_seed(vfs.clone());
        vfs.arm_fault(Some(FaultRule {
            operation_number,
            action,
        }))
        .unwrap();
        let _ = execute(&mut db, &finish, 1300);
        drop(db);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let row = saved_get(&mut recovered, &old["document_id"], 1400);
        assert_eq!(
            &row["receipt"]["build_id"],
            if row["status"] == "ready" {
                &next["build_id"]
            } else {
                &old["build_id"]
            },
            "fault {operation_number}"
        );
        assert_eq!(
            retained_read(&mut recovered, &old, 1401),
            json!({"data":"b2xk"})
        );
        execute(&mut recovered, &finish, 1402).unwrap();
        assert_eq!(
            saved_get(&mut recovered, &old["document_id"], 1403)["receipt"]["build_id"],
            next["build_id"]
        );
    }
}

#[test]
fn saved_document_real_filesystem_growth_without_registry_quota() {
    fn directory_bytes(path: &std::path::Path) -> u64 {
        std::fs::read_dir(path).unwrap().map(|entry| {
            let path = entry.unwrap().path();
            if path.is_dir() { directory_bytes(&path) } else { path.metadata().unwrap().len() }
        }).sum()
    }
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let before = directory_bytes(directory.path());
    let started = std::time::Instant::now();
    let mut ids = Vec::new();
    for index in 0..256 {
        let row = saved_register(&mut db, 11, &format!("measured-{index}.tex"));
        ids.push(row["document_id"].as_str().unwrap().to_owned());
    }
    let creation_ms = started.elapsed().as_secs_f64() * 1000.0;
    drop(db);
    let growth = directory_bytes(directory.path()).checked_sub(before).unwrap();
    let mut recovered = AuthorityDatabase::open(directory.path()).unwrap();
    for id in ids { assert_eq!(saved_get(&mut recovered, &json!(id), 2000)["document_id"], id); }
    eprintln!("PAPER_GROWTH_MEASURE documents=256 creation_ms={creation_ms:.2} directory_growth_bytes={growth}");
}

#[test]
fn paper_document_resolve_preserves_complete_order_beyond_old_64_limit() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut sources = Vec::new();
    let mut identities = Vec::new();
    for index in 0..71 {
        let source = saved_source(&format!("main{index}.tex"));
        let identity = retained_call(&mut db, "paper.document.register",
            json!({"user_id":11,"source":source.clone()}), 1000).unwrap();
        sources.push(source);
        identities.push(identity["document_id"].clone());
    }
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    let result = retained_call(&mut db, "paper.document.resolve",
        json!({"user_id":11,"sources":sources.clone()}), 1001).unwrap();
    assert_eq!(result, json!(identities));
    let foreign = retained_call(&mut db, "paper.document.resolve",
        json!({"user_id":12,"sources":sources}), 1001).unwrap();
    assert_eq!(foreign, json!(vec![Value::Null; 71]));
}
