//! Non-Git negotiation and bounded native walk. The parent runs rg as a joined
//! sibling (this executable execs it in place), so no descendant escapes cleanup.
use serde::Deserialize;
use std::{collections::HashSet, fs, io::Read, path::{Component, Path, PathBuf}, time::Instant};

pub const FALLBACK: u64 = u64::MAX;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Plan {
    paths: Option<Vec<String>>,
    ignored_directories: Vec<String>,
    depth_limit: usize,
    file_limit: usize,
}

pub fn fallback(input: &mut impl Read, request_limit: usize, path_limit: usize,
                deadline: Instant) -> Result<(Box<[u8]>, u8), i32> {
    let mut header = [0; 8];
    input.read_exact(&mut header).map_err(|_| 2)?;
    let length = usize::try_from(u64::from_le_bytes(header)).map_err(|_| 2)?;
    if length == 0 || length > request_limit { return Err(2); }
    let mut bytes = vec![0; length];
    input.read_exact(&mut bytes).map_err(|_| 2)?;
    let plan: Plan = serde_json::from_slice(&bytes).map_err(|_| 2)?;
    if let Some(paths) = plan.paths {
        let mut output = Vec::new();
        for path in paths {
            check(deadline)?;
            let name = Path::new(&path);
            if path.is_empty() || path.contains('\0') || name.is_absolute()
                || name.components().any(|part| matches!(part, Component::ParentDir)) {
                return Err(2);
            }
            append(&mut output, path.as_bytes(), path_limit)?;
        }
        return Ok((output.into_boxed_slice(), 2));
    }
    if plan.file_limit == 0 { return Err(2); }
    walk(Path::new("."), &plan.ignored_directories, plan.depth_limit, plan.file_limit, path_limit, deadline)
        .map(|paths| (paths, 1))
}

fn check(deadline: Instant) -> Result<(), i32> {
    if Instant::now() >= deadline { Err(3) } else { Ok(()) }
}

fn append(output: &mut Vec<u8>, path: &[u8], limit: usize) -> Result<(), i32> {
    if output.len().checked_add(path.len()).and_then(|size| size.checked_add(1))
        .is_none_or(|size| size > limit) { return Err(2); }
    output.extend_from_slice(path);
    output.push(0);
    Ok(())
}

fn walk(root: &Path, ignored: &[String], depth_limit: usize, file_limit: usize,
        path_limit: usize, deadline: Instant) -> Result<Box<[u8]>, i32> {
    let ignored: HashSet<&str> = ignored.iter().map(String::as_str).collect();
    let mut pending = vec![(PathBuf::new(), 0)];
    let mut files = Vec::new();
    let mut count = 0;
    while let Some((relative, depth)) = pending.pop() {
        check(deadline)?;
        if depth > depth_limit { continue; }
        let directory = root.join(&relative);
        // os.walk's default onerror ignores unreadable/disappearing directories.
        let Ok(entries) = fs::read_dir(directory) else { continue; };
        let mut children = Vec::new();
        let mut local_files = Vec::new();
        let mut unreadable = false;
        for entry in entries {
            check(deadline)?;
            let entry = match entry {
                Ok(entry) => entry,
                Err(_) => { unreadable = true; break; }
            };
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with('.') { continue; }
            let kind = entry.file_type().ok();
            let directory = kind.as_ref().is_some_and(|kind| kind.is_dir())
                || kind.as_ref().is_some_and(|kind| kind.is_symlink()) && entry.path().is_dir();
            let path = relative.join(entry.file_name());
            if directory {
                if !ignored.contains(name.as_ref()) && !kind.as_ref().is_some_and(|kind| kind.is_symlink()) {
                    children.push((path, depth + 1));
                }
            } else {
                if local_files.len() < file_limit - count {
                    local_files.push(path);
                }
            }
        }
        // Python discards this directory on a scandir iteration failure.
        if unreadable { continue; }
        for path in local_files {
            append(&mut files, path.as_os_str().as_encoded_bytes(), path_limit)?;
            count += 1;
            if count >= file_limit { return Ok(files.into_boxed_slice()); }
        }
        // os.walk yields all local files, then traverses directories in readdir order.
        pending.extend(children.into_iter().rev());
    }
    Ok(files.into_boxed_slice())
}

pub fn exec_rg(root: &std::ffi::OsStr) -> i32 {
    if std::env::set_current_dir(root).is_err() { return 127; }
    #[cfg(unix)] {
        use std::os::unix::process::CommandExt;
        let _error = std::process::Command::new("rg")
            .args(["--files", "-0", "--no-messages"]).exec();
    }
    127
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    fn deadline() -> Instant { Instant::now() + Duration::from_secs(5) }

    fn plan(paths: &[&str]) -> Vec<u8> {
        let payload = serde_json::to_vec(&serde_json::json!({
            "paths": paths, "ignored_directories": [], "depth_limit": 8, "file_limit": 100000,
        })).unwrap();
        let mut bytes = (payload.len() as u64).to_le_bytes().to_vec();
        bytes.extend(payload);
        bytes
    }

    #[test]
    fn authoritative_empty_and_relative_paths() {
        assert_eq!(&*fallback(&mut plan(&[]).as_slice(), 4096, 4096, deadline()).unwrap().0, b"");
        assert_eq!(&*fallback(&mut plan(&["a.py", "sub/b.py"]).as_slice(), 4096, 4096, deadline()).unwrap().0,
                   b"a.py\0sub/b.py\0");
    }

    #[test]
    fn traversal_malformed_frame_and_budget_refuse_without_partial_output() {
        for name in ["", "/outside.py", "../outside.py", "sub/../../outside.py", "nul\0.py"] {
            assert_eq!(fallback(&mut plan(&[name]).as_slice(), 4096, 4096, deadline()), Err(2));
        }
        assert_eq!(fallback(&mut plan(&["source.py"]).as_slice(), 4096, 4, deadline()), Err(2));
        assert_eq!(fallback(&mut plan(&["source.py"]).as_slice(), 1, 4096, deadline()), Err(2));
        assert_eq!(fallback(&mut &b"short"[..], 4096, 4096, deadline()), Err(2));
        assert_eq!(fallback(&mut plan(&["source.py"]).as_slice(), 4096, 4096, Instant::now()), Err(3));
    }

    struct Directory(PathBuf);
    impl Drop for Directory {
        fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
    }

    #[test]
    fn native_walk_depth_ignore_hidden_and_caller_limits() {
        let nonce = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root = Directory(std::env::temp_dir().join(format!("native-walk-{}-{nonce}", std::process::id())));
        fs::create_dir_all(&root.0).unwrap();
        for name in ["a.py", "sub/b.py", "sub/deep/c.py", "vendor/v.py", ".hidden.py", ".dir/h.py"] {
            let path = root.0.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, "").unwrap();
        }
        let bytes = walk(&root.0, &["vendor".into()], 1, 100, 4096, deadline()).unwrap();
        let paths: HashSet<_> = bytes.split(|byte| *byte == 0).filter(|p| !p.is_empty()).collect();
        assert_eq!(paths, HashSet::from([b"a.py".as_slice(), b"sub/b.py".as_slice()]));
        let limited = walk(&root.0, &[], 8, 1, 4096, deadline()).unwrap();
        assert_eq!(limited.iter().filter(|byte| **byte == 0).count(), 1);
        assert_eq!(walk(&root.0, &[], 8, 100, 1, deadline()), Err(2));
        assert_eq!(walk(&root.0, &[], 8, 100, 4096, Instant::now()), Err(3));
    }
}
