//! Independent, disposable conversation-search projection.
//!
//! This module owns a separate Tofu-DB authority directory. Projection pages
//! are built under an invisible generation and become queryable in one atomic
//! header swap, so a long rebuild never exposes a partial conversation. Small
//! per-turn n-gram summaries reject most candidates before any text chunk is
//! materialized. The source authority remains the sole owner of durable user
//! state; this store may always be deleted and rebuilt from its dirty set.

use std::io;
use std::path::Path;
use std::sync::Arc;

use blake3::Hasher;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::vfs::Vfs;

const HEADER_NAMESPACE: &str = "search_header_v1";
const ACTIVE_NAMESPACE: &str = "search_active_v1";
const BUILD_NAMESPACE: &str = "search_build_v1";
const TURN_META_NAMESPACE: &str = "search_turn_meta_v1";
const TURN_TEXT_NAMESPACE: &str = "search_turn_text_v1";
const USAGE_NAMESPACE: &str = "search_usage_v1";
const USAGE_KEY: &[u8] = b"logical_bytes";
const FORMAT_VERSION: u8 = 1;
const BLOOM_BYTES: usize = 2_048;
const BLOOM_HASHES: usize = 3;
const TEXT_CHUNK_BYTES: usize = 6_000;
const MAX_SEARCH_TEXT_BYTES: usize = 10_000;
const MAX_ID_BYTES: usize = 512;
const MAX_GENERATION_BYTES: usize = 128;
const SCAN_PAGE: usize = 1_000;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TurnSearchDocument {
    pub turn_id: String,
    pub ordinal: u64,
    pub search_text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConversationSearchHit {
    pub id: String,
    pub snippet: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConversationSearchRequest {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub query: String,
    pub limit: usize,
    pub snippet_radius: usize,
}

pub struct TurnSearchProjection {
    database: AuthorityDatabase,
    maximum_logical_bytes_per_owner: u64,
}

#[derive(Clone)]
struct Header {
    conversation_id: String,
    generation: String,
    updated_at_ms: u64,
    generation_bytes: u64,
}

struct Build {
    conversation_id: String,
    generation: String,
    updated_at_ms: u64,
    generation_bytes: u64,
}

struct TurnMeta {
    conversation_id: String,
    generation: String,
    turn_id: String,
    ordinal: u64,
    chunk_count: u16,
    bloom: [u8; BLOOM_BYTES],
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}

fn validate_identity(value: &str, name: &str, maximum: usize) -> io::Result<()> {
    if value.is_empty() || value.len() > maximum || value.as_bytes().contains(&0) {
        return Err(invalid_input(name));
    }
    Ok(())
}

fn digest(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Hasher::new();
    hasher.update(domain);
    for part in parts {
        hasher.update(&(*part).len().to_be_bytes());
        hasher.update(part);
    }
    *hasher.finalize().as_bytes()
}

fn conversation_digest(conversation_id: &str) -> [u8; 32] {
    digest(
        b"tofu-db:turn-search:conversation:v1\0",
        &[conversation_id.as_bytes()],
    )
}

fn generation_digest(conversation_id: &str, generation: &str) -> [u8; 32] {
    digest(
        b"tofu-db:turn-search:generation:v1\0",
        &[conversation_id.as_bytes(), generation.as_bytes()],
    )
}

fn turn_digest(turn_id: &str) -> [u8; 32] {
    digest(b"tofu-db:turn-search:turn:v1\0", &[turn_id.as_bytes()])
}

fn key(transaction: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn build_key(transaction: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    key(
        transaction,
        BUILD_NAMESPACE,
        &conversation_digest(conversation_id),
    )
}

fn active_key(transaction: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    key(
        transaction,
        ACTIVE_NAMESPACE,
        &conversation_digest(conversation_id),
    )
}

fn generation_prefix(conversation_id: &str, generation: &str) -> Vec<u8> {
    generation_digest(conversation_id, generation).to_vec()
}

fn generation_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
    conversation_id: &str,
    generation: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &generation_prefix(conversation_id, generation),
    )
}

fn header_key(
    transaction: &AuthorityTransaction,
    updated_at_ms: u64,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(8 + conversation_id.len() + 1);
    raw.extend_from_slice(&(u64::MAX - updated_at_ms).to_be_bytes());
    raw.extend(conversation_id.as_bytes().iter().map(|byte| !byte));
    raw.push(u8::MAX);
    key(transaction, HEADER_NAMESPACE, &raw)
}

fn turn_meta_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    generation: &str,
    ordinal: u64,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = generation_prefix(conversation_id, generation);
    raw.extend_from_slice(&ordinal.to_be_bytes());
    raw.extend_from_slice(&turn_digest(turn_id));
    key(transaction, TURN_META_NAMESPACE, &raw)
}

fn turn_text_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    generation: &str,
    ordinal: u64,
    turn_id: &str,
    chunk: u16,
) -> io::Result<EntityKey> {
    let mut raw = generation_prefix(conversation_id, generation);
    raw.extend_from_slice(&ordinal.to_be_bytes());
    raw.extend_from_slice(&turn_digest(turn_id));
    raw.extend_from_slice(&chunk.to_be_bytes());
    key(transaction, TURN_TEXT_NAMESPACE, &raw)
}

fn push_string(output: &mut Vec<u8>, value: &str) -> io::Result<()> {
    let length =
        u16::try_from(value.len()).map_err(|_| invalid_input("search identity too long"))?;
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn read_string(input: &mut &[u8]) -> io::Result<String> {
    if input.len() < 2 {
        return Err(invalid_data("search projection string is truncated"));
    }
    let length = u16::from_be_bytes(input[..2].try_into().unwrap()) as usize;
    *input = &input[2..];
    if input.len() < length {
        return Err(invalid_data("search projection string is truncated"));
    }
    let value = std::str::from_utf8(&input[..length])
        .map_err(|_| invalid_data("search projection string is not UTF-8"))?
        .to_owned();
    *input = &input[length..];
    Ok(value)
}

fn take_u64(input: &mut &[u8]) -> io::Result<u64> {
    if input.len() < 8 {
        return Err(invalid_data("search projection integer is truncated"));
    }
    let value = u64::from_be_bytes(input[..8].try_into().unwrap());
    *input = &input[8..];
    Ok(value)
}

fn take_u16(input: &mut &[u8]) -> io::Result<u16> {
    if input.len() < 2 {
        return Err(invalid_data("search projection integer is truncated"));
    }
    let value = u16::from_be_bytes(input[..2].try_into().unwrap());
    *input = &input[2..];
    Ok(value)
}

fn finish_decode(input: &[u8]) -> io::Result<()> {
    if input.is_empty() {
        Ok(())
    } else {
        Err(invalid_data("search projection record has trailing bytes"))
    }
}

fn encode_header(header: &Header) -> io::Result<Vec<u8>> {
    let mut output = vec![FORMAT_VERSION];
    output.extend_from_slice(&header.updated_at_ms.to_be_bytes());
    output.extend_from_slice(&header.generation_bytes.to_be_bytes());
    push_string(&mut output, &header.conversation_id)?;
    push_string(&mut output, &header.generation)?;
    Ok(output)
}

fn decode_header(mut input: &[u8]) -> io::Result<Header> {
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data("search header version is unsupported"));
    }
    input = &input[1..];
    let updated_at_ms = take_u64(&mut input)?;
    let generation_bytes = take_u64(&mut input)?;
    let conversation_id = read_string(&mut input)?;
    let generation = read_string(&mut input)?;
    finish_decode(input)?;
    Ok(Header {
        conversation_id,
        generation,
        updated_at_ms,
        generation_bytes,
    })
}

fn encode_build(build: &Build) -> io::Result<Vec<u8>> {
    encode_header(&Header {
        conversation_id: build.conversation_id.clone(),
        generation: build.generation.clone(),
        updated_at_ms: build.updated_at_ms,
        generation_bytes: build.generation_bytes,
    })
}

fn decode_build(input: &[u8]) -> io::Result<Build> {
    let header = decode_header(input)?;
    Ok(Build {
        conversation_id: header.conversation_id,
        generation: header.generation,
        updated_at_ms: header.updated_at_ms,
        generation_bytes: header.generation_bytes,
    })
}

fn encode_meta(meta: &TurnMeta) -> io::Result<Vec<u8>> {
    let mut output = vec![FORMAT_VERSION];
    output.extend_from_slice(&meta.ordinal.to_be_bytes());
    output.extend_from_slice(&meta.chunk_count.to_be_bytes());
    output.extend_from_slice(&meta.bloom);
    push_string(&mut output, &meta.conversation_id)?;
    push_string(&mut output, &meta.generation)?;
    push_string(&mut output, &meta.turn_id)?;
    Ok(output)
}

fn decode_meta(mut input: &[u8]) -> io::Result<TurnMeta> {
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data("search turn metadata version is unsupported"));
    }
    input = &input[1..];
    let ordinal = take_u64(&mut input)?;
    let chunk_count = take_u16(&mut input)?;
    if input.len() < BLOOM_BYTES {
        return Err(invalid_data("search turn bloom is truncated"));
    }
    let mut bloom = [0; BLOOM_BYTES];
    bloom.copy_from_slice(&input[..BLOOM_BYTES]);
    input = &input[BLOOM_BYTES..];
    let conversation_id = read_string(&mut input)?;
    let generation = read_string(&mut input)?;
    let turn_id = read_string(&mut input)?;
    finish_decode(input)?;
    Ok(TurnMeta {
        conversation_id,
        generation,
        turn_id,
        ordinal,
        chunk_count,
        bloom,
    })
}

fn gram_ranges(value: &str, width: usize) -> Vec<(usize, usize)> {
    let mut boundaries: Vec<usize> = value.char_indices().map(|(index, _)| index).collect();
    boundaries.push(value.len());
    let character_count = boundaries.len().saturating_sub(1);
    if character_count < width {
        return Vec::new();
    }
    (0..=character_count - width)
        .map(|start| (boundaries[start], boundaries[start + width]))
        .collect()
}

fn bloom_add(bloom: &mut [u8; BLOOM_BYTES], value: &str) {
    for width in [2, 3] {
        for (start, end) in gram_ranges(value, width) {
            let hash = digest(
                b"tofu-db:turn-search:gram:v1\0",
                &[&value.as_bytes()[start..end]],
            );
            for index in 0..BLOOM_HASHES {
                let offset = index * 2;
                let bit = u16::from_be_bytes([hash[offset], hash[offset + 1]]) as usize
                    % (BLOOM_BYTES * 8);
                bloom[bit / 8] |= 1 << (bit % 8);
            }
        }
    }
}

fn bloom_might_contain(bloom: &[u8; BLOOM_BYTES], value: &str) -> bool {
    let width = if value.chars().count() >= 3 { 3 } else { 2 };
    gram_ranges(value, width).into_iter().all(|(start, end)| {
        let hash = digest(
            b"tofu-db:turn-search:gram:v1\0",
            &[&value.as_bytes()[start..end]],
        );
        (0..BLOOM_HASHES).all(|index| {
            let offset = index * 2;
            let bit =
                u16::from_be_bytes([hash[offset], hash[offset + 1]]) as usize % (BLOOM_BYTES * 8);
            bloom[bit / 8] & (1 << (bit % 8)) != 0
        })
    })
}

fn usage_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(transaction, USAGE_NAMESPACE, USAGE_KEY)
}

fn read_usage(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    match database.entity_get(transaction, &usage_key(transaction)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_be_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("search projection usage is malformed")),
    }
}

fn write_usage(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    value: u64,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        usage_key(transaction)?,
        value.to_be_bytes().to_vec(),
    )
}

impl TurnSearchProjection {
    pub fn initialize(data_dir: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: AuthorityDatabase::initialize(data_dir)?,
            maximum_logical_bytes_per_owner,
        })
    }

    pub fn open(data_dir: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: AuthorityDatabase::open(data_dir)?,
            maximum_logical_bytes_per_owner,
        })
    }

    pub fn initialize_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: AuthorityDatabase::initialize_with_vfs(data_dir, vfs)?,
            maximum_logical_bytes_per_owner,
        })
    }

    pub fn open_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: AuthorityDatabase::open_with_vfs(data_dir, vfs)?,
            maximum_logical_bytes_per_owner,
        })
    }

    pub fn begin_conversation_rebuild(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
        updated_at_ms: u64,
    ) -> io::Result<()> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        validate_identity(
            generation,
            "invalid search generation",
            MAX_GENERATION_BYTES,
        )?;
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let build_key = build_key(&transaction, conversation_id)?;
        let mut usage = read_usage(&self.database, &mut transaction)?;
        if let Some(raw) = self.database.entity_get(&mut transaction, &build_key)? {
            let previous = decode_build(&raw)?;
            if previous.conversation_id != conversation_id {
                return Err(invalid_data("search build digest collision"));
            }
            usage = usage
                .checked_sub(previous.generation_bytes)
                .ok_or_else(|| invalid_data("search projection usage underflow"))?;
            for namespace in [TURN_META_NAMESPACE, TURN_TEXT_NAMESPACE] {
                let (start, end) = generation_range(
                    &transaction,
                    namespace,
                    conversation_id,
                    &previous.generation,
                )?;
                self.database
                    .entity_retire_range(&mut transaction, &start, &end)?;
            }
        }
        let build = Build {
            conversation_id: conversation_id.to_owned(),
            generation: generation.to_owned(),
            updated_at_ms,
            generation_bytes: 0,
        };
        self.database
            .entity_put(&mut transaction, build_key, encode_build(&build)?)?;
        write_usage(&self.database, &mut transaction, usage)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn append_conversation_page(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
        documents: &[TurnSearchDocument],
    ) -> io::Result<()> {
        if documents.len() > 8 {
            return Err(invalid_input("search projection page exceeds eight turns"));
        }
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let build_key = build_key(&transaction, conversation_id)?;
        let raw = self
            .database
            .entity_get(&mut transaction, &build_key)?
            .ok_or_else(|| invalid_input("search rebuild was not begun"))?;
        let mut build = decode_build(&raw)?;
        if build.conversation_id != conversation_id || build.generation != generation {
            return Err(invalid_input("search rebuild generation is stale"));
        }
        let mut added_bytes = 0u64;
        for document in documents {
            validate_identity(&document.turn_id, "invalid search turn ID", MAX_ID_BYTES)?;
            if document.search_text.len() > MAX_SEARCH_TEXT_BYTES {
                return Err(invalid_input("search text exceeds 10000 UTF-8 bytes"));
            }
            let lowered = document.search_text.to_lowercase();
            let mut bloom = [0; BLOOM_BYTES];
            bloom_add(&mut bloom, &lowered);
            let chunks: Vec<&[u8]> = document
                .search_text
                .as_bytes()
                .chunks(TEXT_CHUNK_BYTES)
                .collect();
            let chunk_count = u16::try_from(chunks.len())
                .map_err(|_| invalid_input("search text has too many chunks"))?;
            let meta = TurnMeta {
                conversation_id: conversation_id.to_owned(),
                generation: generation.to_owned(),
                turn_id: document.turn_id.clone(),
                ordinal: document.ordinal,
                chunk_count,
                bloom,
            };
            let meta_key = turn_meta_key(
                &transaction,
                conversation_id,
                generation,
                document.ordinal,
                &document.turn_id,
            )?;
            if self
                .database
                .entity_get(&mut transaction, &meta_key)?
                .is_some()
            {
                return Err(invalid_input("search projection page repeats a turn"));
            }
            let meta_bytes = encode_meta(&meta)?;
            added_bytes = added_bytes
                .checked_add((meta_key.key_bytes().len() + meta_bytes.len()) as u64)
                .ok_or_else(|| exhausted("search projection usage overflow"))?;
            self.database
                .entity_put(&mut transaction, meta_key, meta_bytes)?;
            for (index, chunk) in chunks.into_iter().enumerate() {
                let chunk_key = turn_text_key(
                    &transaction,
                    conversation_id,
                    generation,
                    document.ordinal,
                    &document.turn_id,
                    index as u16,
                )?;
                added_bytes = added_bytes
                    .checked_add((chunk_key.key_bytes().len() + chunk.len()) as u64)
                    .ok_or_else(|| exhausted("search projection usage overflow"))?;
                self.database
                    .entity_put(&mut transaction, chunk_key, chunk.to_vec())?;
            }
        }
        let usage = read_usage(&self.database, &mut transaction)?;
        let next_usage = usage
            .checked_add(added_bytes)
            .filter(|value| *value <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| {
                exhausted("conversation search projection reached its resource budget")
            })?;
        build.generation_bytes = build
            .generation_bytes
            .checked_add(added_bytes)
            .ok_or_else(|| exhausted("search projection generation size overflow"))?;
        self.database
            .entity_put(&mut transaction, build_key, encode_build(&build)?)?;
        write_usage(&self.database, &mut transaction, next_usage)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn finalize_conversation_rebuild(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        generation: &str,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let build_key = build_key(&transaction, conversation_id)?;
        let build = decode_build(
            &self
                .database
                .entity_get(&mut transaction, &build_key)?
                .ok_or_else(|| invalid_input("search rebuild was not begun"))?,
        )?;
        if build.conversation_id != conversation_id || build.generation != generation {
            return Err(invalid_input("search rebuild generation is stale"));
        }
        let mut usage = read_usage(&self.database, &mut transaction)?;
        let active_key = active_key(&transaction, conversation_id)?;
        if let Some(raw) = self.database.entity_get(&mut transaction, &active_key)? {
            let old = decode_header(&raw)?;
            if old.conversation_id != conversation_id {
                return Err(invalid_data("search active-header digest collision"));
            }
            let old_header_key = header_key(&transaction, old.updated_at_ms, conversation_id)?;
            self.database
                .entity_delete(&mut transaction, old_header_key)?;
            usage = usage
                .checked_sub(old.generation_bytes)
                .ok_or_else(|| invalid_data("search projection usage underflow"))?;
            for namespace in [TURN_META_NAMESPACE, TURN_TEXT_NAMESPACE] {
                let (start, end) =
                    generation_range(&transaction, namespace, conversation_id, &old.generation)?;
                self.database
                    .entity_retire_range(&mut transaction, &start, &end)?;
            }
        }
        let header = Header {
            conversation_id: conversation_id.to_owned(),
            generation: generation.to_owned(),
            updated_at_ms: build.updated_at_ms,
            generation_bytes: build.generation_bytes,
        };
        let new_header_key = header_key(&transaction, build.updated_at_ms, conversation_id)?;
        let encoded_header = encode_header(&header)?;
        self.database
            .entity_put(&mut transaction, new_header_key, encoded_header.clone())?;
        self.database
            .entity_put(&mut transaction, active_key, encoded_header)?;
        self.database.entity_delete(&mut transaction, build_key)?;
        write_usage(&self.database, &mut transaction, usage)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn remove_conversation(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
    ) -> io::Result<bool> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let active_key = active_key(&transaction, conversation_id)?;
        let active = self.database.entity_get(&mut transaction, &active_key)?;
        let mut removed = false;
        let mut usage = read_usage(&self.database, &mut transaction)?;
        if let Some(raw) = active {
            let header = decode_header(&raw)?;
            if header.conversation_id != conversation_id {
                return Err(invalid_data("search active-header digest collision"));
            }
            let candidate_key = header_key(&transaction, header.updated_at_ms, conversation_id)?;
            self.database
                .entity_delete(&mut transaction, candidate_key)?;
            self.database.entity_delete(&mut transaction, active_key)?;
            usage = usage
                .checked_sub(header.generation_bytes)
                .ok_or_else(|| invalid_data("search projection usage underflow"))?;
            for namespace in [TURN_META_NAMESPACE, TURN_TEXT_NAMESPACE] {
                let (range_start, range_end) =
                    generation_range(&transaction, namespace, conversation_id, &header.generation)?;
                self.database
                    .entity_retire_range(&mut transaction, &range_start, &range_end)?;
            }
            removed = true;
        }
        if removed {
            write_usage(&self.database, &mut transaction, usage)?;
            self.database.commit(transaction)?;
        }
        Ok(removed)
    }

    fn load_text(
        &self,
        transaction: &mut AuthorityTransaction,
        meta: &TurnMeta,
    ) -> io::Result<String> {
        let mut text = Vec::new();
        for chunk in 0..meta.chunk_count {
            let chunk_key = turn_text_key(
                transaction,
                &meta.conversation_id,
                &meta.generation,
                meta.ordinal,
                &meta.turn_id,
                chunk,
            )?;
            let bytes = self
                .database
                .entity_get(transaction, &chunk_key)?
                .ok_or_else(|| invalid_data("search projection text chunk is missing"))?;
            text.extend_from_slice(&bytes);
            if text.len() > MAX_SEARCH_TEXT_BYTES {
                return Err(invalid_data("search projection text exceeds its bound"));
            }
        }
        String::from_utf8(text).map_err(|_| invalid_data("search projection text is not UTF-8"))
    }

    fn matching_text(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        header: &Header,
        term: &str,
        deadline_elapsed: &mut impl FnMut() -> bool,
    ) -> io::Result<Option<(u64, String)>> {
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let (mut start, end) = generation_range(
            &transaction,
            TURN_META_NAMESPACE,
            &header.conversation_id,
            &header.generation,
        )?;
        loop {
            if deadline_elapsed() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "conversation search deadline elapsed",
                ));
            }
            let rows = self
                .database
                .entity_scan(&mut transaction, &start, &end, SCAN_PAGE)?;
            if rows.is_empty() {
                return Ok(None);
            }
            for (_, raw) in &rows {
                let meta = decode_meta(raw)?;
                if meta.conversation_id != header.conversation_id
                    || meta.generation != header.generation
                {
                    return Err(invalid_data(
                        "search projection generation digest collision",
                    ));
                }
                if bloom_might_contain(&meta.bloom, term) {
                    let text = self.load_text(&mut transaction, &meta)?;
                    if text.to_lowercase().contains(term) {
                        return Ok(Some((meta.ordinal, text)));
                    }
                }
            }
            if rows.len() < SCAN_PAGE {
                return Ok(None);
            }
            let mut next = rows.last().unwrap().0.key_bytes().to_vec();
            next.push(0);
            start = EntityKey::new(tenant_id, owner_user_id, TURN_META_NAMESPACE, &next)?;
        }
    }

    pub fn search(
        &self,
        request: &ConversationSearchRequest,
    ) -> io::Result<Vec<ConversationSearchHit>> {
        self.search_until(request, || false)
    }

    pub fn search_until(
        &self,
        request: &ConversationSearchRequest,
        mut deadline_elapsed: impl FnMut() -> bool,
    ) -> io::Result<Vec<ConversationSearchHit>> {
        if request.limit == 0 || request.limit > 200 || request.snippet_radius > 400 {
            return Err(invalid_input("invalid conversation search bounds"));
        }
        let query = request.query.trim().to_lowercase();
        if query.chars().count() < 2 {
            return Ok(Vec::new());
        }
        if query.len() > MAX_SEARCH_TEXT_BYTES {
            return Ok(Vec::new());
        }
        let words: Vec<&str> = query.split_whitespace().collect();
        let (mut start, end) = EntityKey::prefix_range(
            request.tenant_id,
            request.owner_user_id,
            HEADER_NAMESPACE,
            b"",
        )?;
        let mut phrase_hits = Vec::new();
        let mut word_hits = Vec::new();
        loop {
            if deadline_elapsed() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "conversation search deadline elapsed",
                ));
            }
            // The projection object is immutably borrowed for the whole query,
            // so no writer can advance its root between these short snapshots.
            // A fresh snapshot per header page/conversation also prevents a
            // large owner corpus from exhausting OCC range-witness memory.
            let mut transaction = self
                .database
                .begin(request.tenant_id, request.owner_user_id)?;
            let rows = self
                .database
                .entity_scan(&mut transaction, &start, &end, SCAN_PAGE)?;
            if rows.is_empty() {
                break;
            }
            for (stored_key, raw) in &rows {
                let header = decode_header(raw)?;
                let expected_key =
                    header_key(&transaction, header.updated_at_ms, &header.conversation_id)?;
                if stored_key != &expected_key {
                    return Err(invalid_data("search header order key is malformed"));
                }
                if let Some((_, text)) = self.matching_text(
                    request.tenant_id,
                    request.owner_user_id,
                    &header,
                    &query,
                    &mut deadline_elapsed,
                )? {
                    phrase_hits.push((header, text));
                    if phrase_hits.len() == request.limit {
                        break;
                    }
                } else if words.len() > 1 {
                    let mut first_text = None;
                    let mut all = true;
                    for word in &words {
                        match self.matching_text(
                            request.tenant_id,
                            request.owner_user_id,
                            &header,
                            word,
                            &mut deadline_elapsed,
                        )? {
                            Some((_, text)) => {
                                if first_text.is_none() {
                                    first_text = Some(text);
                                }
                            }
                            None => {
                                all = false;
                                break;
                            }
                        }
                    }
                    if all && word_hits.len() < request.limit {
                        word_hits.push((header, first_text.unwrap_or_default()));
                    }
                }
            }
            if phrase_hits.len() == request.limit || rows.len() < SCAN_PAGE {
                break;
            }
            let mut next = rows.last().unwrap().0.key_bytes().to_vec();
            next.push(0);
            start = EntityKey::new(
                request.tenant_id,
                request.owner_user_id,
                HEADER_NAMESPACE,
                &next,
            )?;
        }
        phrase_hits.extend(
            word_hits
                .into_iter()
                .take(request.limit - phrase_hits.len()),
        );
        phrase_hits
            .into_iter()
            .map(|(header, text)| {
                let lowered = text.to_lowercase();
                let term = if lowered.contains(&query) {
                    query.as_str()
                } else {
                    words.first().copied().unwrap_or(query.as_str())
                };
                let snippet = make_snippet(&text, &lowered, term, request.snippet_radius);
                Ok(ConversationSearchHit {
                    id: header.conversation_id,
                    snippet,
                })
            })
            .collect()
    }
}

fn make_snippet(text: &str, lowered: &str, term: &str, radius: usize) -> String {
    if radius == 0 {
        return String::new();
    }
    let Some(byte_position) = lowered.find(term) else {
        return String::new();
    };
    let position = lowered[..byte_position].chars().count();
    let width = radius
        .saturating_mul(2)
        .saturating_add(term.chars().count());
    let start = position.saturating_sub(radius);
    let snippet: String = text.chars().skip(start).take(width).collect();
    let snippet = snippet.replace('\n', " ").trim().to_owned();
    if snippet.is_empty() {
        String::new()
    } else {
        format!("…{snippet}…")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    use std::sync::Arc;
    use tempfile::tempdir;

    const BUDGET: u64 = 16 * 1024 * 1024;

    fn document(turn_id: &str, ordinal: u64, text: &str) -> TurnSearchDocument {
        TurnSearchDocument {
            turn_id: turn_id.to_owned(),
            ordinal,
            search_text: text.to_owned(),
        }
    }

    fn request(owner: u64, query: &str) -> ConversationSearchRequest {
        ConversationSearchRequest {
            tenant_id: 7,
            owner_user_id: owner,
            query: query.to_owned(),
            limit: 50,
            snippet_radius: 10,
        }
    }

    fn replace(
        projection: &mut TurnSearchProjection,
        owner: u64,
        conversation: &str,
        generation: &str,
        updated_at: u64,
        documents: &[TurnSearchDocument],
    ) {
        projection
            .begin_conversation_rebuild(7, owner, conversation, generation, updated_at)
            .unwrap();
        for page in documents.chunks(8) {
            projection
                .append_conversation_page(7, owner, conversation, generation, page)
                .unwrap();
        }
        projection
            .finalize_conversation_rebuild(7, owner, conversation, generation)
            .unwrap();
    }

    #[test]
    fn phrase_multi_turn_and_owner_order_semantics_match_contract() {
        let directory = tempdir().unwrap();
        let mut projection = TurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "older",
            "g1",
            100,
            &[document("t1", 1, "the quick brown fox")],
        );
        replace(
            &mut projection,
            11,
            "newer",
            "g2",
            200,
            &[
                document("t2", 1, "alpha here"),
                document("t3", 2, "gamma there"),
            ],
        );
        replace(
            &mut projection,
            12,
            "private",
            "g3",
            300,
            &[document("t4", 1, "fox")],
        );

        let fox = projection.search(&request(11, "FOX")).unwrap();
        assert_eq!(
            fox.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["older"]
        );
        assert!(fox[0].snippet.contains("fox"));
        let across_turns = projection.search(&request(11, "alpha gamma")).unwrap();
        assert_eq!(
            across_turns,
            [ConversationSearchHit {
                id: "newer".to_owned(),
                snippet: "…alpha here…".to_owned()
            }]
        );
        assert!(projection.search(&request(11, "a")).unwrap().is_empty());
    }

    #[test]
    fn generation_is_invisible_until_atomic_publish_and_retracts_old_text() {
        let directory = tempdir().unwrap();
        let mut projection = TurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "conversation",
            "old",
            100,
            &[document("t1", 1, "old phrase")],
        );
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "new", 200)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "new",
                &[document("t1", 1, "new phrase")],
            )
            .unwrap();
        assert_eq!(
            projection.search(&request(11, "old phrase")).unwrap().len(),
            1
        );
        assert!(projection
            .search(&request(11, "new phrase"))
            .unwrap()
            .is_empty());
        projection
            .finalize_conversation_rebuild(7, 11, "conversation", "new")
            .unwrap();
        assert!(projection
            .search(&request(11, "old phrase"))
            .unwrap()
            .is_empty());
        assert_eq!(
            projection.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
        drop(projection);
        let reopened = TurnSearchProjection::open(directory.path(), BUDGET).unwrap();
        assert_eq!(
            reopened.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
    }

    #[test]
    fn phrase_pass_precedes_word_pass_and_equal_timestamps_sort_id_descending() {
        let directory = tempdir().unwrap();
        let mut projection = TurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "z-word-only",
            "g1",
            300,
            &[
                document("t1", 1, "alpha apart"),
                document("t2", 2, "gamma apart"),
            ],
        );
        replace(
            &mut projection,
            11,
            "a-phrase",
            "g2",
            100,
            &[document("t3", 1, "literal alpha gamma phrase")],
        );
        for id in ["a", "aa", "b"] {
            replace(
                &mut projection,
                11,
                id,
                id,
                200,
                &[document(id, 1, "same_time_literal_%")],
            );
        }
        let mixed = projection.search(&request(11, "alpha gamma")).unwrap();
        assert_eq!(
            mixed.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["a-phrase", "z-word-only"]
        );
        let tied = projection.search(&request(11, "literal_%")).unwrap();
        assert_eq!(
            tied.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["b", "aa", "a"]
        );
    }

    #[test]
    fn query_deadline_stops_before_projection_scan() {
        let directory = tempfile::tempdir().unwrap();
        let projection = TurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let error = projection
            .search_until(&request(7, "deadline"), || true)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn logical_budget_rejects_page_without_exposing_it() {
        let directory = tempdir().unwrap();
        let mut projection = TurnSearchProjection::initialize(directory.path(), 100).unwrap();
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "g1", 100)
            .unwrap();
        let error = projection
            .append_conversation_page(7, 11, "conversation", "g1", &[document("t1", 1, "bounded")])
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
        assert!(projection
            .search(&request(11, "bounded"))
            .unwrap()
            .is_empty());
    }

    fn prepared_simulated_projection(vfs: Arc<DeterministicVfs>) -> TurnSearchProjection {
        vfs.create_dir(Path::new("/search")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut projection =
            TurnSearchProjection::initialize_with_vfs(Path::new("/search"), vfs, BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "conversation",
            "old",
            100,
            &[document("turn", 1, "old durable phrase")],
        );
        projection
            .begin_conversation_rebuild(7, 11, "conversation", "new", 200)
            .unwrap();
        projection
            .append_conversation_page(
                7,
                11,
                "conversation",
                "new",
                &[document("turn", 1, "new durable phrase")],
            )
            .unwrap();
        projection
    }

    fn assert_recovered_generation(vfs: Arc<DeterministicVfs>) {
        vfs.arm_fault(None).unwrap();
        vfs.crash().unwrap();
        let projection =
            TurnSearchProjection::open_with_vfs(Path::new("/search"), vfs, BUDGET).unwrap();
        let old = projection
            .search(&request(11, "old durable phrase"))
            .unwrap();
        let new = projection
            .search(&request(11, "new durable phrase"))
            .unwrap();
        assert!(
            (old.len() == 1 && new.is_empty()) || (old.is_empty() && new.len() == 1),
            "recovery exposed neither or both projection generations"
        );
    }

    #[test]
    fn every_publish_io_fault_recovers_exactly_one_complete_generation() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_simulated_projection(Arc::clone(&baseline_vfs));
        baseline_vfs.arm_fault(None).unwrap();
        baseline
            .finalize_conversation_rebuild(7, 11, "conversation", "new")
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut projection = prepared_simulated_projection(Arc::clone(&vfs));
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = projection.finalize_conversation_rebuild(7, 11, "conversation", "new");
            drop(projection);
            assert_recovered_generation(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut projection = prepared_simulated_projection(Arc::clone(&vfs));
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = projection.finalize_conversation_rebuild(7, 11, "conversation", "new");
            drop(projection);
            assert_recovered_generation(vfs);
        }
    }
}
