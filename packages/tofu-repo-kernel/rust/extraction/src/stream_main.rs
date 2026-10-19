//! One owned discovery/read/extraction session; no resident worker or parent cwd.
//! Frames use a u64 little-endian byte length. Final zero-length frame commits
//! the complete stream only after a successful process exit; callers withhold
//! all assembled results on framing, deadline, cancellation or child failure.
mod stream_discovery;
mod stream_paths;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    env,
    fs::File,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use tofu_repository_extraction::{wire, Extractor, Source, Tag};

const FORMAT: &str = "tofu.repository-stream/v2";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    format: String,
    path_indexes: Vec<usize>,
    max_source_bytes: u64,
    max_reference_chars: usize,
    include_refs: bool,
}

#[derive(Serialize)]
struct FileFrame {
    path_index: usize,
    text: Option<String>,
    admitted: bool,
    tags: Vec<StreamTag>,
    grammar_error: Option<&'static str>,
}

// File identity is the discovery-table index. Tags cannot smuggle another path.
#[derive(Serialize)]
struct StreamTag {
    name: String,
    line: usize,
    kind: &'static str,
    signature: String,
    indent: usize,
}

impl From<Tag> for StreamTag {
    fn from(tag: Tag) -> Self {
        Self {
            name: tag.name,
            line: tag.line,
            kind: tag.kind,
            signature: tag.signature,
            indent: tag.indent,
        }
    }
}

fn frame(output: &mut impl Write, bytes: &[u8]) -> io::Result<()> {
    output.write_all(&(bytes.len() as u64).to_le_bytes())?;
    output.write_all(bytes)?;
    output.flush()
}

fn read_request(
    input: &mut impl Read,
    limit: usize,
) -> Result<Request, Box<dyn std::error::Error>> {
    let mut header = [0; 8];
    input.read_exact(&mut header)?;
    let length = usize::try_from(u64::from_le_bytes(header))?;
    if length == 0 || length > limit {
        return Err("invalid request length".into());
    }
    let mut bytes = vec![0; length];
    input.read_exact(&mut bytes)?;
    if input.read(&mut [0])? != 0 {
        return Err("trailing request bytes".into());
    }
    Ok(serde_json::from_slice(&bytes)?)
}

fn python_text(bytes: Vec<u8>) -> String {
    // TextIOWrapper(encoding='utf-8', errors='replace', newline=None).
    let text = match String::from_utf8(bytes) {
        Ok(text) => text,
        Err(error) => String::from_utf8_lossy(error.as_bytes()).into_owned(),
    };
    if text.contains('\r') {
        text.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        text
    }
}

fn read_source(
    path: &Path,
    request: &Request,
    read_limit: usize,
) -> Result<Option<(String, bool)>, Box<dyn std::error::Error>> {
    let metadata = match std::fs::metadata(path) {
        Ok(v) => v,
        Err(_) => return Ok(None),
    };
    let admitted = metadata.len() <= request.max_source_bytes;
    let prefix_limit = request
        .max_reference_chars
        .checked_mul(4)
        .and_then(|n| n.checked_add(4))
        .ok_or("reference size overflow")?;
    let limit = if admitted {
        read_limit.checked_add(1).ok_or("read size overflow")?
    } else {
        prefix_limit
    };
    let mut file = match File::open(path) {
        Ok(v) => v.take(limit as u64),
        Err(_) => return Ok(None),
    };
    let mut bytes = Vec::new();
    if file.read_to_end(&mut bytes).is_err() {
        return Ok(None);
    }
    if admitted && bytes.len() > read_limit {
        return Err("source grew past caller read limit".into());
    }
    let text = python_text(bytes);
    Ok(Some((
        if admitted {
            text
        } else {
            text.chars().take(request.max_reference_chars).collect()
        },
        admitted,
    )))
}

fn run() -> Result<(), i32> {
    let args: Vec<_> = env::args_os().skip(1).collect();
    if args.len() == 4 && args[0] == "--check-grammar-bundle-v1" {
        #[cfg(target_os = "linux")]
        {
            let digest = args[2].to_str().ok_or(2)?;
            let bytes = args[3].to_str().ok_or(2)?.parse::<u64>().map_err(|_| 2)?;
            let bundle = tofu_repository_extraction::sealed_grammars::GrammarBundle::open(
                &PathBuf::from(&args[1]),
                digest,
                bytes,
                usize::try_from(bytes).map_err(|_| 2)?,
            )
            .map_err(|_| 2)?;
            let (libraries, bytes) = bundle.check_all().map_err(|_| 2)?;
            println!(
                "{}",
                serde_json::json!({"libraries":libraries,"bytes":bytes})
            );
            return Ok(());
        }
        #[cfg(not(target_os = "linux"))]
        return Err(2);
    }
    if args.len() == 2 && args[0] == "--list-rg" {
        return Err(stream_discovery::exec_rg(&args[1]));
    }
    if !matches!(args.len(), 8 | 11)
        || args[7] != "--discovery-index-v1"
        || (args.len() == 11 && args[8] != "--grammar-bundle-v1")
    {
        return Err(2);
    }
    let number = |index: usize| {
        args[index]
            .to_str()
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or(2)
    };
    let grammar_bundle = if args.len() == 11 {
        Some((args[9].to_str().ok_or(2)?, number(10)?))
    } else {
        None
    };
    let timeout = number(2)?;
    let paths_limit = usize::try_from(number(3)?).map_err(|_| 2)?;
    let request_limit = usize::try_from(number(4)?).map_err(|_| 2)?;
    let frame_limit = usize::try_from(number(5)?).map_err(|_| 2)?;
    let read_limit = usize::try_from(number(6)?).map_err(|_| 2)?;
    if timeout == 0 {
        return Err(3);
    }
    if [paths_limit, request_limit, frame_limit, read_limit].contains(&0) {
        return Err(2);
    }
    // The parent enforces the session deadline, including blocked chdir, source
    // reads and grammar dlopen. This child's discovery also has a cooperative limit.
    let deadline = Instant::now()
        .checked_add(Duration::from_nanos(timeout))
        .ok_or(2)?;
    std::env::set_current_dir(&args[0]).map_err(|_| 2)?;
    let (paths, codec) = match tofu_repository_discovery::current_directory_files(timeout) {
        Ok(paths) => (paths, 0),
        Err(2) => {
            let mut output = io::stdout().lock();
            output
                .write_all(&stream_discovery::FALLBACK.to_le_bytes())
                .map_err(|_| 2)?;
            output.flush().map_err(|_| 2)?;
            drop(output);
            stream_discovery::fallback(
                &mut io::stdin().lock(),
                request_limit,
                paths_limit,
                deadline,
            )?
        }
        Err(status) => return Err(status),
    };
    if paths.len() > paths_limit {
        return Err(2);
    }
    let table = stream_paths::table(&paths, codec == 1)?;
    let mut output = io::stdout().lock();
    // One codec byte precedes NUL-delimited names; the caller budget covers names.
    output
        .write_all(&((paths.len() as u64) + 1).to_le_bytes())
        .map_err(|_| 2)?;
    output.write_all(&[codec]).map_err(|_| 2)?;
    output.write_all(&paths).map_err(|_| 2)?;
    output.flush().map_err(|_| 2)?;
    drop(paths);
    let request = read_request(&mut io::stdin().lock(), request_limit).map_err(|_| 2)?;
    if request.format != FORMAT
        || request.max_source_bytes > read_limit as u64
        || request
            .max_reference_chars
            .checked_mul(4)
            .and_then(|n| n.checked_add(4))
            .is_none_or(|n| n > read_limit)
    {
        return Err(2);
    }
    let mut seen = HashSet::new();
    for index in &request.path_indexes {
        if *index >= table.len() || !seen.insert(index) {
            return Err(2);
        }
    }
    drop(seen);
    let mut extractor = None;
    for &index in &request.path_indexes {
        let path = &table[index];
        let display_path = path.to_string_lossy();
        let mut row = FileFrame {
            path_index: index,
            text: None,
            admitted: false,
            tags: Vec::new(),
            grammar_error: None,
        };
        if let Some((text, admitted)) = read_source(path, &request, read_limit).map_err(|_| 2)? {
            row.admitted = admitted;
            let python = display_path.ends_with(".py");
            if python && admitted {
                row.text = Some(text);
            } else {
                row.text = Some(if request.include_refs {
                    text.chars().take(request.max_reference_chars).collect()
                } else {
                    String::new()
                });
                if admitted {
                    if extractor.is_none() {
                        extractor = Some(match grammar_bundle {
                            Some((digest, bytes)) => Extractor::installed(
                                PathBuf::from(&args[1]),
                                digest,
                                bytes,
                                request_limit,
                            )
                            .map_err(|_| 2)?,
                            None => Extractor::new(PathBuf::from(&args[1])),
                        });
                    }
                    let result = extractor.as_mut().ok_or(2)?.file(Source {
                        rel_path: display_path.into_owned(),
                        language: None,
                        source: text,
                    });
                    row.tags = result.tags.into_iter().map(StreamTag::from).collect();
                    row.grammar_error = result.error;
                }
            }
        }
        let bytes = wire::serialize(&row, frame_limit).map_err(|_| 2)?;
        frame(&mut output, &bytes).map_err(|_| 2)?;
    }
    frame(&mut output, &[]).map_err(|_| 2)?;
    Ok(())
}

fn main() {
    match std::panic::catch_unwind(run) {
        Ok(Ok(())) => {}
        Ok(Err(status)) => std::process::exit(status),
        Err(_) => std::process::exit(4),
    }
}
