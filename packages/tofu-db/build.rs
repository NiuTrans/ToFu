// Embed retained source identity, not deployment-time Git metadata. Framing and
// logical paths must match serverctl_pkg.native_engine.source_fingerprint.
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    env, fs,
    path::{Component, Path, PathBuf},
};

fn collect(directory: &Path, rust_only: bool, paths: &mut Vec<PathBuf>) {
    if !directory.is_dir() {
        return;
    }
    println!("cargo:rerun-if-changed={}", directory.display());
    for entry in fs::read_dir(directory).expect("read retained sources") {
        let path = entry.expect("source entry").path();
        if path.is_dir() {
            collect(&path, rust_only, paths);
        } else if !rust_only || path.extension().is_some_and(|ext| ext == "rs") {
            paths.push(path);
        }
    }
}

struct Manifest {
    path_deps: Vec<PathBuf>,
    include: Option<Vec<String>>,
    publish_all: bool,
    has_workspace: bool,
    package_workspace: bool,
}

fn parse_manifest(manifest_path: &Path) -> Manifest {
    let document: toml::Table = fs::read_to_string(manifest_path).expect("read source manifest")
        .parse().expect("parse source manifest");
    let package = document.get("package").and_then(toml::Value::as_table).expect("source package");
    assert!(package.get("name").and_then(toml::Value::as_str).is_some_and(|n| !n.is_empty()), "source manifest names no package");
    let mut paths = Vec::new();
    dependencies(&document, &mut paths);
    if let Some(targets) = document.get("target").and_then(toml::Value::as_table) {
        for target in targets.values().filter_map(toml::Value::as_table) { dependencies(target, &mut paths); }
    }
    Manifest {
        path_deps: paths.into_iter().map(|p| manifest_path.parent().unwrap().join(p)).collect(),
        include: package.get("include").and_then(toml::Value::as_array).filter(|a| !a.is_empty())
            .map(|a| a.iter().map(|v| v.as_str().expect("include string").to_owned()).collect()),
        publish_all: package.get("publish").and_then(toml::Value::as_bool) != Some(false),
        has_workspace: document.contains_key("workspace"),
        package_workspace: package.get("workspace").and_then(toml::Value::as_str).is_some(),
    }
}
fn crate_source_paths(crate_root: &Path, manifest: &Manifest) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if manifest.publish_all {
        if let Some(include) = &manifest.include {
            for pattern in include {
                let candidate = crate_root.join(pattern.trim_end_matches('/'));
                if candidate.is_file() {
                    paths.push(candidate);
                }
            }
            for name in ["Cargo.toml", "Cargo.toml.orig", "Cargo.lock"] {
                let candidate = crate_root.join(name);
                if candidate.is_file() && !paths.contains(&candidate) {
                    paths.push(candidate);
                }
            }
        } else {
            for name in ["Cargo.toml", "Cargo.toml.orig", "Cargo.lock", "build.rs"] {
                let candidate = crate_root.join(name);
                if candidate.is_file() {
                    paths.push(candidate);
                }
            }
            collect_rs(&crate_root.join("src"), &mut paths);
        }
    } else {
        for name in ["Cargo.toml", "Cargo.toml.orig", "Cargo.lock", "build.rs"] {
            let candidate = crate_root.join(name);
            if candidate.is_file() {
                paths.push(candidate);
            }
        }
        collect_rs(&crate_root.join("src"), &mut paths);
    }
    // .cargo/config.toml configures the build but is never a packaged input.
    collect_all(&crate_root.join(".cargo"), &mut paths);
    paths
}

fn collect_all(directory: &Path, paths: &mut Vec<PathBuf>) {
    if !directory.is_dir() {
        return;
    }
    for entry in fs::read_dir(directory).expect("read .cargo configuration") {
        let path = entry.expect(".cargo entry").path();
        if path.is_dir() {
            collect_all(&path, paths);
        } else {
            paths.push(path);
        }
    }
}

fn collect_rs(directory: &Path, paths: &mut Vec<PathBuf>) { collect(directory, true, paths); }

fn normalized(path: &Path) -> PathBuf {
    let mut result = PathBuf::new();
    for part in path.components() {
        match part {
            Component::ParentDir => {
                result.pop();
            }
            Component::CurDir => {}
            Component::Normal(name) => result.push(name),
            _ => panic!("source dependency must use a relative path"),
        }
    }
    result
}

fn dependencies(table: &toml::Table, paths: &mut Vec<String>) {
    for section in ["dependencies", "build-dependencies", "dev-dependencies"] {
        if let Some(entries) = table.get(section).and_then(toml::Value::as_table) {
            for entry in entries.values() {
                if let Some(path) = entry
                    .as_table()
                    .and_then(|e| e.get("path"))
                    .and_then(toml::Value::as_str)
                {
                    paths.push(path.to_owned());
                }
            }
        }
    }
}

fn closure(crate_dir: &Path, relative: PathBuf, crates: &mut BTreeMap<PathBuf, PathBuf>) {
    let location = crate_dir.canonicalize().expect("resolve source crate");
    if crates.contains_key(&location) {
        return;
    }
    let manifest: toml::Table = fs::read_to_string(location.join("Cargo.toml"))
        .expect("read source manifest")
        .parse()
        .expect("parse source manifest");
    assert!(
        manifest
            .get("package")
            .and_then(toml::Value::as_table)
            .and_then(|p| p.get("name"))
            .and_then(toml::Value::as_str)
            .is_some_and(|name| !name.is_empty()),
        "source manifest names no package"
    );
    crates.insert(location.clone(), relative.clone());
    let mut paths = Vec::new();
    dependencies(&manifest, &mut paths);
    if let Some(targets) = manifest.get("target").and_then(toml::Value::as_table) {
        for target in targets.values().filter_map(toml::Value::as_table) {
            dependencies(target, &mut paths);
        }
    }
    for path in paths {
        let dependency = location
            .join(&path)
            .canonicalize()
            .expect("resolve path dependency");
        assert!(
            dependency.starts_with(location.parent().unwrap()),
            "path dependency escapes packages directory"
        );
        closure(&dependency, normalized(&relative.join(path)), crates);
    }
}

fn main() {
    // Late allocator TLS destructors retain executable code through teardown.
    if env::var_os("CARGO_FEATURE_EMBEDDED_MIMALLOC").is_some()
        && env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("linux")
    {
        println!("cargo:rustc-link-arg-cdylib=-Wl,-z,nodelete");
    }
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let mut crates = BTreeMap::new();
    closure(&root, PathBuf::new(), &mut crates);
    let mut entries = BTreeMap::new();
    for (location, relative) in crates {
        let manifest = parse_manifest(&location.join("Cargo.toml"));
        let paths = crate_source_paths(&location, &manifest);
        println!("cargo:rerun-if-changed={}", location.join("src").display());
        println!("cargo:rerun-if-changed={}", location.join(".cargo").display());
        let prefix = if relative.as_os_str().is_empty() {
            PathBuf::from("tofu-db")
        } else {
            relative
        };
        for path in paths {
            let key = prefix
                .join(path.strip_prefix(&location).unwrap())
                .to_str()
                .expect("UTF-8 source path")
                .replace('\\', "/");
            assert!(
                entries.insert(key, path).is_none(),
                "source identity path collision"
            );
        }
    }
    let manifest: toml::Table = fs::read_to_string(root.join("Cargo.toml"))
        .expect("read source manifest").parse().expect("parse source manifest");
    if let Some(values) = manifest.get("package").and_then(|v| v.get("metadata"))
        .and_then(|v| v.get("tofu")).and_then(|v| v.get("external-inputs")) {
        let project = root.parent().unwrap().parent().unwrap().canonicalize().unwrap();
        for value in values.as_array().expect("external input path list") {
            let name = value.as_str().expect("external input relative path");
            let path = Path::new(name);
            assert!(path.components().all(|c| matches!(c, Component::Normal(_)))
                && (name.starts_with("contracts/") || name == "VERSION"),
                "external input escapes declared source namespace");
            let file = project.join(path);
            assert_eq!(file.canonicalize().expect("resolve external input"), file,
                "external input uses a symlink");
            assert!(entries.insert(format!("@external/{name}"), file).is_none(),
                "external input paths collide");
        }
    }
    let mut digest = Sha256::new();
    digest.update(b"tofu-db-source-v2\0");
    for (key, path) in entries {
        let bytes = fs::read(&path).expect("read engine build input");
        digest.update(key.as_bytes());
        digest.update([0]);
        digest.update((bytes.len() as u64).to_le_bytes());
        digest.update(bytes);
        println!("cargo:rerun-if-changed={}", path.display());
    }
    println!(
        "cargo:rustc-env=TOFUDB_SOURCE_SHA256={:x}",
        digest.finalize()
    );
}
