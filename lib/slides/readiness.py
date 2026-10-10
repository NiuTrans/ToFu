"""Slide-render worker readiness checked before expensive production starts.

The topic recipe does not need Playwright until its render stage.  Importing
the dependency there made a wrong server interpreter look healthy for several
minutes while research and authoring ran, then fail at the last mile.  This
leaf owns one lazy, process-cached browser launch so the capability boundary
can reject an unusable worker before it creates a task or spends model calls.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading

__all__ = ['SlidesRuntimeUnavailable', 'ensure_slides_runtime_ready']


class SlidesRuntimeUnavailable(RuntimeError):
    """The current worker interpreter cannot render slide previews."""

    retryable = False

    def __init__(self, detail: str):
        self.detail = str(detail or 'unknown slide-render runtime failure')
        self.hint = (
            'Make .tofu_env.json point to the environment installed for this '
            'project, verify that environment can import Playwright and launch '
            'Chromium, then restart Tofu.')
        super().__init__(
            f'PPT rendering is unavailable in Python environment '
            f'{sys.prefix}: {self.detail}')

    def tool_result(self) -> dict:
        """Return the bounded user-facing shape used by ``produce_slides``."""
        return {
            'ok': False,
            'kind': 'slides_runtime_unavailable',
            'retryable': False,
            'detail': str(self),
            'hint': self.hint,
        }

    def error_envelope(self) -> dict:
        """Return a complete task-runtime envelope without leaking traceback."""
        from lib.error_envelope import make_envelope
        return make_envelope(
            'internal',
            message='PPT 渲染环境不可用\nPPT rendering environment unavailable',
            detail=str(self),
            hint=(
                '请让 .tofu_env.json 指向本项目已安装依赖的 Python 环境，'
                '确认 Playwright/Chromium 可启动后重启 Tofu。\n\n'
                + self.hint),
            context='slides:runtime-readiness',
            source='lib.slides.readiness',
            retryable=False,
        )


_ready_lock = threading.Lock()
_ready_identity: tuple[str, str] | None = None
_REQUIRED_EXPORT_IMPORTS = (
    'brotli',
    'fontTools.ttLib',
    'pptx',
    'yaml',
)


def _runtime_identity() -> tuple[str, str]:
    return os.path.realpath(sys.executable), os.path.realpath(sys.prefix)


def ensure_slides_runtime_ready() -> dict:
    """Prove Playwright import + Chromium launch once for this interpreter.

    Success is cached for the process lifetime.  Failures are deliberately not
    cached: an operator may repair the environment and retry without replacing
    an API worker first, although a restart remains the normal recovery path.
    """
    global _ready_identity
    identity = _runtime_identity()
    if _ready_identity == identity:
        return {'python': identity[0], 'prefix': identity[1], 'cached': True}

    with _ready_lock:
        if _ready_identity == identity:
            return {'python': identity[0], 'prefix': identity[1],
                    'cached': True}
        for module_name in _REQUIRED_EXPORT_IMPORTS:
            try:
                importlib.import_module(module_name)
            except (ImportError, ModuleNotFoundError) as exc:
                raise SlidesRuntimeUnavailable(
                    f'cannot import required export dependency '
                    f'{module_name} ({exc})') from exc
        try:
            from playwright.sync_api import sync_playwright
        except (ImportError, ModuleNotFoundError) as exc:
            raise SlidesRuntimeUnavailable(
                f'cannot import playwright.sync_api ({exc})') from exc

        try:
            import chromium_env
            chromium_env.ensure_chromium_env(os.environ)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    version = str(browser.version or '')
                finally:
                    browser.close()
        except Exception as exc:
            raise SlidesRuntimeUnavailable(
                f'Chromium smoke launch failed ({type(exc).__name__}: {exc})'
            ) from exc

        _ready_identity = identity
        return {'python': identity[0], 'prefix': identity[1],
                'browser': version, 'cached': False}
