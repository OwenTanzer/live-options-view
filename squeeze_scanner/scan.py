"""CLI entrypoint: finviz candidates -> Tradier options overlay -> ranked table.

    python -m squeeze_scanner.scan [--limit N] [--out out.csv]

Prints a ranked table to stdout and, if --out is given, writes it as CSV.
Deliberately synchronous and sequential (one Tradier chain fetch per
candidate, in a loop) rather than threaded/async: candidate counts are
small (tens, not thousands) since finviz's own short-float filter already
narrows the universe, and this runs as an on-demand scan, not a hot loop
like collector.py -- see docs/plans/2026-09-short-squeeze-scanner.md for why
this doesn't share collector.py's polling infrastructure.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time

import requests
from dotenv import load_dotenv

from squeeze_scanner.finviz_client import fetch_candidates
from squeeze_scanner.scoring import compute_composite_score
from squeeze_scanner.tradier_options import (
    TradierConfigurationError,
    TradierDataError,
    TradierRateLimitError,
    get_nearest_expiration,
    get_option_chain,
    get_quote,
    summarize_chain,
    to_options_inputs,
    validate_number,
)

# Distinct outcomes for a candidate's options lookup, per PR #99
# review finding 5: a real Tradier/network failure must not be silently
# indistinguishable from "this name genuinely has no usable options
# market" -- both used to fall through to the same factor-only score with
# no way to tell them apart in the output.
OPTIONS_STATUS_VALID = "valid"  # a real chain-derived options score
OPTIONS_STATUS_UNAVAILABLE = "unavailable"  # chain fetched fine, but too thin / no greeks
OPTIONS_STATUS_ERROR = "error"  # the Tradier lookup itself failed (network, auth, rate limit, ...)
OPTIONS_STATUS_SKIPPED_RATE_LIMIT = "skipped_rate_limit"  # not requested after a 429


def _fetch_options_inputs(ticker: str, price: float):
    """Isolate expected provider failures; let programming errors surface.

    Rate limits propagate to scan() to stop further options requests.
    """
    try:
        expiration = get_nearest_expiration(ticker)
        if not expiration:
            return None, OPTIONS_STATUS_UNAVAILABLE

        quote = get_quote(ticker)
        spot = quote.get("last")
        if spot is None:
            spot = price
        if spot is None or spot == 0:
            return None, OPTIONS_STATUS_UNAVAILABLE
        spot = validate_number(spot, "spot price", positive=True)

        chain = get_option_chain(ticker, expiration)
        if not chain:
            return None, OPTIONS_STATUS_UNAVAILABLE

        summary = summarize_chain(chain, spot_price=spot)
        volume = quote.get("average_volume")
        volume = 0.0 if volume is None else validate_number(volume, "average_volume")
        avg_dollar_volume = validate_number(volume * spot, "average dollar volume")
        # iv_rank proxy: relative position of ATM IV within a generic
        # 20-150% band. This is NOT a real 52-week IV rank -- see
        # tradier_options.to_options_inputs's docstring and the "Known
        # gaps" section of the plan doc.
        iv_rank_proxy = 0.0
        if summary.atm_iv:
            iv_rank_proxy = max(0.0, min(100.0, (summary.atm_iv - 0.20) / (1.50 - 0.20) * 100))

        options_inputs = to_options_inputs(summary, iv_rank=iv_rank_proxy, avg_dollar_volume=avg_dollar_volume)
        if options_inputs is None:
            return None, OPTIONS_STATUS_UNAVAILABLE
        return options_inputs, OPTIONS_STATUS_VALID

    except (requests.RequestException, TradierDataError, TradierConfigurationError) as exc:
        print(f"  [warn] {ticker}: options lookup failed ({exc})", file=sys.stderr)
        return None, OPTIONS_STATUS_ERROR


def scan(limit: int = 50) -> list[dict]:
    candidates = fetch_candidates(limit=limit)
    results = []
    rate_limited = False

    for candidate in candidates:
        if rate_limited:
            options_inputs, options_status = None, OPTIONS_STATUS_SKIPPED_RATE_LIMIT
        else:
            try:
                options_inputs, options_status = _fetch_options_inputs(candidate.ticker, candidate.price)
            except TradierRateLimitError as exc:
                print(f"  [warn] {candidate.ticker}: {exc}", file=sys.stderr)
                rate_limited = True
                options_inputs, options_status = None, OPTIONS_STATUS_ERROR
        composite = compute_composite_score(candidate.factor_inputs, options_inputs)
        results.append(
            {
                "ticker": candidate.ticker,
                "company": candidate.company,
                "price": candidate.price,
                "composite_score": round(composite.composite, 4),
                "factor_score": round(composite.factor.score, 4),
                "options_score": round(composite.options.score, 4),
                "options_status": options_status,
                "short_float_pct": candidate.factor_inputs.short_float_pct,
                "days_to_cover": candidate.factor_inputs.days_to_cover,
            }
        )
        if not rate_limited:
            time.sleep(0.2)  # pacing only; a 429 stops requests for this run

    # Composite scores are only directly comparable across rows with the
    # same options_status: "valid" rows have a real 60/40 blend, while
    # "unavailable"/"error" rows are factor-only. Rank within each group
    # rather than pretending a full mixed-basis sort is meaningful (PR #99
    # review finding 5).
    status_rank = {
        OPTIONS_STATUS_VALID: 0, OPTIONS_STATUS_UNAVAILABLE: 1,
        OPTIONS_STATUS_ERROR: 2, OPTIONS_STATUS_SKIPPED_RATE_LIMIT: 3,
    }
    results.sort(key=lambda r: (status_rank[r["options_status"]], -r["composite_score"]))
    return results


def main() -> None:
    load_dotenv()  # local dev convenience (matches tradier_opra_pull.py's pattern) --
    # a Railway deployment injects TRADIER_TOKEN directly and doesn't need a .env file.
    parser = argparse.ArgumentParser(description="Short-squeeze scanner: factor + options composite score")
    parser.add_argument("--limit", type=int, default=50, help="max finviz candidates to pull")
    parser.add_argument("--out", type=str, default=None, help="optional CSV output path")
    args = parser.parse_args()

    results = scan(limit=args.limit)

    header = [
        "ticker", "options_status", "composite_score", "factor_score", "options_score",
        "short_float_pct", "days_to_cover", "price",
    ]
    print("\t".join(header))
    for row in results:
        print("\t".join(str(row[h]) for h in header))

    if args.out:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else header)
            writer.writeheader()
            writer.writerows(results)
        print(f"\nWrote {len(results)} rows to {args.out}", file=sys.stderr)

    if any(row["options_status"] in (OPTIONS_STATUS_ERROR, OPTIONS_STATUS_SKIPPED_RATE_LIMIT) for row in results):
        print("Scan incomplete: provider errors; see options_status in the exported rows.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
