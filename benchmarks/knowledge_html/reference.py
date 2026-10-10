"""Independent pre-promotion HTML oracle, loaded from immutable Git source."""
import ast
import hashlib
from pathlib import Path
import subprocess
from lib.knowledge import ingest

ROOT=Path(__file__).resolve().parents[2]
REFERENCE_COMMIT='8c62baae3'


def load_reference():
    source=subprocess.check_output(['/usr/bin/git','show',f'{REFERENCE_COMMIT}:lib/knowledge/ingest.py'],cwd=ROOT)
    tree=ast.parse(source)
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_html_to_text')
    namespace=dict(ingest.__dict__)
    exec(compile(ast.Module(body=[function],type_ignores=[]),'<pre-promotion-knowledge-html>','exec'),namespace)
    return namespace['_html_to_text'],hashlib.sha256(source).hexdigest()
