//! Installed grammar authority for an owned Linux worker. No disk staging.
//! A pinned manifest authorizes exact library bytes. Only touched grammars are
//! copied to sealed memfds. As in the upstream registry, loaded library handles
//! live until process exit: a returned Language must never outlive its code.
//! The existing registry owns alias/symbol spelling and tree-sitter owns ABI bounds.
use libloading::Library;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs::{File, OpenOptions},
    io::{Read, Seek, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::OpenOptionsExt,
    },
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
};
use tree_sitter::Language;
use tree_sitter_language_pack::{error::Error as RegistryError, LanguageRegistry};

fn contract_value(name: &str) -> &'static str {
    static CONTRACT: OnceLock<serde_json::Value> = OnceLock::new();
    let contract = CONTRACT.get_or_init(|| {
        serde_json::from_str(include_str!(
            "../../../tofu_repo_kernel/grammar_bundle.schema.json"
        ))
        .expect("compiled grammar contract must be valid JSON")
    });
    contract["properties"][name]["const"]
        .as_str()
        .expect("compiled grammar contract constant")
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    format: String,
    registry_version: String,
    libraries: Vec<Entry>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Entry {
    file: String,
    bytes: u64,
    sha256: String,
}

#[derive(Debug, PartialEq, Eq)]
pub enum GrammarError {
    Unavailable,
    Abi,
    Admission,
}

type Result<T> = std::result::Result<T, GrammarError>;

fn digest_hex(bytes: &[u8]) -> String {
    const HEX: &[u8] = b"0123456789abcdef";
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        result.push(HEX[(byte >> 4) as usize] as char);
        result.push(HEX[(byte & 15) as usize] as char);
    }
    result
}

fn digest_valid(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
}

fn filename_valid(value: &str) -> bool {
    value
        .strip_prefix("libtree_sitter_")
        .and_then(|v| v.strip_suffix(".so"))
        .is_some_and(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'))
}

/// Copy a declared number of bytes, not a growing file, and seal exactly the
/// bytes hashed. O_NONBLOCK plus the regular-file check refuses FIFOs/devices.
fn sealed_file(path: &Path, bytes: u64, expected: &str) -> Result<File> {
    if bytes == 0 || bytes >= isize::MAX as u64 || !digest_valid(expected) {
        return Err(GrammarError::Admission);
    }
    let mut source = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| GrammarError::Admission)?;
    let metadata = source.metadata().map_err(|_| GrammarError::Admission)?;
    if !metadata.is_file() || metadata.len() != bytes {
        return Err(GrammarError::Admission);
    }
    // SAFETY: the platform syscall number, a static NUL-terminated name and
    // Linux UAPI flags. A direct syscall also supports older glibc hosts.
    let fd = unsafe {
        libc::syscall(
            libc::SYS_memfd_create,
            c"tofu-repository-grammar".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        ) as i32
    };
    if fd < 0 {
        return Err(GrammarError::Admission);
    }
    // SAFETY: successful memfd_create transfers one fresh descriptor to File.
    let mut image = unsafe { File::from_raw_fd(fd) };
    let mut digest = Sha256::new();
    let mut buffer = [0; 65536];
    let mut remaining = bytes;
    while remaining > 0 {
        let length = usize::try_from(remaining.min(buffer.len() as u64))
            .map_err(|_| GrammarError::Admission)?;
        let count = source
            .read(&mut buffer[..length])
            .map_err(|_| GrammarError::Admission)?;
        if count == 0 {
            return Err(GrammarError::Admission);
        }
        digest.update(&buffer[..count]);
        image
            .write_all(&buffer[..count])
            .map_err(|_| GrammarError::Admission)?;
        remaining -= count as u64;
    }
    if source.read(&mut [0]).map_err(|_| GrammarError::Admission)? != 0
        || digest_hex(&digest.finalize()) != expected
    {
        return Err(GrammarError::Admission);
    }
    let seals = libc::F_SEAL_SEAL | libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_WRITE;
    // SAFETY: fcntl operates on the owned live fd; these commands accept integers.
    if unsafe { libc::fcntl(fd, libc::F_ADD_SEALS, seals) } != 0
        || unsafe { libc::fcntl(fd, libc::F_GET_SEALS) } & seals != seals
    {
        return Err(GrammarError::Admission);
    }
    image.rewind().map_err(|_| GrammarError::Admission)?;
    Ok(image)
}

struct ProcessLibrary {
    library: Library,
    _image: File,
}
// Never unload a grammar while any Language/Parser can still refer to it.
// This is bounded by the libraries touched in one explicitly owned child.
static LIBRARIES: OnceLock<Mutex<HashMap<String, ProcessLibrary>>> = OnceLock::new();

pub struct GrammarBundle {
    directory: PathBuf,
    entries: HashMap<String, Entry>,
    // A regular sealed file cannot contain a library filename. This lets the
    // upstream registry resolve aliases without ever looking in a mutable dir.
    resolver: LanguageRegistry,
    _manifest: File,
}

impl GrammarBundle {
    pub fn open(
        directory: &Path,
        expected: &str,
        bytes: u64,
        metadata_limit: usize,
    ) -> Result<Self> {
        if bytes > metadata_limit as u64 {
            return Err(GrammarError::Admission);
        }
        let mut manifest = sealed_file(&directory.join("grammars.json"), bytes, expected)?;
        let record: Manifest =
            serde_json::from_reader(&mut manifest).map_err(|_| GrammarError::Admission)?;
        if record.format != contract_value("format")
            || record.registry_version != contract_value("registry_version")
        {
            return Err(GrammarError::Admission);
        }
        let mut entries = HashMap::new();
        for entry in record.libraries {
            if !filename_valid(&entry.file)
                || !digest_valid(&entry.sha256)
                || entry.bytes == 0
                || entry.bytes >= isize::MAX as u64
                || entries.contains_key(&entry.file)
            {
                return Err(GrammarError::Admission);
            }
            entries.insert(entry.file.clone(), entry);
        }
        let resolver = LanguageRegistry::with_libs_dir(PathBuf::from(format!(
            "/proc/self/fd/{}",
            manifest.as_raw_fd()
        )));
        Ok(Self {
            directory: directory.to_owned(),
            entries,
            resolver,
            _manifest: manifest,
        })
    }

    pub fn language(&self, requested: &str) -> Result<Language> {
        let canonical = match self.resolver.get_language(requested) {
            // Any statically linked grammar is already part of the sealed worker.
            Ok(language) => return Ok(language),
            Err(RegistryError::LanguageNotFound(name)) => name,
            Err(_) => return Err(GrammarError::Admission),
        };
        let filename = tree_sitter_language_pack::registry::library_file_name(&canonical);
        self.load_file(&filename)
    }

    pub fn check_all(&self) -> Result<(usize, u64)> {
        let mut bytes = 0u64;
        for (filename, entry) in &self.entries {
            self.load_file(filename)?;
            bytes = bytes
                .checked_add(entry.bytes)
                .ok_or(GrammarError::Admission)?;
        }
        Ok((self.entries.len(), bytes))
    }

    fn load_file(&self, filename: &str) -> Result<Language> {
        let entry = self
            .entries
            .get(filename)
            .ok_or(GrammarError::Unavailable)?;
        let mut libraries = LIBRARIES
            .get_or_init(|| Mutex::new(HashMap::new()))
            .lock()
            .map_err(|_| GrammarError::Admission)?;
        if !libraries.contains_key(&entry.sha256) {
            let image = sealed_file(
                &self.directory.join(&entry.file),
                entry.bytes,
                &entry.sha256,
            )?;
            let path = format!("/proc/self/fd/{}", image.as_raw_fd());
            // SAFETY: only the authenticated, sealed image is executed. Its handle
            // and descriptor are process-owned, matching upstream lifetime policy.
            let library = unsafe { Library::new(path) }.map_err(|_| GrammarError::Admission)?;
            libraries.insert(
                entry.sha256.clone(),
                ProcessLibrary {
                    library,
                    _image: image,
                },
            );
        }
        let library = &libraries
            .get(&entry.sha256)
            .ok_or(GrammarError::Admission)?
            .library;
        // The public upstream filename helper owns C symbol overrides too.
        let symbol = filename
            .strip_prefix("lib")
            .and_then(|v| v.strip_suffix(".so"))
            .ok_or(GrammarError::Admission)?;
        // SAFETY: a pinned tree-sitter grammar exports this C entry point; its
        // library remains loaded for every Language and Parser lifetime.
        let language = unsafe {
            let function: libloading::Symbol<
                unsafe extern "C" fn() -> *const tree_sitter::ffi::TSLanguage,
            > = library
                .get(symbol.as_bytes())
                .map_err(|_| GrammarError::Admission)?;
            let pointer = function();
            if pointer.is_null() {
                return Err(GrammarError::Admission);
            }
            Language::from_raw(pointer)
        };
        if !(tree_sitter::MIN_COMPATIBLE_LANGUAGE_VERSION..=tree_sitter::LANGUAGE_VERSION)
            .contains(&language.abi_version())
        {
            return Err(GrammarError::Abi);
        }
        Ok(language)
    }
}

#[cfg(test)]
mod tests;
