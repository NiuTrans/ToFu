//! Canonical contiguous BPE over short pieces, with small call-local storage.
//! Rank/leftmost tie semantics match the full encoder. Longer pieces retain
//! exact-sized heap storage; no cross-piece/request buffer or unsafe access.
use crate::token_pair_ranks::PairRanks;
use rustc_hash::FxHashMap;
use std::{borrow::Borrow, hash::Hash};

const STACK_PARTS: usize = 32;
type Part = (usize, u32);

use std::ops::{Deref, DerefMut};

trait Boundaries: DerefMut<Target = [Part]> {
    fn push_boundary(&mut self, value: Part);
    fn remove_boundary(&mut self, index: usize);
}
impl Boundaries for Vec<Part> {
    fn push_boundary(&mut self, value: Part) { self.push(value); }
    fn remove_boundary(&mut self, index: usize) { self.remove(index); }
}
struct InlineBoundaries { values: [Part; STACK_PARTS], length: usize }
impl Deref for InlineBoundaries {
    type Target = [Part];
    fn deref(&self) -> &[Part] { &self.values[..self.length] }
}
impl DerefMut for InlineBoundaries {
    fn deref_mut(&mut self) -> &mut [Part] { &mut self.values[..self.length] }
}
impl Boundaries for InlineBoundaries {
    fn push_boundary(&mut self, value: Part) {
        self.values[self.length] = value;
        self.length += 1;
    }
    fn remove_boundary(&mut self, index: usize) {
        self.values.copy_within(index + 1..self.length, index);
        self.length -= 1;
    }
}

fn merge<K: Borrow<[u8]> + Eq + Hash>(piece: &[u8], ranks: &FxHashMap<K, u32>, pairs: &PairRanks,
         mut parts: impl Boundaries) -> usize {
    let mut best = (u32::MAX, usize::MAX);
    for index in 0..piece.len() - 1 {
        let rank = pairs.get(piece[index], piece[index + 1]);
        if rank < best.0 { best = (rank, index); }
        parts.push_boundary((index, rank));
    }
    parts.push_boundary((piece.len() - 1, u32::MAX));
    parts.push_boundary((piece.len(), u32::MAX));
    let merged_rank = |parts: &[Part], index: usize| {
        if index + 3 < parts.len() {
            *ranks.get(&piece[parts[index].0..parts[index + 3].0]).unwrap_or(&u32::MAX)
        } else { u32::MAX }
    };
    while best.0 != u32::MAX {
        let index = best.1;
        if index > 0 { parts[index - 1].1 = merged_rank(&parts, index - 1); }
        parts[index].1 = merged_rank(&parts, index);
        parts.remove_boundary(index + 1);
        best = (u32::MAX, usize::MAX);
        for (index, &(_, rank)) in parts[..parts.len() - 1].iter().enumerate() {
            if rank < best.0 { best = (rank, index); }
        }
    }
    parts.len() - 1
}

// Keep small-piece storage out of the regex/whole-token caller's stack frame.
#[inline(never)]
pub(crate) fn count<K: Borrow<[u8]> + Eq + Hash>(piece: &[u8], ranks: &FxHashMap<K, u32>, pairs: &PairRanks) -> usize {
    if piece.len() < STACK_PARTS {
        merge(piece, ranks, pairs, InlineBoundaries { values: [(0, u32::MAX); STACK_PARTS], length: 0 })
    } else {
        merge(piece, ranks, pairs, Vec::with_capacity(piece.len() + 1))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stack_and_heap_pieces_match_full_encoder_including_rank_ties() {
        let mut random = 0x9475_aacf_f124_3500u64;
        for case in 0..1000 {
            random = random.wrapping_mul(6364136223846793005).wrapping_add(1);
            let length = 2 + random as usize % 255;
            let mut piece = Vec::with_capacity(length);
            let mut ranks = FxHashMap::default();
            for byte in 0u8..=255 { ranks.insert(vec![byte], byte as u32); }
            for _ in 0..length {
                random = random.wrapping_mul(6364136223846793005).wrapping_add(1);
                piece.push((random >> 32) as u8 % 8);
            }
            for size in 2..=8 {
                for word in piece.windows(size) {
                    random = random.wrapping_mul(6364136223846793005).wrapping_add(1);
                    if random % 3 != 0 { ranks.insert(word.to_vec(), (random >> 32) as u32 % 13); }
                }
            }
            let pairs = PairRanks::from_ranks(&ranks);
            let expected = tiktoken_rs::byte_pair_split(&piece, &ranks).len();
            assert_eq!(count(&piece, &ranks, &pairs), expected, "case {case}");
        }
    }
}
