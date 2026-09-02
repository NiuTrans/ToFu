"""Verified offline/online catalog download and installation service.

Both the Settings route and the model-facing request tool call this boundary.
It accepts only an exact catalog id, requires an explicit owner, streams into a
bounded buffer, and delegates activation only with an immutable revision plus
either a canonical content digest or an exact registry file manifest.
"""

from __future__ import annotations

import io
import re
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from lib.http_client import http_get
from lib.identity import require_user_id
from lib.log import get_logger
from lib.skills.catalog import get_catalog_entry
from lib.skills.installer import InstallerError, install_skill_package

logger = get_logger(__name__)

_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60
_HEX_40 = re.compile(r'^[0-9a-f]{40}$')
_HEX_64 = re.compile(r'^[0-9a-f]{64}$')
_ALLOWED_DOWNLOAD_HOSTS = frozenset({'codeload.github.com'})


class CatalogInstallError(InstallerError):
    """Typed catalog failure suitable for HTTP and model-tool adapters."""

    def __init__(self, message: str, *, code: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _approved_download_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ''))
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == 'https'
        and parsed.hostname in _ALLOWED_DOWNLOAD_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def _sealed_entry(catalog_id: str) -> dict[str, Any]:
    exact_id = str(catalog_id or '').strip()
    entry = get_catalog_entry(exact_id)
    if entry is None:
        raise CatalogInstallError(
            f'Unknown skill catalog id: {exact_id!r}',
            code='unknown_catalog_id', http_status=404)
    if not entry.get('installable', True):
        reason = str(entry.get('unavailable_reason') or 'not installable')
        raise CatalogInstallError(
            f'Skill {exact_id!r} is unavailable: {reason}',
            code='catalog_entry_unavailable', http_status=409)
    revision = str(entry.get('source_revision') or '')
    digest = str(entry.get('content_sha256') or '')
    url = str(entry.get('download_url') or '')
    if (not _HEX_40.fullmatch(revision)
            or not _HEX_64.fullmatch(digest)
            or not _approved_download_url(url)
            or revision not in url):
        raise CatalogInstallError(
            f'Skill {exact_id!r} has an invalid immutable catalog seal',
            code='invalid_catalog_seal', http_status=409)
    return entry


def _download_archive(
    entry: dict[str, Any],
    *,
    getter: Callable[..., Any],
) -> bytes:
    catalog_id = str(entry['id'])
    response = None
    try:
        response = getter(
            str(entry['download_url']),
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            stream=True,
        )
        response.raise_for_status()
        final_url = str(getattr(response, 'url', '') or entry['download_url'])
        if not _approved_download_url(final_url):
            raise CatalogInstallError(
                'Catalog download redirected outside the approved host set',
                code='download_redirect_rejected', http_status=502)
        headers = getattr(response, 'headers', None)
        declared = headers.get('Content-Length') if headers else None
        if declared:
            try:
                if int(declared) > _DOWNLOAD_MAX_BYTES:
                    raise CatalogInstallError(
                        f'Catalog archive exceeds '
                        f'{_DOWNLOAD_MAX_BYTES // (1024 * 1024)} MiB',
                        code='archive_too_large', http_status=413)
            except ValueError:
                logger.debug('[Skills] invalid Content-Length for %s: %r',
                             catalog_id, declared)
        buffer = io.BytesIO()
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _DOWNLOAD_MAX_BYTES:
                raise CatalogInstallError(
                    f'Catalog archive exceeds '
                    f'{_DOWNLOAD_MAX_BYTES // (1024 * 1024)} MiB',
                    code='archive_too_large', http_status=413)
            buffer.write(chunk)
        if total == 0:
            raise CatalogInstallError(
                'Catalog download returned an empty archive',
                code='empty_download', http_status=502)
        return buffer.getvalue()
    except CatalogInstallError:
        raise
    except requests.RequestException as exc:
        logger.warning('[Skills] catalog download failed for %s: %s',
                       catalog_id, exc)
        raise CatalogInstallError(
            'Catalog download failed; try again later.',
            code='download_failed', http_status=502) from exc
    except (OSError, ValueError) as exc:
        logger.warning('[Skills] catalog download failed for %s: %s',
                       catalog_id, exc)
        raise CatalogInstallError(
            'Catalog download failed; try again later.',
            code='download_failed', http_status=502) from exc
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            close()


def install_catalog_skill(
    catalog_id: str,
    *,
    owner_user_id: int,
    source_revision: str | None = None,
    project_path: str | None = None,
    scope: str = 'global',
    overwrite: bool = False,
    http_get_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Download and install one exact sealed offline or ClawHub entry."""
    owner = require_user_id(owner_user_id, context='skill catalog install')
    if scope not in ('project', 'global'):
        raise CatalogInstallError(
            f'Invalid skill scope: {scope!r}',
            code='invalid_scope', http_status=400)
    if scope == 'project' and not project_path:
        raise CatalogInstallError(
            'Project scope requires an attached project',
            code='project_required', http_status=400)
    from lib.skills.online_catalog import (
        OnlineCatalogError,
        install_clawhub_skill,
        parse_clawhub_catalog_id,
    )
    try:
        clawhub_identity = parse_clawhub_catalog_id(catalog_id)
    except OnlineCatalogError as exc:
        raise CatalogInstallError(
            str(exc), code=exc.code, http_status=exc.http_status) from exc
    if clawhub_identity is not None:
        try:
            return install_clawhub_skill(
                catalog_id,
                str(source_revision or ''),
                owner_user_id=owner,
                project_path=project_path,
                scope=scope,
                overwrite=bool(overwrite),
                http_get_fn=http_get_fn,
            )
        except OnlineCatalogError as exc:
            raise CatalogInstallError(
                str(exc), code=exc.code, http_status=exc.http_status) from exc

    entry = _sealed_entry(catalog_id)
    requested_revision = str(source_revision or '').strip()
    if (requested_revision
            and requested_revision != str(entry['source_revision'])):
        raise CatalogInstallError(
            'The catalog entry changed after discovery; search again before '
            'installing it.',
            code='catalog_revision_changed', http_status=409)
    archive = _download_archive(entry, getter=http_get_fn or http_get)
    try:
        return install_skill_package(
            archive,
            scope=scope,
            project_path=project_path,
            owner_user_id=owner,
            overwrite=bool(overwrite),
            original_filename=f'{entry["id"]}.zip',
            catalog_id=str(entry['id']),
            subdir=str(entry.get('subdir') or '') or None,
            expected_content_sha256=str(entry['content_sha256']),
            source_revision=str(entry['source_revision']),
            source_registry='curated',
            source_url=str(entry.get('homepage') or '') or None,
        )
    except CatalogInstallError:
        raise
    except InstallerError as exc:
        raise CatalogInstallError(
            str(exc), code='package_rejected', http_status=400) from exc


__all__ = ['CatalogInstallError', 'install_catalog_skill']
