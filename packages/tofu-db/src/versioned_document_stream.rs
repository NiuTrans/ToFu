//! Reachable versioned-document bytes without logical-value hydration.
//! Only callers holding an owner-scoped entity envelope may open this reader.
//! Inline envelopes stay bounded by the entity codec; blobs decode one physical
//! chunk at a time, and random reads verify only the intersecting chunk blocks.

use super::{invalid_data, Document, DocumentValue};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::blob::BlobReader;
use std::io::{self, Cursor, Read};

pub(crate) struct Source<'a> {
    value: SourceValue<'a>,
    pub(crate) logical_key: String,
    pub(crate) total_bytes: u64,
}

enum SourceValue<'a> {
    Inline(Cursor<Vec<u8>>),
    Blob {
        reader: BlobReader<'a>,
        chunk: Cursor<Vec<u8>>,
    },
}

impl<'a> Source<'a> {
    pub(crate) fn open(
        database: &'a AuthorityDatabase,
        transaction: &AuthorityTransaction,
        stored: &[u8],
        namespace: &str,
    ) -> io::Result<Self> {
        let document = Document::decode(stored)?;
        if document.namespace != namespace {
            return Err(invalid_data("streamed document namespace differs"));
        }
        let (value, total_bytes) = match document.value {
            DocumentValue::Inline(bytes) => {
                let count = bytes.len() as u64;
                (SourceValue::Inline(Cursor::new(bytes)), count)
            }
            DocumentValue::Blob(reference) => (
                SourceValue::Blob {
                    reader: database.reachable_blob_reader(
                        transaction.tenant_id(),
                        transaction.owner_user_id(),
                        reference,
                    )?,
                    chunk: Cursor::new(Vec::new()),
                },
                reference.logical_bytes,
            ),
        };
        Ok(Self {
            value,
            logical_key: document.key,
            total_bytes,
        })
    }

    pub(crate) fn range(&self, offset: u64, length: usize) -> io::Result<Vec<u8>> {
        if length > 65_536 || offset > self.total_bytes || length as u64 > self.total_bytes - offset
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "document range exceeds source",
            ));
        }
        match &self.value {
            SourceValue::Inline(bytes) => {
                Ok(bytes.get_ref()[offset as usize..offset as usize + length].to_vec())
            }
            SourceValue::Blob { reader, .. } => reader.read_range(offset, length),
        }
    }
}

impl Read for Source<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if output.is_empty() {
            return Ok(0);
        }
        match &mut self.value {
            SourceValue::Inline(bytes) => bytes.read(output),
            SourceValue::Blob { reader, chunk } => {
                if chunk.position() == chunk.get_ref().len() as u64 {
                    match reader.next_chunk()? {
                        Some(bytes) => *chunk = Cursor::new(bytes),
                        None => return Ok(0),
                    }
                }
                chunk.read(output)
            }
        }
    }
}
