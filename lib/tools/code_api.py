"""Bounded, request-local tool API documentation for code-first experiments.

Entry: compile_code_api_action. Input is the existing wire catalog, never a
registry lookup. Declarations help author JavaScript; the complete argument
schemas and normal task gateway remain the execution authority. Types derive from producer schemas. Reference caches are local to one projection;
no worker, persistent cache, tool aliases or execution path lives here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from urllib.parse import unquote


MAX_CODE_API_TOKENS = 1200
MAX_CODE_API_BYTES = 32768
MAX_CODE_API_METHODS = 64
MAX_CODE_API_INDEX_SUMMARY_CHARS = 120


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


class _TypeProjectionLimit(Exception):
    """A convenience type exceeded the existing metadata size boundary."""


def _type(schema, depth=0):
    """Render bounded convenience types; exact constraints stay in the schema.

    Local JSON-pointer references never fetch another schema. The per-projection
    target cache and node/text budgets prevent alias graphs from amplifying work.
    """
    root, targets = schema, {}
    remaining = {"nodes": MAX_CODE_API_BYTES, "chars": MAX_CODE_API_BYTES}

    def local_target(reference):
        if reference in targets:
            return targets[reference]
        target = None
        try:
            fragment = unquote(reference[1:], errors="strict")
            if not reference.startswith("#") or (fragment and not fragment.startswith("/")):
                raise ValueError("nonlocal reference")
            target = root
            for encoded in fragment[1:].split("/") if fragment else []:
                if re.search(r"~(?![01])", encoded):
                    raise ValueError("invalid pointer escape")
                key = encoded.replace("~1", "/").replace("~0", "~")
                if isinstance(target, dict):
                    target = target[key]
                elif isinstance(target, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                    target = target[int(key)]
                else:
                    raise ValueError("invalid pointer target")
            if not isinstance(target, dict):
                target = None
        except (KeyError, IndexError, ValueError, UnicodeError):
            target = None
        targets[reference] = target
        return target

    def charge(text):
        remaining["chars"] -= len(text)  # _json makes all rendered text ASCII.
        if remaining["chars"] < 0:
            raise _TypeProjectionLimit()
        return text

    def render(value, level, active=frozenset()):
        remaining["nodes"] -= 1
        if remaining["nodes"] < 0:
            raise _TypeProjectionLimit()
        if level >= 8 or not isinstance(value, dict):
            return charge("unknown")
        if "$ref" in value:
            reference = value["$ref"]
            if not isinstance(reference, str) or reference in active:
                return charge("unknown")
            target = local_target(reference)
            if target is None:
                return charge("unknown")
            resolved = render(target, level + 1, active | {reference})
            siblings = {key: part for key, part in value.items() if key != "$ref"}
            if any(key in siblings for key in ("type", "enum", "anyOf", "oneOf", "allOf")):
                resolved = "(" + resolved + ") & (" + render(siblings, level + 1, active) + ")"
            return charge(resolved)
        for keyword, separator in (("anyOf", " | "), ("oneOf", " | "), ("allOf", " & ")):
            alternatives = value.get(keyword)
            if isinstance(alternatives, list) and alternatives:
                if not all(isinstance(part, dict) for part in alternatives):
                    return charge("unknown")
                rendered = [render(part, level + 1, active) for part in alternatives]
                if keyword != "allOf" and "unknown" in rendered:
                    return charge("unknown")
                if keyword == "allOf":
                    rendered = ["(" + part + ")" for part in rendered]
                return charge("(" + separator.join(rendered) + ")")
        enum = value.get("enum")
        if isinstance(enum, list) and enum and all(
                part is None or type(part) in (str, int, float, bool) for part in enum):
            return charge(" | ".join(_json(part) for part in enum))
        kind = value.get("type")
        if isinstance(kind, list):
            return charge(" | ".join(render({**value, "type": part}, level + 1, active) for part in kind))
        if kind == "object":
            properties = value.get("properties")
            if not isinstance(properties, dict):
                return charge("Record<string, unknown>")
            required = value.get("required") or []
            fields = [_json(name) + ("" if name in required else "?") + ": " +
                      render(part, level + 1, active) for name, part in sorted(properties.items())]
            if value.get("additionalProperties", True) is not False:
                fields.append("[key: string]: unknown")
            return charge("{" + "; ".join(fields) + "}")
        if kind == "array":
            return charge("Array<" + render(value.get("items"), level + 1, active) + ">")
        return charge({"string": "string", "integer": "number | bigint", "number": "number",
                       "boolean": "boolean", "null": "null"}.get(kind, "unknown"))

    try:
        return render(schema, depth)
    except _TypeProjectionLimit:
        return "unknown"


def _compile_complete_code_api_action(catalog, action, *, count_tokens, excluded_names):
    """Return a complete bounded action or an explicit additive fallback.

    Full schemas are retained beside signatures, including refs/combinators
    and numeric constraints that convenience types cannot express. Hosted
    tools, ambiguous identities or excessive metadata keep the original wire.
    """
    methods = []
    seen = set()
    size = 0
    try:
        for tool in catalog or []:
            if not isinstance(tool, dict) or tool.get('type') != 'function':
                return None, {'reason': 'code_api_non_function_catalog'}
            function = tool.get('function')
            if not isinstance(function, dict):
                return None, {'reason': 'code_api_invalid_catalog'}
            name = function.get('name')
            if name in excluded_names:
                continue
            if not isinstance(name, str) or not name or name in seen:
                return None, {'reason': 'code_api_ambiguous_catalog'}
            seen.add(name)
            parameters = function.get('parameters')
            if not isinstance(parameters, dict):
                return None, {'reason': 'code_api_invalid_schema'}
            descriptor = {'name': name, 'description': function.get('description') or '',
                          'parameters': parameters}
            if tool.get('decoded_result_schema') is not None:
                from lib.tools.contracts import clone_decoded_result_schema
                descriptor['decoded_result_schema'] = clone_decoded_result_schema(
                    tool['decoded_result_schema'])
            encoded = _json(descriptor)
            size += len(encoded.encode('utf-8'))
            if size > MAX_CODE_API_BYTES or len(methods) >= MAX_CODE_API_METHODS:
                return None, {'reason': 'code_api_metadata_bound'}
            methods.append((name, parameters, encoded))
        if not methods:
            return None, {'reason': 'code_api_empty_catalog'}
        lines = ['Tool API: call these exact names from JavaScript using tools.invoke.',
                 'Signatures document successful decoded values, not executable TypeScript. '
                 'Undeclared outputs are unknown; inspect structure in code or return '
                 'keys/types or a small sample. Declared schemas do not establish '
                 'completeness or freshness. Exact JSON schemas follow.',
                 'interface ToolAPI {']
        groups = {}
        for name, parameters, descriptor in sorted(methods):
            definition = json.loads(descriptor)
            key = _json({key: value for key, value in definition.items() if key != 'name'})
            group = groups.setdefault(key, {'names': [], 'definition': definition})
            group['names'].append(name)
        for group in groups.values():
            definition = group['definition']
            group_names = group['names']
            if len(group_names) > 1:
                definition = {key: value for key, value in definition.items() if key != 'name'}
                definition['names'] = group_names
            lines += ['// ' + _json(definition),
                      'invoke(name: ' + ' | '.join(_json(name) for name in group_names) +
                      ', args: ' + _type(definition['parameters']) + '): Promise<' +
                      _type(definition.get('decoded_result_schema')) + '>;']
        lines.append('}')
        documentation = '\n'.join(lines)
        result = copy.deepcopy(action)
        result['function']['description'] += '\n' + documentation
        tokens = count_tokens([result])
        if tokens > MAX_CODE_API_TOKENS:
            return None, {'reason': 'code_api_token_bound', 'projectedTokens': tokens}
        return result, {'reason': 'code_api_compiled', 'schemaTokens': tokens,
                        'methodNames': sorted(seen), 'schemaGroupCount': len(groups),
                        'apiSha256': hashlib.sha256(documentation.encode()).hexdigest()}
    except (TypeError, ValueError, RecursionError):
        return None, {'reason': 'code_api_invalid_schema'}


def compile_code_api_action(catalog, action, *, count_tokens, excluded_names,
                            discovery_result_chars):
    """Document small catalogs completely; discover larger catalogs on demand.

    Discovery uses the existing task-owned catalog RPC, not a second registry.
    Unsupported identities, schemas or hosted tools still keep direct exposure.
    """
    result, proof = _compile_complete_code_api_action(
        catalog, action, count_tokens=count_tokens, excluded_names=excluded_names)
    if result is not None or proof['reason'] not in {
            'code_api_metadata_bound', 'code_api_token_bound'}:
        return result, proof
    seen = set()
    index = []
    try:
        for tool in catalog or []:
            if not isinstance(tool, dict) or tool.get('type') != 'function':
                return None, {'reason': 'code_api_non_function_catalog'}
            function = tool.get('function')
            if not isinstance(function, dict):
                return None, {'reason': 'code_api_invalid_catalog'}
            name = function.get('name')
            if not isinstance(name, str) or not name or name in seen:
                return None, {'reason': 'code_api_ambiguous_catalog'}
            if name in excluded_names:
                continue
            seen.add(name)
            parameters = function.get('parameters')
            if not isinstance(parameters, dict):
                return None, {'reason': 'code_api_invalid_schema'}
            # Every exact lookup must fit the existing whole-item search page.
            descriptor = {'name': name, 'arguments_schema': parameters}
            if tool.get('decoded_result_schema') is not None:
                from lib.tools.contracts import clone_decoded_result_schema
                descriptor['decoded_result_schema'] = clone_decoded_result_schema(
                    tool['decoded_result_schema'])
            descriptor = _json(descriptor)
            if len(descriptor) > discovery_result_chars - 2048:
                return None, {'reason': 'code_api_discovery_schema_bound'}
            summary = function.get('description')
            index.append({'name': name, 'summary': summary[:MAX_CODE_API_INDEX_SUMMARY_CHARS]
                          if isinstance(summary, str) else ''})
        documentation = (
            'Tool API discovery: argument and declared decoded-result schemas are retrieved on demand. '
            'catalog.search({query:"purpose",limit:4}) returns items with exact '
            'name, summary and argument-schema hints. Query an item\'s full name '
            'to retrieve its complete arguments_schema and optional decoded_result_schema before using '
            'tools.invoke(name,args). Search describes tool capabilities, not source data. '
            'Search performs no tool actions. When the method and arguments are '
            'clear, retrieve its schema and invoke it in the same program. '
            'Return focused discovery or tools.observe(reason,data) when model '
            'judgment is needed; then continue with a new program. Branches and '
            'pagination can depend on actual tool results, not a predeclared plan.')
        result = copy.deepcopy(action)
        base_description = result['function']['description'] + '\n' + documentation
        # A deterministic prefix is an index, never a claim of complete authority.
        # JSON escaping keeps descriptions and unusual names on one data line.
        entries = []
        index_bytes = 0
        for row in sorted(index, key=lambda item: item['name']):
            encoded = _json(row)
            index_bytes += len(encoded.encode('utf-8'))
            if index_bytes > MAX_CODE_API_BYTES:
                break
            entries.append(encoded)

        def render_index(count):
            result['function']['description'] = (
                base_description + '\nMethod index (' + str(count) + '/' + str(len(seen)) +
                '; names and brief summaries only; unlisted methods remain searchable):\n[' +
                ','.join(entries[:count]) + ']')
            return count_tokens([result])

        tokens = render_index(0)
        if tokens > MAX_CODE_API_TOKENS:
            return None, {'reason': 'code_api_token_bound', 'projectedTokens': tokens}
        # Tokenize logarithmically rather than once for every catalog member.
        lower, upper = 0, len(entries)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if render_index(middle) <= MAX_CODE_API_TOKENS:
                lower = middle
            else:
                upper = middle - 1
        tokens = render_index(lower)
        documentation = result['function']['description'][len(action['function']['description']) + 1:]
        return result, {'reason': 'code_api_discovery', 'schemaTokens': tokens,
                        'methodCount': len(seen), 'completeCatalogDocumented': False,
                        'indexMethodCount': lower, 'completeMethodIndex': lower == len(seen),
                        'exactSchemaDiscovery': True,
                        'apiSha256': hashlib.sha256(documentation.encode()).hexdigest()}
    except (TypeError, ValueError, RecursionError):
        return None, {'reason': 'code_api_invalid_schema'}
