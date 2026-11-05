"""Search-owned rule reads and deferred repair effects.

search_knowledge_scope captures an explicit task principal and browser decision.
Bound providers carry that scope into search workers; drift callbacks only record
observations. Only the admitted handler's successful exit runs repairs, borrowing
its live cancellation/account ports. Speculative reads never receive that port.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import threading
from urllib.parse import urlsplit

from lib.identity import PrincipalContext
from lib.log import get_logger
from lib.site_knowledge.repository import SiteKnowledgeRepository

logger = get_logger(__name__)
_current = ContextVar('site_knowledge_scope', default=None)


def current_knowledge_scope():
    return _current.get()


@contextmanager
def bind_knowledge_scope(scope):
    """Borrow a captured scope; only its creating handler may settle it."""
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


class SearchKnowledgeScope:
    def __init__(self, task, binding, *, repair=False, emit=None):
        from lib.tasks_pkg.manager import task_user_id
        owner = task_user_id(task)
        principal = PrincipalContext.from_payload(task['_principalContext'])
        if str(owner) != binding.user_id or not binding.client_id:
            raise ValueError('Site knowledge requires the exact task browser')
        self.repository = SiteKnowledgeRepository(principal,
            client_id=binding.client_id, profile=binding.profile)
        self.task = task
        self.principal_payload = deepcopy(task['_principalContext'])
        self.budget = task.get('_executionBudget')
        self.binding = binding
        self.emit = emit or (lambda event: None)
        self.repair = repair
        self._observations = {}
        self._lock = threading.Lock()
        self._closed = False
        self.observed_drift = False

    def matches_browser(self, user_id, client_id, profile):
        return (user_id, client_id, profile) == tuple(self.binding)

    def observe(self, site, url, evidence, browser):
        from .catalog import SITE_BRIEFS
        host = urlsplit(url).hostname or ''
        if (site not in SITE_BRIEFS
                or not (host == site or host.endswith('.' + site))
                or evidence.get('transport') == 'native'):
            return
        with self._lock:
            if not self._closed:
                self.observed_drift = True
                self._observations.setdefault((site, url), (deepcopy(evidence), browser))

    def close(self, *, complete=False):
        with self._lock:
            self._closed = True
            observations, self._observations = self._observations, {}
        if not complete or not self.repair:
            return
        from .trigger import repair_observation
        for (site, url), (evidence, browser) in observations.items():
            repair_observation(self, browser, site, url, evidence)


@contextmanager
def search_knowledge_scope(task, binding, *, repair=False, emit=None):
    scope = None
    if binding.client_id and task.get('_principalContext'):
        scope = SearchKnowledgeScope(task, binding, repair=repair, emit=emit)
    with bind_knowledge_scope(scope):
        complete = False
        try:
            yield scope
            complete = True
        finally:
            if scope is not None:
                scope.close(complete=complete)
