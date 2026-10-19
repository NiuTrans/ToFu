//! Discovery table identity and POSIX byte-path admission for stream selections.
use std::path::{Component, PathBuf};

fn native_path(bytes: &[u8]) -> Result<PathBuf, i32> {
    #[cfg(unix)] {
        use std::{ffi::OsStr, os::unix::ffi::OsStrExt};
        Ok(PathBuf::from(OsStr::from_bytes(bytes)))
    }
    #[cfg(not(unix))] {
        // The sealed stream is not admitted on this host, but the package's
        // independent ranking/C ABI must still build. Never lose path bytes.
        Ok(PathBuf::from(std::str::from_utf8(bytes).map_err(|_| 2)?))
    }
}

pub fn table(bytes: &[u8], preserve_bytes: bool) -> Result<Vec<PathBuf>, i32> {
    if !bytes.is_empty() && (!bytes.ends_with(&[0]) || bytes.starts_with(&[0])
        || bytes.windows(2).any(|pair| pair == [0, 0])) { return Err(2); }
    bytes.split(|&byte| byte == 0).filter(|part| !part.is_empty()).map(|part| {
        // Git/rg have always used UTF-8 replacement in the canonical owner.
        // os.walk uses surrogateescape: retain its original bytes for opening.
        let path = if preserve_bytes { native_path(part)? }
                   else { PathBuf::from(String::from_utf8_lossy(part).into_owned()) };
        if path.is_absolute() || path.components().any(|part| matches!(part, Component::ParentDir)) {
            Err(2)
        } else { Ok(path) }
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn modes_keep_canonical_identity_without_conflating_walk_names() {
        let raw = b"invalid-\xff.py\0invalid-\xef\xbf\xbd.py\0";
        let walked = table(raw, true).unwrap();
        assert_eq!(walked[0].as_os_str().as_encoded_bytes(), b"invalid-\xff.py");
        assert_ne!(walked[0], walked[1]);
        let replaced = table(raw, false).unwrap();
        assert_eq!(replaced[0], replaced[1]);
        assert!(table(b"", true).unwrap().is_empty());
    }

    #[test]
    fn malformed_and_outside_root_names_are_refused() {
        for bytes in [b"no-terminator".as_slice(), b"\0", b"a\0\0", b"/outside\0", b"../outside\0", b"dir/../../outside\0"] {
            assert_eq!(table(bytes, true), Err(2));
            assert_eq!(table(bytes, false), Err(2));
        }
    }
}
