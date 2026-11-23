#!/usr/bin/env python3
"""Write #130 window outputs in the proposed oa203/windows/ layout (local only).

    <root>/oa203/windows/<policy>/<kind>/<window_id>/summary.json
    <root>/oa203/windows/<policy>/<kind>/<window_id>/rankings.csv     # bounded shortlist per basis
    <root>/oa203/windows/<policy>/<kind>/<window_id>/paths.csv.gz     # every contract path

This module only writes to a local directory. Publishing under the
oa203/windows/ R2 prefix is a separate, unapproved step. Files are written
to a sibling .tmp and atomically renamed, like the daily scanner, so a
published name only ever holds complete content.

Window state:
- ``final``: every window session has a final daily summary with
  assessment ``complete``.
- ``partial``: every session is final, but at least one is assessed
  partial or a source is missing/damaged (reasons listed).
- ``partial`` (bounded subset): an emit whose scope says
  ``universe_scope == "bounded_subset"`` is always ``partial`` with
  reason ``bounded_universe_subset``, even when every session it covers
  is complete. A 50-underlying subset never claims full-universe
  completion or final availability.
- ``in_progress``: at least one window session has no final daily summary
  yet. Its exit is not invented.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import market_data.options_scanner.windows as w

SHORTLIST = 200
PATH_COLUMNS = ["symbol", "underlying", "root", "option_type", "strike", "expiration", "contract_size",
                "flags", "contract_exclusions"]
BASIS_COLUMNS = ["status", "reasons", "pct", "entry", "exit", "abs_change_per_contract", "entry_ms", "exit_ms",
                 "entry_session", "exit_session", "entry_delay_from_boundary_s", "exit_lead_before_close_s",
                 "entry_bid", "entry_ask", "entry_bid_ms", "entry_ask_ms", "entry_ask_size",
                 "entry_bid_quote_age_s", "entry_ask_quote_age_s", "entry_quote_age_s", "entry_relative_spread",
                 "exit_bid", "exit_ask", "exit_bid_ms", "exit_ask_ms", "exit_bid_size",
                 "exit_bid_quote_age_s", "exit_ask_quote_age_s", "exit_quote_age_s", "exit_relative_spread"]


@contextmanager
def _atomic(path: Path) -> Iterator[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    yield tmp
    with open(tmp, "r+b") as f:  # writable handle: fsync on a read-only fd fails on Windows
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _cell(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return ";".join(str(x) for x in v)
    return "" if v is None else v


def window_state(window: w.Window, session_status: Mapping[str, Mapping[str, Any]],
                 missing_sources: Sequence[str] = ()) -> tuple[str, list[str]]:
    """State and reasons from each window session's daily summary status."""
    reasons: list[str] = []
    pending = [s.day for s in window.sessions if not session_status.get(s.day, {}).get("final")]
    if pending:
        return "in_progress", [f"session_not_final:{d}" for d in pending]
    for s in window.sessions:
        assessed = session_status[s.day].get("assessment")
        if assessed != "complete":
            reasons.append(f"session_{assessed or 'unassessed'}:{s.day}")
    reasons += [f"source_missing:{k}" for k in missing_sources]
    return ("partial" if reasons else "final"), reasons


def path_rows_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(PATH_COLUMNS + [f"{b}_{c}" for b in w.BASES for c in BASIS_COLUMNS])
    for r in sorted(rows, key=lambda r: r["symbol"]):
        wr.writerow([_cell(r.get(c)) for c in PATH_COLUMNS] +
                    [_cell(r[b].get(c)) for b in w.BASES for c in BASIS_COLUMNS])
    return buf.getvalue().encode("utf-8")


def rankings_csv(rows: Sequence[Mapping[str, Any]], shortlist: int) -> tuple[bytes, dict[str, Any]]:
    """Ranks are taken over the full eligible universe, then the shortlist is cut."""
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(["basis", "rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry",
                 "exit", "abs_change_per_contract", "entry_session", "exit_session", "flags"])
    counts: dict[str, Any] = {}
    for basis in w.BASES:
        ranked = w.rank_window(rows, basis)
        eligible = [r for r in ranked if r[f"{basis}_rank"]]
        reasons: dict[str, int] = {}
        for r in ranked:
            if not r[f"{basis}_rank"]:
                key = (r[basis]["reasons"] or ["unknown"])[0].split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
        counts[basis] = {"eligible": len(eligible), "negative": sum(r[basis]["pct"] < 0 for r in eligible),
                         "excluded": len(ranked) - len(eligible),
                         "exclusion_reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
                         "shortlist": min(shortlist, len(eligible))}
        for r in eligible[:shortlist]:
            b = r[basis]
            wr.writerow([basis, r[f"{basis}_rank"], r["symbol"], r["underlying"], r["option_type"], r["strike"],
                         r["expiration"], b["pct"], b["entry"], b["exit"], b["abs_change_per_contract"],
                         b["entry_session"], b["exit_session"], _cell(r["flags"])])
    return buf.getvalue().encode("utf-8"), counts


def write_window(root: Path, window: w.Window, rows: Sequence[Mapping[str, Any]], policy: w.WindowPolicy,
                 session_status: Mapping[str, Mapping[str, Any]], sources: Sequence[Mapping[str, Any]],
                 generated_at: str, scope: Mapping[str, Any], shortlist: int = SHORTLIST) -> Path:
    """Write one window's three files; returns the window directory."""
    out = root / "oa203" / "windows" / w.WINDOW_POLICY_VERSION / window.kind / window.window_id
    state, reasons = window_state(window, session_status,
                                  [s["key"] for s in sources if s.get("missing")])
    subset_reason = None
    if scope.get("mode") == "offline_replay":
        subset_reason = "bounded_universe_replay"
    elif scope.get("universe_scope") == "bounded_subset":
        subset_reason = "bounded_universe_subset"
    if subset_reason:
        if state == "final":
            state, reasons = "partial", [subset_reason]
        elif subset_reason not in reasons:
            reasons = [subset_reason, *reasons]
    rankings, counts = rankings_csv(rows, shortlist)
    paths = gzip.compress(path_rows_csv(rows), mtime=0)
    summary = {
        "schema_version": 1, **window.as_dict(), "state": state, "state_reasons": reasons,
        "policy": policy.as_dict(), "generated_at": generated_at, "scope": dict(scope),
        "calendar": "XNYS (session bounds from each daily summary.json)",
        "sessions": [{"day": s.day, "open_ms": s.open_ms, "close_ms": s.close_ms, "early_close": s.early_close,
                      **{k: v for k, v in session_status.get(s.day, {}).items()}} for s in window.sessions],
        "sources": list(sources), "contracts_evaluated": len(rows), "counts": counts,
        "outputs": {"rankings": "rankings.csv", "paths": "paths.csv.gz"},
        "labels": ["Sampled quotes (~5-minute sweeps); between-sample moves are missed.",
                   "Quote comparisons, not executed fills; spread/size qualification does not establish fills, fees or capacity.",
                   "Cheapest-entry and best-exit variants are hindsight results, not predictions."
                   if window.kind.startswith("weekly_cheapest") else
                   ("Separate opening-price comparison; first qualifying sampled ask near the open."
                    if window.kind == "weekly_fixed" else "Fixed-endpoint window.")],
    }
    for name, data in (("rankings.csv", rankings), ("paths.csv.gz", paths)):
        with _atomic(out / name) as tmp:
            tmp.write_bytes(data)
    with _atomic(out / "summary.json") as tmp:
        tmp.write_text(json.dumps(summary, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return out
