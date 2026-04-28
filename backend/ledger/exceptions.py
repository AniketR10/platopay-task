class LedgerError(Exception):
    """Base for all expected, business-logic errors raised by the ledger layer."""


class InsufficientFunds(LedgerError):
    pass


class IllegalStateTransition(LedgerError):
    pass


class IdempotencyKeyReused(LedgerError):
    pass
