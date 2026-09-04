"""Bounded LaTeX source-tree scaffolding and portable ZIP export.

This owner never invokes TeX and never writes user sources to the host.  A
compiler is an explicit bound capability with its own receipt; export is a
pure in-memory transformation over normalized relative paths.
"""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Mapping
from typing import Any

from .program import normalize_program_fields


def latex_escape(value: Any) -> str:
    text = str(value or '')
    replacements = {
        '\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$',
        '#': r'\#', '_': r'\_', '{': r'\{', '}': r'\}',
        '~': r'\textasciitilde{}', '^': r'\textasciicircum{}',
    }
    return ''.join(replacements.get(char, char) for char in text)


def _section(text: Any, fallback: str) -> str:
    cleaned = latex_escape(text).strip()
    return f'{cleaned}\n' if cleaned else f'% {fallback}\n'


def scaffold_source_files(program: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Merge a conference-shaped source scaffold without overwriting edits."""
    normalized = normalize_program_fields(program)
    manuscript = normalized['manuscript']
    existing = {
        str(row.get('path')): dict(row)
        for row in normalized['source_files'] if row.get('path')
    }
    title = latex_escape(manuscript.get('title') or 'Untitled Research Project')
    templates = {
        'main.tex': rf"""\documentclass[11pt]{{article}}
\usepackage[margin=1in]{{geometry}}
\usepackage{{amsmath,amssymb,booktabs,graphicx,microtype}}
\usepackage[numbers,sort&compress]{{natbib}}
\usepackage[colorlinks=true,allcolors=blue]{{hyperref}}
\usepackage[nameinlink,noabbrev]{{cleveref}}
\title{{{title}}}
\author{{Anonymous Authors}}
\date{{}}
\begin{{document}}
\maketitle
\input{{sections/abstract}}
\input{{sections/introduction}}
\input{{sections/related_work}}
\input{{sections/method}}
\input{{sections/experiments}}
\input{{sections/results}}
\input{{sections/limitations}}
\input{{sections/conclusion}}
\input{{sections/ethics}}
\bibliographystyle{{plainnat}}
\bibliography{{references}}
\end{{document}}
""",
        'sections/abstract.tex': '\\begin{abstract}\n'
            + _section(manuscript.get('abstract'), 'State problem, method, evidence, and result.')
            + '\\end{abstract}\n',
        'sections/introduction.tex': '\\section{Introduction}\n'
            + _section(manuscript.get('introduction'), 'Motivation, gap, contributions.'),
        'sections/related_work.tex': '\\section{Related Work}\n'
            + _section(manuscript.get('related_work'), 'Position against the closest work.'),
        'sections/method.tex': '\\section{Method}\n'
            + _section(manuscript.get('method'), 'Define the method precisely and reproducibly.'),
        'sections/experiments.tex': '\\section{Experiments}\n'
            + _section(manuscript.get('experiments'), 'Datasets, baselines, metrics, seeds, resources.'),
        'sections/results.tex': '\\section{Results}\n'
            + _section(manuscript.get('results'), 'Report evidence with uncertainty and ablations.'),
        'sections/limitations.tex': '\\section{Limitations}\n'
            + _section(manuscript.get('limitations'), 'Scope, failure cases, external validity.'),
        'sections/conclusion.tex': '\\section{Conclusion}\n'
            + _section(manuscript.get('conclusion'), 'Conclusions supported by the evidence.'),
        'sections/ethics.tex': '\\section*{Ethics Statement}\n'
            + _section(manuscript.get('ethics'), 'Risks, consent, licenses, and broader impacts.'),
        'references.bib': '% Add verified BibTeX records. Do not cite unverified references.\n',
        'figures/README.md': 'Generated figures belong here; retain data and script references in the workspace.\n',
    }
    now = int(time.time())
    for path, content in templates.items():
        if path not in existing:
            existing[path] = {'path': path, 'content': content, 'updated_at': now}
    return normalize_program_fields({'source_files': list(existing.values())})['source_files']


def export_source_zip(program: Mapping[str, Any]) -> bytes:
    """Return a deterministic ZIP of normalized manuscript sources."""
    files = normalize_program_fields(program)['source_files']
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for row in sorted(files, key=lambda item: item['path']):
            info = zipfile.ZipInfo(row['path'], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, row['content'].encode('utf-8'))
    return output.getvalue()


__all__ = ['export_source_zip', 'latex_escape', 'scaffold_source_files']
