//! Encoder-owned direct ranks for the complete two-byte key space.
//! Derived while admitting the canonical vocabulary; no second vocabulary,
//! hashing, mutable request state or global cache. Byte order is explicit.
pub(crate) const BYTE_PAIR_COUNT: usize = 256 * 256;

pub(crate) struct PairRanks(Box<[u32; BYTE_PAIR_COUNT]>);

impl PairRanks {
    pub(crate) fn new() -> Self {
        let values = vec![u32::MAX; BYTE_PAIR_COUNT].into_boxed_slice().try_into()
            .unwrap_or_else(|_| unreachable!("fixed byte-pair index size"));
        Self(values)
    }

    pub(crate) fn insert(&mut self, left: u8, right: u8, rank: u32) {
        self.0[usize::from(u16::from_le_bytes([left, right]))] = rank;
    }

    #[inline]
    pub(crate) fn get(&self, left: u8, right: u8) -> u32 {
        self.0[usize::from(u16::from_le_bytes([left, right]))]
    }

    #[cfg(test)]
    pub(crate) fn from_ranks(ranks: &rustc_hash::FxHashMap<Vec<u8>, u32>) -> Self {
        let mut pairs = Self::new();
        for (bytes, rank) in ranks {
            if bytes.len() == 2 { pairs.insert(bytes[0], bytes[1], *rank); }
        }
        pairs
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_byte_pair_has_one_distinct_ordered_cell() {
        let mut pairs = PairRanks::new();
        for left in 0u8..=255 {
            for right in 0u8..=255 {
                assert_eq!(pairs.get(left, right), u32::MAX);
                pairs.insert(left, right, u32::from(left) * 256 + u32::from(right));
            }
        }
        for left in 0u8..=255 {
            for right in 0u8..=255 {
                assert_eq!(pairs.get(left, right), u32::from(left) * 256 + u32::from(right));
            }
        }
    }
}
