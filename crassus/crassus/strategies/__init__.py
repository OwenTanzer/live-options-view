"""Strategy implementations.

Importing this package registers every strategy in `strategy.REGISTRY`.
"""

from . import gex  # noqa: F401
from . import iv_skew  # noqa: F401
from . import macro_cross_market  # noqa: F401
from . import momentum_qqq  # noqa: F401
from . import reddit_sentiment  # noqa: F401
from . import smoke  # noqa: F401
from . import stat_arb_qqq_smh  # noqa: F401
from . import trump_whisperer  # noqa: F401
from . import vix_term_structure  # noqa: F401
from . import vwap_breakout  # noqa: F401
from . import vwap_fade  # noqa: F401

__all__ = ["gex", "iv_skew", "macro_cross_market", "momentum_qqq", "reddit_sentiment", "smoke", "stat_arb_qqq_smh", "trump_whisperer", "vix_term_structure", "vwap_breakout", "vwap_fade"]
