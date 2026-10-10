"""Bounded native PPTX package QA, independent of Office installation.

Entry point: inspect_pptx_package. Dependencies: stdlib and lazy defusedxml.
Checks ZIP integrity, XML, relationship targets, slide order, shape identity,
editable-object counts and native chart axes before publication. No external
relationship is fetched. Actual Office rendering remains separate evidence.
"""

from __future__ import annotations

import os
import posixpath
import zipfile

_MAX_PACKAGE_BYTES = 256 * 1024 * 1024
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024
_MAX_XML_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_XML_BYTES = 32 * 1024 * 1024
_MAX_PART_BYTES = 128 * 1024 * 1024
_MAX_PARTS = 10_000
_P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_C = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
_CT = '{http://schemas.openxmlformats.org/package/2006/content-types}'


def inspect_pptx_package(path: str, *, expected_pages: int | None = None) -> dict:
    """Return a finite verdict; malformed or incomplete packages cannot pass."""
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException

    result = {'ok': False, 'bytes': 0, 'slides': 0, 'parts': 0,
              'editableTextBoxes': 0, 'nativeTables': 0, 'nativeCharts': 0,
              'findings': []}
    try:
        result['bytes'] = os.path.getsize(path)
        if not 0 < result['bytes'] <= _MAX_PACKAGE_BYTES:
            raise ValueError('package byte limit exceeded or empty')
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_PARTS:
                raise ValueError('package part count exceeds limit')
            names = {entry.filename for entry in entries}
            if len(names) != len(entries):
                raise ValueError('package contains duplicate part names')
            unpacked = 0
            xml_bytes = 0
            for entry in entries:
                name = entry.filename
                if (name.startswith('/') or '\\' in name
                        or posixpath.normpath(name) != name or name.startswith('../')):
                    raise ValueError(f'unsafe package part: {name}')
                cap = _MAX_XML_BYTES if name.endswith(('.xml', '.rels')) else _MAX_PART_BYTES
                if name.endswith(('.xml', '.rels')):
                    xml_bytes += entry.file_size
                    if xml_bytes > _MAX_TOTAL_XML_BYTES:
                        raise ValueError('package XML exceeds aggregate byte limit')
                if entry.file_size > cap:
                    raise ValueError(f'package part exceeds byte limit: {name}')
                unpacked += entry.file_size
                if unpacked > _MAX_UNPACKED_BYTES:
                    raise ValueError('unpacked package exceeds byte limit')
            result['parts'] = len(entries)
            for required in ('[Content_Types].xml', '_rels/.rels', 'ppt/presentation.xml',
                             'ppt/_rels/presentation.xml.rels'):
                if required not in names:
                    raise ValueError(f'missing package part: {required}')
            broken = archive.testzip()
            if broken:
                raise ValueError(f'CRC failed: {broken}')
            documents = {name: ET.fromstring(archive.read(name)) for name in names
                         if name.endswith(('.xml', '.rels'))}
            defaults = {child.get('Extension') for child in documents['[Content_Types].xml']
                        if child.tag == _CT + 'Default'}
            overrides = {str(child.get('PartName') or '').lstrip('/')
                         for child in documents['[Content_Types].xml']
                         if child.tag == _CT + 'Override'}
            for name in names - {'[Content_Types].xml'}:
                if name not in overrides and name.rsplit('.', 1)[-1] not in defaults:
                    raise ValueError(f'part has no declared content type: {name}')
            for name in overrides:
                if name not in names:
                    raise ValueError(f'content type points to missing part: {name}')
            relationships = {}
            for name, document in documents.items():
                if not name.endswith('.rels'):
                    continue
                if name == '_rels/.rels':
                    base, source = '', ''
                else:
                    directory, filename = posixpath.split(name)
                    if posixpath.basename(directory) != '_rels':
                        raise ValueError(f'invalid relationship part: {name}')
                    base = posixpath.dirname(directory)
                    source = posixpath.join(base, filename[:-5])
                    if source not in names:
                        raise ValueError(f'relationship source missing: {source}')
                links = {}
                for rel in document.findall(_REL + 'Relationship'):
                    identity, target = rel.get('Id'), rel.get('Target')
                    if not identity or not target or identity in links:
                        raise ValueError(f'invalid or duplicate relationship: {name}')
                    external = rel.get('TargetMode') == 'External'
                    resolved = (target if external else posixpath.normpath(
                        target.lstrip('/') if target.startswith('/')
                        else posixpath.join(base, target)))
                    if not external and resolved not in names:
                        raise ValueError(f'{name}: missing relationship target {resolved}')
                    links[identity] = (resolved, external)
                relationships[source] = links
            for name, document in documents.items():
                if name.endswith('.rels'):
                    continue
                links = relationships.get(name, {})
                for element in document.iter():
                    for key, value in element.attrib.items():
                        if key.startswith(_R) and value not in links:
                            raise ValueError(f'{name}: unresolved relationship {value}')
            presentation = documents['ppt/presentation.xml']
            slide_ids = presentation.findall(_P + 'sldIdLst/' + _P + 'sldId')
            links = relationships['ppt/presentation.xml']
            ordered = []
            ids = set()
            for slide in slide_ids:
                identity = slide.get('id')
                if not identity or identity in ids:
                    raise ValueError('duplicate or absent presentation slide id')
                ids.add(identity)
                target, external = links[slide.get(_R + 'id')]
                if external or target not in documents or documents[target].tag != _P + 'sld':
                    raise ValueError('presentation slide relationship is not a slide')
                ordered.append(target)
            if not ordered or len(set(ordered)) != len(ordered):
                raise ValueError('presentation has no slides or duplicate slide references')
            if expected_pages is not None and len(ordered) != expected_pages:
                raise ValueError(f'expected {expected_pages} slides, found {len(ordered)}')
            result['slides'] = len(ordered)
            for name in ordered:
                root = documents[name]
                shape_ids = [node.get('id') for node in root.iter(_P + 'cNvPr')]
                if None in shape_ids or len(set(shape_ids)) != len(shape_ids):
                    raise ValueError(f'{name}: duplicate or absent shape id')
                order = [child.tag for child in root]
                allowed = [_P + tag for tag in ('cSld', 'clrMapOvr', 'transition', 'timing', 'extLst')]
                ranks = [allowed.index(tag) for tag in order if tag in allowed]
                if not order or order[0] != _P + 'cSld' or ranks != sorted(set(ranks)):
                    raise ValueError(f'{name}: invalid slide child order')
                result['editableTextBoxes'] += len(root.findall('.//' + _P + 'sp/' + _P + 'txBody'))
                result['nativeTables'] += len(root.findall('.//' + _A + 'tbl'))
                result['nativeCharts'] += len(root.findall('.//' + _C + 'chart'))
            for name, document in documents.items():
                if document.tag != _C + 'chartSpace':
                    continue
                for plot in document.iter(_C + 'plotArea'):
                    axes = {axis.find(_C + 'axId').get('val') for axis in plot
                            if axis.tag in {_C + t for t in ('catAx', 'valAx', 'dateAx', 'serAx')}
                            and axis.find(_C + 'axId') is not None}
                    for child in plot:
                        for axis in child.findall(_C + 'axId'):
                            if axis.get('val') not in axes:
                                raise ValueError(f'{name}: chart references undeclared axis {axis.get("val")}')
                        for cross in child.findall(_C + 'crossAx'):
                            if cross.get('val') not in axes:
                                raise ValueError(f'{name}: chart references missing cross axis')
        result['ok'] = True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError,
            DefusedXmlException, RuntimeError) as exc:
        result['findings'] = [str(exc)[:1000]]
    return result
