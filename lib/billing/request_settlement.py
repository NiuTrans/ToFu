"""Durable invoices for the shared direct-model request owner.

The existing transport budget port reports physical attempts. Required wallet
admission and terminal persistence run on the owner's joined worker; transport
callbacks retain bounded usage only, with no storage I/O on the event loop.
Cold recovery consumes the frozen invoice, never current prices or estimates.
Missing usage/invoices retain the original hold; they grant no provider retry.
"""
import json
import threading
from functools import lru_cache

from lib.cost import observed_usage_totals, billable_usage_evidence
from lib.storage_authority_sql.generated_billing_contract import (
    MAX_REQUEST_INVOICE_CHARACTERS, REQUEST_ADMISSION_KIND, REQUEST_INVOICE_KIND,
    REQUEST_INVOICE_SCHEMA,
)


class IncompleteRequestUsage(ValueError):
    """Provider spend is unresolved; retain the original reservation."""


def _encoded(value):
    encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    if len(encoded) > MAX_REQUEST_INVOICE_CHARACTERS:
        raise IncompleteRequestUsage('Request invoice exceeds its durable evidence bound')
    return encoded


def _find(client, wallet, request_id, kind):
    return client.query('billing.ledger.find', dict(user_id=wallet, kind=kind,
        ref_type='reserve', ref_id=request_id), deadline=2.0)


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(REQUEST_INVOICE_SCHEMA)


def _decode(note, *, wallet, request_id, owner, held):
    invoice = json.loads(note)
    if not _validator().is_valid(invoice):
        raise IncompleteRequestUsage('Request invoice lacks complete bounded evidence')
    if (invoice['request_id'] != request_id or invoice['owner_user_id'] != owner
            or invoice['wallet_user_id'] != wallet or invoice['reserved_micro'] != held
            or invoice['prepared_attempt_count'] < len(invoice['quotes'])):
        raise ValueError('Request invoice ownership or shape differs')
    total = 0
    for quote in invoice['quotes']:
        if not observed_usage_totals(quote['usage'], require_valid_cache=True)[1]:
            raise ValueError('Request invoice lacks complete physical-attempt evidence')
        total += quote['cost_micro']
    if total != invoice['cost_micro'] or total > 9_223_372_036_854_775_807:
        raise ValueError('Request invoice total differs')
    _encoded(invoice)
    return invoice


def _settle(invoice):
    from lib.billing import settle
    snapshot = settle(invoice['wallet_user_id'], reserved_micro=invoice['reserved_micro'],
        actual_micro=invoice['cost_micro'], ref_id=invoice['request_id'],
        note='Frozen request invoice; see request_invoice ledger entry')
    return {'cost_micro': invoice['cost_micro'], 'reserved_micro': invoice['reserved_micro'],
            'balance_micro': snapshot.balance_micro, 'attempt_count': len(invoice['quotes'])}


def recover(*, wallet_user_id, task_id, held_micro):
    """Return False for legacy requests; protected unknown requests fail closed."""
    from lib.billing.users import get_user
    from lib.billing.reservation_admission import validate_entry
    from lib.storage import get_storage_client
    client = get_storage_client(write=True)
    admission = _find(client, wallet_user_id, task_id, REQUEST_ADMISSION_KIND)
    if admission is None:
        return False
    validate_entry(admission, wallet_user_id, task_id, REQUEST_ADMISSION_KIND)
    entry = _find(client, wallet_user_id, task_id, REQUEST_INVOICE_KIND)
    if entry is None:
        raise IncompleteRequestUsage('Admitted request has no complete durable invoice')
    validate_entry(entry, wallet_user_id, task_id, REQUEST_INVOICE_KIND)
    user = get_user(wallet_user_id)
    if user is None:
        raise ValueError('Request invoice wallet owner is unavailable')
    invoice = _decode(entry['note'], wallet=wallet_user_id, request_id=task_id,
                      owner=user.owner_user_id, held=held_micro)
    _settle(invoice)
    return True


class RequestSettlement:
    """One request's immutable payer and all its physical transport attempts."""
    def __init__(self, *, request_id, owner, wallet, held):
        self._identity = (request_id, owner, wallet, held)
        self._lock = threading.RLock()
        self._admitted = False
        self._closed = False
        self._attempts = []
        self._retained_characters = 0
        self._invoice = None

    request_id = property(lambda self: self._identity[0])
    owner = property(lambda self: self._identity[1])
    wallet = property(lambda self: self._identity[2])
    held = property(lambda self: self._identity[3])

    def protect(self):
        from lib.billing.reservation_admission import acknowledge
        with self._lock:
            if self._closed:
                raise ValueError('Request settlement already closed')
            acknowledge(owner=self.owner, wallet=self.wallet, request_id=self.request_id,
                        held=self.held, kind=REQUEST_ADMISSION_KIND)
            self._admitted = True

    def __call__(self, *, input_tokens, max_output_tokens, minimum_output_tokens=1):
        with self._lock:
            if not self._admitted or self._closed:
                raise IncompleteRequestUsage('Request has no active acknowledged wallet admission')
            # Each retained attempt consumes at least one byte of the fixed
            # invoice budget; unusually long retry chains cannot grow forever.
            if len(self._attempts) >= MAX_REQUEST_INVOICE_CHARACTERS:
                raise IncompleteRequestUsage('Request attempt evidence exhausted')
            lease = _Attempt(self, max_output_tokens)
            self._attempts.append(lease)
            return lease

    def settle(self):
        from lib.billing import compute_request_cost
        from lib.billing.wallet import _stable_id
        from lib.billing.reservation_admission import validate_entry
        from lib.storage import get_storage_client
        from lib.ids import short_id
        with self._lock:
            self._closed = True
            if self._invoice is None:
                quotes = []
                for attempt in self._attempts:
                    if not attempt.settled:
                        raise IncompleteRequestUsage('Physical request has not settled')
                    if not attempt.dispatched:
                        continue
                    if (not attempt.verified_complete or not attempt.model
                            or not observed_usage_totals(attempt.usage, require_valid_cache=True)[1]):
                        raise IncompleteRequestUsage('Physical request usage is unknown or incomplete')
                    cost = compute_request_cost(attempt.model, provider_id=attempt.provider_id or None,
                                                raw_usage=attempt.usage)
                    if cost.snapshot.get('source') == 'default_estimate':
                        raise IncompleteRequestUsage('Actual request price is unavailable')
                    quotes.append(dict(model=attempt.model, provider_id=attempt.provider_id,
                        usage=attempt.usage, cost_micro=cost.micro, snapshot=cost.snapshot))
                candidate = dict(version=1, request_id=self.request_id, owner_user_id=self.owner,
                    wallet_user_id=self.wallet, reserved_micro=self.held, quotes=quotes,
                    prepared_attempt_count=len(self._attempts),
                    cost_micro=sum(q['cost_micro'] for q in quotes))
                # No attempt evidence cannot prove a free provider invocation.
                self._invoice = _decode(_encoded(candidate), wallet=self.wallet,
                    request_id=self.request_id, owner=self.owner, held=self.held)
            invoice = self._invoice
            note = _encoded(invoice)
            client = get_storage_client(write=True)
            entry = _find(client, self.wallet, self.request_id, REQUEST_INVOICE_KIND)
            if entry is None:
                result = client.command('billing.wallet.apply', dict(
                    user_id=self.wallet, amount_micro=0, kind=REQUEST_INVOICE_KIND,
                    ref_type='reserve', ref_id=self.request_id, note=note, allow_negative=True,
                    ledger_id=_stable_id('led_', self.wallet, REQUEST_INVOICE_KIND, 'reserve', self.request_id)),
                    short_id('cmd_'), deadline=5.0)
                entry = result.get('entry') if isinstance(result, dict) else None
            validate_entry(entry, self.wallet, self.request_id, REQUEST_INVOICE_KIND)
            if entry.get('note') != note:
                raise ValueError('Request invoice differs from immutable ledger evidence')
            return _settle(invoice)


class _Attempt:
    def __init__(self, owner, maximum):
        self.owner, self.max_output_tokens = owner, maximum
        self.dispatched = self.settled = False
        self.verified_complete = False
        self.model = self.provider_id = ''
        self.usage = None

    def prepare_input(self, request):
        from lib.llm.request import AdmittedRequest
        if not isinstance(request, AdmittedRequest):
            raise ValueError('Request settlement requires the sealed physical request')
        self.model, self.provider_id = request.logical_model or request.model, request.provider_id

    def mark_dispatched(self):
        with self.owner._lock:
            if self.owner._closed or self.settled or not self.owner._admitted:
                raise IncompleteRequestUsage('Physical request admission is no longer active')
            self.dispatched = True

    def confirm_input(self, *, successful=False):
        with self.owner._lock:
            self.verified_complete = bool(successful and self.dispatched and self.settled)

    def settle(self, usage=None, *, unsent=False):
        with self.owner._lock:
            if self.settled:
                return
            self.settled = True
            if self.dispatched or not unsent:
                self.dispatched = True
                if isinstance(usage, dict):
                    # Internal diagnostics can contain secrets and do not price
                    # tokens. Retain a detached public usage shape only.
                    raw = _encoded(billable_usage_evidence(usage))
                    self.owner._retained_characters += len(raw)
                    if self.owner._retained_characters > MAX_REQUEST_INVOICE_CHARACTERS:
                        raise IncompleteRequestUsage('Request usage evidence exceeds its bound')
                    self.usage = json.loads(raw)
