"""Cancellable Overleaf HTTP export; one compile and bounded same-build downloads.

The configured origin supplies credentials. Output URLs never select credential
destinations or local paths. ExportStore owns temporary files and admission.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .contract import ExportError, limit

OUTPUT_NAMES = {'output.pdf': 'document.pdf', 'output.synctex.gz': 'document.synctex.gz',
                'output.log': 'build.log'}


class _CsrfParser(HTMLParser):
    token = ''

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'meta' and attributes.get('name') == 'ol-csrfToken':
            self.token = attributes.get('content') or ''


def _origin(url: str) -> tuple:
    parts = urlsplit(url)
    if (parts.scheme not in ('https', 'http') or not parts.hostname
            or parts.username or parts.password or parts.fragment):
        raise ExportError('export_origin')
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)


class OverleafTransport:
    def __init__(self, base_url: str, headers, *, output_origins=(), client_factory=None):
        self.base_url = base_url.rstrip('/')
        self.origin = _origin(self.base_url)
        if urlsplit(self.base_url).path not in ('', '/') or urlsplit(self.base_url).query:
            raise ExportError('export_origin')
        if len(output_origins) > 8:
            raise ExportError('export_origin')
        self.output_origins = {self.origin, *(_origin(url) for url in output_origins)}
        self.headers = headers
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=120), follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1)))

    def _headers(self, url: str) -> dict:
        # A redirect/CDN must never inherit the Overleaf session cookie.
        return {'Accept-Encoding': 'identity',
                **(self.headers() if _origin(url) == self.origin else {})}

    @staticmethod
    async def _body(response, maximum: int) -> bytes:
        output = bytearray()
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise ExportError('export_encoding')
        async for chunk in response.aiter_raw(chunk_size=limit('transfer_chunk_bytes')):
            if len(output) + len(chunk) > maximum:
                raise ExportError('export_response_limit')
            output.extend(chunk)
        return bytes(output)

    async def _request(self, client, method, url, **kwargs) -> bytes:
        async with client.stream(method, url, headers=self._headers(url), **kwargs) as response:
            if response.status_code in (401, 403) or response.is_redirect:
                raise ExportError('overleaf_authentication')
            if not response.is_success:
                raise ExportError('overleaf_http')
            return await self._body(response, limit('mcp_export_response_bytes'))

    def output_url(self, raw: str, project_id: str, build_id: str, name: str,
                   clsi_server_id: str | None) -> str:
        if not isinstance(raw, str) or len(raw) > 4096:
            raise ExportError('export_output_identity')
        url = urljoin(self.base_url + '/', raw)
        parts = urlsplit(url)
        path = (rf'/project/{re.escape(project_id)}(?:/user/[0-9a-f]{{24}})?'
                rf'/build/{re.escape(build_id)}/output/{re.escape(name)}')
        if _origin(url) not in self.output_origins or not re.fullmatch(path, parts.path):
            raise ExportError('export_output_identity')
        query = [(key, value) for key, value in parse_qsl(parts.query)
                 if key != 'clsiserverid']
        if clsi_server_id:
            query.append(('clsiserverid', clsi_server_id))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))

    async def compile(self, project_id: str, write_artifact) -> tuple[str, str]:
        async with self.client_factory() as client:
            page_url = self.base_url + '/project/' + project_id
            parser = _CsrfParser()
            parser.feed((await self._request(client, 'GET', page_url)).decode('utf-8', 'replace'))
            if not parser.token or len(parser.token) > 1024:
                raise ExportError('overleaf_authentication')
            async with client.stream('POST', page_url + '/compile', headers={
                    **self._headers(page_url), 'x-csrf-token': parser.token}, json={
                        'check': 'silent', 'draft': False, 'incrementalCompilesEnabled': True,
                        'rootDocId': None, 'stopOnFirstError': False}) as response:
                if response.status_code in (401, 403):
                    raise ExportError('overleaf_authentication')
                if not response.is_success:
                    raise ExportError('overleaf_compile_http')
                raw = await self._body(response, limit('mcp_export_response_bytes'))
            try:
                info = json.loads(raw)
            except (ValueError, RecursionError) as exc:
                raise ExportError('overleaf_compile_result') from exc
            if not isinstance(info, dict) or info.get('status') not in ('success', 'failure'):
                raise ExportError('overleaf_compile_result')
            rows = info.get('outputFiles')
            if not isinstance(rows, list) or not 0 < len(rows) <= limit('mcp_export_output_files'):
                raise ExportError('overleaf_compile_outputs')
            clsi = info.get('clsiServerId')
            if clsi is not None and (not isinstance(clsi, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', clsi)):
                raise ExportError('overleaf_compile_result')
            selected = {}
            build_id = None
            for row in rows:
                if not isinstance(row, dict):
                    raise ExportError('overleaf_compile_outputs')
                name = row.get('path')
                if name not in OUTPUT_NAMES:
                    continue
                current_build = row.get('build')
                if (name in selected or not isinstance(current_build, str)
                        or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', current_build)
                        or (build_id is not None and current_build != build_id)):
                    raise ExportError('export_output_identity')
                build_id = current_build
                selected[name] = self.output_url(row.get('url'), project_id, build_id, name, clsi)
            status = 'success' if info['status'] == 'success' else 'failed'
            if not build_id or (status == 'success' and 'output.pdf' not in selected):
                raise ExportError('overleaf_compile_outputs')
            # A failed compile may return a previous PDF. Never export it.
            if status != 'success':
                selected.pop('output.pdf', None)
            if not selected:
                raise ExportError('overleaf_compile_outputs')
            for name, url in selected.items():
                for redirect in range(4):
                    async with client.stream('GET', url, headers=self._headers(url)) as response:
                        if response.is_redirect:
                            if redirect == 3:
                                raise ExportError('export_redirect_limit')
                            url = self.output_url(urljoin(url, response.headers.get('location', '')),
                                                  project_id, build_id, name, clsi)
                            continue
                        if not response.is_success:
                            raise ExportError('overleaf_artifact_http')
                        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                            raise ExportError('export_encoding')
                        await write_artifact(OUTPUT_NAMES[name], response.aiter_raw(
                            chunk_size=limit('transfer_chunk_bytes')))
                        break
            return build_id, status
