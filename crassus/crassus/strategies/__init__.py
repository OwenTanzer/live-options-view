"""Strategy implementations.

Importing this package registers every strategy in `strategy.REGISTRY`.

Only the P1 smoke strategy lives here so far. The six dip/rip/cheap-ATM
strategies are P3 work: the MOO-24 contract explicitly defers rolling out
every strategy before one complete loop is demonstrably alive.
"""

from . import smoke  # noqa: F401

__all__ = ["smoke"]
