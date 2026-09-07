"""Small esbuild bridge for Paper tests that execute native owners."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
ESBUILD = ROOT / 'scripts' / 'vite_test_bundle.mjs'
SRC_ROOT = ROOT / 'frontend' / 'src'


def _shimmed_parent_dir(real_parent: Path) -> Path:
    """Mirror ``real_parent`` in a tmp dir so temp entries never touch src.

    A temp file written next to the retained sources is a newer authoring
    input: it flips the Vite authoring digest and the manifest-freshness
    scan for every concurrently running bundle test.  The shim recreates
    the ancestor chain with symlinks so relative imports from the temp
    entry resolve exactly as they would inside ``frontend/src``.
    """
    shim_root = Path(tempfile.mkdtemp(prefix='tofu-paper-vite-'))
    real_dir = SRC_ROOT
    shim_dir = shim_root
    for component in real_parent.relative_to(SRC_ROOT).parts:
        for entry in real_dir.iterdir():
            if entry.name == component:
                continue
            try:
                (shim_dir / entry.name).symlink_to(entry)
            except OSError:
                pass
        shim_dir = shim_dir / component
        shim_dir.mkdir()
        real_dir = real_dir / component
    for entry in real_dir.iterdir():
        try:
            (shim_dir / entry.name).symlink_to(entry)
        except OSError:
            pass
    return shim_dir


@contextmanager
def compiled_typescript(
    source: str | os.PathLike[str],
    *,
    contents: str | None = None,
    expose_feature_registry_to_window: bool = False,
) -> Iterator[str]:
    """Bundle one browser owner to a disposable IIFE and yield its path.

    Raw retained-section harnesses use ``window`` as their runtime scope.  The
    production entry connects that scope to ``featureRegistry`` before loading
    feature owners; opt into the same boundary here when a harness combines a
    native owner with raw retained sections.
    """
    original_source_path = Path(source).resolve()
    source_path = original_source_path
    temporary_sources: list[str] = []
    shim_parent: Path | None = None

    def write_temporary_source(source_contents: str) -> Path:
        nonlocal shim_parent
        if shim_parent is None:
            shim_parent = _shimmed_parent_dir(original_source_path.parent)
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.ts', prefix='.paper-test-',
            dir=shim_parent, delete=False, encoding='utf-8',
        ) as source_handle:
            source_handle.write(source_contents)
            temporary_sources.append(source_handle.name)
            return Path(source_handle.name)

    if contents is not None:
        source_path = write_temporary_source(contents)

    if expose_feature_registry_to_window:
        owner_source_path = source_path

        def module_specifier(target: Path) -> str:
            relative = os.path.relpath(
                target, original_source_path.parent,
            ).replace(os.sep, '/')
            return relative if relative.startswith('.') else f'./{relative}'

        registry_source = ROOT / 'frontend' / 'src' / 'feature-registry.ts'
        registry_import = module_specifier(registry_source)
        owner_import = module_specifier(owner_source_path)
        source_path = write_temporary_source(f"""
import {{ connectFeatureRuntime, featureRegistry }} from '{registry_import}';
import '{owner_import}';

type TestRuntime = Record<string, unknown>;
connectFeatureRuntime(
  (name) => (window as unknown as TestRuntime)[name],
  (name, value) => {{ (window as unknown as TestRuntime)[name] = value; }},
);
Object.defineProperty(window, '__tofuTestFeatureRegistry', {{
  configurable: true,
  value: featureRegistry,
}});
""")
    with tempfile.NamedTemporaryFile(suffix='.js', delete=False) as handle:
        output = handle.name
    try:
        proc = subprocess.run(
            [str(ESBUILD), os.fspath(source_path), '--bundle', '--format=iife',
             '--platform=browser', f'--outfile={output}'],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr or proc.stdout)
        yield output
    finally:
        try:
            os.unlink(output)
        except OSError:
            pass
        for temporary_source in reversed(temporary_sources):
            try:
                os.unlink(temporary_source)
            except OSError:
                pass
        if shim_parent is not None:
            depth = len(
                original_source_path.parent.relative_to(SRC_ROOT).parts
            )
            shim_root = shim_parent if depth == 0 else shim_parent.parents[
                depth - 1
            ]
            shutil.rmtree(shim_root, ignore_errors=True)
