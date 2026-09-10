#!/usr/bin/env python3
"""Compact daily review over the whole bot's history -- not just confirmed
alerts (MOO-170 finding 8). Prints sample sizes per scorer/score stratum,
alert burden, and unexplained-move coverage from Storage.evaluation_report().

Read-only: does not touch the live bot loop or post to Discord.

    python scripts/daily_review.py [path/to/market_pin_bot.sqlite3]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from db.storage import Storage  # noqa: E402


def render(report: dict) -> str:
    lines = ["=" * 60, "news_pin_bot daily review", "=" * 60, ""]

    lines.append("By scorer (never pooled -- MOO-170 finding 7):")
    if not report["by_scorer"]:
        lines.append("  (no scored+resolved headlines yet)")
    for row in report["by_scorer"]:
        rate = f"{row['confirmed'] / row['total']:.0%}" if row["total"] else "n/a"
        lines.append(f"  {row['scorer'] or '(unknown)'}: n={row['total']} confirmed={row['confirmed']} "
                      f"({rate}) incomplete={row['incomplete']}")

    lines.append("")
    lines.append("By score stratum (includes shadow observations below the posting floor):")
    if not report["score_strata"]:
        lines.append("  (no strata yet)")
    for row in report["score_strata"]:
        rate = f"{row['confirmed'] / row['total']:.0%}" if row["total"] else "n/a"
        lines.append(f"  score~{row['score_bucket']}: n={row['total']} confirmed={row['confirmed']} ({rate})")

    lines.append("")
    lines.append(f"Shadow (sub-threshold) observations: n={report['shadow_total']} "
                 f"confirmed={report['shadow_confirmed']}")
    lines.append(f"Alert burden: {report['alert_burden_pins_posted']} pins posted to Discord")
    lines.append(f"Unexplained moves with no matched headline (monitored sources only): "
                 f"{report['unexplained_moves_no_match']}")
    lines.append(f"Unexplained moves reconciled by a later-arriving headline: "
                 f"{report['unexplained_moves_reconciled_later']}")

    lines.append("")
    lines.append(f"No-news control comparison (approximate, n={report['control_sample_size']}, "
                 f"{report['control_no_clean_window']} pins had no clean control window):")
    if report["control_sample_size"]:
        lines.append(f"  pin avg |move|={report['control_pin_avg_abs_pct_move']:.2f}% vs. "
                      f"control avg |move|={report['control_baseline_avg_abs_pct_move']:.2f}%")
    else:
        lines.append("  (no pins with a clean control window yet)")

    latency = report["scoring_latency"]
    lines.append("")
    if latency["count"]:
        lines.append(f"Scoring latency: n={latency['count']} mean={latency['mean_secs']:.1f}s "
                      f"max={latency['max_secs']:.1f}s")
    else:
        lines.append("Scoring latency: no scored headlines yet")

    lines.append("")
    lines.append("Note: an 'associated' move is an observation, not proof of causation. "
                 "'No matching headline' describes monitored-source coverage, not evidence "
                 "of leaks or hidden activity.")
    return "\n".join(lines)


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.DB_PATH
    if not db_path.exists():
        print(f"No database at {db_path} yet.")
        return 1
    storage = Storage(db_path)
    print(render(storage.evaluation_report()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
