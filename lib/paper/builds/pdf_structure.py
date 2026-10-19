"""Strict PDF structure audit, executed only inside the bounded PDF worker.

Entry point: inspect_pdf. pypdf owns PDF syntax/decoding; this module checks
complete object references, page-tree consistency, geometry and complete decoded
content syntax. It neither repairs malformed input nor renders/rasterizes it.
"""
from __future__ import annotations

import logging
import math
import sys
import zlib
from pathlib import Path



class PdfValidationFailure(RuntimeError):
    def __init__(self, code: str = 'pdf_invalid'):
        self.code = code
        super().__init__(code)


class _RejectRepair(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            raise PdfValidationFailure()


def inspect_pdf(path: Path) -> dict[str, int]:
    from pypdf import PdfReader, filters, get_configuration, overwrite_configuration
    from pypdf.generic import (ArrayObject, DictionaryObject, IndirectObject,
                               StreamObject, NumberObject)

    # These are worker-local library limits, never process-global app policy.
    original_configuration = get_configuration()
    overwrite_configuration(original_configuration,
        maximum_declared_stream_length=sys.maxsize,
        array_based_stream_maximum_output_length=sys.maxsize,
        zlib_maximum_output_length=sys.maxsize,
        lzw_maximum_output_length=sys.maxsize,
        run_length_maximum_output_length=sys.maxsize,
        jbig2_maximum_output_length=sys.maxsize)
    original_decompress = filters.decompress
    def strict_decompress(data):
        # PdfReader.strict does not disable the filter's partial-zlib repair.
        # A compiled artifact must contain a complete compressed stream.
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(data)
        if not decoder.eof or decoder.unused_data:
            raise PdfValidationFailure()
        return decoded
    filters.decompress = strict_decompress
    # Third-party library logger, not this module's own log channel.
    pypdf_logger = logging.getLogger('pypdf')
    original_level, original_propagation = (
        pypdf_logger.level, pypdf_logger.propagate)
    reject = _RejectRepair()
    pypdf_logger.addHandler(reject)
    pypdf_logger.setLevel(logging.WARNING)
    pypdf_logger.propagate = False
    metrics = {'pages': 0, 'objects': 0, 'decoded_bytes': 0}
    try:
        with path.open('rb') as source:
            reader = PdfReader(source, strict=True, root_object_recovery_limit=0)
            if reader.is_encrypted:
                raise PdfValidationFailure()
            size = reader.trailer.get('/Size')
            highest = max((number for rows in reader.xref.values() for number in rows), default=0)
            highest = max(highest, max(reader.xref_objStm, default=0))
            if not isinstance(size, NumberObject) or size <= highest:
                raise PdfValidationFailure()

            seen_references: set[tuple[int, int]] = set()
            seen_containers: set[int] = set()

            def resolve(value):
                return value.get_object() if isinstance(value, IndirectObject) else value

            def audit(value):
                pending = [value]
                while pending:
                    current = pending.pop()
                    if isinstance(current, IndirectObject):
                        key = (current.idnum, current.generation)
                        if key in seen_references:
                            continue
                        seen_references.add(key)
                        metrics['objects'] += 1
                        current = current.get_object()
                    if isinstance(current, (DictionaryObject, ArrayObject)):
                        if id(current) in seen_containers:
                            continue
                        seen_containers.add(id(current))
                    if isinstance(current, StreamObject):
                        decoded = current.get_data()
                        metrics['decoded_bytes'] += len(decoded)
                    if isinstance(current, DictionaryObject):
                        pending.extend(current.values())
                    elif isinstance(current, ArrayObject):
                        pending.extend(current)

            audit(reader.trailer)
            # Check every live xref entry, including objects not reached by a page.
            # pypdf's supported reader adapter is confined to this one module.
            for generation, rows in reader.xref.items():
                for number in rows:
                    if number == 0 or reader.xref_free_entry.get(generation, {}).get(number, False):
                        continue
                    audit(IndirectObject(number, generation, reader))
            for number in reader.xref_objStm:
                audit(IndirectObject(number, 0, reader))

            root = resolve(reader.trailer.get('/Root'))
            if not isinstance(root, DictionaryObject) or root.get('/Type') != '/Catalog':
                raise PdfValidationFailure()
            pages_root = resolve(root.get('/Pages'))
            if not isinstance(pages_root, DictionaryObject) or pages_root.get('/Type') != '/Pages':
                raise PdfValidationFailure()
            # Traverse Kids only: Parent links are allowed graph cycles, while
            # shared/repeated Kids and incorrect subtree Counts are malformed.
            pending = [(pages_root, None, False)]
            tree_seen: set[int] = set()
            subtree_counts: dict[int, int] = {}
            while pending:
                node, parent, leaving = pending.pop()
                node = resolve(node)
                if not isinstance(node, DictionaryObject):
                    raise PdfValidationFailure()
                identity = id(node)
                if leaving:
                    kids = resolve(node.get('/Kids'))
                    actual = sum(subtree_counts[id(resolve(child))] for child in kids)
                    if actual != int(node['/Count']):
                        raise PdfValidationFailure()
                    subtree_counts[identity] = actual
                    continue
                if identity in tree_seen:
                    raise PdfValidationFailure()
                if parent is not None and resolve(node.get('/Parent')) is not parent:
                    raise PdfValidationFailure()
                tree_seen.add(identity)
                if node.get('/Type') == '/Page':
                    metrics['pages'] += 1
                    subtree_counts[identity] = 1
                elif node.get('/Type') == '/Pages':
                    count, kids = resolve(node.get('/Count')), resolve(node.get('/Kids'))
                    if not isinstance(count, NumberObject) or count < 0 or not isinstance(kids, ArrayObject):
                        raise PdfValidationFailure()
                    pending.append((node, parent, True))
                    pending.extend((child, node, False) for child in kids)
                else:
                    raise PdfValidationFailure()
            if metrics['pages'] < 1:
                raise PdfValidationFailure()
            for page in reader.pages:
                box = [float(value) for value in page.mediabox]
                if len(box) != 4 or not all(math.isfinite(value) for value in box) or box[2] <= box[0] or box[3] <= box[1]:
                    raise PdfValidationFailure()
                content = page.get_contents()
                if content is not None:
                    # Force complete operator parsing; malformed syntax still refuses.
                    _ = content.operations
            return metrics
    finally:
        filters.decompress = original_decompress
        overwrite_configuration(original_configuration)
        pypdf_logger.setLevel(original_level)
        pypdf_logger.propagate = original_propagation
        pypdf_logger.removeHandler(reject)
