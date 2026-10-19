//! Count-only ordinary BPE over exact pinned upstream merge ranks/patterns.
//! Owns forward ranks, a derived byte-pair index and pattern; no decoder, special
//! token encoder, unstable-completion index, token-ID output or global cache.
//! Request-local regex scratch avoids shared-worker contention and is dropped.
use base64::{engine::general_purpose::STANDARD, Engine};
use fancy_regex::Regex;
use rustc_hash::FxHashMap;
use sha2::{Digest, Sha256};
use crate::token_ingress::{Encoding, Error};

#[path = "token_ranks/cl100k_base.rs"]
mod cl100k;
#[path = "token_ranks/o200k_base.rs"]
mod o200k;

pub(crate) struct RankCounter {
    ranks: FxHashMap<&'static [u8], tiktoken_rs::Rank>,
    pattern: Regex,
    pairs: crate::token_pair_ranks::PairRanks,
}
impl RankCounter {
    pub(crate) fn new(encoding: Encoding) -> Result<Self, Error> {
        let (source, expected, fingerprint, packed_fingerprint, pattern) = match encoding {
            Encoding::Cl100k => (cl100k::PACKED,cl100k::TOKEN_COUNT,cl100k::SHA256,cl100k::PACKED_SHA256,
                "'(?i:[sdmt]|ll|ve|re)|[^\\r\\n\\p{L}\\p{N}]?+\\p{L}++|\\p{N}{1,3}+| ?[^\\s\\p{L}\\p{N}]++[\\r\\n]*+|\\s++$|\\s*[\\r\\n]|\\s+(?!\\S)|\\s"),
            Encoding::O200k => (o200k::PACKED,o200k::TOKEN_COUNT,o200k::SHA256,o200k::PACKED_SHA256,
                tiktoken_rs::O200K_BASE_PAT_STR),
        };
        if format!("{:x}", Sha256::digest(source)) != packed_fingerprint {
            return Err(Error::Encoder);
        }
        let mut ranks = FxHashMap::with_capacity_and_hasher(expected, Default::default());
        let mut pairs = crate::token_pair_ranks::PairRanks::new();
        const MAXIMUM: usize = if cl100k::MAX_TOKEN_BYTES > o200k::MAX_TOKEN_BYTES {
            cl100k::MAX_TOKEN_BYTES
        } else { o200k::MAX_TOKEN_BYTES };
        const ENCODED_BYTES: usize = MAXIMUM.div_ceil(3) * 4;
        // One bounded canonical line includes Base64, separator, u32 rank and LF.
        let mut encoded = [0u8; ENCODED_BYTES + 12];
        let mut original = Sha256::new();
        let mut offset = 0usize;
        for ordinal in 0..expected {
            let length = source.get(offset..offset + 2).ok_or(Error::Encoder)?;
            let length = u16::from_le_bytes([length[0], length[1]]) as usize;
            offset += 2;
            if length == 0 || length > MAXIMUM { return Err(Error::Encoder); }
            let bytes = source.get(offset..offset + length).ok_or(Error::Encoder)?;
            offset += length;
            let rank = u32::try_from(ordinal).map_err(|_| Error::Encoder)?;
            let written = STANDARD.encode_slice(bytes, &mut encoded[..ENCODED_BYTES]).map_err(|_| Error::Encoder)?;
            encoded[written] = b' ';
            // Reconstruct exact pinned upstream decimal ranks without per-token
            // allocation; runtime still verifies the original asset SHA.
            let mut digits = [0u8; 10];
            let mut begin = digits.len();
            let mut value = rank;
            loop {
                begin -= 1;
                digits[begin] = b'0' + (value % 10) as u8;
                value /= 10;
                if value == 0 { break; }
            }
            let rank_bytes = digits.len() - begin;
            let end = written + 1 + rank_bytes;
            encoded[written + 1..end].copy_from_slice(&digits[begin..]);
            encoded[end] = b'\n';
            original.update(&encoded[..end + 1]);
            if bytes.len() == 2 { pairs.insert(bytes[0], bytes[1], rank); }
            if ranks.insert(bytes, rank).is_some() { return Err(Error::Encoder); }
        }
        if offset != source.len() || format!("{:x}", original.finalize()) != fingerprint {
            return Err(Error::Encoder);
        }
        if ranks.len() != expected || (0..=255).any(|byte| !ranks.contains_key(&[byte][..])) {
            return Err(Error::Encoder);
        }
        Ok(Self { ranks, pairs, pattern: Regex::new(pattern).map_err(|_| Error::Encoder)? })
    }
    pub(crate) fn count(&self, text: &str, scratch: &mut Option<Regex>) -> Result<u64, Error> {
        let regex = scratch.get_or_insert_with(|| self.pattern.clone());
        let mut count = 0u64;
        for matched in regex.find_iter(text) {
            let piece = matched.map_err(|_| Error::Encoder)?.as_str().as_bytes();
            let tokens = match piece.len() {
                0 => return Err(Error::Encoder),
                1 => 1, // Admission verified all single-byte tokens.
                2 => if self.pairs.get(piece[0], piece[1]) == u32::MAX { 2 } else { 1 },
                _ if self.ranks.contains_key(piece) => 1,
                _ if piece.len() >= crate::token_merge_heap::MIN_PIECE_BYTES =>
                    crate::token_merge_heap::count(piece, &self.ranks, &self.pairs),
                _ => crate::token_merge_linear::count(piece, &self.ranks, &self.pairs),
            };
            count = count.checked_add(tokens as u64).ok_or(Error::Overflow)?;
        }
        Ok(count)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_borrowed_rank_matches_the_exact_upstream_asset() {
        for (encoding, source, packed) in [
            (Encoding::Cl100k, cl100k::DATA, cl100k::PACKED),
            (Encoding::O200k, o200k::DATA, o200k::PACKED),
        ] {
            let counter = RankCounter::new(encoding).unwrap();
            for (ordinal, line) in source.lines().enumerate() {
                let (encoded, rank) = line.split_once(' ').unwrap();
                let bytes = STANDARD.decode(encoded).unwrap();
                assert_eq!(rank.parse::<usize>().unwrap(), ordinal);
                let (borrowed, actual) = counter.ranks.get_key_value(bytes.as_slice()).unwrap();
                assert_eq!(*actual as usize, ordinal);
                let begin = packed.as_ptr() as usize;
                let pointer = borrowed.as_ptr() as usize;
                assert!(pointer >= begin && pointer + borrowed.len() <= begin + packed.len());
            }
        }
    }
}
