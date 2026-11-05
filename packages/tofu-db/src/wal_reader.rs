//! Bounded read-ahead for the existing WAL frame decoder. Entry: buffered_reader.
//! The captured file length bounds I/O, never accepted durable work. Depends only
//! on VfsFile and the generated fixed read-buffer size; framing stays in wal.rs.

use std::io::{self, BufReader, Read};

use crate::generated_tofudb_ir::WAL_RECOVERY_READ_BUFFER_BYTES;
use crate::vfs::VfsFile;

struct WalReadCursor<'a> {
    file: &'a mut dyn VfsFile,
    length: u64,
    offset: u64,
}

impl Read for WalReadCursor<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let count = (self.length - self.offset).min(output.len() as u64) as usize;
        if count != 0 {
            self.file.read_exact_at(self.offset, &mut output[..count])?;
            self.offset += count as u64;
        }
        Ok(count)
    }
}

pub(super) fn buffered_reader(file: &mut dyn VfsFile, length: u64) -> impl Read + '_ {
    BufReader::with_capacity(
        WAL_RECOVERY_READ_BUFFER_BYTES,
        WalReadCursor { file, length, offset: 0 },
    )
}
