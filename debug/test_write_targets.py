"""Test suite for _extract_write_targets and _filter_changes_by_targets."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.project_mod.tools import (
    _extract_write_targets, _is_destructive_command, _filter_changes_by_targets,
)

passed = 0
failed = 0

def check(label, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print(f'  ✅ {label}')
    else:
        failed += 1
        print(f'  ❌ {label}' + (f' — {detail}' if detail else ''))


print('═══ _extract_write_targets ═══')

# ── Read-only commands → empty set ──
t = _extract_write_targets('cat logs/postgresql.log 2>/dev/null | grep -i error | tail -40')
check('cat|grep|tail → empty', t == set(), f'got {t}')

t = _extract_write_targets('grep -r TODO src/')
check('grep → empty', t == set(), f'got {t}')

t = _extract_write_targets('ls -la')
check('ls → empty', t == set(), f'got {t}')

t = _extract_write_targets('find . -name "*.py" | wc -l')
check('find|wc → empty', t == set(), f'got {t}')

t = _extract_write_targets('git status')
check('git status → empty', t == set(), f'got {t}')

t = _extract_write_targets('git log --oneline')
check('git log → empty', t == set(), f'got {t}')

t = _extract_write_targets('head -100 data.csv | sort | uniq -c')
check('head|sort|uniq → empty', t == set(), f'got {t}')

t = _extract_write_targets('diff file1.py file2.py')
check('diff → empty', t == set(), f'got {t}')

# ── Redirect targets → specific files ──
t = _extract_write_targets('echo hello > output.txt')
check('echo > output.txt', 'output.txt' in (t or set()), f'got {t}')

t = _extract_write_targets('cat a.txt >> log.txt')
check('cat >> log.txt', 'log.txt' in (t or set()), f'got {t}')

t = _extract_write_targets('sort data.csv > sorted.csv 2>/dev/null')
check('sort > sorted.csv (with 2>/dev/null)', 
      t is not None and 'sorted.csv' in t and '/dev/null' not in str(t), f'got {t}')

t = _extract_write_targets('echo err 2> errors.log')
check('2> errors.log', t is not None and 'errors.log' in t, f'got {t}')

# ── rm → specific targets ──
t = _extract_write_targets('rm file1.txt file2.txt')
check('rm file1 file2', t is not None and 'file1.txt' in t and 'file2.txt' in t, f'got {t}')

t = _extract_write_targets('rm -rf build/')
check('rm -rf build/', t is not None and 'build/' in t, f'got {t}')

# ── cp → destination only ──
t = _extract_write_targets('cp src.txt dst.txt')
check('cp src dst → dst only', t is not None and 'dst.txt' in t and 'src.txt' not in t, f'got {t}')

# ── mv → both source and destination ──
t = _extract_write_targets('mv old.txt new.txt')
check('mv old new → both', t is not None and 'new.txt' in t and 'old.txt' in t, f'got {t}')

# ── touch → all args ──
t = _extract_write_targets('touch new_file.py')
check('touch new_file.py', t is not None and 'new_file.py' in t, f'got {t}')

# ── chmod → specific files ──
t = _extract_write_targets('chmod +x script.sh')
check('chmod +x script.sh', t is not None and 'script.sh' in t, f'got {t}')

# ── sed -i → file targets ──
t = _extract_write_targets("sed -i 's/old/new/g' file1.py file2.py")
check('sed -i → file1 file2', t is not None and 'file1.py' in t and 'file2.py' in t, f'got {t}')

# ── Opaque commands → None ──
check('python3 → None', _extract_write_targets('python3 script.py') is None)
check('make → None', _extract_write_targets('make build') is None)
check('npm → None', _extract_write_targets('npm install') is None)
check('cargo → None', _extract_write_targets('cargo build') is None)
check('bash script → None', _extract_write_targets('bash deploy.sh') is None)

# ── Mixed pipeline with opaque → None ──
check('grep|python3 → None', _extract_write_targets('grep foo bar.txt | python3 process.py') is None)

# ── git destructive → None ──
check('git checkout → None', _extract_write_targets('git checkout main') is None)
check('git reset → None', _extract_write_targets('git reset --hard') is None)

# ── The original problem case ──
cmd = 'cat logs/postgresql.log 2>/dev/null | grep -i "drop\\|delete\\|fatal\\|error\\|shutdown\\|corrupt\\|recover" | tail -40'
t = _extract_write_targets(cmd)
check('Original problem → empty set', t == set(), f'got {t}')
check('Original problem → NOT destructive', not _is_destructive_command(cmd))

# ── Complex: read-only + redirect ──
t = _extract_write_targets('grep -r TODO src/ | sort > todo_list.txt')
check('grep|sort > file → {file}', t is not None and 'todo_list.txt' in t, f'got {t}')

# ── Chained commands ──
t = _extract_write_targets('rm old.txt && touch new.txt')
check('rm && touch → both targets', 
      t is not None and 'old.txt' in t and 'new.txt' in t, f'got {t}')

# ── Quoted args with special chars should not break splitting ──
t = _extract_write_targets('grep -i "error\\|warning\\|fatal" app.log | tail -20')
check('grep with \\| in quotes → empty', t == set(), f'got {t}')

t = _extract_write_targets("grep 'foo|bar' file.txt | wc -l")
check("grep with | in single quotes → empty", t == set(), f'got {t}')

# ── sed without -i is read-only ──
t = _extract_write_targets("sed 's/foo/bar/g' input.txt")
check('sed without -i → empty (filter)', t == set(), f'got {t}')
check('sed without -i → NOT destructive', 
      not _is_destructive_command("sed 's/foo/bar/g' input.txt"))

# ── sed -i IS destructive ──
check('sed -i → destructive', 
      _is_destructive_command("sed -i 's/foo/bar/' file.py"))

# ── Redirect to /dev/null should not pollute targets ──
t = _extract_write_targets('ls 2>/dev/null')
check('ls 2>/dev/null → empty', t == set(), f'got {t}')

t = _extract_write_targets('echo test > /dev/null')
check('echo > /dev/null → empty', t == set(), f'got {t}')

# ── Multiple redirects ──
t = _extract_write_targets('process 2>/dev/null > output.txt')
check('2>/dev/null > output.txt → {output.txt}', 
      t is None or 'output.txt' in t, f'got {t}')

# ── tee writes to specific files ──
# tee is not in _READONLY_COMMANDS, so it's opaque → None
t = _extract_write_targets('echo hello | tee output.txt')
check('tee → None (opaque)', t is None, f'got {t}')


print()
print('═══ _filter_changes_by_targets ═══')

changes = [
    {'rel_path': 'logs/postgresql.log', 'change_type': 'modified'},
    {'rel_path': 'logs/app.log', 'change_type': 'modified'},
    {'rel_path': 'src/main.py', 'change_type': 'modified'},
    {'rel_path': 'build/output.js', 'change_type': 'created'},
    {'rel_path': 'old_file.txt', 'change_type': 'deleted'},
]

# Specific targets
f = _filter_changes_by_targets(changes, {'src/main.py', 'old_file.txt'}, '/tmp')
check('specific targets → 2 matches', len(f) == 2 and 
      {c['rel_path'] for c in f} == {'src/main.py', 'old_file.txt'}, f'got {len(f)}')

# None (opaque) → keep all
f = _filter_changes_by_targets(changes, None, '/tmp')
check('None → keep all', len(f) == 5)

# Empty set → keep none
f = _filter_changes_by_targets(changes, set(), '/tmp')
check('empty set → keep none', len(f) == 0)

# Directory prefix: 'build/' should match 'build/output.js'
f = _filter_changes_by_targets(changes, {'build/'}, '/tmp')
check('build/ → build/output.js', 
      any(c['rel_path'] == 'build/output.js' for c in f), f'got {[c["rel_path"] for c in f]}')

# Directory without slash: 'logs' should match children
f = _filter_changes_by_targets(changes, {'logs'}, '/tmp')
paths = {c['rel_path'] for c in f}
check('logs → matches logs/*', 
      'logs/postgresql.log' in paths and 'logs/app.log' in paths, f'got {paths}')

# Exact match only — unrelated files excluded
f = _filter_changes_by_targets(changes, {'src/main.py'}, '/tmp')
check('src/main.py → only that file', 
      len(f) == 1 and f[0]['rel_path'] == 'src/main.py', f'got {[c["rel_path"] for c in f]}')


print()
print(f'Results: {passed} passed, {failed} failed')
if failed:
    sys.exit(1)
else:
    print('✅ ALL TESTS PASSED')
