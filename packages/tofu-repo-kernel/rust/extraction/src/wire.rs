//! Shared bounded JSON preparation for the process and coarse C interfaces.
use crate::{extract, Request};
use std::{io, io::Write, path::PathBuf};

struct BoundedOutput {
    bytes: Vec<u8>,
    limit: usize,
}

impl Write for BoundedOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            return Err(io::Error::other("response exceeds caller byte limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub fn response(
    grammar_directory: PathBuf,
    request: &[u8],
    limit: usize,
) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let request: Request = serde_json::from_slice(request)?;
    let response = extract(grammar_directory, request)?;
    serialize(&response, limit)
}

pub fn serialize(
    value: &impl serde::Serialize,
    limit: usize,
) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let mut output = BoundedOutput {
        bytes: Vec::new(),
        limit,
    };
    serde_json::to_writer(&mut output, value)?;
    Ok(output.bytes)
}
