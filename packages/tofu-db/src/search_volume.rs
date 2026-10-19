//! Physical search-volume write admission. Geometry derives a bounded reserve;
//! independently unknown dimensions never hide a known exhausted dimension.
use crate::generated_conversation_search::*;
use crate::vfs::{FilesystemCapacity, Vfs};
use std::io;
use std::path::Path;

fn reserve(total: u64, divisor: u64, minimum: u64, maximum: u64) -> u64 {
    (total / divisor)
        .clamp(minimum, maximum)
        .min(total / SEARCH_VOLUME_MAXIMUM_RESERVE_FRACTION_DIVISOR)
}

pub(crate) fn check_capacity(capacity: FilesystemCapacity) -> io::Result<()> {
    let dimensions = [
        (
            capacity.total_bytes,
            capacity.available_bytes,
            SEARCH_VOLUME_BYTES_RESERVE_DIVISOR,
            SEARCH_VOLUME_MINIMUM_BYTES_RESERVE,
            SEARCH_VOLUME_MAXIMUM_BYTES_RESERVE,
            "bytes",
        ),
        (
            capacity.total_inodes,
            capacity.available_inodes,
            SEARCH_VOLUME_INODES_RESERVE_DIVISOR,
            SEARCH_VOLUME_MINIMUM_INODES_RESERVE,
            SEARCH_VOLUME_MAXIMUM_INODES_RESERVE,
            "inodes",
        ),
    ];
    for (total, available, divisor, minimum, maximum, dimension) in dimensions {
        if let (Some(total), Some(available)) = (total, available) {
            if total > 0
                && (available == 0 || available < reserve(total, divisor, minimum, maximum))
            {
                return Err(io::Error::new(io::ErrorKind::StorageFull, format!("search projection volume has insufficient free {dimension}; indexing paused to preserve other writers")));
            }
        }
    }
    Ok(())
}

pub(crate) fn require_capacity(vfs: &dyn Vfs, path: &Path) -> io::Result<()> {
    check_capacity(vfs.filesystem_capacity(path)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dimensions_are_independent_and_unknown_is_not_full() {
        assert!(check_capacity(FilesystemCapacity::default()).is_ok());
        for capacity in [
            FilesystemCapacity {
                total_bytes: Some(1_000_000_000),
                available_bytes: Some(0),
                ..FilesystemCapacity::default()
            },
            FilesystemCapacity {
                total_inodes: Some(1_000_000),
                available_inodes: Some(0),
                ..FilesystemCapacity::default()
            },
        ] {
            assert_eq!(
                check_capacity(capacity).unwrap_err().kind(),
                io::ErrorKind::StorageFull
            );
        }
    }
    #[test]
    fn reserve_scales_without_rejecting_small_healthy_volumes() {
        assert_eq!(reserve(16_777_216, 1000, 1024, 100_000), 16_777);
        assert_eq!(reserve(400, 1000, 1024, 100_000), 40);
        assert!(check_capacity(FilesystemCapacity {
            total_inodes: Some(400),
            available_inodes: Some(200),
            total_bytes: Some(100_000_000),
            available_bytes: Some(50_000_000)
        })
        .is_ok());
    }
}
