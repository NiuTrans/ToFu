use super::*;
use std::path::{Path, PathBuf};

use crate::semantic_executor::map_storage_error;

const LABELS: [(Reason, &str); REASONS] = [
    (Reason::ArchiveOwner, "raw_archive_owner_capacity"),
    (Reason::ArchiveConversation, "raw_archive_conversation_capacity"),
    (Reason::ArchiveRestoreBudget, "raw_archive_restore_byte_budget"),
    (Reason::DomainBound, "domain_resource_bound"),
    (Reason::CapacityQuota, "capacity_quota"),
    (Reason::AccountingOverflow, "request_accounting_overflow"),
    (Reason::ReservationFailure, "allocation_reservation_failure"),
    (Reason::OsMemoryFailure, "os_memory_failure"),
    (Reason::Other, "other"),
];

fn reservation() -> TryReserveError {
    Vec::<u8>::new().try_reserve(usize::MAX).unwrap_err()
}

#[test]
fn reason_labels_follow_the_generated_contract_order() {
    for (reason, label) in LABELS {
        assert_eq!(NATIVE_RESOURCE_EXHAUSTION_REASON_NAMES[reason as usize], label);
    }
}

#[test]
fn declared_classes_reach_their_contract_reason() {
    let cases = [
        (raw_archive_owner_capacity("raw archive owner capacity is exhausted"), Reason::ArchiveOwner),
        (raw_archive_conversation_capacity("raw archive conversation capacity is exhausted"),
            Reason::ArchiveConversation),
        (raw_archive_restore_budget("raw archive snapshot exceeds retained-byte budget"),
            Reason::ArchiveRestoreBudget),
        (domain_bound("turn list exceeds 10000 rows"), Reason::DomainBound),
        (capacity_quota("swarm session capacity reached"), Reason::CapacityQuota),
        (accounting_overflow("storage.v2 frame byte accounting overflow"), Reason::AccountingOverflow),
        (reservation_failure("storage.v2 streamed request allocation failed"),
            Reason::ReservationFailure),
        (reservation_error(reservation()), Reason::ReservationFailure),
    ];
    for (error, expected) in cases {
        assert!(is_refusal(&error));
        assert_eq!(classify(&error), expected);
    }
    #[cfg(unix)]
    assert_eq!(classify(&io::Error::from_raw_os_error(12)), Reason::OsMemoryFailure);
    // Neither a bare kind nor text equal to a declared literal carries a class.
    assert_eq!(classify(&io::Error::from(io::ErrorKind::OutOfMemory)), Reason::Other);
    assert_eq!(classify(&io::Error::new(io::ErrorKind::OutOfMemory, "turn list exceeds 10000 rows")),
        Reason::Other);
}

#[test]
fn retryable_table_marks_only_the_domain_bound_deterministic() {
    assert_eq!(NATIVE_RESOURCE_EXHAUSTION_REASON_RETRYABLE,
        [true, true, true, false, true, true, true, true, true]);
}

#[test]
fn a_domain_bound_is_refused_once_with_its_literal_and_never_retried() {
    for bound in ["swarm response exceeds its aggregate bound", "conversation sync page exceeds 8 MiB",
        "turn list exceeds 10000 rows", "versioned document response exceeds 8 MiB",
        "Turn event patch exceeds its bound"] {
        let error = map_storage_error(domain_bound(bound));
        assert_eq!(error.code(), "database_resource_exhausted");
        assert!(!error.retryable(), "{bound}");
        assert_eq!(error, SemanticExecutionError::ResourceBound(BoundRefusal(bound)));
        assert_eq!(error.to_string(), "database_resource_exhausted");
    }
    let before = resource_exhaustion_snapshot().counters[Reason::DomainBound as usize].count;
    let error = semantic_bound("billing ledger list scan exceeds its row bound");
    assert_eq!(error.code(), "database_resource_exhausted");
    assert!(!error.retryable());
    assert!(resource_exhaustion_snapshot().counters[Reason::DomainBound as usize].count > before);
}

#[test]
fn transient_capacity_and_memory_refusals_stay_retryable() {
    let transient = [
        capacity_quota("swarm session capacity reached"),
        capacity_quota("swarm agent capacity reached"),
        raw_archive_owner_capacity("raw archive owner capacity is exhausted"),
        raw_archive_conversation_capacity("raw archive conversation capacity is exhausted"),
        raw_archive_restore_budget("raw archive snapshot exceeds retained-byte budget"),
        accounting_overflow("storage.v2 frame byte accounting overflow"),
        reservation_failure("storage.v2 streamed request allocation failed"),
        reservation_error(reservation()),
        io::Error::from(io::ErrorKind::OutOfMemory),
    ];
    for error in transient {
        let mapped = map_storage_error(error);
        assert_eq!(mapped.code(), "database_resource_exhausted");
        assert!(mapped.retryable());
        assert!(matches!(mapped, SemanticExecutionError::ResourceExhausted(_)));
    }
    let payload = map_storage_error(io::Error::new(
        io::ErrorKind::InvalidInput, "storage_payload_too_large: retained existing priority"));
    assert_eq!(payload.code(), "storage_payload_too_large");
    assert!(!payload.retryable());
    assert!(semantic_refusal(&io::Error::from(io::ErrorKind::InvalidInput)).is_none());
}

#[test]
fn snapshot_exports_fixed_labels_and_counts_never_message_text() {
    let counters = [const { AtomicU64::new(0) }; REASONS];
    for error in [io::Error::new(io::ErrorKind::OutOfMemory, "/private/user-key payload-secret"),
        io::Error::new(io::ErrorKind::OutOfMemory, "secret-body".repeat(1024)),
        domain_bound("declared secret-free literal"), capacity_quota("declared quota literal")] {
        increment(&counters, classify(&error));
    }
    let encoded = serde_json::to_string(&snapshot(&counters)).unwrap();
    for text in ["private", "secret", "declared", "literal"] {
        assert!(!encoded.contains(text), "{text}");
    }
    let snapshot = snapshot(&counters);
    assert_eq!(snapshot.counters[Reason::Other as usize].count, 2);
    assert_eq!(snapshot.counters[Reason::DomainBound as usize].count, 1);
    assert_eq!(snapshot.counters[Reason::CapacityQuota as usize].count, 1);
}

fn request(operation: &str, payload: serde_json::Value, command_id: Option<&str>)
    -> crate::protocol::Request {
    crate::protocol::Request {
        correlation_id: [3; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: 11,
        tenant_id: Some(7),
        command_id: command_id.map(str::to_owned),
        schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        operation: operation.to_owned(),
        payload: serde_json::to_vec(&payload).unwrap(),
    }
}

fn execute(database: &mut crate::authority::AuthorityDatabase, request: &crate::protocol::Request,
    now_unix_ms: u64) -> Result<Vec<u8>, SemanticExecutionError> {
    use crate::semantic::{admit_request, AuthenticatedScope};
    let negotiated = crate::protocol::NegotiatedProtocol {
        protocol_version: crate::protocol::PROTOCOL_VERSION,
        schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
    };
    let scope = AuthenticatedScope { owner_id: request.owner_id, tenant_id: request.tenant_id };
    let admitted = admit_request(request, negotiated, scope, now_unix_ms).unwrap();
    crate::semantic_executor::execute_admitted_request(database, admitted, now_unix_ms)
}

// The real refusal sites: an unwindowed `conversation.get` and a
// `conversation.list` over one oversized transcript must classify as
// domain_resource_bound and therefore be non-retryable on the wire.
#[test]
fn oversized_transcript_reads_refuse_as_a_non_retryable_domain_bound() {
    use serde_json::json;

    let directory = tempfile::tempdir().unwrap();
    let mut database = crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap();
    execute(&mut database, &request("conversation.create", json!({
        "conv_id": "oversized", "user_id": 11,
    }), Some("oversized-create")), 1_000).unwrap();
    let chunk = "x".repeat(3_000_000);
    let turn_count = crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES / 3_000_000 + 1;
    for ordinal in 0..turn_count as u64 {
        let command_id = format!("oversized-append-{ordinal}");
        execute(&mut database, &request("turn.append_settled", json!({
            "conversation_id": "oversized",
            "user_id": 11,
            "command_id": command_id,
            "actor": "assistant",
            "projection": {"content": chunk},
            "turn_id": format!("oversized-turn-{ordinal}"),
        }), Some(&command_id)), 1_001 + ordinal).unwrap();
    }
    let before = resource_exhaustion_snapshot().counters[Reason::DomainBound as usize].count;

    let refused = execute(&mut database, &request("conversation.get",
        json!({"conv_id": "oversized", "user_id": 11}), None), 2_000).unwrap_err();
    assert_eq!(refused.code(), "database_resource_exhausted");
    assert!(!refused.retryable());

    let refused = execute(&mut database, &request("conversation.list",
        json!({"user_id": 11, "ids": ["oversized"], "include_messages": true}), None), 2_001)
        .unwrap_err();
    assert_eq!(refused.code(), "database_resource_exhausted");
    assert!(!refused.retryable());
    assert!(resource_exhaustion_snapshot().counters[Reason::DomainBound as usize].count > before + 1);

    // A bounded window of the same transcript still reads.
    execute(&mut database, &request("conversation.get",
        json!({"conv_id": "oversized", "user_id": 11, "message_window": 2}), None), 2_002).unwrap();
}

// A semantic scan bound refuses before any read; it used to be emitted as
// retryable load shedding, so the client replayed a request that can never fit.
#[test]
fn a_list_offset_beyond_its_scan_bound_is_a_non_retryable_bound() {
    use serde_json::json;

    let directory = tempfile::tempdir().unwrap();
    let mut database = crate::authority::AuthorityDatabase::initialize(directory.path()).unwrap();
    let scan_rows = crate::generated_tofudb_ir::MAX_BILLING_LIST_SCAN_ROWS;
    let refused = execute(&mut database, &request("billing.ledger.list",
        json!({"user_id": "billing-user", "limit": 1, "offset": scan_rows, "kinds": []}), None),
        1_000).unwrap_err();
    assert_eq!(refused,
        SemanticExecutionError::ResourceBound(BoundRefusal("billing ledger list scan exceeds its row bound")));
    assert!(!refused.retryable());
    let within = execute(&mut database, &request("billing.ledger.list",
        json!({"user_id": "billing-user", "limit": 1, "offset": scan_rows - 1, "kinds": []}),
        None), 1_001).unwrap();
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&within).unwrap(), json!([]));
}

#[test]
fn concurrent_snapshot_stays_fixed_and_counts_saturate() {
    let counters = std::sync::Arc::new([const { AtomicU64::new(0) }; REASONS]);
    let mut writers = Vec::new();
    for _ in 0..4 {
        let counters = counters.clone();
        writers.push(std::thread::spawn(move || {
            for _ in 0..1000 { increment(&counters, Reason::ArchiveConversation); }
        }));
    }
    for _ in 0..100 {
        let snap = snapshot(&counters);
        assert_eq!(snap.fixed_counter_bytes, 72);
        assert_eq!(snap.counters.len(), 9);
        assert!(snap.counters[Reason::ArchiveConversation as usize].count <= 4000);
    }
    for writer in writers { writer.join().unwrap(); }
    assert_eq!(snapshot(&counters).counters[Reason::ArchiveConversation as usize].count, 4000);
    counters[Reason::Other as usize].store(u64::MAX, Ordering::Relaxed);
    increment(&counters, Reason::Other);
    assert_eq!(snapshot(&counters).counters[Reason::Other as usize].count, u64::MAX);
}

// ── Construction gate ────────────────────────────────────────────────────

/// Source text with comments and string/char literals blanked. Newlines
/// survive, so line numbers stay exact.
fn blank_comments_and_literals(source: &str) -> String {
    let chars: Vec<char> = source.chars().collect();
    let mut output = String::with_capacity(source.len());
    let blank = |output: &mut String, character: char| {
        output.push(if character == '\n' { '\n' } else { ' ' });
    };
    let mut index = 0;
    while index < chars.len() {
        let character = chars[index];
        let next = chars.get(index + 1).copied();
        let identifier_before = index > 0
            && (chars[index - 1].is_alphanumeric() || chars[index - 1] == '_');
        if character == '/' && next == Some('/') {
            while index < chars.len() && chars[index] != '\n' {
                blank(&mut output, chars[index]);
                index += 1;
            }
        } else if character == '/' && next == Some('*') {
            let mut depth = 0usize;
            while index < chars.len() {
                if chars[index] == '/' && chars.get(index + 1) == Some(&'*') {
                    depth += 1;
                    output.push_str("  ");
                    index += 2;
                } else if chars[index] == '*' && chars.get(index + 1) == Some(&'/') {
                    depth -= 1;
                    output.push_str("  ");
                    index += 2;
                    if depth == 0 { break; }
                } else {
                    blank(&mut output, chars[index]);
                    index += 1;
                }
            }
        } else if !identifier_before && (character == 'r' || (character == 'b' && next == Some('r')))
            && raw_string_hashes(&chars, index).is_some() {
            let prefix = if character == 'b' { 2 } else { 1 };
            let hashes = raw_string_hashes(&chars, index).unwrap();
            let start = index;
            index += prefix + hashes + 1;
            loop {
                if index >= chars.len() { break; }
                if chars[index] == '"'
                    && (1..=hashes).all(|offset| chars.get(index + offset) == Some(&'#')) {
                    index += hashes + 1;
                    break;
                }
                index += 1;
            }
            for blanked in &chars[start..index.min(chars.len())] { blank(&mut output, *blanked); }
        } else if character == '"' {
            blank(&mut output, character);
            index += 1;
            while index < chars.len() && chars[index] != '"' {
                if chars[index] == '\\' && index + 1 < chars.len() {
                    blank(&mut output, chars[index]);
                    index += 1;
                }
                blank(&mut output, chars[index]);
                index += 1;
            }
            if index < chars.len() { blank(&mut output, chars[index]); index += 1; }
        } else if character == '\'' && (next == Some('\\') || chars.get(index + 2) == Some(&'\'')) {
            // A char literal; a lifetime or label has no closing quote.
            let start = index;
            index += 1;
            if chars[index] == '\\' { index += 2; } else { index += 1; }
            while index < chars.len() && chars[index] != '\'' { index += 1; }
            index += 1;
            for blanked in &chars[start..index.min(chars.len())] { blank(&mut output, *blanked); }
        } else {
            output.push(character);
            index += 1;
        }
    }
    output
}

/// The `#` count of a raw string literal starting at `index` (`r"`, `r#"`, `br"`).
fn raw_string_hashes(chars: &[char], index: usize) -> Option<usize> {
    let mut cursor = index + if chars[index] == 'b' { 2 } else { 1 };
    let mut hashes = 0;
    while chars.get(cursor) == Some(&'#') { hashes += 1; cursor += 1; }
    (chars.get(cursor) == Some(&'"')).then_some(hashes)
}

/// Blank every item attributed `#[cfg(test)]` (a test module, helper or
/// declaration) in comment- and literal-free code.
fn blank_test_items(code: &str) -> String {
    let mut chars: Vec<char> = code.chars().collect();
    let marker: Vec<char> = "#[cfg(test)]".chars().collect();
    let mut index = 0;
    while index + marker.len() <= chars.len() {
        if chars[index..index + marker.len()] != marker[..] {
            index += 1;
            continue;
        }
        let start = index;
        let mut cursor = index + marker.len();
        let mut depth = 0usize;
        while cursor < chars.len() {
            match chars[cursor] {
                '(' | '[' => depth += 1,
                ')' | ']' => depth = depth.saturating_sub(1),
                ';' if depth == 0 => { cursor += 1; break; }
                '{' if depth == 0 => {
                    let mut braces = 0usize;
                    while cursor < chars.len() {
                        match chars[cursor] {
                            '{' => braces += 1,
                            '}' => {
                                braces -= 1;
                                if braces == 0 { break; }
                            }
                            _ => {}
                        }
                        cursor += 1;
                    }
                    cursor += 1;
                    break;
                }
                _ => {}
            }
            cursor += 1;
        }
        let end = cursor.min(chars.len());
        for character in &mut chars[start..end] {
            if *character != '\n' { *character = ' '; }
        }
        index = cursor;
    }
    chars.into_iter().collect()
}

/// 1-based lines of production code that name the class-free kind.
fn raw_refusal_lines(source: &str) -> Vec<usize> {
    blank_test_items(&blank_comments_and_literals(source))
        .lines()
        .enumerate()
        .filter(|(_, line)| line.contains("OutOfMemory"))
        .map(|(index, _)| index + 1)
        .collect()
}

fn is_test_only_source(path: &Path) -> bool {
    let name = path.file_name().and_then(|name| name.to_str()).unwrap_or_default();
    name == "tests.rs" || name.ends_with("_tests.rs") || name.starts_with("tests_")
        || name.starts_with("test_") || name.ends_with("_test_support.rs")
}

fn production_sources(directory: &Path, sources: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(directory).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            production_sources(&path, sources);
        } else if path.extension().is_some_and(|extension| extension == "rs")
            && !is_test_only_source(&path) {
            sources.push(path);
        }
    }
}

#[test]
fn every_resource_refusal_declares_its_class() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let owner = root.join("resource_exhaustion.rs");
    let mut sources = Vec::new();
    production_sources(&root, &mut sources);
    assert!(sources.len() > 200 && sources.contains(&owner), "the gate must scan the engine sources");
    let mut offenders = Vec::new();
    for path in sources.iter().filter(|path| **path != owner) {
        let source = std::fs::read_to_string(path).unwrap();
        for line in raw_refusal_lines(&source) {
            offenders.push(format!("{}:{line}", path.strip_prefix(&root).unwrap().display()));
        }
    }
    assert!(offenders.is_empty(),
        "construct resource refusals with crate::resource_exhaustion::{{domain_bound, \
         capacity_quota, accounting_overflow, reservation_failure, reservation_error}} and test \
         them with is_refusal: a raw OutOfMemory carries no class and is replayed as load \
         shedding:\n{}", offenders.join("\n"));
}

#[test]
fn the_construction_gate_sees_code_and_ignores_comments_literals_and_tests() {
    let constructed = "fn f() -> io::Error {\n    io::Error::new(io::ErrorKind::OutOfMemory, \"x\")\n}\n";
    assert_eq!(raw_refusal_lines(constructed), vec![2]);
    assert_eq!(raw_refusal_lines("use std::io::ErrorKind::OutOfMemory;\n"), vec![1]);
    assert_eq!(raw_refusal_lines("if error.kind() == io::ErrorKind::OutOfMemory {}\n"), vec![1]);
    let ignored = concat!(
        "// io::ErrorKind::OutOfMemory in a line comment\n",
        "/* io::ErrorKind::OutOfMemory /* nested */ still a comment */\n",
        "const A: &str = \"io::ErrorKind::OutOfMemory \\\" escaped\";\n",
        "const B: &str = r#\"OutOfMemory \"quoted\" OutOfMemory\"#;\n",
        "const C: &[u8] = br\"OutOfMemory\";\n",
        "const D: char = '\\'';\n",
        "const E: char = '\"';\n",
        "fn lifetime<'a>(value: &'a str) -> &'a str { value }\n",
        "#[cfg(test)]\nmod tests {\n    fn t() { let _ = std::io::ErrorKind::OutOfMemory; }\n}\n",
        "#[cfg(test)]\n#[path = \"x_tests.rs\"]\nmod more;\n",
    );
    assert!(raw_refusal_lines(ignored).is_empty(), "{:?}", raw_refusal_lines(ignored));
    let after = "#[cfg(test)]\nfn helper() {}\nfn f() { let _ = io::ErrorKind::OutOfMemory; }\n";
    assert_eq!(raw_refusal_lines(after), vec![3]);
    assert!(is_test_only_source(Path::new("src/turn/model_context/tests.rs")));
    assert!(is_test_only_source(Path::new("src/resource_exhaustion_tests.rs")));
    assert!(!is_test_only_source(Path::new("src/turn.rs")));
}
