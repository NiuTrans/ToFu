//! Streaming top-level JSON object fields. Large projection/messages values
//! are lexically skipped, never deserialized or copied. Stored JSON was already
//! validated by the writer; this scanner additionally checks nesting, strings,
//! duplicate fields, bounded metadata, and the outer document framing.

use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::io::{self, BufRead, BufReader, Read};
use std::ops::Range;

pub(super) const MAX_METADATA_BYTES: usize = 1024 * 1024;

fn corrupt() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "diagnostic source JSON is malformed",
    )
}
fn bounded() -> io::Error {
    io::Error::new(
        io::ErrorKind::OutOfMemory,
        "diagnostic metadata exceeds 1 MiB",
    )
}

pub(super) struct Fields {
    pub metadata: Map<String, Value>,
    pub skipped: BTreeMap<String, Range<u64>>,

    pub kinds: BTreeMap<String, u8>,
}

struct Scanner<R> {
    input: BufReader<R>,
    position: u64,
    retained: usize,
}
impl<R: Read> Scanner<R> {
    fn peek(&mut self) -> io::Result<Option<u8>> {
        Ok(self.input.fill_buf()?.first().copied())
    }
    fn byte(&mut self, output: &mut Option<Vec<u8>>) -> io::Result<u8> {
        let byte = self.peek()?.ok_or_else(corrupt)?;
        if let Some(output) = output {
            self.retained += 1;
            if self.retained > MAX_METADATA_BYTES {
                return Err(bounded());
            }
            output.push(byte);
        }
        self.input.consume(1);
        self.position += 1;
        Ok(byte)
    }
    fn whitespace(&mut self) -> io::Result<()> {
        while self
            .peek()?
            .is_some_and(|c| matches!(c, b' ' | b'\n' | b'\t' | b'\r'))
        {
            self.byte(&mut None)?;
        }
        Ok(())
    }
    fn string(&mut self, output: &mut Option<Vec<u8>>) -> io::Result<()> {
        if self.byte(output)? != b'"' {
            return Err(corrupt());
        }
        loop {
            // Skip ordinary string runs in one buffered operation. No allocation
            // proportional to content, even for a single enormous JSON string.
            let bytes = self.input.fill_buf()?;
            if bytes.is_empty() {
                return Err(corrupt());
            }
            let count = bytes
                .iter()
                .position(|c| *c == b'"' || *c == b'\\' || *c < 0x20)
                .unwrap_or(bytes.len());
            if let Some(output) = output {
                self.retained += count;
                if self.retained > MAX_METADATA_BYTES {
                    return Err(bounded());
                }
                output.extend_from_slice(&bytes[..count]);
            }
            let exhausted = count == bytes.len();
            self.input.consume(count);
            self.position += count as u64;

            if exhausted {
                continue;
            }
            match self.byte(output)? {
                b'"' => return Ok(()),
                b'\\' => match self.byte(output)? {
                    b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't' => {}
                    b'u' => {
                        for _ in 0..4 {
                            if !self.byte(output)?.is_ascii_hexdigit() {
                                return Err(corrupt());
                            }
                        }
                    }
                    _ => return Err(corrupt()),
                },
                _ => return Err(corrupt()),
            }
        }
    }
    fn value(&mut self, output: &mut Option<Vec<u8>>) -> io::Result<()> {
        let first = self.peek()?.ok_or_else(corrupt)?;
        if first == b'"' {
            return self.string(output);
        }
        if first != b'{' && first != b'[' {
            while self
                .peek()?
                .is_some_and(|c| !matches!(c, b',' | b'}' | b']' | b' ' | b'\n' | b'\r' | b'\t'))
            {
                self.byte(output)?;
            }
            return Ok(());
        }
        let mut stack = [0_u8; 128];
        let mut depth = 0;
        loop {
            if self.peek()? == Some(b'"') {
                self.string(output)?;
                continue;
            }
            match self.byte(output)? {
                c @ (b'{' | b'[') => {
                    if depth == stack.len() {
                        return Err(corrupt());
                    }
                    stack[depth] = c;
                    depth += 1;
                }
                c @ (b'}' | b']') => {
                    if depth == 0 || stack[depth - 1] != if c == b'}' { b'{' } else { b'[' } {
                        return Err(corrupt());
                    }
                    depth -= 1;
                    if depth == 0 {
                        return Ok(());
                    }
                }
                _ => {}
            }
        }
    }
}

struct DeadlineReader<'a, R> {
    reader: R,
    deadline_elapsed: &'a mut dyn FnMut() -> bool,
}
impl<R: Read> Read for DeadlineReader<'_, R> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        super::check_deadline(self.deadline_elapsed)?;
        self.reader.read(output)
    }
}

pub(super) fn object(
    reader: impl Read,
    skip: &[&str],
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Fields> {
    let reader = DeadlineReader {
        reader,
        deadline_elapsed,
    };
    let mut scanner = Scanner {
        input: BufReader::with_capacity(65_536, reader),
        position: 0,
        retained: 0,
    };
    scanner.whitespace()?;
    if scanner.byte(&mut None)? != b'{' {
        return Err(corrupt());
    }
    let mut fields = Fields {
        metadata: Map::new(),
        skipped: BTreeMap::new(),
        kinds: BTreeMap::new(),
    };
    scanner.whitespace()?;
    if scanner.peek()? != Some(b'}') {
        loop {
            let mut key = Some(Vec::new());
            scanner.string(&mut key)?;
            let key: String = serde_json::from_slice(&key.unwrap()).map_err(|_| corrupt())?;
            if key.len() > 256
                || fields.metadata.contains_key(&key)
                || fields.skipped.contains_key(&key)
            {
                return Err(corrupt());
            }
            scanner.whitespace()?;
            if scanner.byte(&mut None)? != b':' {
                return Err(corrupt());
            }
            scanner.whitespace()?;
            let start = scanner.position;

            let kind = scanner.peek()?.ok_or_else(corrupt)?;
            fields.kinds.insert(key.clone(), kind);
            let mut raw = if skip.contains(&key.as_str()) {
                None
            } else {
                Some(Vec::new())
            };
            scanner.value(&mut raw)?;
            if scanner.position == start {
                return Err(corrupt());
            }
            if let Some(raw) = raw {
                fields
                    .metadata
                    .insert(key, serde_json::from_slice(&raw).map_err(|_| corrupt())?);
            } else {
                fields.skipped.insert(key, start..scanner.position);
            }
            scanner.whitespace()?;
            if scanner.peek()? == Some(b'}') {
                break;
            }
            if scanner.byte(&mut None)? != b',' {
                return Err(corrupt());
            }
            scanner.whitespace()?;
        }
    }
    scanner.byte(&mut None)?;
    scanner.whitespace()?;
    if scanner.peek()?.is_some() {
        return Err(corrupt());
    }
    Ok(fields)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn skips_large_nested_and_escaped_projection_with_exact_byte_span() {
        let raw = format!("{{\"before\":1,\"projection\":{{\"x\":\"{}\\\"\\\\汉字\",\"nested\":[{{}}]}},\"after\":2}}", "x".repeat(9 * 1024 * 1024));
        let fields = object(raw.as_bytes(), &["projection"], &mut || false).unwrap();
        assert_eq!(
            fields.metadata,
            serde_json::from_value::<Map<String, Value>>(serde_json::json!({"before":1,"after":2}))
                .unwrap()
        );
        let span = &fields.skipped["projection"];
        assert!(serde_json::from_slice::<Value>(
            &raw.as_bytes()[span.start as usize..span.end as usize]
        )
        .unwrap()
        .is_object());
        assert!(object(raw.as_bytes(), &[], &mut || false).is_err());
        assert!(object(
            b"{\"projection\":{},\"projection\":{}}".as_slice(),
            &["projection"],
            &mut || false
        )
        .is_err());
    }
}
