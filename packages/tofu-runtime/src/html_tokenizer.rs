//! Iterative tokenizer for the generated Python HTML projection profile.
//! Attributes are scanned for boundaries but never allocated or decoded: the
//! owning text projection does not consume them. Feed and EOF remain distinct.
use super::{contract, Error, Projection};

pub(super) fn space(value: u8) -> bool {
    matches!(value, b' ' | b'\t' | b'\n' | b'\r' | b'\x0c')
}
fn letter(value: u8) -> bool {
    value.is_ascii_alphabetic()
}
fn name_byte(value: u8) -> bool {
    !space(value) && !matches!(value, b'/' | b'>')
}

fn unicode_flag(ch: char, ranges: &[(u32, u32)]) -> bool {
    ranges
        .binary_search_by(|(start, end)| {
            if (ch as u32) < *start {
                std::cmp::Ordering::Greater
            } else if (ch as u32) > *end {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

fn lower_tag(source: &str) -> String {
    if source.is_ascii() {
        return source.to_ascii_lowercase();
    }
    let mut output = String::with_capacity(source.len());
    let mut chars = source.chars();
    let mut previous_cased = false;
    while let Some(ch) = chars.next() {
        // Python's default Unicode lowercase has one contextual mapping. Its
        // predicates are generated too, rather than borrowed from Rust's possibly
        // newer Unicode tables (which would change malformed-tag nesting).
        if ch == 'Σ'
            && previous_cased
            && !chars
                .clone()
                .find(|c| !unicode_flag(*c, contract::CASE_IGNORABLE))
                .is_some_and(|c| unicode_flag(c, contract::CASED))
        {
            output.push('ς');
        } else if let Ok(at) = contract::LOWER.binary_search_by_key(&(ch as u32), |(code, _)| *code)
        {
            output.push_str(contract::LOWER[at].1);
        } else {
            output.push(ch);
        }
        if !unicode_flag(ch, contract::CASE_IGNORABLE) {
            previous_cased = unicode_flag(ch, contract::CASED);
        }
    }
    output
}

// Return the original tag-name boundary, complete tag end and actual XHTML
// close marker. A slash inside an unquoted attribute value is not that marker.
fn tag(source: &str, start: usize) -> Option<(usize, usize, bool)> {
    let bytes = source.as_bytes();
    let mut cursor = start;
    while cursor < bytes.len() && name_byte(bytes[cursor]) {
        cursor += 1;
    }
    let name_end = cursor;
    loop {
        while cursor < bytes.len()
            && (space(bytes[cursor])
                || (bytes[cursor] == b'/' && bytes.get(cursor + 1) != Some(&b'>')))
        {
            cursor += 1;
        }
        match bytes.get(cursor) {
            Some(b'>') => return Some((name_end, cursor + 1, false)),
            Some(b'/') if bytes.get(cursor + 1) == Some(&b'>') => {
                return Some((name_end, cursor + 2, true))
            }
            None => return None,
            _ => {}
        }
        if !matches!(
            bytes[cursor - 1],
            b'\'' | b'"' | b'/' | b' ' | b'\t' | b'\n' | b'\r' | b'\x0c'
        ) {
            return None;
        }
        // Attribute's first character may be '='; subsequent characters may not.
        cursor += 1;
        while cursor < bytes.len()
            && !space(bytes[cursor])
            && !matches!(bytes[cursor], b'/' | b'=' | b'>')
        {
            cursor += 1;
        }
        let attribute_end = cursor;
        while cursor < bytes.len() && space(bytes[cursor]) {
            cursor += 1;
        }
        if bytes.get(cursor) == Some(&b'=') {
            cursor += 1;
            let value_start = cursor;
            while cursor < bytes.len() && space(bytes[cursor]) {
                cursor += 1;
            }
            if matches!(bytes.get(cursor), Some(b'\'' | b'"')) {
                let leading_space = cursor > value_start;
                let quote = bytes[cursor];
                cursor += 1;
                while cursor < bytes.len() && bytes[cursor] != quote {
                    cursor += 1;
                }
                if cursor == bytes.len() {
                    // An unterminated optional value is abandoned. If whitespace
                    // or a quote preceded '=', it may start another attribute.
                    cursor = if leading_space {
                        value_start
                    } else {
                        attribute_end
                    };
                    continue;
                }
                cursor += 1;
            } else {
                while cursor < bytes.len() && !space(bytes[cursor]) && bytes[cursor] != b'>' {
                    cursor += 1;
                }
            }
        }
    }
}

struct RawText {
    name: String,
    entities: bool,
}
fn interesting(source: &str, start: usize, mode: &Option<RawText>) -> Option<usize> {
    let bytes = source.as_bytes();
    for at in start..bytes.len() {
        match mode {
            None if matches!(bytes[at], b'&' | b'<') => return Some(at),
            Some(raw) if raw.name != "plaintext" => {
                if raw.entities && bytes[at] == b'&' {
                    return Some(at);
                }
                let end = at + 2 + raw.name.len();
                if bytes[at..].starts_with(b"</")
                    && end < bytes.len()
                    && bytes[at + 2..end].eq_ignore_ascii_case(raw.name.as_bytes())
                    && (space(bytes[end]) || matches!(bytes[end], b'/' | b'>'))
                {
                    return Some(at);
                }
            }
            _ => {}
        }
    }
    None
}

fn entity(source: &str, at: usize, projection: &mut Projection) -> Option<usize> {
    let bytes = source.as_bytes();
    if !bytes.get(at + 1).copied().is_some_and(letter) {
        return None;
    }
    let mut end = at + 2;
    while end < bytes.len()
        && (bytes[end].is_ascii_alphanumeric() || matches!(bytes[end], b'.' | b'-'))
    {
        end += 1;
    }
    // The terminal punctuation can itself be the regex delimiter at EOF.
    if end == bytes.len() && matches!(bytes[end - 1], b'.' | b'-') {
        end -= 1;
    }
    if end == bytes.len() {
        return None;
    }
    let name = &source[at + 1..end];
    match contract::ENTITIES.binary_search_by_key(&name, |(name, _)| name) {
        Ok(index) => projection.text(contract::ENTITIES[index].1),
        Err(_) => {
            projection.text("&");
            projection.text(name);
        }
    }
    Some(end + usize::from(bytes[end] == b';'))
}

fn number(source: &str, at: usize, projection: &mut Projection) -> Result<Option<usize>, Error> {
    let bytes = source.as_bytes();
    let hex = matches!(bytes.get(at + 2), Some(b'x' | b'X'));
    let first = at + if hex { 3 } else { 2 };
    let mut end = first;
    while end < bytes.len()
        && if hex {
            bytes[end].is_ascii_hexdigit()
        } else {
            bytes[end].is_ascii_digit()
        }
    {
        end += 1;
    }
    if end == first || end == bytes.len() || bytes[end].is_ascii_hexdigit() {
        return Ok(None);
    }
    if !hex && contract::DECIMAL_DIGITS != 0 && end - first > contract::DECIMAL_DIGITS {
        return Err(Error::NumericReference);
    }
    let mut value = 0u32;
    for &digit in &bytes[first..end] {
        value = value
            .saturating_mul(if hex { 16 } else { 10 })
            .saturating_add((digit as char).to_digit(if hex { 16 } else { 10 }).unwrap());
    }
    let value = if (128..160).contains(&value) {
        contract::C1[(value - 128) as usize]
    } else if value == 0 {
        '\u{fffd}'
    } else {
        char::from_u32(value).unwrap_or('\u{fffd}')
    };
    projection.pending.push(value);
    Ok(Some(end + usize::from(bytes[end] == b';')))
}

fn comment_end(source: &str, start: usize) -> Option<usize> {
    let tail = &source[start..];
    let mut offset = 0;
    while let Some(next) = tail[offset..].find("--") {
        let at = offset + next;
        if tail[at..].starts_with("-->") {
            return Some(start + at + 3);
        }
        if tail[at..].starts_with("--!>") {
            return Some(start + at + 4);
        }
        offset = at + 1;
    }
    if tail.starts_with('>') {
        Some(start + 1)
    } else if tail.starts_with("->") {
        Some(start + 2)
    } else {
        None
    }
}

fn scan(
    source: &str,
    mut at: usize,
    eof: bool,
    mode: &mut Option<RawText>,
    projection: &mut Projection,
) -> Result<usize, Error> {
    let bytes = source.as_bytes();
    while at < bytes.len() {
        let next = match interesting(source, at, mode) {
            Some(next) => next,
            None if mode.is_some() => break,
            None => bytes.len(),
        };
        projection.text(&source[at..next]);
        at = next;
        if at == bytes.len() {
            break;
        }
        let tail = &source[at..];
        if bytes[at] == b'<' {
            if bytes.get(at + 1).copied().is_some_and(letter) {
                if let Some((name_end, end, empty)) = tag(source, at + 1) {
                    let name = lower_tag(&source[at + 1..name_end]);
                    projection.start(&name, empty);
                    if !empty
                        && (contract::CDATA.contains(&name.as_str())
                            || name == "plaintext"
                            || contract::RCDATA.contains(&name.as_str()))
                    {
                        *mode = Some(RawText {
                            entities: contract::RCDATA.contains(&name.as_str()),
                            name,
                        });
                    }
                    at = end;
                    continue;
                }
                if !eof {
                    break;
                }
                at = bytes.len(); // EOF inside a start tag is discarded by this profile.
            } else if let Some(rest) = tail.strip_prefix("</") {
                if let Some(close) = rest.find('>') {
                    if bytes.get(at + 2).copied().is_some_and(letter) {
                        if let Some((name_end, end, _)) = tag(source, at + 2) {
                            projection.end(&lower_tag(&source[at + 2..name_end]));
                            *mode = None;
                            at = end;
                        } else if !eof {
                            break;
                        } else {
                            at = bytes.len();
                        }
                    } else {
                        if bytes.get(at + 2) != Some(&b'>') {
                            projection.flush();
                        }
                        at += 3 + close;
                    }
                } else {
                    if !eof {
                        break;
                    }
                    if tail == "</" {
                        projection.text(tail);
                    } else if !bytes.get(at + 2).copied().is_some_and(letter) {
                        projection.flush();
                    }
                    at = bytes.len();
                }
            } else if tail.starts_with("<!--") {
                if let Some(end) = comment_end(source, at + 4) {
                    projection.flush();
                    at = end;
                } else if !eof {
                    break;
                } else {
                    projection.flush();
                    at = bytes.len();
                }
            } else if let Some(body) = tail.strip_prefix("<![CDATA[") {
                if let Some(end) = body.find("]]>") {
                    projection.flush();
                    projection.text(&body[..end]);
                    projection.flush();
                    at += 12 + end;
                } else if !eof {
                    break;
                } else {
                    projection.flush();
                    projection.text(body);
                    projection.flush();
                    at = bytes.len();
                }
            } else if tail.starts_with("<!") || tail.starts_with("<?") {
                if let Some(end) = tail[2..].find('>') {
                    let end = end + 2;
                    projection.flush();
                    if tail.starts_with("<![") && bytes[at + end - 1] == b']' {
                        let body = &tail[3..end - 1];
                        if body
                            .as_bytes()
                            .get(..6)
                            .is_some_and(|prefix| prefix.eq_ignore_ascii_case(b"CDATA["))
                        {
                            projection.text(&body[6..]);
                            projection.flush();
                        }
                    }
                    at += end + 1;
                } else if !eof {
                    break;
                } else {
                    projection.flush();
                    at = bytes.len();
                }
            } else if at + 1 < bytes.len() || eof {
                projection.text("<");
                at += 1;
            } else {
                break;
            }
        } else if tail.starts_with("&#") {
            if let Some(end) = number(source, at, projection)? {
                at = end;
            } else {
                if tail.contains(';') {
                    projection.text("&#");
                    at += 2;
                }
                break;
            }
        } else if let Some(end) = entity(source, at, projection) {
            at = end;
        } else if bytes.get(at + 1).is_some_and(|v| letter(*v) || *v == b'#') {
            if eof && tail.len() == 2 {
                at += 1;
            }
            break;
        } else if at + 1 < bytes.len() {
            projection.text("&");
            at += 1;
        } else {
            break;
        }
    }
    if eof && at < bytes.len() {
        projection.text(&source[at..]);
        at = bytes.len();
    }
    Ok(at)
}

pub(super) fn parse(source: &str, projection: &mut Projection) -> Result<(), Error> {
    let mut mode = None;
    let end = scan(source, 0, false, &mut mode, projection)?;
    scan(source, end, true, &mut mode, projection)?;
    Ok(())
}
