//! Faults are checked before dlopen; an installed fixture checks code lifetime.
use super::*;
use serde_json::json;
use std::os::unix::fs::symlink;
use tempfile::tempdir;

fn digest(bytes: &[u8]) -> String {
    digest_hex(&Sha256::digest(bytes))
}

fn write_manifest(root: &Path, libraries: serde_json::Value) -> (String, u64) {
    let bytes = serde_json::to_vec(
        &json!({"format": contract_value("format"), "registry_version": contract_value("registry_version"), "libraries": libraries}),
    )
    .unwrap();
    std::fs::write(root.join("grammars.json"), &bytes).unwrap();
    (digest(&bytes), bytes.len() as u64)
}

#[test]
fn sealed_image_keeps_original_bytes_and_cannot_be_modified() {
    let directory = tempdir().unwrap();
    let source = directory.path().join("input");
    let bytes = vec![23; 200_000];
    std::fs::write(&source, &bytes).unwrap();
    let mut image = sealed_file(&source, bytes.len() as u64, &digest(&bytes)).unwrap();
    std::fs::write(&source, b"replacement").unwrap();
    let mut actual = Vec::new();
    image.read_to_end(&mut actual).unwrap();
    assert_eq!(actual, bytes);
    assert!(image.write_all(b"changed").is_err());
    assert!(image.set_len(0).is_err());
}

#[test]
fn declared_length_hash_and_regular_file_are_required() {
    let directory = tempdir().unwrap();
    let source = directory.path().join("input");
    std::fs::write(&source, b"original").unwrap();
    for (size, hash) in [
        (7, digest(b"original")),
        (9, digest(b"original")),
        (8, digest(b"foreign")),
    ] {
        assert!(matches!(
            sealed_file(&source, size, &hash),
            Err(GrammarError::Admission)
        ));
    }
    let link = directory.path().join("link");
    symlink(&source, &link).unwrap();
    assert!(matches!(
        sealed_file(&link, 8, &digest(b"original")),
        Err(GrammarError::Admission)
    ));
    assert!(matches!(
        sealed_file(directory.path(), 8, &digest(b"original")),
        Err(GrammarError::Admission)
    ));
}

#[test]
fn fifo_refuses_without_waiting_for_a_writer() {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let directory = tempdir().unwrap();
    let source = directory.path().join("fifo");
    let name = CString::new(source.as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    assert!(matches!(
        sealed_file(&source, 8, &digest(b"original")),
        Err(GrammarError::Admission)
    ));
}

#[test]
fn manifest_bounds_pins_names_and_duplicate_entries_refuse() {
    let directory = tempdir().unwrap();
    let root = directory.path();
    let good = json!({"file":"libtree_sitter_rust.so","bytes":8,"sha256":digest(b"original")});
    for entries in [
        json!([good.clone(), good.clone()]),
        json!([{"file":"../outside","bytes":8,"sha256":digest(b"original")}]),
        json!([{"file":"libtree_sitter_rust.so","bytes":0,"sha256":digest(b"original")}]),
        json!([{"file":"libtree_sitter_rust.so","bytes":8,"sha256":"bad"}]),
    ] {
        let (hash, size) = write_manifest(root, entries);
        assert!(matches!(
            GrammarBundle::open(root, &hash, size, 4096),
            Err(GrammarError::Admission)
        ));
    }
    let (hash, size) = write_manifest(root, json!([good]));
    assert!(matches!(
        GrammarBundle::open(root, &hash, size, size as usize - 1),
        Err(GrammarError::Admission)
    ));
    assert!(matches!(
        GrammarBundle::open(root, &digest(b"other"), size, 4096),
        Err(GrammarError::Admission)
    ));
    let bundle = GrammarBundle::open(root, &hash, size, 4096).unwrap();
    assert!(matches!(
        bundle.language("typescript"),
        Err(GrammarError::Unavailable)
    ));
    assert!(matches!(
        bundle.language("rust"),
        Err(GrammarError::Admission)
    ));
}

#[test]
fn unlisted_mutable_library_cannot_be_executed() {
    let directory = tempdir().unwrap();
    let root = directory.path();
    let (hash, size) = write_manifest(root, json!([]));
    let bundle = GrammarBundle::open(root, &hash, size, 4096).unwrap();
    std::fs::write(root.join("libtree_sitter_rust.so"), b"not authorized").unwrap();
    assert!(matches!(
        bundle.language("rust"),
        Err(GrammarError::Unavailable)
    ));
    // Alias resolution stays upstream-owned even when there is no admitted code.
    assert!(matches!(
        bundle.language("shell"),
        Err(GrammarError::Unavailable)
    ));
}

#[test]
#[ignore = "requires TOFU_NATIVE_GRAMMAR_FIXTURE pointing to a trusted Rust grammar"]
fn loaded_language_survives_authority_drop_and_source_replacement() {
    let source = std::env::var_os("TOFU_NATIVE_GRAMMAR_FIXTURE")
        .expect("explicit installed fixture required");
    let mut bytes = std::fs::read(source).unwrap();
    // ELF permits an unused trailing suffix; isolate this test from other cache keys.
    bytes.extend_from_slice(b"tofu-sealed-lifetime-fixture");
    let directory = tempdir().unwrap();
    let root = directory.path();
    let path = root.join("libtree_sitter_rust.so");
    std::fs::write(&path, &bytes).unwrap();
    let (hash, size) = write_manifest(
        root,
        json!([{"file":"libtree_sitter_rust.so","bytes":bytes.len(),"sha256":digest(&bytes)}]),
    );
    let bundle = GrammarBundle::open(root, &hash, size, 4096).unwrap();
    let grammar = bundle.language("rust").unwrap();
    std::fs::write(&path, b"replaced after admission").unwrap();
    assert_eq!(
        bundle.language("rust").unwrap().abi_version(),
        grammar.abi_version()
    );
    drop(bundle);
    drop(directory);
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&grammar).unwrap();
    let tree = parser.parse("pub fn retained() {}", None).unwrap();
    assert!(!tree.root_node().has_error());
    assert!(tree.root_node().to_sexp().contains("function_item"));
}

#[test]
fn null_pointer_and_incompatible_abi_refuse_before_parser_creation() {
    for (definition, expected) in [
        ("const void *tree_sitter_probe(void) { return 0; }", GrammarError::Admission),
        ("static const unsigned int language[1024] = {999}; const void *tree_sitter_probe(void) { return language; }", GrammarError::Abi),
    ] {
        let directory = tempdir().unwrap();let root = directory.path();
        let source = root.join("probe.c");std::fs::write(&source, definition).unwrap();
        let code = root.join("libtree_sitter_probe.so");
        let status = std::process::Command::new("cc").args(["-shared", "-fPIC", "-o"])
            .arg(&code).arg(&source).status().unwrap();assert!(status.success());
        let bytes = std::fs::read(&code).unwrap();
        let (hash, size) = write_manifest(root, json!([{"file":"libtree_sitter_probe.so","bytes":bytes.len(),"sha256":digest(&bytes)}]));
        let bundle = GrammarBundle::open(root, &hash, size, 4096).unwrap();
        assert_eq!(bundle.language("probe").err(), Some(expected));
    }
}
