"""Task specifications for the forced-operation edit benchmark (60 tasks).

Line numbers are 1-based in the pinned source file. Every multi-site task's
line numbers refer to the ORIGINAL file. `stratum` is declared here and
re-derived by build_tasks.py, which refuses a mismatch.

Site kinds:
  ins      position=after|before, line, text (one or more whole new lines, '\n'-terminated)
  rep_span line, old, new          (old occurs exactly once in that line)
  rep_lines start, end, text       (replace whole lines start..end with text)
  del_span line, old               (delete a fragment inside one line)
  del_lines start, end             (delete whole lines start..end)
"""

REPOS = {
    'requests': ('https://github.com/psf/requests', '3c7baaa05ab4aa49cc819ce3a819d260de19c157'),
    'click': ('https://github.com/pallets/click', 'ac0e297ec2d7d78455a7d18ce306996e668c4a85'),
    'express': ('https://github.com/expressjs/express', '43fca52d28a422c8be48fd489cbd4f2d6152bab6'),
    'cobra': ('https://github.com/spf13/cobra', 'adbc8813901bba65827259daa8e22ff94ec1f30e'),
    'log': ('https://github.com/rust-lang/log', '75fcb86da1cce3b9476c58c911482ec43ec235f4'),
    'ky': ('https://github.com/sindresorhus/ky', '3541888878c4d303a8bc2ad61815f4e048f55a5d'),
}

FILES = {
    'A': ('requests', 'src/requests/auth.py'),
    'F': ('click', 'src/click/formatting.py'),
    'V': ('express', 'lib/view.js'),
    'G': ('cobra', 'flag_groups.go'),
    'K': ('log', 'src/kv/key.rs'),
    'T': ('ky', 'source/utils/body.ts'),
}


def ins(position, line, text):
    return {'kind': 'ins', 'position': position, 'line': line, 'text': text}


def rep_span(line, old, new):
    return {'kind': 'rep_span', 'line': line, 'old': old, 'new': new}


def rep_lines(start, end, text):
    return {'kind': 'rep_lines', 'start': start, 'end': end, 'text': text}


def del_span(line, old):
    return {'kind': 'del_span', 'line': line, 'old': old}


def del_lines(start, end):
    return {'kind': 'del_lines', 'start': start, 'end': end}


# (task_id, file_key, edit_type, declared_stratum, [sites])
TASKS = [
    # ---------------- requests/src/requests/auth.py (Python, 4-space) ----------------
    ('A01', 'A', 'insert', 'S', [ins('after', 10, 'import hmac\n')]),
    ('A02', 'A', 'insert', 'S', [ins('before', 15, 'import uuid\n')]),
    ('A03', 'A', 'insert', 'M', [ins('after', 112, '        r.headers.setdefault("X-Auth-Scheme", "basic")\n')]),
    ('A04', 'A', 'insert', 'L', [ins('before', 343, '        self._thread_local.registered = True\n')]),
    ('A05', 'A', 'replace', 'S', [rep_span(170, 'MD5', 'SHA-256')]),
    ('A06', 'A', 'replace', 'M', [rep_lines(82, 82, '        raise NotImplementedError("Auth hooks must implement __call__().")\n')]),
    ('A07', 'A', 'delete', 'M', [del_lines(281, 281)]),
    ('A08', 'A', 'delete', 'L', [del_lines(334, 337)]),
    ('A09', 'A', 'multi', 'S', [ins('before', 12, 'import logging\n'),
                                rep_span(300, 'r.content', '_ = r.content')]),
    ('A10', 'A', 'multi', 'L', [ins('before', 283, '            self._thread_local.chal = {}\n'),
                                rep_lines(312, 312, '            _r = r.connection.send(prep, **kwargs)  # resend with the digest header\n')]),
    # ---------------- click/src/click/formatting.py (Python, 4-space) ----------------
    ('F01', 'F', 'insert', 'S', [ins('after', 137, '            import os\n')]),
    ('F02', 'F', 'insert', 'S', [ins('before', 254, '                # Nothing to describe: end the row here.\n')]),
    ('F03', 'F', 'insert', 'M', [ins('after', 244, '        if not rows:\n            return\n')]),
    ('F04', 'F', 'insert', 'L', [ins('before', 271, '                # No wrapped text: just terminate the row.\n')]),
    ('F05', 'F', 'replace', 'S', [rep_span(135, '80', '100')]),
    ('F06', 'F', 'replace', 'M', [rep_lines(167, 167, '            prefix = f"{_(\'Usage:\')} "\n')]),
    ('F07', 'F', 'delete', 'M', [del_lines(10, 10)]),
    ('F08', 'F', 'delete', 'L', [del_lines(173, 175)]),
    ('F09', 'F', 'multi', 'S', [ins('after', 66, '        break_long_words=False,\n'),
                                rep_span(232, '30', '40')]),
    ('F10', 'F', 'multi', 'L', [ins('before', 286, '            self.write_paragraph()\n'),
                                rep_lines(299, 299, '        return "".join(self.buffer).rstrip("\\n") + "\\n"\n')]),
    # ---------------- express/lib/view.js (JavaScript, 2-space) ----------------
    ('V01', 'V', 'insert', 'S', [ins('before', 200, '  if (!path) return undefined;\n')]),
    ('V02', 'V', 'insert', 'M', [ins('after', 58, '  this.cache = opts.cache || false;\n')]),
    ('V03', 'V', 'insert', 'L', [ins('before', 120, '    debug(\'tried "%s"\', loc);\n')]),
    ('V04', 'V', 'insert', 'L', [ins('after', 186, '  debug(\'miss "%s"\', path);\n')]),
    ('V05', 'V', 'replace', 'M', [rep_lines(84, 84, '      throw new Error(\'Module "\' + mod + \'" does not provide a view engine (missing __express export).\')\n')]),
    ('V06', 'V', 'replace', 'L', [rep_lines(145, 150, '    var args = Array.prototype.slice.call(arguments);\n    var cntx = this;\n')]),
    ('V07', 'V', 'delete', 'S', [del_span(17, 'node:')]),
    ('V08', 'V', 'delete', 'L', [del_lines(189, 195)]),
    ('V09', 'V', 'multi', 'S', [ins('after', 2, ' * view.js: template lookup and rendering\n'),
                                del_span(18, 'node:')]),
    ('V10', 'V', 'multi', 'M', [ins('after', 108, '  var start = Date.now();\n'),
                                rep_lines(136, 136, '  debug(\'render "%s" with %d option keys\', this.path, Object.keys(options).length);\n')]),
    # ---------------- cobra/flag_groups.go (Go, tabs) ----------------
    ('G01', 'G', 'insert', 'S', [ins('before', 18, '\t"errors"\n')]),
    ('G02', 'G', 'insert', 'S', [ins('after', 20, '\t"sync"\n')]),
    ('G03', 'G', 'insert', 'M', [ins('after', 122, '\tif len(groupInfo) == 0 {\n\t\treturn\n\t}\n')]),
    ('G04', 'G', 'insert', 'L', [ins('before', 108, '\tif err := validateCustomFlagGroups(flags); err != nil {\n\t\treturn err\n\t}\n')]),
    ('G05', 'G', 'replace', 'S', [rep_span(130, 'continue', 'break')]),
    ('G06', 'G', 'replace', 'L', [rep_lines(94, 96, (
        '\t\tfor annotation, status := range map[string]map[string]map[string]bool{\n'
        '\t\t\trequiredAsGroupAnnotation:   groupStatus,\n'
        '\t\t\toneRequiredAnnotation:       oneRequiredGroupStatus,\n'
        '\t\t\tmutuallyExclusiveAnnotation: mutuallyExclusiveGroupStatus,\n'
        '\t\t} {\n'
        '\t\t\tprocessFlagForGroupAnnotation(flags, pflag, annotation, status)\n'
        '\t\t}\n'))]),
    ('G07', 'G', 'delete', 'M', [del_lines(72, 72)]),
    ('G08', 'G', 'delete', 'S', [del_span(22, 'flag ')]),
    ('G09', 'G', 'multi', 'M', [ins('after', 113, '\t\t// A missing flag means the group cannot be satisfied.\n'),
                                rep_lines(118, 118, '\treturn len(flagnames) > 0\n')]),
    ('G10', 'G', 'multi', 'L', [ins('before', 106, '\t\t// More than one mutually exclusive flag was set.\n'),
                                del_lines(128, 128)]),
    # ---------------- log/src/kv/key.rs (Rust, 4-space) ----------------
    ('K01', 'K', 'insert', 'M', [ins('after', 5, 'use std::hash::{Hash, Hasher};\n')]),
    ('K02', 'K', 'insert', 'M', [ins('before', 86, (
        "impl<'k> Default for Key<'k> {\n"
        '    fn default() -> Self {\n'
        '        Key::from_str_static("")\n'
        '    }\n'
        '}\n'
        '\n'))]),
    ('K03', 'K', 'insert', 'L', [ins('after', 175, (
        '\n'
        '    #[test]\n'
        '    fn key_display() {\n'
        '        assert_eq!("a key", Key::from_str("a key").to_string());\n'
        '    }\n'))]),
    ('K04', 'K', 'insert', 'L', [ins('before', 94, '        // Same representation as Display.\n')]),
    ('K05', 'K', 'replace', 'M', [rep_lines(44, 44, '    #[allow(clippy::should_implement_trait)] // Kept for API compatibility.\n')]),
    ('K06', 'K', 'replace', 'L', [rep_lines(79, 82, (
        '        if let MaybeStaticStr::Static(s) = self.key {\n'
        '            Some(s)\n'
        '        } else {\n'
        '            None\n'
        '        }\n'))]),
    ('K07', 'K', 'delete', 'S', [del_span(8, 'pub ')]),
    ('K08', 'K', 'delete', 'L', [del_lines(71, 73)]),
    ('K09', 'K', 'multi', 'S', [del_span(52, 'pub '), del_span(78, 'pub ')]),
    ('K10', 'K', 'multi', 'M', [ins('after', 5, 'use std::hash::Hash;\n'),
                                rep_lines(74, 74, '        Some(self.key.get()).filter(|s| !s.is_empty())\n')]),
    # ---------------- ky/source/utils/body.ts (TypeScript, tabs) ----------------
    ('T01', 'T', 'insert', 'S', [ins('before', 121, '\t\t\t\t// Not a body method on this runtime.\n')]),
    ('T02', 'T', 'insert', 'M', [ins('after', 5, 'const decoder = new TextDecoder();\n')]),
    ('T03', 'T', 'insert', 'M', [ins('before', 22, '// `content-length` may be missing or invalid; both count as 0.\n')]),
    ('T04', 'T', 'insert', 'L', [ins('after', 32, '\n\tif (body instanceof ReadableStream) {\n\t\treturn 0;\n\t}\n')]),
    ('T05', 'T', 'replace', 'S', [rep_span(235, 'half', 'full')]),
    ('T06', 'T', 'replace', 'L', [rep_lines(219, 226, (
        "\tif (!request.body || request.bodyUsed || request.body.locked || request.keepalive"
        " || request.mode === 'no-cors' || !(request instanceof globalThis.Request)) {\n"))]),
    ('T07', 'T', 'delete', 'S', [del_span(18, 'void ')]),
    ('T08', 'T', 'delete', 'M', [del_lines(140, 140)]),
    ('T09', 'T', 'multi', 'M', [ins('after', 168, '\tconst {status} = response;\n'),
                                rep_span(169, 'nullBodyStatuses.has(response.status)', 'nullBodyStatuses.has(status)')]),
    ('T10', 'T', 'multi', 'L', [ins('before', 108, '\t\t\tpreviousChunk = undefined;\n'),
                                rep_lines(89, 89, '\t\t\t\tlet percent = totalBytes > 0 ? transferredBytes / totalBytes : 0;\n')]),
]
