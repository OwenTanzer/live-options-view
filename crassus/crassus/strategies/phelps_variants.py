"""Guideline-Phelps variants of the four currently-deployed strategies.

Each variant is exactly its base strategy's entry/signal logic, run through
`phelps.phelps_wrap` (see that module for the full argument): a proposed
close of a held position is deferred until the position has stood for one
Phelps window (`ctx.params["phelps_minutes"]`, default
`phelps.PHELPS_MINUTES_DEFAULT`), then released if the base strategy still
wants to close it. Nothing about entries, sizing, or signal computation
changes -- these are stress-test twins of the live roster (`smoke_atm_roundtrip`,
`reddit_sentiment_qqq`, `trump_whisperer_qqq`, `momentum_qqq`), not new
strategies, and are meant to run against the same accounts.example.json
`params` each base bot already uses (e.g. Luigi/Jesus/Doris's differing
`reddit_sentiment_qqq` thresholds) so a side-by-side comparison isolates the
effect of the hold-time floor and nothing else.

`oi_skew_qqq`, `max_pain_qqq`, and `put_call_ratio_qqq` are implemented but
not currently running as live accounts (see `crassus/README.md`'s account
table), so they have no Phelps twin here yet either -- add one the same way
if/when they're deployed.
"""

from __future__ import annotations

from ..phelps import phelps_wrap
from .momentum_qqq import STRATEGY_VERSION as _MOMENTUM_VERSION
from .momentum_qqq import momentum_qqq as _momentum_qqq
from .reddit_sentiment import STRATEGY_VERSION as _REDDIT_VERSION
from .reddit_sentiment import reddit_sentiment_qqq as _reddit_sentiment_qqq
from .smoke import STRATEGY_VERSION as _SMOKE_VERSION
from .smoke import smoke_atm_roundtrip as _smoke_atm_roundtrip
from .trump_whisperer import STRATEGY_VERSION as _TRUMP_VERSION
from .trump_whisperer import trump_whisperer_qqq as _trump_whisperer_qqq
from ..strategy import register

smoke_atm_roundtrip_phelps = register(
    phelps_wrap(
        _smoke_atm_roundtrip,
        strategy_id="smoke_atm_roundtrip_phelps",
        strategy_version=_SMOKE_VERSION,
    )
)

reddit_sentiment_qqq_phelps = register(
    phelps_wrap(
        _reddit_sentiment_qqq,
        strategy_id="reddit_sentiment_qqq_phelps",
        strategy_version=_REDDIT_VERSION,
    )
)

trump_whisperer_qqq_phelps = register(
    phelps_wrap(
        _trump_whisperer_qqq,
        strategy_id="trump_whisperer_qqq_phelps",
        strategy_version=_TRUMP_VERSION,
    )
)

momentum_qqq_phelps = register(
    phelps_wrap(
        _momentum_qqq,
        strategy_id="momentum_qqq_phelps",
        strategy_version=_MOMENTUM_VERSION,
    )
)
