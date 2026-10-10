"""Instrument owned upload stages without changing their production bodies.

Entry: ProfiledHtmlCorpus. Uses durable.py's actual native upload/readback harness.
These instrumented observations diagnose costs; they are never admission evidence.
"""
from functools import wraps as wraps
from time import perf_counter_ns as perf_counter_ns

from benchmarks.knowledge_html.durable import DurableHtmlCorpus as DurableHtmlCorpus, semantic_projection as semantic_projection


class ProfiledHtmlCorpus(DurableHtmlCorpus):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phase_samples = []
        self._phases = None
        namespace = self.add_document.__globals__
        for name, stage in (('extract', 'extract'), ('chunk_document', 'chunk'),
                            ('_trimmed_index_chunks', 'search_index'),
                            ('_write_source', 'source_write_fsync')):
            namespace[name] = self._observe(stage, namespace[name])
        repository = self.repository
        def instrument_repository(user_id):
            result = repository(user_id)
            result.document_by_digest = self._observe('digest_lookup', result.document_by_digest)
            result.create_document = self._observe('native_create', result.create_document)
            result.document_content = self._observe('native_content_read', result.document_content)
            result.settings = self._observe('native_visual_settings', result.settings)
            return result
        namespace['_repository'] = instrument_repository
        add = self.add_document
        @wraps(add)
        def upload(*arguments, **options):
            if self._phases is not None:
                raise RuntimeError('previous upload has no matching content read')
            self._phases = {}
            try:
                return self._observe('upload_total', add)(*arguments, **options)
            except BaseException:
                self._phases = None
                raise
        self.add_document = upload
        read = self.get_document_content
        @wraps(read)
        def content(*arguments, **options):
            if self._phases is None:
                return read(*arguments, **options)  # reopen checks are untimed
            try:
                return self._observe('public_read_total', read)(*arguments, **options)
            finally:
                self.phase_samples.append(self._phases)
                self._phases = None
        self.get_document_content = content

    def _observe(self, stage, function):
        @wraps(function)
        def observed(*arguments, **options):
            if self._phases is None:
                return function(*arguments, **options)
            started = perf_counter_ns()
            try:
                return function(*arguments, **options)
            finally:
                elapsed = (perf_counter_ns() - started) / 1e6
                self._phases[stage] = self._phases.get(stage, 0) + elapsed
        return observed
