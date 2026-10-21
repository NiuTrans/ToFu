//! Canonical tool-result queries over the existing owner-bound artifact/blob store.
//!
//! Shared policy: contracts/tool_result_query_v1.json. Routes seek only ancestors;
//! raw segments use verified blob ranges. Persistent private HMAC keys bind opaque
//! continuations to owner/root/operation/query/selector and observed expiry.
use super::*;
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hmac::{Hmac, Mac};
use rand_core::{OsRng, RngCore};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, Instant};

static CONTRACT: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../../../contracts/tool_result_query_v1.json"))
        .expect("checked tool-result query contract")
});
fn bound(name: &str) -> usize {
    CONTRACT["limits"][name].as_u64().expect("query limit") as usize
}
fn cursor_max() -> usize {
    CONTRACT["cursor"]["maxLength"]
        .as_u64()
        .expect("cursor limit") as usize
}

#[derive(Debug)]
struct QueryFailure {
    kind: &'static str,
    message: &'static str,
}

impl std::fmt::Display for QueryFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message)
    }
}

impl std::error::Error for QueryFailure {}

fn query_failure(kind: &'static str, message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, QueryFailure { kind, message })
}

pub(crate) fn failure_classification(error: &io::Error) -> Option<(&'static str, bool)> {
    let failure = error.get_ref()?.downcast_ref::<QueryFailure>()?;
    let policy = &CONTRACT["failures"][failure.kind];
    Some((
        policy["storageCode"].as_str().expect("query failure code"),
        policy["retryable"]
            .as_bool()
            .expect("query failure retryability"),
    ))
}

fn signed_cursor(raw: &str) -> bool {
    raw.split_once('.').is_some_and(|(prefix, _)| {
        Some(prefix) == CONTRACT["cursor"]["currentPrefix"].as_str()
            || Some(prefix) == CONTRACT["cursor"]["previousPrefix"].as_str()
            || Some(prefix) == CONTRACT["cursor"]["legacyPrefix"].as_str()
    })
}
fn encode<T: Serialize>(value: &T) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data("tool-result query encoding failed"))
}
fn private_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(
        transaction,
        crate::generated_tofudb_ir::TOOL_RESULT_CURSOR_KEY_NAMESPACE,
        b"cursor-key-v1",
    )
}
pub(crate) fn ensure_key(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<()> {
    let key = private_key(transaction)?;
    if database.entity_get(transaction, &key)?.is_none() {
        let mut bytes = [0u8; 32];
        OsRng
            .try_fill_bytes(&mut bytes)
            .map_err(|_| invalid_data("cursor key randomness unavailable"))?;
        database.entity_put(transaction, key, bytes.to_vec())?;
    }
    Ok(())
}
fn signing_key(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    database
        .entity_get(transaction, &private_key(transaction)?)?
        .filter(|bytes| bytes.len() == 32)
        .ok_or_else(|| {
            query_failure(
                "artifact_query_unavailable",
                "Continuation key unavailable; publish an artifact before querying",
            )
        })
}

pub(crate) fn validate_input(cursor: &Value, selector: &str) -> io::Result<()> {
    if cursor.as_u64().is_none()
        && !cursor.as_str().is_some_and(|s| {
            s.len() <= cursor_max()
                && (s.is_empty() || s.bytes().all(|b| b.is_ascii_digit()) || signed_cursor(s))
        })
    {
        return Err(query_failure(
            "invalid_cursor",
            "Invalid continuation cursor",
        ));
    }
    if !selector.is_empty() {
        if !selector.starts_with('/')
            || selector.chars().count() > 1024
            || selector[1..].split('/').count() > bound("pointerDepth")
        {
            return Err(query_failure("invalid_selector", "Invalid JSON pointer"));
        }
        let mut chars = selector.chars();
        while let Some(ch) = chars.next() {
            if ch == '~' && !matches!(chars.next(), Some('0' | '1')) {
                return Err(query_failure(
                    "invalid_selector",
                    "Invalid JSON pointer escape",
                ));
            }
        }
    }
    Ok(())
}

pub(crate) fn validate_manifest(text: &str) -> io::Result<Value> {
    let fail = || invalid_input("Invalid result manifest");
    if text.len() > bound("manifestBytes") {
        return Err(fail());
    }
    let v: Value = serde_json::from_str(text).map_err(|_| fail())?;
    let object = v.as_object().ok_or_else(fail)?;
    let fields = CONTRACT["manifestRequired"]
        .as_array()
        .expect("required manifest fields");
    if object.len() != fields.len()
        || fields
            .iter()
            .any(|key| !object.contains_key(key.as_str().unwrap()))
    {
        return Err(fail());
    }
    let id = v["resultId"].as_str().ok_or_else(fail)?;
    if uuid::Uuid::parse_str(id).map_err(|_| fail())?.to_string() != id {
        return Err(fail());
    }
    if !CONTRACT["executionStatuses"]
        .as_array()
        .unwrap()
        .contains(&v["executionStatus"])
    {
        return Err(fail());
    }
    for field in ["sourceCoverage", "retentionCoverage"] {
        if !matches!(
            v[field].as_str(),
            Some("complete" | "full" | "partial" | "incomplete" | "unavailable" | "unknown")
        ) {
            return Err(fail());
        }
    }
    let parts = v["parts"].as_array().ok_or_else(fail)?;
    let children = v["children"].as_array().ok_or_else(fail)?;
    if parts.len() + children.len() > bound("scanNodes") {
        return Err(fail());
    }
    for part in parts {
        let part = part.as_object().ok_or_else(fail)?;
        let required: &[&str] = match part.get("kind").and_then(Value::as_str) {
            Some("text" | "json") => &["kind", "text"],
            Some("artifact") => &["kind", "artifactRef"],
            Some("media") => &["kind", "reference", "mimeType"],
            _ => return Err(fail()),
        };
        if required
            .iter()
            .any(|k| !part.get(*k).is_some_and(Value::is_string))
        {
            return Err(fail());
        }
        for (k, value) in part {
            if required.contains(&k.as_str()) {
                continue;
            }
            if !CONTRACT["manifestOptionalPartFields"]
                .as_array()
                .unwrap()
                .contains(&json!(k))
            {
                return Err(fail());
            }
            if matches!(k.as_str(), "sourceOrdinal" | "byteSize" | "omittedCount") {
                if value.as_u64().is_none() {
                    return Err(fail());
                }
            } else if !value.as_str().is_some_and(|s| s.chars().count() <= 2048) {
                return Err(fail());
            }
            if k == "sha256"
                && !value.as_str().is_some_and(|s| {
                    s.len() == 64
                        && s.bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                })
            {
                return Err(fail());
            }
        }
        match part["kind"].as_str().unwrap() {
            "json" => {
                serde_json::from_str::<Value>(part["text"].as_str().unwrap())
                    .map_err(|_| fail())?;
            }
            "artifact" => {
                parse_artifact_ref(part["artifactRef"].as_str().unwrap())?;
            }
            "media" => {
                let reference = part["reference"].as_str().unwrap();
                if (reference.is_empty()
                    && part.get("status").and_then(Value::as_str) != Some("unavailable"))
                    || reference.chars().count() > 2048
                    || part["mimeType"].as_str().unwrap().chars().count() > 128
                {
                    return Err(fail());
                }
            }
            _ => {}
        }
    }
    for child in children {
        let child = child.as_object().ok_or_else(fail)?;
        if child.len() != 3
            || ["name", "callId", "artifactRef"]
                .iter()
                .any(|k| !child.get(*k).is_some_and(Value::is_string))
        {
            return Err(fail());
        }
        if child["name"].as_str().unwrap().chars().count() > 200
            || child["callId"].as_str().unwrap().chars().count() > 200
        {
            return Err(fail());
        }
        parse_artifact_ref(child["artifactRef"].as_str().unwrap())?;
    }
    Ok(v)
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Position {
    r: Vec<usize>,
    p: usize,
    b: usize,
    o: usize,
    skip: usize,
    expiry: u64,
    partial: bool,
}
fn position_bytes(position: &Position) -> Vec<u8> {
    let policy = &CONTRACT["cursor"]["positionEncoding"];
    let mut values = vec![
        policy["byteOffsetFlag"].as_u64().unwrap()
            | if position.partial {
                policy["partialFlag"].as_u64().unwrap()
            } else {
                0
            },
        position.r.len() as u64,
    ];
    values.extend(position.r.iter().map(|n| *n as u64));
    for field in policy["fields"].as_array().unwrap() {
        values.push(match field.as_str().unwrap() {
            "p" => position.p as u64,
            "b" => position.b as u64,
            "o" => position.o as u64,
            "skip" => position.skip as u64,
            "expiry" => position.expiry,
            _ => unreachable!("declared position field"),
        });
    }
    let mut bytes = Vec::with_capacity(values.len());
    for mut value in values {
        while value >= 128 {
            bytes.push((value as u8 & 127) | 128);
            value >>= 7;
        }
        bytes.push(value as u8);
    }
    bytes
}

fn position_from_bytes(bytes: &[u8]) -> io::Result<Position> {
    let fail = || query_failure("invalid_cursor", "Invalid continuation cursor");
    let mut offset = 0;
    let mut integer = || -> io::Result<u64> {
        let mut value = 0;
        for shift in (0..64).step_by(7) {
            let byte = *bytes.get(offset).ok_or_else(fail)?;
            offset += 1;
            if (shift == 63 && byte > 1) || (shift != 0 && byte == 0) {
                return Err(fail());
            }
            value |= u64::from(byte & 127) << shift;
            if byte & 128 == 0 {
                return Ok(value);
            }
        }
        Err(fail())
    };
    let policy = &CONTRACT["cursor"]["positionEncoding"];
    let partial = policy["partialFlag"].as_u64().unwrap();
    let byte_offset = policy["byteOffsetFlag"].as_u64().unwrap();
    let flags = integer()?;
    let count = usize::try_from(integer()?).map_err(|_| fail())?;
    if flags & !(partial | byte_offset) != 0
        || flags & byte_offset == 0
        || count > bound("depth") + 1
    {
        return Err(fail());
    }
    let mut position = Position {
        partial: flags & partial != 0,
        ..Position::default()
    };
    for _ in 0..count {
        position
            .r
            .push(usize::try_from(integer()?).map_err(|_| fail())?);
    }
    for field in policy["fields"].as_array().unwrap() {
        let value = integer()?;
        if field == "expiry" {
            position.expiry = value;
            continue;
        }
        let value = usize::try_from(value).map_err(|_| fail())?;
        match field.as_str().unwrap() {
            "p" => position.p = value,
            "b" => position.b = value,
            "o" => position.o = value,
            "skip" => position.skip = value,
            _ => unreachable!("declared position field"),
        }
    }
    if offset != bytes.len() {
        return Err(fail());
    }
    Ok(position)
}

fn scoped_mac(
    key: &[u8],
    binding: &Value,
    bytes: &[u8],
    previous: bool,
) -> io::Result<Hmac<Sha256>> {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(key).map_err(|_| invalid_data("Invalid cursor key"))?;
    let domain = CONTRACT["cursor"]["authentication"]
        [if previous { "previousDomain" } else { "domain" }]
    .as_str()
    .expect("cursor signing domain");
    mac.update(domain.as_bytes());
    mac.update(b"\0");
    mac.update(&encode(binding)?);
    mac.update(b"\0");
    mac.update(bytes);
    Ok(mac)
}
fn sign(key: &[u8], binding: &Value, position: &Position) -> io::Result<String> {
    let bytes = position_bytes(position);
    let signature = scoped_mac(key, binding, &bytes, false)?
        .finalize()
        .into_bytes();
    let prefix = CONTRACT["cursor"]["currentPrefix"]
        .as_str()
        .expect("current cursor prefix");
    let token = format!(
        "{prefix}.{}.{}",
        URL_SAFE_NO_PAD.encode(bytes),
        URL_SAFE_NO_PAD.encode(signature)
    );
    if token.len() > cursor_max() {
        return Err(query_failure(
            "artifact_query_unavailable",
            "Continuation metadata too large",
        ));
    }
    Ok(token)
}
fn decode(
    key: &[u8],
    binding: &Value,
    raw: &Value,
    search: bool,
    now: u64,
) -> io::Result<Position> {
    let fail = || query_failure("invalid_cursor", "Invalid continuation cursor");
    let string = raw
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| raw.to_string());
    if string.len() > cursor_max() {
        return Err(fail());
    }
    if string.is_empty() || string.bytes().all(|c| c.is_ascii_digit()) {
        let skip: usize = if string.is_empty() {
            0
        } else {
            string.parse().map_err(|_| fail())?
        };
        if search && skip != 0 {
            return Err(fail());
        }
        return Ok(Position {
            skip,
            ..Position::default()
        });
    }
    let mut split = string.split('.');
    let prefix = split.next().ok_or_else(fail)?;
    let bytes = URL_SAFE_NO_PAD
        .decode(split.next().ok_or_else(fail)?)
        .map_err(|_| fail())?;
    let signature = split.next().ok_or_else(fail)?;
    if split.next().is_some() {
        return Err(fail());
    }
    let previous = Some(prefix) == CONTRACT["cursor"]["previousPrefix"].as_str();
    let position: Position =
        if previous || Some(prefix) == CONTRACT["cursor"]["currentPrefix"].as_str() {
            let signature = URL_SAFE_NO_PAD.decode(signature).map_err(|_| fail())?;
            scoped_mac(key, binding, &bytes, previous)?
                .verify_slice(&signature)
                .map_err(|_| fail())?;
            if previous {
                serde_json::from_slice(&bytes).map_err(|_| fail())?
            } else {
                position_from_bytes(&bytes)?
            }
        } else if Some(prefix) == CONTRACT["cursor"]["legacyPrefix"].as_str() {
            if signature.len() != 64 || !signature.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Err(fail());
            }
            let signature: Vec<u8> = (0..64)
                .step_by(2)
                .map(|i| u8::from_str_radix(&signature[i..i + 2], 16).map_err(|_| fail()))
                .collect::<io::Result<_>>()?;
            let mut mac = Hmac::<Sha256>::new_from_slice(key).map_err(|_| fail())?;
            mac.update(&bytes);
            mac.verify_slice(&signature).map_err(|_| fail())?;
            let (scope, position): (Value, Position) =
                serde_json::from_slice(&bytes).map_err(|_| fail())?;
            if scope != *binding {
                return Err(fail());
            }
            position
        } else {
            return Err(fail());
        };
    if position.r.len() > bound("depth") + 1 || position.r.iter().any(|i| *i >= bound("scanNodes"))
    {
        return Err(fail());
    }
    if position.expiry != 0 && position.expiry <= now {
        return Err(query_failure(
            "continuation_unavailable",
            "Continuation evidence expired; start a new query",
        ));
    }
    Ok(position)
}

#[derive(Clone)]
struct Node {
    document: ToolResultDocument,
    manifest: Option<Arc<Value>>,
}
enum Segment {
    Raw(ToolResultDocument),
    Part(Arc<Value>, usize),
    Issue(&'static str),
    End,
}
struct Resolved {
    segment: Segment,
    reference: String,
    path: Vec<Value>,
    frames: Vec<usize>,
}
struct Walker<'a> {
    database: &'a AuthorityDatabase,
    transaction: &'a mut AuthorityTransaction,
    now: u64,
    root: &'a str,
    state: Position,
    cache: HashMap<String, Option<Node>>,
    scanned: usize,
    nodes: usize,
    issues: Vec<Value>,
    deadline: Instant,
}
impl Walker<'_> {
    fn issue(&mut self, path: &[Value], reason: &str) {
        if self.issues.len() < bound("scanNodes") {
            self.issues.push(json!({"path":path,"reason":reason}));
        }
        self.state.partial = true;
    }
    fn load(&mut self, reference: &str) -> io::Result<Option<Node>> {
        if let Some(node) = self.cache.get(reference) {
            return Ok(node.clone());
        }
        let digest = parse_artifact_ref(reference)?;
        self.nodes += 1;
        let Some(document) = read_tool(self.database, self.transaction, digest)?
            .filter(|doc| doc.expires_at_ms > self.now)
        else {
            self.cache.insert(reference.to_owned(), None);
            return Ok(None);
        };
        self.state.expiry = if self.state.expiry == 0 {
            document.expires_at_ms
        } else {
            self.state.expiry.min(document.expires_at_ms)
        };
        let manifest = if document.media_type == TOOL_RESULT_MANIFEST_MEDIA_TYPE {
            let bytes = materialize_blob(
                self.database,
                self.transaction,
                document.content,
                bound("manifestBytes"),
            )?;
            self.scanned += bytes.len();
            Some(Arc::new(validate_manifest(
                std::str::from_utf8(&bytes).map_err(|_| invalid_data("Invalid manifest UTF-8"))?,
            )?))
        } else {
            None
        };
        let node = Node { document, manifest };
        self.cache.insert(reference.to_owned(), Some(node.clone()));
        Ok(Some(node))
    }
    fn resolve(&mut self) -> io::Result<Resolved> {
        let mut reference = self.root.to_owned();
        let mut path: Vec<Value> = Vec::new();
        let mut frames = Vec::new();
        let mut seen = HashSet::new();
        let mut depth = 0;
        loop {
            let issue = if !seen.insert(reference.clone()) {
                Some("cycle")
            } else if depth > bound("depth") {
                Some("depth_budget")
            } else {
                None
            };
            if let Some(reason) = issue {
                return Ok(Resolved {
                    segment: Segment::Issue(reason),
                    reference,
                    path,
                    frames,
                });
            }
            let Some(node) = self.load(&reference)? else {
                return Ok(Resolved {
                    segment: Segment::Issue("unavailable"),
                    reference,
                    path,
                    frames,
                });
            };
            let Some(manifest) = node.manifest else {
                if self.state.r.len() != depth {
                    return Err(query_failure(
                        "continuation_unavailable",
                        "Continuation route unavailable",
                    ));
                }
                return Ok(Resolved {
                    segment: Segment::Raw(node.document),
                    reference,
                    path,
                    frames,
                });
            };
            if let Some(Value::Object(last)) = path.last_mut() {
                last.insert("resultId".into(), manifest["resultId"].clone());
                last.insert(
                    "executionStatus".into(),
                    manifest["executionStatus"].clone(),
                );
            }
            for key in ["sourceCoverage", "retentionCoverage"] {
                if !matches!(manifest[key].as_str(), Some("complete" | "full")) {
                    self.issue(&path, key);
                }
            }
            let parts = manifest["parts"].as_array().unwrap();
            let children = manifest["children"].as_array().unwrap();
            if self.state.r.len() <= depth {
                self.state.r.push(0);
            }
            let index = self.state.r[depth];
            if index >= parts.len() + children.len() {
                return Ok(Resolved {
                    segment: Segment::End,
                    reference,
                    path,
                    frames,
                });
            }
            frames.push(parts.len() + children.len());
            if index < parts.len() {
                path.push(json!(format!("/parts/{index}")));
                if parts[index]["kind"] != "artifact" {
                    if self.state.r.len() != depth + 1 {
                        return Err(query_failure(
                            "continuation_unavailable",
                            "Continuation route unavailable",
                        ));
                    }
                    return Ok(Resolved {
                        segment: Segment::Part(manifest, index),
                        reference,
                        path,
                        frames,
                    });
                }
                reference = parts[index]["artifactRef"].as_str().unwrap().to_owned();
            } else {
                let child = &children[index - parts.len()];
                path.push(json!({"name":child["name"], "callId":child["callId"]}));
                reference = child["artifactRef"].as_str().unwrap().to_owned();
            }
            depth += 1;
        }
    }
    fn advance(&mut self, mut frames: Vec<usize>) -> bool {
        self.state.p = 0;
        self.state.b = 0;
        while let Some(size) = frames.pop() {
            self.state.r.truncate(frames.len() + 1);
            let last = self.state.r.last_mut().unwrap();
            *last += 1;
            if *last < size {
                return false;
            }
        }
        self.state.r.clear();
        true
    }
    fn raw_window(&self, document: &ToolResultDocument) -> io::Result<(String, bool)> {
        let offset = self.state.b as u64;
        if offset > document.size_bytes {
            return Err(query_failure(
                "continuation_unavailable",
                "Continuation segment unavailable",
            ));
        }
        let reader = self.database.blob_reader(
            self.transaction.tenant_id(),
            self.transaction.owner_user_id(),
            document.content.into(),
        )?;
        let bytes =
            reader.read_range(offset, (document.size_bytes - offset).min(65536) as usize)?;
        let length = match std::str::from_utf8(&bytes) {
            Ok(_) => bytes.len(),
            Err(error)
                if error.error_len().is_none()
                    && offset + (bytes.len() as u64) < document.size_bytes =>
            {
                error.valid_up_to()
            }
            Err(_) => return Err(invalid_data("Tool-result content is not UTF-8")),
        };
        Ok((
            std::str::from_utf8(&bytes[..length]).unwrap().to_owned(),
            offset + length as u64 == document.size_bytes,
        ))
    }
    fn budget_exhausted(&self) -> bool {
        self.nodes >= bound("scanNodes")
            || self.scanned >= bound("scanBytes")
            || Instant::now() >= self.deadline
    }
}
fn selected(text: &str, selector: &str) -> Option<String> {
    if selector.is_empty() {
        return Some(text.to_owned());
    }
    let value: Value = serde_json::from_str(text).ok()?;
    serde_json::to_string(value.pointer(selector)?).ok()
}
fn text_window(text: &str, position: usize) -> (String, bool) {
    let mut chars = text.chars().skip(position);
    let piece: String = chars.by_ref().take(65536).collect();
    (piece, chars.next().is_none())
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn run(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    root: &str,
    now: u64,
    raw: &Value,
    limit: usize,
    selector: &str,
    query: Option<&str>,
) -> io::Result<Option<Vec<u8>>> {
    validate_input(raw, selector)?;
    parse_artifact_ref(root)?;
    if now == 0
        || limit == 0
        || limit
            > if query.is_some() {
                20
            } else {
                MAX_TOOL_RESULT_RANGE_BYTES
            }
        || query.is_some_and(|s| s.is_empty() || s.chars().count() > 200)
    {
        return Err(invalid_input("Invalid tool-result query"));
    }
    // Invalid numeric search cursors refuse even when the root is unavailable.
    if query.is_some()
        && raw
            .as_u64()
            .or_else(|| raw.as_str().and_then(|s| s.parse().ok()))
            .is_some_and(|n| n != 0)
    {
        return Err(query_failure(
            "invalid_cursor",
            "Invalid continuation cursor",
        ));
    }
    let operation = if query.is_some() { "search" } else { "read" };
    let binding = json!([
        transaction.owner_user_id(),
        root,
        operation,
        query.unwrap_or(""),
        selector
    ]);
    // Missing roots remain unavailable without requiring a key from a newer put.
    if read_tool(database, transaction, parse_artifact_ref(root)?)?
        .filter(|doc| doc.expires_at_ms > now)
        .is_none()
    {
        if raw.as_str().is_some_and(signed_cursor) {
            return Err(query_failure(
                "continuation_unavailable",
                "Continuation root unavailable",
            ));
        }
        return Ok(None);
    }
    let key = signing_key(database, transaction)?;
    let initial = decode(&key, &binding, raw, query.is_some(), now)?;
    let mut walker = Walker {
        database,
        transaction,
        now,
        root,
        state: initial.clone(),
        cache: HashMap::new(),
        scanned: 0,
        nodes: 0,
        issues: Vec::new(),
        deadline: Instant::now() + Duration::from_secs(bound("scanSeconds") as u64),
    };
    let first = walker.load(root)?.unwrap();
    let mut result = json!({"artifactRef":root});
    if let Some(manifest) = first.manifest {
        for key in [
            "resultId",
            "executionStatus",
            "sourceCoverage",
            "retentionCoverage",
        ] {
            result[key] = manifest[key].clone();
        }
    }
    let mut finished = false;
    let mut content = String::new();
    let mut matches: Vec<Value> = Vec::new();
    let mut provenance = Vec::new();
    let mut media = Vec::new();
    let needle: Vec<char> = python_casefold(query.unwrap_or("")).chars().collect();
    while !finished && !walker.budget_exhausted() {
        let resolved = walker.resolve()?;
        let (piece, eof) = match resolved.segment {
            Segment::Issue(reason) => {
                if reason == "unavailable" && initial.expiry != 0 {
                    return Err(query_failure(
                        "continuation_unavailable",
                        "Continuation child unavailable; start a new query",
                    ));
                }
                walker.issue(&resolved.path, reason);
                finished = walker.advance(resolved.frames);
                continue;
            }
            Segment::End => {
                finished = walker.advance(resolved.frames);
                continue;
            }
            Segment::Raw(document) => {
                if selector.is_empty() {
                    walker.raw_window(&document)?
                } else {
                    let bytes = materialize_blob(
                        database,
                        walker.transaction,
                        document.content,
                        bound("artifactBytes"),
                    )?;
                    walker.scanned += bytes.len();
                    let text = std::str::from_utf8(&bytes)
                        .map_err(|_| invalid_data("Invalid result UTF-8"))?;
                    let Some(text) = selected(text, selector) else {
                        walker.issue(&resolved.path, "selector_unavailable");
                        finished = walker.advance(resolved.frames);
                        continue;
                    };
                    text_window(&text, walker.state.p)
                }
            }
            Segment::Part(manifest, index) => {
                let part = &manifest["parts"][index];
                let kind = part["kind"].as_str().unwrap();
                if kind == "media" {
                    if query.is_none() {
                        let mut descriptor = part.clone();
                        descriptor["artifactRef"] = json!(resolved.reference);
                        descriptor["path"] = json!(resolved.path);
                        media.push(descriptor);
                    }
                    walker.issue(&resolved.path, "non_text_media");
                    finished = walker.advance(resolved.frames);
                    continue;
                }
                if !selector.is_empty() && kind != "json" {
                    finished = walker.advance(resolved.frames);
                    continue;
                }
                let Some(text) = selected(part["text"].as_str().unwrap(), selector) else {
                    walker.issue(&resolved.path, "selector_unavailable");
                    finished = walker.advance(resolved.frames);
                    continue;
                };
                text_window(&text, walker.state.p)
            }
        };
        if piece.is_empty() {
            finished = walker.advance(resolved.frames);
            continue;
        }
        walker.scanned += piece.len();
        if query.is_none() {
            let mut skip = walker.state.skip.min(piece.len());
            while !piece.is_char_boundary(skip) {
                skip += 1;
            }
            walker.state.skip = walker.state.skip.saturating_sub(skip);
            let mut end = (skip + limit.saturating_sub(content.len())).min(piece.len());
            while !piece.is_char_boundary(end) {
                end -= 1;
            }
            if end == skip && skip < piece.len() && content.is_empty() {
                end += piece[skip..].chars().next().unwrap().len_utf8();
            }
            // A partially filled page may have less than one codepoint left.
            // Yield immediately; rescanning that segment wastes the scan budget.
            if end == 0 && !content.is_empty() {
                break;
            }
            if end > skip {
                content.push_str(&piece[skip..end]);
                provenance.push(json!({"artifactRef":resolved.reference,"path":resolved.path,"segment":walker.state.r,"start":walker.state.o+skip,"end":walker.state.o+end}));
            }
            walker.state.p += piece[..end].chars().count();
            walker.state.b += end;
            walker.state.o += end;
            if end == piece.len() && eof {
                finished = walker.advance(resolved.frames);
            }
            if content.len() >= limit {
                break;
            }
        } else {
            let characters: Vec<char> = piece.chars().collect();
            let mut window = VecDeque::with_capacity(needle.len());
            let mut origins = VecDeque::with_capacity(needle.len());
            let mut consumed = 0;
            let mut last_match_start = None;
            for (index, ch) in characters.iter().enumerate() {
                if index != 0 && index % 4096 == 0 && Instant::now() >= walker.deadline {
                    break;
                }
                for folded in python_casefold(&ch.to_string()).chars() {
                    if window.len() == needle.len() {
                        window.pop_front();
                        origins.pop_front();
                    }
                    window.push_back(folded);
                    origins.push_back(index);
                    if window.iter().eq(needle.iter()) {
                        let origin = origins[0];
                        let found = walker.state.o + origin;
                        if matches
                            .last()
                            .is_none_or(|m| m["offset"].as_u64() != Some(found as u64))
                        {
                            matches.push(json!({"offset":found,"text":characters[origin.saturating_sub(160)..(index+321).min(characters.len())].iter().collect::<String>(),"artifactRef":resolved.reference,"path":resolved.path}));
                            last_match_start = Some(origin);
                            if matches.len() >= limit {
                                consumed = origin + 1;
                                break;
                            }
                        }
                    }
                }
                if matches.len() >= limit {
                    break;
                }
                consumed = index + 1;
            }
            if matches.len() < limit && (!eof || consumed < characters.len()) {
                consumed = consumed
                    .saturating_sub(needle.len())
                    .max(1)
                    .max(last_match_start.map_or(0, |p| p + 1));
            }
            walker.state.p += consumed;
            walker.state.o += consumed;
            walker.state.b += characters[..consumed]
                .iter()
                .map(|c| c.len_utf8())
                .sum::<usize>();
            if consumed == characters.len() && eof {
                finished = walker.advance(resolved.frames);
            }
            if matches.len() >= limit {
                break;
            }
        }
    }
    if !finished
        && walker.state.r == initial.r
        && walker.state.p == initial.p
        && walker.state.o == initial.o
        && walker.state.skip == initial.skip
    {
        return Err(query_failure(
            "query_budget_exhausted",
            "Query budget too small to make progress",
        ));
    }
    if !finished && walker.issues.iter().any(|v| v["reason"] == "unavailable") {
        return Err(query_failure(
            "continuation_unavailable",
            "Continuation child unavailable; start a new query",
        ));
    }
    result["scope"] = json!(if query.is_some() {
        "text_only"
    } else {
        "text_and_media_descriptors"
    });
    result["coverage"] = json!(if walker.state.partial || !finished {
        "incomplete"
    } else {
        "complete"
    });
    result["unavailable"] = json!(walker.issues);
    result["truncated"] = json!(!finished);
    result["nextCursor"] = if finished {
        Value::Null
    } else {
        json!(sign(&key, &binding, &walker.state)?)
    };
    result["resumeCursor"] = json!(sign(&key, &binding, &initial)?);
    if let Some(query) = query {
        result["query"] = json!(query);
        result["items"] = json!(matches);
    } else {
        result["content"] = json!(content);
        result["offset"] = json!(initial.o);
        result["provenance"] = json!(provenance);
        result["media"] = json!(media);
        if finished {
            result["sizeBytes"] = json!(walker.state.o);
        }
    }
    encode(&result).map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_failures_keep_typed_recovery_through_semantic_settlement() {
        for (kind, policy) in CONTRACT["failures"].as_object().unwrap() {
            let failure = crate::semantic_executor::map_storage_error(query_failure(
                kind,
                "a diagnostic cannot change the classification",
            ));
            assert_eq!(failure.code(), policy["storageCode"].as_str().unwrap());
            assert_eq!(failure.retryable(), policy["retryable"].as_bool().unwrap());
        }
        let untyped = crate::semantic_executor::map_storage_error(invalid_input(
            "Invalid continuation cursor",
        ));
        assert_eq!(untyped.code(), "database_protocol_error");
        assert!(!untyped.retryable());
    }

    fn cursor_vectors() -> Value {
        serde_json::from_str(include_str!(
            "../../../tests/fixtures/tool_result_cursor_protocol.json"
        ))
        .unwrap()
    }

    #[test]
    fn cursor_versions_authenticate_scope_and_replay_exact_positions() {
        let fixture = cursor_vectors();
        let key: Vec<u8> = serde_json::from_value(fixture["keyBytes"].clone()).unwrap();
        let binding = &fixture["binding"];
        let position: Position =
            serde_json::from_value(fixture["native"]["position"].clone()).unwrap();
        let now = fixture["nowMs"].as_u64().unwrap();
        let current = sign(&key, binding, &position).unwrap();
        assert_eq!(
            current,
            fixture["native"]["currentCursor"].as_str().unwrap()
        );
        assert!(
            current.len() * 100 < fixture["native"]["previousCursor"].as_str().unwrap().len() * 60
        );
        for version in ["legacyCursor", "previousCursor", "currentCursor"] {
            let cursor = &fixture["native"][version];
            assert_eq!(decode(&key, binding, cursor, true, now).unwrap(), position);
            assert!(decode(&[255; 32], binding, cursor, true, now).is_err());
            assert!(decode(&key, binding, cursor, true, position.expiry).is_err());
            for index in 0..5 {
                let mut changed = binding.clone();
                changed[index] = if index == 0 {
                    json!(8)
                } else {
                    json!("changed")
                };
                assert!(decode(&key, &changed, cursor, true, now).is_err());
            }
        }
        let mut expanded = binding.clone();
        expanded[3] = json!("🙂".repeat(200));
        expanded[4] = json!(format!("/{}", "段".repeat(1023)));
        let longer_scope = sign(&key, &expanded, &position).unwrap();
        assert_eq!(longer_scope.len(), current.len());
        assert_ne!(longer_scope, current);
        assert!(decode(&key, binding, &json!(format!("{current}=")), true, now).is_err());
        assert!(decode(
            &key,
            binding,
            &json!(current.replacen("trq3.", "trq2.", 1)),
            true,
            now,
        )
        .is_err());
        let invalid = Position {
            r: vec![bound("scanNodes")],
            ..position
        };
        let signed = sign(&key, binding, &invalid).unwrap();
        assert!(decode(&key, binding, &json!(signed), true, now).is_err());
    }

    #[test]
    fn compact_positions_reject_authenticated_malformed_bytes() {
        let fixture = cursor_vectors();
        let key: Vec<u8> = serde_json::from_value(fixture["keyBytes"].clone()).unwrap();
        let binding = &fixture["binding"];
        for raw in fixture["native"]["invalidPositionBytesHex"]
            .as_array()
            .unwrap()
        {
            let raw = raw.as_str().unwrap();
            let bytes: Vec<u8> = (0..raw.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&raw[i..i + 2], 16).unwrap())
                .collect();
            let mac = scoped_mac(&key, binding, &bytes, false)
                .unwrap()
                .finalize()
                .into_bytes();
            let token = format!(
                "trq3.{}.{}",
                URL_SAFE_NO_PAD.encode(&bytes),
                URL_SAFE_NO_PAD.encode(mac)
            );
            assert!(
                decode(&key, binding, &json!(token), true, 0).is_err(),
                "{raw}"
            );
        }
    }

    #[test]
    fn compact_positions_keep_large_offsets_and_partial_state() {
        for value in [0, 127, 128, 16383, 16384, u32::MAX as usize, usize::MAX] {
            let position = Position {
                r: vec![127; 9],
                p: value,
                b: value,
                o: value,
                skip: value,
                expiry: u64::MAX,
                partial: true,
            };
            assert_eq!(
                position_from_bytes(&position_bytes(&position)).unwrap(),
                position
            );
        }
    }

    fn put(database: &mut AuthorityDatabase, text: &str, media: &str) -> String {
        let mut transaction = database.begin(7, 11).unwrap();
        let result: Value = serde_json::from_slice(
            &tool_put(database, &mut transaction, text, media, 100, 10100).unwrap(),
        )
        .unwrap();
        database.commit(transaction).unwrap();
        result["artifactRef"].as_str().unwrap().to_owned()
    }
    fn manifest(database: &mut AuthorityDatabase, parts: Value, children: Value) -> String {
        put(database, &json!({"resultId":uuid::Uuid::new_v4().to_string(),"executionStatus":"ok","sourceCoverage":"complete","retentionCoverage":"complete","parts":parts,"children":children}).to_string(), TOOL_RESULT_MANIFEST_MEDIA_TYPE)
    }
    #[test]
    fn nested_unicode_route_cursor_covers_each_repeated_child() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let text = "ß中🙂é\n".repeat(100);
        let leaf = put(&mut database, &text, "text/plain");
        let child = manifest(
            &mut database,
            json!([{"kind":"artifact","artifactRef":leaf}]),
            json!([]),
        );
        let root = manifest(
            &mut database,
            json!([{"kind":"text","text":"header"}]),
            json!([{"name":"same","callId":"same","artifactRef":child},{"name":"same","callId":"same","artifactRef":child}]),
        );
        let mut cursor = json!(0);
        let mut content = String::new();
        for page in 0..1000 {
            let mut transaction = database.begin(7, 11).unwrap();
            let result = tool_read(&database, &mut transaction, &root, 101, &cursor, 37, "")
                .unwrap_or_else(|error| panic!("page {page}: {error}; cursor={cursor}"));
            let result: Value = serde_json::from_slice(&result.unwrap()).unwrap();
            content.push_str(result["content"].as_str().unwrap());
            cursor = result["nextCursor"].clone();
            if cursor.is_null() {
                break;
            }
        }
        assert_eq!(content, format!("header{text}{text}"));
        assert!(cursor.is_null());
    }
    #[test]
    fn large_graph_continuation_reaches_each_leaf() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let children: Vec<_> = (0..18).map(|i| json!({"name":"large","callId":i.to_string(),"artifactRef":put(&mut database, &format!("{} needle-{i}", "x".repeat(1024*1024)), "text/plain")})).collect();
        let root = manifest(&mut database, json!([]), json!(children));
        let mut cursor = json!(0);
        let mut items = Vec::new();
        for page in 0..100 {
            let mut transaction = database.begin(7, 11).unwrap();
            let result = tool_search(
                &database,
                &mut transaction,
                &root,
                "needle-",
                101,
                &cursor,
                20,
                "",
            )
            .unwrap_or_else(|error| panic!("page {page}: {error}; cursor={cursor}"));
            let result: Value = serde_json::from_slice(&result.unwrap()).unwrap();
            items.extend(result["items"].as_array().unwrap().clone());
            cursor = result["nextCursor"].clone();
            if cursor.is_null() {
                break;
            }
        }
        assert_eq!(items.len(), 18);
        assert!(cursor.is_null());
    }
}
