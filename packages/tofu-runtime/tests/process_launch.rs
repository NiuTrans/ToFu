#![cfg(unix)]
use std::collections::BTreeMap;
use std::path::PathBuf;
use tofu_runtime::process_launch::{Launch, LaunchError};

#[test]
fn real_child_receives_only_explicit_mapping_and_arguments() {
    let environment = BTreeMap::from([("ONLY_DECLARED".into(), "literal $HOME `id`".into())]);
    let launch =
        Launch::validate("/usr/bin/env", vec![], PathBuf::from("/tmp"), environment).unwrap();
    let output = launch.command().output().unwrap();
    assert!(output.status.success());
    assert_eq!(output.stdout, b"ONLY_DECLARED=literal $HOME `id`\n");
    assert!(output.stderr.is_empty());
    let launch = Launch::validate(
        "/bin/echo",
        vec!["$(id); *".into()],
        PathBuf::from("/tmp"),
        BTreeMap::new(),
    )
    .unwrap();
    assert_eq!(launch.command().output().unwrap().stdout, b"$(id); *\n");
}
#[test]
fn resolution_uses_child_path_and_working_directory() {
    let env = BTreeMap::from([("PATH".into(), "bin".into())]);
    let launch = Launch::validate("sh", vec![], PathBuf::from("/"), env).unwrap();
    assert_eq!(launch.executable(), std::path::Path::new("/bin/sh"));
    let launch = Launch::validate("bin/sh", vec![], PathBuf::from("/"), BTreeMap::new()).unwrap();
    assert_eq!(launch.executable(), std::path::Path::new("/bin/sh"));
    let env = BTreeMap::from([("PATH".into(), "/definitely-missing-tofu-bin".into())]);
    assert!(matches!(
        Launch::validate("sh", vec![], PathBuf::from("/"), env),
        Err(LaunchError::ExecutableUnavailable)
    ));
    let launch = Launch::validate("pwd", vec![], PathBuf::from("/tmp"), BTreeMap::new()).unwrap();
    assert_eq!(launch.command().output().unwrap().stdout, b"/tmp\n");
}
#[test]
fn rejects_bad_inputs_without_exposing_values() {
    for key in ["", "A=B", "A\0B"] {
        let env = BTreeMap::from([(key.into(), "private-secret".into())]);
        assert!(matches!(
            Launch::validate("sh", vec![], PathBuf::from("/"), env),
            Err(LaunchError::InvalidEnvironmentName)
        ));
    }
    let env = BTreeMap::from([("KEY".into(), "private\0secret".into())]);
    assert!(matches!(
        Launch::validate("sh", vec![], PathBuf::from("/"), env),
        Err(LaunchError::InvalidEnvironmentValue)
    ));
    assert!(matches!(
        Launch::validate(
            "sh",
            vec!["a\0b".into()],
            PathBuf::from("/"),
            BTreeMap::new()
        ),
        Err(LaunchError::InvalidArgument)
    ));
    assert!(matches!(
        Launch::validate("sh", vec![], PathBuf::from("."), BTreeMap::new()),
        Err(LaunchError::InvalidWorkingDirectory)
    ));
    assert!(matches!(
        Launch::validate("/etc/passwd", vec![], PathBuf::from("/"), BTreeMap::new()),
        Err(LaunchError::ExecutableUnavailable)
    ));
}

#[test]
fn path_skips_files_not_executable_by_the_current_identity() {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    // Root can execute any file with an executable bit; this fixture exercises
    // the non-root owner permission class, not privileged access semantics.
    if unsafe { libc::getuid() } == 0 {
        return;
    }
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let base =
        std::env::temp_dir().join(format!("tofu-native-path-{}-{nonce}", std::process::id()));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&base)
        .unwrap();
    let first = base.join("first");
    let second = base.join("second");
    std::fs::create_dir(&first).unwrap();
    std::fs::create_dir(&second).unwrap();
    for dir in [&first, &second] {
        std::fs::copy("/bin/true", dir.join("tool")).unwrap();
    }
    std::fs::set_permissions(first.join("tool"), std::fs::Permissions::from_mode(0o001)).unwrap();
    std::fs::set_permissions(second.join("tool"), std::fs::Permissions::from_mode(0o700)).unwrap();
    let env = BTreeMap::from([(
        "PATH".into(),
        format!("{}:{}", first.display(), second.display()),
    )]);
    let launch = Launch::validate("tool", vec![], base.clone(), env).unwrap();
    assert_eq!(launch.executable(), second.join("tool"));
    std::fs::remove_dir_all(base).unwrap();
}
