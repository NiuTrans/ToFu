"""Pure, bounded shell-source scanning shared by command analysis owners.

Entry points (all private to ``lib.project_mod``): ``_scan`` and
``_substitution_end`` delimit quotes, substitutions and continuations;
``_LOOP_HEAD``, ``_literal_loop_values`` and ``_substitute_loop_reference``
bind a bounded literal ``for`` loop's plain values. Consumers are
``git_command_scope``, ``command_heredoc``, ``command_network_probe``,
``download_intent`` and ``package_network``. Nothing here rewrites executed
source or grants authority.
"""
import re
import shlex

_REFERENCE = re.compile(r'\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})')
_MAX_LOOP_CANDIDATES = 8
_LOOP_HEAD = re.compile(r'\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in(?:\s+(.*))?\Z', re.S)
# A loop value is bound verbatim, so it must stay one plain shell word: no
# whitespace (unquoted use would word-split), no quote/metacharacter that the
# in-place substitution below could not reproduce. A comma is data to the
# shell except inside a brace pattern (see _substitute_loop_reference).
_PLAIN_LOOP_VALUE = re.compile(r'[A-Za-z0-9_./:@%+=~,-]+\Z')


def _ordinary_loop_variable(name):
    # Shell-maintained variables (including _) cannot retain the loop binding
    # across predicates/printf. Uppercase/special setup is not a probe variable.
    return (name != '_' and not name.isupper()
            and not name.startswith(('BASH_', 'GIT_', 'ZSH_')))


def _substitution_end(source, start):
    depth, quote, index = 1, '', start + 2
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif not quote:
            if char == '#' and (source[index - 1].isspace()
                                or source[index - 1] in ';&|()'):
                newline = source.find('\n', index)
                if newline < 0:
                    break  # a comment cannot close the substitution
                index = newline + 1
                continue
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return index + 1
        index += 1
    raise ValueError('Unclosed substitution')


def _drop_continuations(text):
    """Remove shell line continuations outside single quotes.

    `\\`+newline joins physical lines wherever the backslash is live, so the
    joined text is what the shell parses. Inside single quotes the backslash is
    literal and is kept, which also keeps the word content exact.
    """
    if '\\' not in text:
        return text
    output, quote, index = [], '', 0
    while index < len(text):
        char = text[index]
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif char == '\\' and quote != "'" and text[index + 1:index + 2] in ('\n', '\r'):
            index += 3 if text[index + 1:index + 3] == '\r\n' else 2
            continue
        output.append(char)
        index += 1
    return ''.join(output)


def _scan(source, *, words=False):
    """Return raw spans and separators, preserving quote/substitution context.

    A backslash-newline continuation is not a command boundary: the physical
    lines are joined before the shell tokenizes them, so the span carries the
    joined text. A trailing lone backslash stays opaque.
    """
    result, start, index, quote = [], 0, 0, ''
    while index < len(source):
        char = source[index]
        if char == '\\' and quote != "'":
            if index + 1 == len(source):
                raise ValueError('Unsupported trailing escape')
            if source[index + 1] in '\r\n':
                index += 3 if source[index + 1:index + 3] == '\r\n' else 2
                continue
            index += 2
            continue
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
        elif quote != "'" and source.startswith('$(', index):
            index = _substitution_end(source, index)
            continue
        elif not quote:
            separator = ''
            if words and char.isspace():
                separator = char
            elif (not words and char == '&' and index > start
                  and source[index - 1] in '<>'
                  and source[index + 1:index + 2] in tuple('0123456789-')):
                # `2>&1`/`>&2` duplicate a descriptor; they are not a pipeline
                # or detach boundary, and splitting there would erase a binding
                # proven before the redirect.
                index += 1
                continue
            elif not words and char in ';\n|&':
                separator = source[index:index + 2] if source[index:index + 2] in ('&&', '||') else char
            elif char in '()\r#':
                raise ValueError('Unsupported shell grouping/comment')
            if separator:
                result.append((_drop_continuations(source[start:index]), separator))
                index += len(separator)
                start = index
                continue
        index += 1
    if quote:
        raise ValueError('Unclosed quote')
    result.append((_drop_continuations(source[start:]), ''))
    return result


def _literal_loop_values(raw):
    """Decoded plain-word iterable of one loop, else None.

    Only statically known single words qualify. Expansion or word splitting
    cannot be reproduced verbatim. An unmatched POSIX glob remains one literal
    loop value, while matches have filesystem-dependent values/cardinality;
    neither is a concrete list that this pure unroller may guess.
    """
    words = [word for word, _ in _scan(raw, words=True) if word.strip()]
    if not words or len(words) > _MAX_LOOP_CANDIDATES:
        return None
    values = []
    for word in words:
        quote, index = '', 0
        while index < len(word):
            character = word[index]
            if character == '\\' and quote != "'":
                index += 2
                continue
            if character in "'\"" and (not quote or quote == character):
                quote = '' if quote else character
            elif quote != "'" and character in '$`':
                return None
            elif not quote and (character in '*?[<>|&;(){}#'
                                or character == '~' and index == 0):
                return None
            index += 1
        if quote:
            return None
        try:
            decoded = shlex.split(word, comments=False)
        except ValueError:
            return None
        if len(decoded) != 1 or _PLAIN_LOOP_VALUE.fullmatch(decoded[0]) is None:
            return None
        values.append(decoded[0])
    return values


def _substitute_loop_reference(token, variable, value):
    """Replace live ``$variable`` references in one token; None if unprovable.

    Single-quoted text and escaped dollars are literal to the shell and stay
    untouched. Any other reference (``$@``, another name) makes the loop
    opaque rather than silently binding a guessed value. A value with a comma
    never enters a token with an unquoted brace: bash expands braces before
    it substitutes the value and dash never does, so the text ``{a,b}`` would
    show admission an expansion that no shell performs.
    """
    output, quote, index = [], '', 0
    braces = substituted = False
    while index < len(token):
        character = token[index]
        if character == '\\' and quote != "'":
            output.append(token[index:index + 2])
            index += 2
            continue
        if character in "'\"" and (not quote or quote == character):
            quote = '' if quote else character
        elif quote != "'" and character == '`':
            return None
        elif quote != "'" and token.startswith('$(', index) and not token.startswith('$((', index):
            try:
                end = _substitution_end(token, index)
            except ValueError:
                return None
            body = _substitute_loop_body(token[index + 2:end - 1], variable, value)
            if body is None:
                return None
            output.append(f'$({body})')
            index, substituted = end, True
            continue
        elif quote != "'" and character == '$':
            reference = _REFERENCE.match(token, index)
            if reference is None or (reference.group(1) or reference.group(2)) != variable:
                return None
            output.append(value)
            index, substituted = reference.end(), True
            continue
        elif not quote and character in '{}':
            braces = True
        output.append(character)
        index += 1
    if quote or braces and substituted and ',' in value:
        return None
    return ''.join(output)


def _substitute_loop_body(body, variable, value):
    """A ``$(…)`` body with the loop variable bound, else None.

    Only a proven literal Git inspection chain (Git reads piped into
    head/tail/wc/cat/cut/tr) qualifies once bound: it cannot rebind the
    variable, and unlike ``$(touch victim)`` it cannot replace an executable
    that the unrolled view names by path.
    """
    from .command_analysis import _lease_free_git_command, _split_pipeline, command_segment_words
    substituted = _substitute_loop_reference(body, variable, value)
    if substituted is None or _lease_free_git_command(substituted) is None:
        return None
    for segment in _split_pipeline(substituted):
        head = command_segment_words(segment)[0]
        if head != 'git' and head not in {'head', 'tail', 'wc', 'cat', 'cut', 'tr'}:
            return None  # an opaque runtime consumer may still change the filesystem
    return substituted


