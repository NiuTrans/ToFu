"""Python re-implementation of Claude Code's Edit tool (claude-code 2.1.207 linux-x64,
binary sha256 85e7e988a392..., strings dump /tmp/cc-7eab8b27-.../build/claude-strings.txt).

Ported from the minified bundle (function names as minified in that build):
  oTu   input aliases: replace_name->replace_all, path->file_path, old_str->old_string,
        new_str->new_string
  CJn   zod strictObject {file_path, old_string, new_string, replace_all?=false}
  validateInput  (errorCode 1/3/4/8/9 texts; CRLF read as LF)
  Mrt   findActualString: exact -> curly-quote normalized (BTu) -> \\uXXXX decoded
        (h7i) -> non-ASCII as \\uXXXX regex (FTu)
  qYt   re-applies curly quote style to new_string when the match was normalized
  jTu   re-applies \\uXXXX escapes to new_string when the match was escape-normalized
  WTu   new_string=="" and old_string lacks a trailing "\\n" and file contains
        old+"\\n" -> delete old+"\\n" (the line break goes too)
  g7i   "String not found in file. Failed to apply edit." /
        "Original and edited file match exactly. Failed to apply edit."
  result "The file {path} has been updated successfully. (file state is current in
        your context — no need to Read it back)"
Not modelled (harness setup, not edit semantics): read-before-edit state (the file
is shown to the model in the prompt), permissions, notebook/size/encoding checks.
"""
from __future__ import annotations

import re

LSQ, RSQ, LDQ, RDQ = '\u2018', '\u2019', '\u201c', '\u201d'
_ESC = re.compile(r'\\u[0-9a-fA-F]{4}')
_NON_ASCII = re.compile('[\u0080-\uffff]')
SUFFIX = ' (file state is current in your context \u2014 no need to Read it back)'
KEYS = {'file_path', 'old_string', 'new_string', 'replace_all'}


def _btu(s):
    return s.replace(LSQ, "'").replace(RSQ, "'").replace(LDQ, '"').replace(RDQ, '"')


def _h7i(s):
    return re.sub(r'(\\\\)|\\u([0-9a-fA-F]{4})',
                  lambda m: m.group(0) if m.group(1) is not None else chr(int(m.group(2), 16)), s)


def _ftu(s):
    out = []
    for ch in s:
        code = ord(ch)
        if code >= 128:
            out.append('\\\\u' + ''.join(f'[{c}{c.upper()}]' if c >= 'a' else c
                                         for c in f'{code:04x}'))
        else:
            out.append(re.escape(ch))
    return ''.join(out)


def find_actual_string(content, old):
    if old in content:
        return old
    normalized = _btu(old)
    index = _btu(content).find(normalized)
    if index != -1:
        return content[index:index + len(old)]
    if _ESC.search(old):
        decoded = _h7i(old)
        if decoded != old and decoded in content:
            return decoded
    if _NON_ASCII.search(old):
        m = re.search(_ftu(old), content)
        if m:
            return m.group(0)
    return None


def _opening(chars, i):
    if i == 0:
        return True
    return chars[i - 1] in (' ', '\t', '\n', '\r', '(', '[', '{', '\u2014', '\u2013')


def _curly_double(s):
    chars = list(s)
    return ''.join((LDQ if _opening(chars, i) else RDQ) if c == '"' else c for i, c in enumerate(chars))


def _curly_single(s):
    chars = list(s)
    out = []
    for i, c in enumerate(chars):
        if c != "'":
            out.append(c)
            continue
        prev = chars[i - 1] if i > 0 else None
        nxt = chars[i + 1] if i < len(chars) - 1 else None
        if prev is not None and prev.isalpha() and nxt is not None and nxt.isalpha():
            out.append(RSQ)
        else:
            out.append(LSQ if _opening(chars, i) else RSQ)
    return ''.join(out)


def _qyt(old, actual, new):
    if old == actual:
        return new
    dq = LDQ in actual or RDQ in actual
    sq = LSQ in actual or RSQ in actual
    if not dq and not sq:
        return new
    if dq:
        new = _curly_double(new)
    if sq:
        new = _curly_single(new)
    return new


def _jtu(old, actual, new):
    if old == actual:
        return new
    if _NON_ASCII.search(old) and re.fullmatch(_ftu(old), actual):
        mapping, upper, lower, a = {}, 0, 0, 0
        for ch in old:
            if ord(ch) >= 128:
                hexd = actual[a + 2:a + 6]
                mapping[ord(ch)] = hexd
                upper += sum(1 for c in hexd if 'A' <= c <= 'F')
                lower += sum(1 for c in hexd if 'a' <= c <= 'f')
                a += 6
            else:
                a += 1

        def esc(m):
            code = ord(m.group(0))
            if code in mapping:
                return '\\u' + mapping[code]
            h = f'{code:04x}'
            return '\\u' + (h.upper() if upper > lower else h)
        return _NON_ASCII.sub(esc, new)
    if _ESC.search(old) and _h7i(old) == actual:
        return _h7i(new)
    return new


def _wtu(content, old, new, replace_all):
    def sub(text, a, b):
        return text.replace(a, b) if replace_all else text.replace(a, b, 1)
    if new != '':
        return sub(content, old, new)
    if not old.endswith('\n') and (old + '\n') in content:
        return sub(content, old + '\n', new)
    return sub(content, old, new)


def repair_input(args):
    t = dict(args)
    if 'replace_name' in t:
        n = t.pop('replace_name')
        if 'replace_all' not in t:
            t['replace_all'] = n is True or n == 'true'
    if 'path' in t and 'file_path' not in t and isinstance(t['path'], str):
        t['file_path'] = t.pop('path')
    if 'old_str' in t and 'old_string' not in t and isinstance(t['old_str'], str):
        t['old_string'] = t.pop('old_str')
    if 'new_str' in t and 'new_string' not in t and isinstance(t['new_str'], str):
        t['new_string'] = t.pop('new_str')
    return t


def validate_schema(args):
    """zod strictObject: required strings, optional boolean, no unknown keys."""
    if not isinstance(args, dict):
        return 'InputValidationError: expected object'
    problems = []
    for key in ('file_path', 'old_string', 'new_string'):
        if key not in args:
            problems.append(f'The required parameter `{key}` is missing')
        elif not isinstance(args[key], str):
            problems.append(f'The parameter `{key}` type is expected as `string`')
    if 'replace_all' in args and not isinstance(args['replace_all'], bool):
        problems.append('The parameter `replace_all` type is expected as `boolean`')
    extra = sorted(set(args) - KEYS)
    if extra:
        problems.append('An unexpected parameter `' + '`, `'.join(extra) + '` was provided')
    if problems:
        return 'InputValidationError: Edit failed due to the following issues:\n' + '\n'.join(problems)
    return None


def cc_edit(files, args, resolve):
    """Apply one Edit call. `files` maps canonical path -> text (mutated on success).

    Returns (ok, model_text, error_kind)."""
    if isinstance(args, dict):
        args = repair_input(args)
    schema_error = validate_schema(args)
    if schema_error:
        return False, schema_error, 'schema'
    path, old, new = args['file_path'], args['old_string'], args['new_string']
    replace_all = bool(args.get('replace_all', False))
    if old == new:
        return False, 'No changes to make: old_string and new_string are exactly the same.', 'noop'
    key = resolve(path)
    if key is None or key not in files:
        if old == '':
            return False, 'File creation is outside this benchmark.', 'path'
        return False, 'File does not exist. Current working directory: /workspace.', 'path'
    content = files[key].replace('\r\n', '\n')
    if old == '':
        if content.strip() != '':
            return False, 'Cannot create new file - file already exists.', 'exists'
    actual = find_actual_string(content, old)
    if actual is None:
        note = ''
        if _ESC.search(old) or _NON_ASCII.search(old):
            note = ('\n(note: Edit also tried swapping \\uXXXX escapes and their characters; neither '
                    'form matched, so the mismatch is likely elsewhere in old_string. Re-read the file '
                    'and copy the exact surrounding text.)')
        return False, f'String to replace not found in file.\nString: {old}{note}', 'not_found'
    count = len(content.split(actual)) - 1
    if count > 1 and not replace_all:
        return False, (f'Found {count} matches of the string to replace, but replace_all is false. '
                       'To replace all occurrences, set replace_all to true. To replace only one '
                       'occurrence, please provide more context to uniquely identify the instance.\n'
                       f'String: {old}'), 'ambiguous'
    new_actual = _jtu(old, actual, _qyt(old, actual, new))
    updated = _wtu(content, actual, new_actual, replace_all) if old != '' else new_actual
    if updated == content:
        return False, 'Original and edited file match exactly. Failed to apply edit.', 'noop'
    files[key] = updated
    if replace_all:
        return True, f'The file {path} has been updated. All occurrences were successfully replaced.{SUFFIX}', None
    return True, f'The file {path} has been updated successfully.{SUFFIX}', None
