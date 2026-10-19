//! Explicit native tag extraction candidate. Python outline.py owns semantics.
//! Each batch owns its registry, parsers, trees and results; no runtime download.
//! CLI callers own process cancellation; C callers must supply their own isolation.
//! Grammar-library retirement remains process-wide for both interfaces.
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, path::PathBuf};
use tree_sitter::{Parser, Tree};
use tree_sitter_language_pack::LanguageRegistry;

mod catalog;
mod catalog_generated;
pub mod ffi;
#[cfg(target_os = "linux")]
pub mod sealed_grammars;
pub mod wire;

pub const FORMAT: &str = "tofu.repository-extraction/v1";
pub const CATALOG_FORMAT: &str = "tofu.repository-extraction/v2";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub format: String,
    pub files: Vec<Source>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub rel_path: String,
    pub language: Option<String>,
    pub source: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Tag {
    pub rel_path: String,
    pub name: String,
    pub line: usize,
    pub kind: &'static str,
    pub signature: String,
    pub indent: usize,
}

#[derive(Serialize)]
pub struct FileResult {
    pub rel_path: String,
    pub tags: Vec<Tag>,
    pub error: Option<&'static str>,
}

#[derive(Serialize)]
pub struct Response {
    pub format: &'static str,
    pub files: Vec<FileResult>,
}

fn line_break(character: char) -> bool {
    matches!(
        character,
        '\n' | '\r'
            | '\u{b}'
            | '\u{c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// Python str.splitlines boundaries, including CRLF and no empty final line.
fn source_lines(source: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut positions = source.char_indices().peekable();
    while let Some((offset, character)) = positions.next() {
        if line_break(character) {
            lines.push(&source[start..offset]);
            start = offset + character.len_utf8();
            if character == '\r' && positions.peek().is_some_and(|(_, next)| *next == '\n') {
                start = positions.next().unwrap().0 + 1;
            }
        }
    }
    if start < source.len() {
        lines.push(&source[start..]);
    }
    lines
}

fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

fn identifier(name: &str) -> bool {
    // Python's anchored '$' also permits one final LF. Retain the original name.
    let bytes = name.strip_suffix('\n').unwrap_or(name).as_bytes();
    !bytes.is_empty()
        && (bytes[0].is_ascii_alphabetic() || bytes[0] == b'_')
        && bytes[1..]
            .iter()
            .all(|value| value.is_ascii_alphanumeric() || *value == b'_')
}

#[derive(Clone, Copy)]
struct NodeMetadata {
    kind: &'static str,
    contributes_depth: bool,
}

fn metadata<'a>(cache: &mut HashMap<&'a str, NodeMetadata>, node_type: &'a str) -> NodeMetadata {
    *cache.entry(node_type).or_insert_with(|| NodeMetadata {
        kind: if node_type.contains("class") {
            "class"
        } else if ["function", "method", "def"]
            .iter()
            .any(|part| node_type.contains(part))
        {
            "def"
        } else {
            ""
        },
        contributes_depth: ["class", "function", "method"]
            .iter()
            .any(|part| node_type.contains(part)),
    })
}

fn collect_tags(tree: &Tree, input: &Source) -> Vec<Tag> {
    let lines = source_lines(&input.source);
    let mut cursor = tree.walk();
    let mut tags = Vec::new();
    let mut depth = 0;
    let mut type_metadata = HashMap::new();
    loop {
        let node = cursor.node();
        let properties = metadata(&mut type_metadata, node.kind());
        if !properties.kind.is_empty() {
            if let Some(name_node) = node.child_by_field_name("name") {
                let name =
                    String::from_utf8_lossy(&input.source.as_bytes()[name_node.byte_range()]);
                if identifier(&name) {
                    let row = node.start_position().row;
                    let signature = if lines.is_empty() {
                        String::new()
                    } else {
                        lines[row.min(lines.len() - 1)]
                            .trim_matches(python_whitespace)
                            .chars()
                            .take(160)
                            .collect()
                    };
                    tags.push(Tag {
                        rel_path: input.rel_path.clone(),
                        name: name.into_owned(),
                        line: row + 1,
                        kind: properties.kind,
                        signature,
                        indent: depth,
                    });
                }
            }
        }
        if cursor.goto_first_child() {
            depth += usize::from(properties.contributes_depth);
            continue;
        }
        while !cursor.goto_next_sibling() {
            if !cursor.goto_parent() {
                tags.sort_by_key(|tag| tag.line);
                return tags;
            }
            depth -=
                usize::from(metadata(&mut type_metadata, cursor.node().kind()).contributes_depth);
        }
    }
}

/// Caller selects a trusted grammar directory. Registry failure remains file-local.
pub fn extract(grammar_directory: PathBuf, request: Request) -> Result<Response, &'static str> {
    let format = match request.format.as_str() {
        FORMAT if request.files.iter().all(|file| file.language.is_some()) => FORMAT,
        // v1 continues to require an explicit language. v2 opts into the pinned
        // POSIX catalog; no installed Python import or current cache is consulted.
        CATALOG_FORMAT => CATALOG_FORMAT,
        _ => return Err("unsupported request format or missing v1 language"),
    };
    let mut extractor = Extractor::new(grammar_directory);
    let files = request
        .files
        .into_iter()
        .map(|input| extractor.file(input))
        .collect();
    Ok(Response { format, files })
}

/// One request/session owns its parser reuse; grammar handles retire with process.
pub struct Extractor {
    parsers: HashMap<String, Parser>,
    registry: LanguageRegistry,
    #[cfg(target_os = "linux")]
    bundle: Option<sealed_grammars::GrammarBundle>,
}

impl Extractor {
    pub fn new(grammar_directory: PathBuf) -> Self {
        Self {
            registry: LanguageRegistry::with_libs_dir(grammar_directory),
            parsers: HashMap::new(),
            #[cfg(target_os = "linux")]
            bundle: None,
        }
    }

    pub fn installed(
        grammar_directory: PathBuf,
        digest: &str,
        bytes: u64,
        metadata_limit: usize,
    ) -> Result<Self, &'static str> {
        #[cfg(target_os = "linux")]
        {
            let bundle = sealed_grammars::GrammarBundle::open(
                &grammar_directory,
                digest,
                bytes,
                metadata_limit,
            )
            .map_err(|_| "grammar_admission")?;
            let mut extractor = Self::new(grammar_directory);
            extractor.bundle = Some(bundle);
            Ok(extractor)
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (grammar_directory, digest, bytes, metadata_limit);
            Err("grammar_admission")
        }
    }

    fn language(&self, name: &str) -> Result<tree_sitter::Language, &'static str> {
        #[cfg(target_os = "linux")]
        if let Some(bundle) = &self.bundle {
            return bundle.language(name).map_err(|error| match error {
                sealed_grammars::GrammarError::Unavailable => "grammar_unavailable",
                sealed_grammars::GrammarError::Abi => "grammar_admission",
                sealed_grammars::GrammarError::Admission => "grammar_admission",
            });
        }
        self.registry
            .get_language(name)
            .map_err(|_| "grammar_unavailable")
    }

    pub fn file(&mut self, input: Source) -> FileResult {
        let mut error = None;
        let language = input
            .language
            .as_deref()
            .or_else(|| catalog::filename_to_language(&input.rel_path))
            .unwrap_or("");
        // Resolve names, never grammar paths supplied by the document itself.
        if language.is_empty()
            || !language
                .bytes()
                .all(|v| v.is_ascii_alphanumeric() || b"_-+#".contains(&v))
        {
            error = Some("invalid_language");
        } else if !self.parsers.contains_key(language) {
            match self.language(language) {
                Ok(grammar) => {
                    let mut parser = Parser::new();
                    if parser.set_language(&grammar).is_ok() {
                        self.parsers.insert(language.to_owned(), parser);
                    } else {
                        error = Some("grammar_abi");
                    }
                }
                Err(failure) => error = Some(failure),
            }
        }
        let tags = if error.is_some() {
            Vec::new()
        } else {
            let parser = self.parsers.get_mut(language).unwrap();
            match parser.parse(input.source.as_bytes(), None) {
                Some(tree) => collect_tags(&tree, &input),
                None => {
                    parser.reset();
                    error = Some("parse_failed");
                    Vec::new()
                }
            }
        };
        FileResult {
            rel_path: input.rel_path,
            tags,
            error,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_text_boundaries() {
        assert_eq!(source_lines(""), Vec::<&str>::new());
        assert_eq!(source_lines("a\r\nb\nc\r\n"), vec!["a", "b", "c"]);
        assert_eq!(source_lines("\n\n"), vec!["", ""]);
        assert_eq!(
            source_lines("α\u{85}β\u{2028}γ\u{1c}δ\u{1f}ε"),
            vec!["α", "β", "γ", "δ\u{1f}ε"]
        );
        assert!(python_whitespace('\u{1f}'));
        assert!(!python_whitespace('\u{200b}'));
        for name in ["_", "name_2", "name\n"] {
            assert!(identifier(name));
        }
        for name in ["", "2name", "é", "name\n\n", "name\r", "with-space "] {
            assert!(!identifier(name));
        }
    }
}
