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

from squeeze_scanner.finviz_client import fetch_candidates
from squeeze_scanner.scoring import compute_composite_score
from squeeze_scanner.tradier_options import (
    get_nearest_expiration,
    get_option_chain,
    get_quote,
    summarize_chain,
    to_options_inputs,
)


def scan(limit: int = 50) -> list[dict]:
    candidates = fetch_candidates(limit=limit)
    results = []

    for candidate in candidates:
        options_inputs = None
        try:
            expiration = get_nearest_expiration(candidate.ticker)
            if expiration:
                quote = get_quote(candidate.ticker)
                spot = quote.get("last") or candidate.price
                chain = get_option_chain(candidate.ticker, expiration)
                if chain and spot:
                    summary = summarize_chain(chain, spot_price=spot)
                    avg_dollar_volume = (quote.get("average_volume") or 0) * spot
                    # iv_rank proxy: relative position of ATM IV within a
                    # generic 20-150% band. This is NOT a real 52-week IV
                    # rank -- see tradier_options.to_options_inputs's
                    # docstring and the "Known gaps" section of the plan doc.
                    iv_rank_proxy = 0.0
                    if summary.atm_iv:
                        iv_rank_proxy = max(0.0, min(100.0, (summary.atm_iv - 0.20) / (1.50 - 0.20) * 100))
                    options_inputs = to_options_inputs(
                        summary, iv_rank=iv_rank_proxy, avg_dollar_volume=avg_dollar_volume
                    )
        except Exception as exc:  # noqa: BLE001 - a single bad ticker must not kill the scan
            print(f"  [warn] {candidate.ticker}: options lookup failed ({exc})", file=sys.stderr)

        composite = compute_composite_score(candidate.factor_inputs, options_inputs)
        results.append(
            {
                "ticker": candidate.ticker,
                "company": candidate.company,
                "price": candidate.price,
                "composite_score": round(composite.composite, 4),
                "factor_score": round(composite.factor.score, 4),
                "options_score": round(composite.options.score, 4),
                "short_float_pct": candidate.factor_inputs.short_float_pct,
                "days_to_cover": candidate.factor_inputs.days_to_cover,
                "has_options_signal": options_inputs is not None,
            }
        )
        time.sleep(0.2)  # stay well under Tradier's rate limit across a full scan

    results.sort(key=lambda r: r["composite_score"], reverse=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Short-squeeze scanner: factor + options composite score")
    parser.add_argument("--limit", type=int, default=50, help="max finviz candidates to pull")
    parser.add_argument("--out", type=str, default=None, help="optional CSV output path")
    args = parser.parse_args()

    results = scan(limit=args.limit)

    header = ["ticker", "composite_score", "factor_score", "options_score", "short_float_pct", "days_to_cover", "price"]
    print("\t".join(header))
    for row in results:
        print("\t".join(str(row[h]) for h in header))

    if args.out:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else header)
            writer.writeheader()
            writer.writerows(results)
        print(f"\nWrote {len(results)} rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
