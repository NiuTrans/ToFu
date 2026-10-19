//! Versioned immutable entity-page encoding and byte-aware page construction.
//!
//! Entry points: Page::{encode,decode,encoded_bytes}, LeafFrame and internal
//! entry sizing. Depends only on the entity types and generated format bounds.
//! Version 1 remains readable; version 2 front-codes sorted keys against the
//! preceding key. Expanded bytes remain independently bounded before allocation.

use super::{
    invalid_data, push_u16, read_u16, take, Child, EntityValue, Page,
    MAX_ENTITY_INLINE_VALUE_BYTES, MAX_ENTITY_KEY_BYTES, MAX_TREE_DEPTH, PAGE_MAGIC,
    PAGE_MAX_BYTES, PAGE_PACK_TARGET_BYTES,
};
use crate::generated_tofudb_ir::{ENTITY_PAGE_WRITE_VERSION, MAX_ENTITY_PAGE_EXPANDED_BYTES};
use std::io;

const HEADER_BYTES: usize = 16;
const LEAF_ENTRY_BYTES: usize = 15;
const INTERNAL_ENTRY_BYTES: usize = 36;

fn common_prefix(previous: &[u8], key: &[u8]) -> usize {
    previous
        .iter()
        .zip(key)
        .take_while(|(left, right)| left == right)
        .count()
}

pub(super) fn internal_entry_bytes(previous: &[u8], key: &[u8]) -> usize {
    INTERNAL_ENTRY_BYTES + key.len() - common_prefix(previous, key)
}

fn header(kind: u8, level: u8, count: usize, capacity: usize) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::with_capacity(capacity);
    bytes.extend_from_slice(PAGE_MAGIC);
    bytes.extend_from_slice(&ENTITY_PAGE_WRITE_VERSION.to_le_bytes());
    bytes.push(kind);
    bytes.push(level);
    push_u16(&mut bytes, count)?;
    Ok(bytes)
}

fn write_key_lengths(bytes: &mut Vec<u8>, previous: &[u8], key: &[u8]) -> io::Result<usize> {
    let prefix = common_prefix(previous, key);
    push_u16(bytes, prefix)?;
    push_u16(bytes, key.len() - prefix)?;
    Ok(prefix)
}

fn key_lengths(
    bytes: &[u8],
    offset: &mut usize,
    previous: &[u8],
    version: u32,
) -> io::Result<(usize, usize)> {
    let prefix = if version == 2 {
        read_u16(bytes, offset)?
    } else {
        0
    };
    let suffix = read_u16(bytes, offset)?;
    if prefix > previous.len() || prefix + suffix > MAX_ENTITY_KEY_BYTES + 82 {
        return Err(invalid_data("invalid entity key prefix or expanded length"));
    }
    Ok((prefix, suffix))
}

fn reconstruct_key(previous: &[u8], prefix: usize, suffix: &[u8]) -> Vec<u8> {
    let mut key = Vec::with_capacity(prefix + suffix.len());
    key.extend_from_slice(&previous[..prefix]);
    key.extend_from_slice(suffix);
    key
}

fn expand_size(expanded: &mut usize, bytes: usize) -> io::Result<()> {
    *expanded = expanded
        .checked_add(bytes)
        .filter(|size| *size <= MAX_ENTITY_PAGE_EXPANDED_BYTES)
        .ok_or_else(|| invalid_data("entity page exceeds expanded byte bound"))?;
    Ok(())
}

impl Page {
    pub(super) fn encoded_bytes(&self) -> io::Result<usize> {
        let mut previous = &[][..];
        let mut encoded = HEADER_BYTES;
        let mut expanded = HEADER_BYTES;
        match self {
            Self::Leaf(entries) => {
                for (key, entity) in entries {
                    let value_len = entity.value.as_ref().map_or(0, Vec::len);
                    if key.len() > MAX_ENTITY_KEY_BYTES + 82
                        || value_len > MAX_ENTITY_INLINE_VALUE_BYTES
                    {
                        return Err(invalid_data("unbounded entity leaf entry"));
                    }
                    expand_size(&mut expanded, 13 + key.len() + value_len)?;
                    encoded +=
                        LEAF_ENTRY_BYTES + key.len() - common_prefix(previous, key) + value_len;
                    previous = key;
                }
            }
            Self::Internal { children, .. } => {
                for child in children {
                    if child.lower_bound.len() > MAX_ENTITY_KEY_BYTES + 82 {
                        return Err(invalid_data("unbounded entity internal key"));
                    }
                    expand_size(&mut expanded, 34 + child.lower_bound.len())?;
                    encoded += internal_entry_bytes(previous, &child.lower_bound);
                    previous = &child.lower_bound;
                }
            }
        }
        if encoded > PAGE_MAX_BYTES {
            return Err(invalid_data("entity page exceeds maximum encoding size"));
        }
        Ok(encoded)
    }

    pub(super) fn encode(&self) -> io::Result<Vec<u8>> {
        self.validate()?;
        let size = self.encoded_bytes()?;
        let mut previous = &[][..];
        let mut bytes = match self {
            Self::Leaf(entries) => header(0, 0, entries.len(), size)?,
            Self::Internal { level, children } => {
                if *level == 0 || *level as usize > MAX_TREE_DEPTH {
                    return Err(invalid_data("invalid internal entity page level"));
                }
                header(1, *level, children.len(), size)?
            }
        };
        match self {
            Self::Leaf(entries) => {
                for (key, entity) in entries {
                    let prefix = write_key_lengths(&mut bytes, previous, key)?;
                    push_u16(&mut bytes, entity.value.as_ref().map_or(0, Vec::len))?;
                    bytes.extend_from_slice(&entity.version.to_le_bytes());
                    bytes.push(u8::from(entity.value.is_none()));
                    bytes.extend_from_slice(&key[prefix..]);
                    if let Some(value) = &entity.value {
                        bytes.extend_from_slice(value);
                    }
                    previous = key;
                }
            }
            Self::Internal { children, .. } => {
                for child in children {
                    let prefix = write_key_lengths(&mut bytes, previous, &child.lower_bound)?;
                    bytes.extend_from_slice(&child.lower_bound[prefix..]);
                    bytes.extend_from_slice(&child.block_id.0);
                    previous = &child.lower_bound;
                }
            }
        }
        debug_assert_eq!(bytes.len(), size);
        Ok(bytes)
    }

    pub(super) fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < HEADER_BYTES || bytes.len() > PAGE_MAX_BYTES || &bytes[..8] != PAGE_MAGIC {
            return Err(invalid_data("entity page magic mismatch"));
        }
        let version = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        if !matches!(version, 1 | 2) {
            return Err(invalid_data("unsupported entity page version"));
        }
        let kind = bytes[12];
        let level = bytes[13];
        let count = u16::from_le_bytes(bytes[14..16].try_into().unwrap()) as usize;
        // Refuse impossible counts before allocating a decoded entry vector.
        let minimum_entry = match kind {
            0 => {
                if version == 2 {
                    15
                } else {
                    13
                }
            }
            1 => {
                if version == 2 {
                    36
                } else {
                    34
                }
            }
            _ => 1,
        };
        if count == 0 || count > (bytes.len() - HEADER_BYTES) / minimum_entry {
            return Err(invalid_data("invalid entity page entry count"));
        }
        let mut offset = HEADER_BYTES;
        let mut expanded = HEADER_BYTES;
        let page = match kind {
            0 if level == 0 => {
                let mut entries: Vec<(Vec<u8>, EntityValue)> = Vec::with_capacity(count);
                for _ in 0..count {
                    let previous = entries.last().map_or(&[][..], |entry| entry.0.as_slice());
                    let (prefix, suffix) = key_lengths(bytes, &mut offset, previous, version)?;
                    let value_len = read_u16(bytes, &mut offset)?;
                    let commit_version =
                        u64::from_le_bytes(take(bytes, &mut offset, 8)?.try_into().unwrap());
                    let tombstone = take(bytes, &mut offset, 1)?[0];
                    if value_len > MAX_ENTITY_INLINE_VALUE_BYTES || tombstone > 1 {
                        return Err(invalid_data("unbounded entity leaf entry"));
                    }
                    expand_size(&mut expanded, 13 + prefix + suffix + value_len)?;
                    let key = reconstruct_key(previous, prefix, take(bytes, &mut offset, suffix)?);
                    let value = if tombstone == 1 {
                        if value_len != 0 {
                            return Err(invalid_data("entity tombstone carries bytes"));
                        }
                        None
                    } else {
                        Some(take(bytes, &mut offset, value_len)?.to_vec())
                    };
                    entries.push((
                        key,
                        EntityValue {
                            version: commit_version,
                            value,
                        },
                    ));
                }
                Self::Leaf(entries)
            }
            1 if level > 0 && level as usize <= MAX_TREE_DEPTH => {
                let mut children: Vec<Child> = Vec::with_capacity(count);
                for _ in 0..count {
                    let previous = children
                        .last()
                        .map_or(&[][..], |child| child.lower_bound.as_slice());
                    let (prefix, suffix) = key_lengths(bytes, &mut offset, previous, version)?;
                    expand_size(&mut expanded, 34 + prefix + suffix)?;
                    children.push(Child {
                        lower_bound: reconstruct_key(
                            previous,
                            prefix,
                            take(bytes, &mut offset, suffix)?,
                        ),
                        block_id: super::BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                    });
                }
                Self::Internal { level, children }
            }
            _ => return Err(invalid_data("invalid entity page kind or level")),
        };
        if offset != bytes.len() {
            return Err(invalid_data("entity page has trailing bytes"));
        }
        page.validate()?;
        Ok(page)
    }
}

/// One adjacent modified leaf run. Both physical and expanded sizes participate
/// in splitting, so prefix compression cannot create an unbounded decode/cache.
pub(super) struct LeafFrame {
    bytes: Vec<u8>,
    entries: usize,
    first_key: Vec<u8>,
    previous_key: Vec<u8>,
    expanded_bytes: usize,
}

impl LeafFrame {
    pub(super) fn new() -> Self {
        Self {
            bytes: header(0, 0, 0, 256).expect("empty header count fits"),
            entries: 0,
            first_key: Vec::new(),
            previous_key: Vec::new(),
            expanded_bytes: HEADER_BYTES,
        }
    }

    pub(super) fn is_empty(&self) -> bool {
        self.entries == 0
    }

    pub(super) fn would_overflow(&self, key: &[u8], value: Option<&[u8]>) -> bool {
        let value_len = value.map_or(0, <[u8]>::len);
        !self.is_empty()
            && (self.bytes.len() + LEAF_ENTRY_BYTES + key.len()
                - common_prefix(&self.previous_key, key)
                + value_len
                > PAGE_PACK_TARGET_BYTES
                || self.expanded_bytes + 13 + key.len() + value_len
                    > MAX_ENTITY_PAGE_EXPANDED_BYTES)
    }

    pub(super) fn push(
        &mut self,
        key: &[u8],
        version: u64,
        value: Option<&[u8]>,
    ) -> io::Result<()> {
        let value_len = value.map_or(0, <[u8]>::len);
        if key.len() > MAX_ENTITY_KEY_BYTES + 82 || value_len > MAX_ENTITY_INLINE_VALUE_BYTES {
            return Err(invalid_data("unbounded entity leaf entry"));
        }
        if self.entries > 0 && self.previous_key.as_slice() >= key {
            return Err(invalid_data("entity leaf frame keys are unsorted"));
        }
        expand_size(&mut self.expanded_bytes, 13 + key.len() + value_len)?;
        if self.is_empty() {
            self.first_key.extend_from_slice(key);
        }
        let prefix = write_key_lengths(&mut self.bytes, &self.previous_key, key)?;
        push_u16(&mut self.bytes, value_len)?;
        self.bytes.extend_from_slice(&version.to_le_bytes());
        self.bytes.push(u8::from(value.is_none()));
        self.bytes.extend_from_slice(&key[prefix..]);
        if let Some(value) = value {
            self.bytes.extend_from_slice(value);
        }
        if self.bytes.len() > PAGE_MAX_BYTES {
            return Err(invalid_data("entity page exceeds maximum after split"));
        }
        self.previous_key.clear();
        self.previous_key.extend_from_slice(key);
        self.entries += 1;
        Ok(())
    }

    pub(super) fn finish(&mut self) -> Option<(Vec<u8>, Vec<u8>)> {
        if self.is_empty() {
            return None;
        }
        let mut frame = std::mem::replace(self, Self::new());
        frame.bytes[14..16].copy_from_slice(&(frame.entries as u16).to_le_bytes());
        // Vec growth while constructing a leaf is temporary; pending storage
        // retains exactly the final page rather than its spare capacity.
        let bytes = frame.bytes.into_boxed_slice().into_vec();
        Some((frame.first_key, bytes))
    }
}

#[cfg(test)]
#[path = "entity_page_codec_tests.rs"]
mod tests;
