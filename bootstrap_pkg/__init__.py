"""bootstrap_pkg — stdlib-only launcher internals (split from bootstrap.py).

Keep __init__ EMPTY of heavy imports: facade bootstrap.py controls
import-time ordering (env re-exec BEFORE anything else).
"""
