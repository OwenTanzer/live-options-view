"""Strategy implementations.

Importing this package registers every strategy in `strategy.REGISTRY`.

The P1 smoke strategy, `reddit_sentiment_qqq`, and `trump_whisperer_qqq`
live here. The six dip/rip/cheap-ATM strategies are P3 work: the MOO-24
contract explicitly defers rolling out every strategy before one complete
loop is demonstrably alive.
"""

from . import reddit_sentiment  # noqa: F401
from . import smoke  # noqa: F401
from . import trump_whisperer  # noqa: F401

__all__ = ["smoke", "reddit_sentiment", "trump_whisperer"]
