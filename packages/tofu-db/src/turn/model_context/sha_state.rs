//! Versioned resumable SHA-256 source state for the unchanged public v2 proof.
//! RustCrypto owns compression; this module owns bounded state framing/padding.
use sha2::{compress256, digest::generic_array::GenericArray};
use std::io;
const INITIAL: [u32; 8] = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];
#[derive(Clone, Debug)]
pub(super) struct ShaState {
    words: [u32; 8],
    bytes: u64,
    tail: Vec<u8>,
}
impl Default for ShaState {
    fn default() -> Self {
        Self {
            words: INITIAL,
            bytes: 0,
            tail: Vec::new(),
        }
    }
}
fn bad() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "context SHA state is malformed")
}
impl ShaState {
    pub(super) fn update(&mut self, mut input: &[u8]) -> io::Result<()> {
        self.bytes = self
            .bytes
            .checked_add(input.len() as u64)
            .filter(|n| *n <= u64::MAX / 8)
            .ok_or_else(bad)?;
        if !self.tail.is_empty() {
            let take = (64 - self.tail.len()).min(input.len());
            self.tail.extend_from_slice(&input[..take]);
            input = &input[take..];
            if self.tail.len() == 64 {
                compress256(
                    &mut self.words,
                    &[GenericArray::clone_from_slice(&self.tail)],
                );
                self.tail.clear();
            }
        }
        let mut chunks = input.chunks_exact(64);
        for block in &mut chunks {
            compress256(&mut self.words, &[GenericArray::clone_from_slice(block)]);
        }
        self.tail.extend_from_slice(chunks.remainder());
        Ok(())
    }
    pub(super) fn digest(&self) -> String {
        let mut words = self.words;
        let mut padding = [0_u8; 128];
        padding[..self.tail.len()].copy_from_slice(&self.tail);
        padding[self.tail.len()] = 0x80;
        let size = if self.tail.len() < 56 { 64 } else { 128 };
        padding[size - 8..size].copy_from_slice(&(self.bytes * 8).to_be_bytes());
        for block in padding[..size].chunks_exact(64) {
            compress256(&mut words, &[GenericArray::clone_from_slice(block)]);
        }
        words.iter().map(|word| format!("{word:08x}")).collect()
    }
    pub(super) fn encode(&self) -> Vec<u8> {
        let mut output = Vec::with_capacity(41 + self.tail.len());
        for word in self.words {
            output.extend_from_slice(&word.to_be_bytes());
        }
        output.extend_from_slice(&self.bytes.to_be_bytes());
        output.push(self.tail.len() as u8);
        output.extend_from_slice(&self.tail);
        output
    }
    pub(super) fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < 41 {
            return Err(bad());
        }
        let len = usize::from(encoded[40]);
        if len >= 64 || encoded.len() != 41 + len {
            return Err(bad());
        }
        let bytes = u64::from_be_bytes(encoded[32..40].try_into().unwrap());
        if bytes > u64::MAX / 8 || bytes % 64 != len as u64 {
            return Err(bad());
        }
        let mut words = [0; 8];
        for (slot, bytes) in words.iter_mut().zip(encoded[..32].chunks_exact(4)) {
            *slot = u32::from_be_bytes(bytes.try_into().unwrap());
        }
        if bytes < 64 && words != INITIAL {
            return Err(bad());
        }
        Ok(Self {
            words,
            bytes,
            tail: encoded[41..].to_vec(),
        })
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    #[test]
    fn resumable_sha_matches_independent_digest_at_every_block_and_padding_boundary() {
        let mut seed = 0x72ab357c_u64;
        let data: Vec<_> = (0..8193)
            .map(|_| {
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                seed as u8
            })
            .collect();
        for length in (0..260).chain([511, 512, 513, 1023, 4095, 8193]) {
            for step in [1, 3, 55, 56, 63, 64, 65, 127, 512, 8193] {
                let mut state = ShaState::default();
                for piece in data[..length].chunks(step) {
                    state.update(piece).unwrap();
                    state = ShaState::decode(&state.encode()).unwrap();
                }
                assert_eq!(
                    state.digest(),
                    format!("{:x}", Sha256::digest(&data[..length])),
                    "len={length} step={step}"
                );
            }
        }
    }
    #[test]
    fn resumable_sha_refuses_inconsistent_lengths_and_trailing_data() {
        let encoded = ShaState::default().encode();
        for length in 0..encoded.len() {
            assert!(ShaState::decode(&encoded[..length]).is_err());
        }
        let mut bad = encoded.clone();
        bad.push(0);
        assert!(ShaState::decode(&bad).is_err());
        let mut bad = encoded.clone();
        bad[39] = 1;
        assert!(ShaState::decode(&bad).is_err());
        let mut bad = encoded;
        bad[0] ^= 1;
        assert!(ShaState::decode(&bad).is_err());
    }
}
