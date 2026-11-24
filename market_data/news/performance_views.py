"""Display hook: attach forward performance to news view rows.

This module does NOT change ``views.py`` or ``site/js/news-panel.js``.
It is a pure post-processing step: given one sanitized view page (as
built by ``views.py``) and the ``news-performance/v1`` rows for the
same window, it returns a copy of the page with a small
``performance`` summary added to each row that has one.

The panel never has to fetch a second artifact and never has to
re-validate a key: the performance summary travels inside the same
view page the panel already validated. If the summary is absent, the
panel renders "performance not yet available" — it never renders a
zero, and it never renders a return without its honesty label.

Honesty labels (the only labels the panel shows in the line of sight)
----------------------------------------------------------------------
* ``fresh`` — a matched return whose forward price was available by
  the cutoff and whose base was matched (not stale).
* ``stale base`` — the return is computed, but the base price was
  already stale at the event. The number is shown; the label says so.
* ``pending`` — the horizon target is after the cutoff (the move has
  not happened yet in the data). No number.
* ``not yet available`` — the cutoff is past the target but no forward
  price qualifies (gap / thin series / market closed is not asserted).
  No number.
* ``no coverage`` — no price series for the mapped instrument.
* ``unknown mapping`` — the story did not map to an instrument.
* ``hindsight`` — publication-mode rows. No return is shown, ever.

The full provenance (event, published, observed, archived, revision,
classification, mapping basis, base and forward record ids and times)
stays in the row's existing fields and in the performance row; the
panel shows it one interaction away (a collapsed "Methodology &
provenance" disclosure), never as a wall of 9px grey text before the
headline. See MEDIA_PERFORMANCE.md, Gate 1 and Gate 3.
"""

from __future__ import annotations

HONESTY_LABELS = (
    "fresh", "stale base", "pending", "not yet available",
    "no coverage", "unknown mapping", "hindsight",
)


def honesty_label(row: dict) -> str:
    """Short in-sight label for one performance row. Never a number."""
    if row.get("hindsight") or row.get("status") == "hindsight":
        return "hindsight"
    if row.get("status") == "matched":
        return "stale base" if row.get("base_stale") else "fresh"
    reason = row.get("missing_reason") or ""
    if reason == "not_yet_available":
        return "pending"
    if reason == "no_series":
        return "no coverage"
    if reason.startswith("unknown mapping") or row.get("instrument_id") is None:
        return "unknown mapping"
    return "not yet available"


def summarise_for_event(performance_rows: list[dict]) -> dict | None:
    """One summary per event, keyed by horizon, or None if no rows."""
    if not performance_rows:
        return None
    by_horizon: dict[str, dict] = {}
    for row in performance_rows:
        horizon = row["horizon"]
        by_horizon[horizon] = {
            "horizon": horizon,
            "status": row["status"],
            "honesty_label": honesty_label(row),
            # Numbers only when matched; otherwise explicitly None so
            # a renderer cannot mistake "missing" for 0.0.
            "return_pct": row["return_pct"] if row["status"] == "matched" else None,
            "return_bps": row["return_bps"] if row["status"] == "matched" else None,
            "forward_price": (row["forward"] or {}).get("price")
                             if row.get("forward") else None,
            "forward_price_time": (row["forward"] or {}).get("price_time")
                                  if row.get("forward") else None,
            "base_price": ((row["base"] or {}).get("record") or {}).get("price")
                          if isinstance(row.get("base"), dict) else None,
        }
    return {"horizons": by_horizon}


def attach_performance(view_page: dict, performance_result: dict) -> dict:
    """Return a copy of ``view_page`` with ``performance`` on each row.

    Rows are matched by ``(item_id, revision_id)``. A row with no
    performance rows gets ``performance: None`` — the panel renders
    "performance not yet available", never a zero.
    """
    by_event: dict[tuple, list[dict]] = {}
    for row in performance_result.get("rows", []):
        by_event.setdefault((row["item_id"], row["revision_id"]), []).append(row)
    page = dict(view_page)
    new_rows = []
    for row in view_page.get("rows", []):
        key = (row.get("item_id"), row.get("revision_id"))
        new_rows.append({**row, "performance": summarise_for_event(by_event.get(key, []))})
    page["rows"] = new_rows
    page["performance_schema"] = performance_result.get("schema_version")
    return page
