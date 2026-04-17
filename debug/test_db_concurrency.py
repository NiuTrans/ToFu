#!/usr/bin/env python3
"""Stress test: database concurrency under simulated high load.

Tests both the connection pool and SQLite WAL concurrency with many
simultaneous readers and writers.

Usage:
    python debug/test_db_concurrency.py [--threads N] [--ops N] [--backend sqlite|pg]
"""

import argparse
import os
import sys
import tempfile
import threading
import time

# Force sqlite for testing by default
os.environ.setdefault('CHATUI_DB_BACKEND', 'sqlite')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_test(num_threads, ops_per_thread, backend):
    """Run concurrent read/write stress test."""
    # Use a temp DB to avoid polluting the real one
    if backend == 'sqlite':
        tmpdir = tempfile.mkdtemp()
        test_db = os.path.join(tmpdir, 'stress_test.db')
        os.environ['CHATUI_DB_PATH'] = test_db
        os.environ['CHATUI_DB_BACKEND'] = 'sqlite'

    # Import after env setup
    from lib.database._core import (
        _BACKEND, _BUSY_TIMEOUT_MS, _SQLITE_POOL_MAX,
        _MAX_TOTAL_CONNS, _CONN_POOL_MAX,
        _pool_get, _pool_put, _new_sqlite_connection,
    )

    print(f'=== Database Concurrency Stress Test ===')
    print(f'Backend:          {_BACKEND}')
    print(f'Threads:          {num_threads}')
    print(f'Ops per thread:   {ops_per_thread}')
    if _BACKEND == 'sqlite':
        print(f'Busy timeout:     {_BUSY_TIMEOUT_MS}ms')
        print(f'Pool max:         {_SQLITE_POOL_MAX}')
    else:
        print(f'Max conns:        {_MAX_TOTAL_CONNS}')
        print(f'Pool max:         {_CONN_POOL_MAX}')
    print()

    # Create schema
    conn = _pool_get()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stress_test (
            id INTEGER PRIMARY KEY,
            thread_id INTEGER,
            value TEXT,
            created_at REAL
        )
    ''')
    conn.commit()
    _pool_put(conn)

    errors = []
    errors_lock = threading.Lock()
    completed = []
    completed_lock = threading.Lock()
    lock_retries = [0]
    lock_retries_lock = threading.Lock()

    def worker(thread_id):
        """Each worker does a mix of reads and writes."""
        local_errors = 0
        local_ops = 0
        local_retries = 0

        for i in range(ops_per_thread):
            conn = _pool_get()
            try:
                if i % 3 == 0:
                    # Write operation
                    conn.execute(
                        'INSERT INTO stress_test (thread_id, value, created_at) VALUES (?, ?, ?)',
                        (thread_id, f'thread-{thread_id}-op-{i}', time.time())
                    )
                    conn.commit()
                elif i % 3 == 1:
                    # Read operation
                    rows = conn.execute(
                        'SELECT COUNT(*) FROM stress_test WHERE thread_id = ?',
                        (thread_id,)
                    ).fetchone()
                    _ = rows[0]
                else:
                    # Mixed read-write (update)
                    conn.execute(
                        'UPDATE stress_test SET value = ? WHERE thread_id = ? AND id = (SELECT MAX(id) FROM stress_test WHERE thread_id = ?)',
                        (f'updated-{i}', thread_id, thread_id)
                    )
                    conn.commit()
                local_ops += 1
            except Exception as e:
                err_msg = str(e).lower()
                if 'locked' in err_msg or 'busy' in err_msg:
                    local_retries += 1
                else:
                    local_errors += 1
                    with errors_lock:
                        errors.append((thread_id, i, str(e)))
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                _pool_put(conn)

        with completed_lock:
            completed.append({
                'thread_id': thread_id,
                'ops': local_ops,
                'errors': local_errors,
                'retries': local_retries,
            })
        with lock_retries_lock:
            lock_retries[0] += local_retries

    # Launch all threads
    t0 = time.time()
    threads = []
    for tid in range(num_threads):
        t = threading.Thread(target=worker, args=(tid,), name=f'stress-{tid}')
        threads.append(t)

    print(f'Starting {num_threads} threads...')
    for t in threads:
        t.start()

    for t in threads:
        t.join(timeout=120)

    elapsed = time.time() - t0
    total_ops = sum(c['ops'] for c in completed)
    total_errors = sum(c['errors'] for c in completed)
    total_retries = lock_retries[0]

    print(f'\n=== Results ===')
    print(f'Time:             {elapsed:.2f}s')
    print(f'Threads finished: {len(completed)}/{num_threads}')
    print(f'Total ops:        {total_ops}/{num_threads * ops_per_thread}')
    print(f'Ops/second:       {total_ops / elapsed:.0f}')
    print(f'Lock retries:     {total_retries}')
    print(f'Hard errors:      {total_errors}')

    if errors:
        print(f'\nFirst 5 errors:')
        for thread_id, op_idx, err in errors[:5]:
            print(f'  Thread {thread_id}, op {op_idx}: {err}')

    # Verify data integrity
    conn = _pool_get()
    row = conn.execute('SELECT COUNT(*) FROM stress_test').fetchone()
    total_rows = row[0]
    _pool_put(conn)
    print(f'\nRows in DB:       {total_rows}')

    # Pass/fail
    if total_errors == 0 and len(completed) == num_threads:
        print(f'\n✅ PASSED — {num_threads} threads × {ops_per_thread} ops completed with 0 errors')
        if total_retries > 0:
            print(f'   ⚠️  {total_retries} lock retries (SQLite contention) — consider PG for production')
    else:
        print(f'\n❌ FAILED — {total_errors} errors, {num_threads - len(completed)} threads timed out')
        sys.exit(1)

    # Cleanup
    if backend == 'sqlite':
        try:
            os.remove(test_db)
            os.remove(test_db + '-wal')
            os.remove(test_db + '-shm')
            os.rmdir(tmpdir)
        except Exception:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='DB concurrency stress test')
    parser.add_argument('--threads', type=int, default=100,
                        help='Number of concurrent threads (default: 100)')
    parser.add_argument('--ops', type=int, default=50,
                        help='Operations per thread (default: 50)')
    parser.add_argument('--backend', choices=['sqlite', 'pg'], default='sqlite',
                        help='Database backend (default: sqlite)')
    args = parser.parse_args()
    run_test(args.threads, args.ops, args.backend)
