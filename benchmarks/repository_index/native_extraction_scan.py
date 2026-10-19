"""Explicit native batch scanner for owned offline tool-publication experiments.

Canonical admission/read/Python/reference helpers remain borrowed from outline.
This candidate retains one batch of admitted non-Python sources and starts one
owned child per scan. It has no application selection, repository cache or
production lifecycle admission. Grammar lookup still uses the ordinary Python
catalog here; cold import elimination is not claimed by this adapter.
"""
import json
import os
from pathlib import Path
import subprocess

from lib.repo_map import outline


class NativeBatchScanner:
    format = 'tofu.repository-extraction/v1'

    def __init__(self, binary, grammar_directory, *, strict=True):
        self.binary = Path(binary).resolve(strict=True)
        self.grammar_directory = Path(grammar_directory).resolve(strict=True)
        self.calls = 0
        self.strict = strict

    def source_request(self, name, text):
        from grep_ast.parsers import filename_to_lang
        return dict(rel_path=name, language=filename_to_lang(name) or '', source=text)

    def is_code_path(self, name):
        return outline._is_code_path(name)

    def invoke(self, payload, response_limit):
        completed = subprocess.run([str(self.binary), str(self.grammar_directory), str(len(payload)),
                                    str(response_limit)],
                                   input=payload, capture_output=True, timeout=60, check=True)
        return completed.stdout

    def __call__(self, root, *, path='', focus='', include_refs=True, include_tests=False):
        root = os.path.abspath(root)
        files = outline.list_files(root)
        if path:
            prefix = path.strip('/').rstrip('/') + '/'
            files = [name for name in files if name.startswith(prefix) or name == prefix.rstrip('/')]
        if focus:
            files = [name for name in files if focus.lower() in name.lower()]
        result = outline.RepoMap(root=root, files=files)
        python_tags, batch, ordered = {}, [], []
        for name in files:
            if not self.is_code_path(name) or outline._is_generated(name):
                continue
            if not include_tests and outline._is_test_path(name):
                continue
            content = outline._read_code_source(root, name)
            if content is None:
                continue
            text, admitted = content
            ordered.append(name)
            if admitted:
                if name.endswith('.py'):
                    python_tags[name] = outline._extract_python_tags(name, text)
                else:
                    batch.append(self.source_request(name, text))
            if include_refs:
                result.add_references(name, outline.extract_identifiers(text[:outline._MAX_REF_BYTES]))
        native_tags = {}
        if batch:
            payload = json.dumps(dict(format=self.format, files=batch), ensure_ascii=False).encode('utf8')
            response = json.loads(self.invoke(payload, max(1_048_576, len(payload) * 64)))
            if response['format'] != self.format or len(response['files']) != len(batch):
                raise ValueError('native extraction response differs from the request')
            for request, row in zip(batch, response['files']):
                if row['rel_path'] != request['rel_path'] or any(tag['rel_path'] != row['rel_path'] for tag in row['tags']):
                    raise ValueError('native extraction returned another file')
                if row['error'] is not None and self.strict:
                    # Strict experiments never relabel a missing grammar as a speedup.
                    raise RuntimeError('native extraction failed for an admitted file: ' + row['error'])
                if row['error'] is not None and row['tags']:
                    raise ValueError('failed native extraction returned partial tags')
                native_tags[row['rel_path']] = [outline.Tag(**tag) for tag in row['tags']]
            self.calls += 1
        # Preserve canonical file/tag insertion order for ranking and references.
        for name in ordered:
            for tag in python_tags.get(name, native_tags.get(name, ())):
                result.add_tag(tag)
        return result
