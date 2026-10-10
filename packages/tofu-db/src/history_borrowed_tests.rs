// Borrowed raw-byte provenance, full decoder parity and fail-closed segment checks.
fn borrowed_history_fixture() -> (Vec<u8>, HistorySegmentReference) {
    let mut parent = [0; 32];
    let mut transactions = Vec::new();
    for sequence in 1..=3 {
        let mut tx = transaction(sequence, parent);
        let payload = vec![sequence as u8; 512 * 1024];
        tx.envelope.block_ids = vec![BlockId::for_payload(&payload)];
        tx.encoded_envelope = tx.envelope.encode_with_carried(&[Some(&payload)], None).unwrap();
        tx.record_hash = calculate_record_hash(sequence, parent, &tx.encoded_envelope).unwrap();
        parent = tx.record_hash;
        transactions.push(tx);
    }
    let segment = build_segments(&transactions, [0; 32]).unwrap().remove(0);
    let reference = HistorySegmentReference { block_id: BlockId::for_payload(&segment.encoded),
        payload_index_block_id: None, first_sequence: segment.first_sequence,
        last_sequence: segment.last_sequence, parent_hash: segment.parent_hash,
        terminal_hash: segment.terminal_hash };
    (segment.encoded, reference)
}

#[test]
fn borrowed_history_views_preserve_metadata_and_exact_carried_source_bytes() {
    let (source, reference) = borrowed_history_fixture();
    let owned = decode_segment(&source, &reference).unwrap();
    let views = decode_segment_views(&source, &reference).unwrap();
    assert_eq!(views.len(), owned.len());
    for (view, full) in views.iter().zip(&owned) {
        assert_eq!(view.sequence, full.sequence);
        assert_eq!(view.record_hash, full.record_hash);
        assert_eq!(view.envelope, full.envelope);
        assert_eq!(view.encoded_envelope, full.encoded_envelope.as_slice());
        let offset = (view.encoded_envelope.as_ptr() as usize).checked_sub(source.as_ptr() as usize).unwrap();
        assert_eq!(source.get(offset..offset + view.encoded_envelope.len()).unwrap(), view.encoded_envelope);
        assert_eq!(view.encoded_envelope.as_ptr(), source[offset..].as_ptr());
    }
}

#[test]
fn borrowed_history_views_reject_corrupt_headers_carried_bytes_and_terminal_witnesses() {
    let (source, reference) = borrowed_history_fixture();
    for offset in [0, 8, 12, 20, 52, 56, 64, 100, source.len() / 2, source.len() - 1] {
        let mut corrupt = source.clone();
        corrupt[offset] ^= 1;
        let full_error = decode_segment(&corrupt, &reference).err().expect("full decode must reject corruption");
        let view_error = decode_segment_views(&corrupt, &reference).err().expect("views must reject corruption");
        assert_eq!(view_error.kind(), full_error.kind());
    }
    let mut changed = reference.clone();
    changed.terminal_hash[0] ^= 1;
    assert_eq!(decode_segment_views(&source, &changed).err().unwrap().kind(), io::ErrorKind::InvalidData);
    assert_eq!(decode_segment_views(&source[..source.len() - 1], &reference).err().unwrap().kind(), io::ErrorKind::InvalidData);
}
