//! Canonical delta-varint encoding for stable, owner-scoped generation IDs.
//! IDs are allocated by the projection dictionary, never truncated digests.
//! This codec does not allocate IDs, publish generations, or decide budgets.

use std::io;

const VERSION: u8 = 1;

fn malformed() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid generation-ID posting list")
}

/// Encode strictly increasing, nonzero IDs. The caller supplies the physical
/// value bound; exhausting it never silently discards or universalizes entries.
pub(super) fn encode(ids: &[u64], maximum_bytes: usize) -> io::Result<Vec<u8>> {
    let mut bytes = vec![VERSION];
    if bytes.len() > maximum_bytes {
        return Err(io::Error::new(io::ErrorKind::OutOfMemory, "posting value exceeds bound"));
    }
    let mut previous = 0;
    for &id in ids {
        let mut delta = id.checked_sub(previous).filter(|delta| *delta != 0).ok_or_else(malformed)?;
        previous = id;
        loop {
            if bytes.len() == maximum_bytes {
                return Err(io::Error::new(io::ErrorKind::OutOfMemory, "posting value exceeds bound"));
            }
            let remaining = delta >> 7;
            bytes.push((delta as u8 & 127) | if remaining != 0 { 128 } else { 0 });
            delta = remaining;
            if delta == 0 { break; }
        }
    }
    Ok(bytes)
}

/// Decode only canonical encodings, enforcing both byte and entry budgets
/// before growing the result. Corruption never produces an incomplete list.
pub(super) fn decode(bytes: &[u8], maximum_bytes: usize, maximum_entries: usize) -> io::Result<Vec<u64>> {
    if bytes.first() != Some(&VERSION) || bytes.len() > maximum_bytes { return Err(malformed()); }
    let mut ids = Vec::new();
    let mut position = 1;
    let mut previous = 0_u64;
    while position < bytes.len() {
        if ids.len() == maximum_entries { return Err(malformed()); }
        let mut delta = 0_u64;
        let mut shift = 0;
        loop {
            let byte = *bytes.get(position).ok_or_else(malformed)?;
            position += 1;
            let payload = byte & 127;
            if shift == 63 && payload > 1 { return Err(malformed()); }
            delta |= u64::from(payload) << shift;
            if byte & 128 == 0 {
                if shift != 0 && payload == 0 { return Err(malformed()); }
                break;
            }
            if shift == 63 { return Err(malformed()); }
            shift += 7;
        }
        previous = previous.checked_add(delta).filter(|_| delta != 0).ok_or_else(malformed)?;
        ids.push(previous);
    }
    Ok(ids)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sparse_and_dense_generation_ids_preserve_exact_membership() {
        let mut ids: Vec<u64> = (1..=4096).collect();
        ids.extend([8192, 1 << 32, u64::MAX]);
        let bytes = encode(&ids, 8192).unwrap();
        assert_eq!(decode(&bytes, 8192, ids.len()).unwrap(), ids);
        assert!(bytes.len() < 4120);
        for removed in [1, 127, 128, 4096, 8192, u64::MAX] {
            let mut remaining = ids.clone();
            remaining.retain(|id| *id != removed);
            let bytes = encode(&remaining, 8192).unwrap();
            assert_eq!(decode(&bytes, 8192, remaining.len()).unwrap(), remaining);
        }
    }

    #[test]
    fn corrupt_or_over_budget_postings_are_never_partial_results() {
        for bytes in [vec![], vec![0], vec![1, 0], vec![1, 128], vec![1, 129, 0],
            vec![1, 255, 255, 255, 255, 255, 255, 255, 255, 255, 2],
            vec![1, 255, 255, 255, 255, 255, 255, 255, 255, 255, 1, 1]] {
            assert!(decode(&bytes, 8192, 8192).is_err(), "accepted {bytes:?}");
        }
        for ids in [vec![0], vec![1, 1], vec![2, 1]] { assert!(encode(&ids, 8192).is_err()); }
        let bytes = encode(&[1, 128, u64::MAX], 8192).unwrap();
        assert!(decode(&bytes, bytes.len() - 1, 3).is_err());
        assert!(decode(&bytes, bytes.len(), 2).is_err());
        assert!(encode(&[1, 128, u64::MAX], bytes.len() - 1).is_err());
        assert_eq!(decode(&[1], 1, 0).unwrap(), Vec::<u64>::new());
    }
}
