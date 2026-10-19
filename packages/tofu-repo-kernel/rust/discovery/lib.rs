//! Read-only Git index and untracked discovery. No retained repository or worker.
//! The C ABI owns its returned NUL-separated byte buffer until exactly one free.
//! Deadline checks are cooperative; blocking filesystem calls require a caller
//! process boundary when a hard timeout is required. Partial paths never escape.
use std::{
    collections::BTreeSet,
    path::Path,
    sync::atomic::AtomicBool,
    time::{Duration, Instant},
};

fn discover_files(
    root: &Path,
    deadline: Instant,
    process_owns_root: bool,
) -> Result<Vec<Vec<u8>>, Box<dyn std::error::Error + Send + Sync>> {
    check_deadline(deadline)?;
    // Environment path overrides are resolved by gix against process cwd.
    // Never mutate that shared cwd to emulate an explicit per-call root.
    if ["GIT_COMMON_DIR", "GIT_ALTERNATE_OBJECT_DIRECTORIES"]
        .iter()
        .any(|name| std::env::var_os(name).is_some())
    {
        return Err(Box::new(UnsupportedEnvironment));
    }
    for name in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_SHALLOW_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    ] {
        if !process_owns_root
            && std::env::var_os(name).is_some_and(|value| Path::new(&value).is_relative())
        {
            return Err(Box::new(UnsupportedEnvironment));
        }
    }
    let root = root.canonicalize()?;
    let repo = gix::discover_with_environment_overrides(&root)?;
    let workdir = repo
        .workdir()
        .ok_or("repository has no worktree")?
        .canonicalize()?;
    let prefix = match root.strip_prefix(&workdir) {
        Ok(path) => gix::path::into_bstr(path.to_path_buf())?.into_owned(),
        Err(_) => gix::bstr::BString::default(),
    };
    let prefix: &[u8] = prefix.as_ref();
    let mut index = match repo.open_index() {
        Ok(index) => index,
        Err(error) => {
            if repo.index_path().try_exists()? {
                return Err(error.into());
            }
            gix::index::File::from_state(
                gix::index::State::new(repo.object_hash()),
                repo.index_path(),
            )
        }
    };
    check_deadline(deadline)?;
    for entry in index.entries_mut() {
        entry.flags.insert(gix::index::entry::Flags::UPTODATE);
    }
    let mut paths = BTreeSet::<Vec<u8>>::new();
    let trim = |path: &[u8]| -> Option<Vec<u8>> {
        if prefix.is_empty() {
            return Some(path.to_vec());
        }
        let rest = path.strip_prefix(prefix)?;
        rest.strip_prefix(b"/").map(|relative| relative.to_vec())
    };
    for entry in index.entries() {
        check_deadline(deadline)?;
        if entry.mode.is_sparse() {
            let mut pending = vec![(entry.id, entry.path(&index).to_vec())];
            while let Some((id, prefix)) = pending.pop() {
                check_deadline(deadline)?;
                let tree = repo.find_tree(id)?;
                for item in tree.iter() {
                    check_deadline(deadline)?;
                    let item = item?;
                    let mut path = prefix.clone();
                    path.extend_from_slice(item.filename());
                    if item.mode().is_tree() {
                        path.push(b'/');
                        pending.push((item.object_id(), path));
                    } else if let Some(relative) = trim(&path) {
                        paths.insert(relative);
                    }
                }
            }
        } else if let Some(relative) = trim(entry.path(&index).as_ref()) {
            paths.insert(relative);
        }
    }
    let patterns: Vec<gix::bstr::BString> = if prefix.is_empty() {
        Vec::new()
    } else {
        let mut pattern = b":(top,literal)".to_vec();
        pattern.extend_from_slice(prefix);
        pattern.push(b'/');
        vec![pattern.into()]
    };
    let mut collected = Collector {
        paths: &mut paths,
        prefix,
        deadline,
    };
    repo.dirwalk(
        &index,
        &patterns,
        &AtomicBool::new(false),
        repo.dirwalk_options()?,
        &mut collected,
    )?;
    check_deadline(deadline)?;
    Ok(paths.into_iter().collect())
}

#[derive(Debug)]
struct UnsupportedEnvironment;
impl std::fmt::Display for UnsupportedEnvironment {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("unsupported Git environment path")
    }
}
impl std::error::Error for UnsupportedEnvironment {}

#[derive(Debug)]
struct Deadline;
impl std::fmt::Display for Deadline {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("discovery deadline elapsed")
    }
}
impl std::error::Error for Deadline {}
fn check_deadline(deadline: Instant) -> Result<(), Deadline> {
    if Instant::now() >= deadline {
        Err(Deadline)
    } else {
        Ok(())
    }
}

struct Collector<'a> {
    paths: &'a mut BTreeSet<Vec<u8>>,
    prefix: &'a [u8],
    deadline: Instant,
}
impl gix::dir::walk::Delegate for Collector<'_> {
    fn emit(
        &mut self,
        entry: gix::dir::EntryRef<'_>,
        _: Option<gix::dir::entry::Status>,
    ) -> gix::dir::walk::Action {
        if check_deadline(self.deadline).is_err() {
            return std::ops::ControlFlow::Break(());
        }
        let path: &[u8] = entry.rela_path.as_ref();
        let relative = if self.prefix.is_empty() {
            Some(path)
        } else {
            path.strip_prefix(self.prefix)
                .and_then(|rest| rest.strip_prefix(b"/"))
        };
        if let Some(relative) = relative {
            let mut path = relative.to_vec();
            if entry.disk_kind == Some(gix::dir::entry::Kind::Repository) {
                path.push(b'/');
            }
            self.paths.insert(path);
        }
        std::ops::ControlFlow::Continue(())
    }
    fn can_recurse(
        &mut self,
        entry: gix::dir::EntryRef<'_>,
        deletion: Option<gix::dir::walk::ForDeletionMode>,
        root_is_repo: bool,
    ) -> bool {
        check_deadline(self.deadline).is_ok()
            && entry.status.can_recurse(
                entry.disk_kind,
                entry.pathspec_match,
                deletion,
                root_is_repo,
            )
    }
}

/// # Safety
/// root references root_len immutable bytes. output and output_len point to
/// writable, disjoint slots. On success free the returned buffer once using
/// tofu_repository_files_free_v1, including an empty successful result.
/// Status: 0 success; 1 invalid input; 2 discovery error; 3 deadline; 4 panic; 5 unsupported environment.
#[no_mangle]
pub unsafe extern "C" fn tofu_repository_files_v1(
    root: *const u8,
    root_len: usize,
    timeout_ns: u64,
    output: *mut *mut u8,
    output_len: *mut usize,
) -> i32 {
    if output.is_null() || output_len.is_null() {
        return 1;
    }
    unsafe {
        *output = std::ptr::null_mut();
        *output_len = 0;
    }
    if root.is_null() || root_len == 0 || root_len > isize::MAX as usize {
        return 1;
    }
    let result = std::panic::catch_unwind(|| {
        let bytes = unsafe { std::slice::from_raw_parts(root, root_len) };
        if bytes.contains(&0) {
            return Err(1);
        }
        let root = gix::path::from_bstr(gix::bstr::BStr::new(bytes)).map_err(|_| 1)?;
        serialized_files(root.as_ref(), timeout_ns, false)
    });
    match result {
        Ok(Ok(bytes)) => {
            unsafe {
                *output_len = bytes.len();
                *output = Box::into_raw(bytes) as *mut u8;
            }
            0
        }
        Ok(Err(status)) => status,
        Err(_) => 4,
    }
}

/// # Safety
/// ptr and length are the unchanged pair returned by a successful files call;
/// the buffer has not already been freed, and no reader retains its contents.
#[no_mangle]
pub unsafe extern "C" fn tofu_repository_files_free_v1(ptr: *mut u8, length: usize) {
    if !ptr.is_null() {
        unsafe {
            drop(Box::from_raw(std::ptr::slice_from_raw_parts_mut(
                ptr, length,
            )));
        }
    }
}

/// The standalone process owns its cwd before entry; relative environment
/// overrides now have Git's root-relative meaning without touching host state.
pub fn current_directory_files(timeout_ns: u64) -> Result<Box<[u8]>, i32> {
    std::panic::catch_unwind(|| serialized_files(Path::new("."), timeout_ns, true))
        .unwrap_or(Err(4))
}

fn serialized_files(
    root: &Path,
    timeout_ns: u64,
    process_owns_root: bool,
) -> Result<Box<[u8]>, i32> {
    let deadline = Instant::now()
        .checked_add(Duration::from_nanos(timeout_ns))
        .ok_or(1)?;
    let paths = discover_files(root, deadline, process_owns_root).map_err(|err| {
        if err.is::<Deadline>() {
            3
        } else if err.is::<UnsupportedEnvironment>() {
            5
        } else {
            2
        }
    })?;
    let length = paths
        .iter()
        .try_fold(0usize, |n, p| n.checked_add(p.len())?.checked_add(1))
        .ok_or(1)?;
    if length > isize::MAX as usize {
        return Err(1);
    }
    let mut bytes = Vec::with_capacity(length);
    for path in paths {
        check_deadline(deadline).map_err(|_| 3)?;
        bytes.extend_from_slice(&path);
        bytes.push(0);
    }
    Ok(bytes.into_boxed_slice())
}
