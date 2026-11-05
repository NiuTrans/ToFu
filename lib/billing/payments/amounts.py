"""Exact payment amounts; no provider IO, configuration reads or storage writes.

Entry points validate decimal rates, convert yuan to cents and cents to credit
micro-units. The wallet's existing credit unit and signed storage range apply.
Model usage pricing remains owned by lib.cost and lib.billing.cost.
"""

from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext

from lib.billing.cost import MICRO_PER_CREDIT

_MAX_PAYMENT_INTEGER = (1 << 63) - 1


def positive_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f'{field} must be a finite positive decimal')
    try:
        number = Decimal(str(value))
    except DecimalException as exc:
        raise ValueError(f'{field} must be a finite positive decimal') from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f'{field} must be a finite positive decimal')
    return number


def _scaled_integer(number: Decimal, multiplier: int, *, field: str,
                    exact: bool = False) -> int:
    # Product precision includes every coefficient digit, independent of a
    # caller's thread-local Decimal context. Never construct huge integer powers.
    precision = len(number.as_tuple().digits) + len(str(multiplier))
    try:
        with localcontext(Context(prec=max(28, precision), rounding=ROUND_HALF_EVEN)):
            scaled = number * multiplier
            integer = scaled.to_integral_value()
            if exact and integer != scaled:
                raise ValueError(f'{field} must contain whole minor units')
            if not 0 < integer <= _MAX_PAYMENT_INTEGER:
                raise ValueError(f'{field} must fit a positive signed 64-bit integer')
            return int(integer)
    except DecimalException as exc:
        raise ValueError(f'{field} is outside the supported payment range') from exc


def credit_micro(amount_minor: int, rate: Decimal) -> int:
    if type(amount_minor) is not int or not 0 < amount_minor <= _MAX_PAYMENT_INTEGER:
        raise ValueError('amount_minor must be a positive signed 64-bit integer')
    return _scaled_integer(rate, amount_minor * MICRO_PER_CREDIT, field='credit_micro')


def yuan_to_minor(value: object) -> int:
    return _scaled_integer(positive_decimal(value, field='amount_yuan'), 100,
                           field='amount_minor', exact=True)
