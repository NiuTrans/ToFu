//! Exact POSIX filename resolution for the pinned grep-ast language-pack map.
//! Generated values have one owner; this module owns lexical lookup only.
use crate::catalog_generated::LANGUAGES;

fn lookup(key: &str) -> Option<&'static str> {
    LANGUAGES
        .binary_search_by_key(&key, |&(name, _)| name)
        .ok()
        .map(|index| LANGUAGES[index].1)
}

pub fn filename_to_language(filename: &str) -> Option<&'static str> {
    let basename = filename.rsplit('/').next().unwrap_or("");
    if let Some(language) = lookup(basename) {
        return Some(language);
    }
    let dot = basename.rfind('.')?;
    // CPython posixpath.splitext ignores a prefix consisting entirely of dots.
    if basename[..dot].bytes().all(|byte| byte == b'.') {
        return None;
    }
    lookup(&basename[dot..])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_pinned_python_posix_filename_oracle() {
        let cases: Vec<(String, Option<String>)> =
            serde_json::from_str(include_str!("../tests/fixtures/catalog_cases.json")).unwrap();
        for (name, expected) in cases {
            assert_eq!(filename_to_language(&name), expected.as_deref(), "{name:?}");
        }
    }
}
