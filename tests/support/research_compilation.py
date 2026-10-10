"""Synthetic accepted build references for pure scientific-validity unit tests.

Native compilation and persistence authority are exercised separately in
test_research_compilation; these fixtures are not native build evidence.
"""
from lib.research.program import source_tree_digest
from lib.paper.builds.records import SourceRecords
from lib.paper.builds.sources import manifest_digest


def compilation_projection(source_files):
    digest = source_tree_digest(source_files)
    return {'mode': 'paper_build', 'status': 'passing', 'detail': '',
            'source_digest': digest, 'engine': 'pdflatex', 'compiled_at': 1,
            'build': {'document_id': 'a' * 64, 'build_id': 'b' * 32,
                      'source_digest': digest,
                      'manifest_digest': manifest_digest(SourceRecords.from_text(source_files).manifest),
                      'pdf_sha256': 'c' * 64}}
