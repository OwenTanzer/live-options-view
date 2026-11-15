#!/usr/bin/env python3
"""Publish validated #130 window outputs to R2 (offline-tested only).

Takes a local ``oa203/windows/`` tree produced by ``windows_replay`` /
``windows_outputs`` and publishes one window under::

    oa203/windows/oa203-windows-v1/<kind>/<window_id>/{summary.json,rankings.csv,paths.csv.gz}

mirroring the F1 active-snapshot pattern (``active.py`` / ``active_budget.py``):

- every member is validated against the consumer's exact contract
  (``market_mcp/consumer.js`` ``windowSummary`` + rankings + paths-detail
  checks) *before* anything is uploaded;
- members are written to an immutable ``rev_<sha256(summary.json)>/``
  directory, then to the flat consumer keys (paths, rankings, summary last,
  so an interruption leaves either the old complete window or a new one the
  consumer's before/after digest check rejects, never a mixed ranking), then
  a per-window ``latest.json`` pointer last;
- each upload is verified by a full readback (GET + SHA-256), not just a
  length check, and a digest mismatch fails closed with no pointer written;
- a conditional reservation token bounds retries (3 attempts per revision)
  and a retention/budget policy bounds storage; truncated inventories and
  unknown keys fail closed;
- a retry of the same window is idempotent (same revision, no duplicate
  upload) when the remote pointer and revision members already match.

States (decision, recorded explicitly):

- ``final`` and ``partial`` windows are published with their true state.
  A partial window is never marked final.
- ``in_progress`` windows are REFUSED entirely: no member is published,
  not even a summary-only one. A half-populated window directory is a
  footgun (the consumer serves in_progress as summary-only with no rows,
  and a later final publish must overwrite it); an in-progress window
  becomes publishable when its sessions finalize and it is re-emitted.
- A window is published only with all three members present and valid.
  A missing ``paths.csv.gz`` is a refusal, not a rankings-only partial
  publish: the consumer tolerates a missing paths file at read time, but
  the publisher never creates that state.

The storage backend is injectable. Tests use an in-memory fake and a
local-filesystem backend; the real R2 backend reuses the scanner's
existing upload client (``market_data.tradier.probe`` via
``market_data.options_scanner._probe``) with no new credentials, host, or
secrets. No live R2 publication happens from this module without the
release gate in ``docs/130-window-publisher.md`` (Chej/Owen approval plus
OA203-style pre-checks). This module never sets an env flag, starts a
collector, or deploys anything.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from zoneinfo import ZoneInfo

import market_data.options_scanner.windows as w
import market_data.options_scanner.windows_outputs as outputs
from market_data.options_scanner.archive import _content_type

POLICY = w.WINDOW_POLICY_VERSION
BASES = w.BASES
SHORTLIST = outputs.SHORTLIST
RANKING_COLUMNS = ["basis", "rank", "symbol", "underlying", "option_type", "strike", "expiration", "pct", "entry",
                   "exit", "abs_change_per_contract", "entry_session", "exit_session", "flags"]
PATH_COLUMNS = outputs.PATH_COLUMNS
BASIS_COLUMNS = outputs.BASIS_COLUMNS

# Ordinary consumer byte ceilings (market_mcp/source.js LIMITS): a single artifact is
# at most 4 MiB, a decompressed artifact at most 48 MiB. The publisher
# enforces them before upload so a published window is always consumable.
# Measured full-universe members (real producer, 14-batch combine,
# 71,710 contracts/window, 2026-11-09): summary at most 55,492 B,
# rankings at most 65,497 B (shortlist-capped at 600 rows), paths.gz at
# most 2,436,802 B -- all comfortably inside their caps, which are NOT
# raised. Only the expanded-paths ceiling was demonstrated insufficient:
# the same windows expand to 32,806,912-34,295,267 B, over the old 24 MiB
# bound, so it is raised to 48 MiB: 1.46x the measured maximum, and still
# inside the 64 MiB per-query budget with the other members (48 + ~2.4
# compressed + ~0.1 summary/rankings < 64 MiB).
MAX_SUMMARY_BYTES = 1 * 1024 * 1024
MAX_RANKINGS_BYTES = 4 * 1024 * 1024
MAX_PATHS_BYTES = 4 * 1024 * 1024
MAX_PATHS_EXPANDED_BYTES = 48 * 1024 * 1024
MAX_WEEKLY_PATHS_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_PATH_LINE_BYTES = 64 * 1024
MEMBER_MAX_BYTES = {"summary.json": MAX_SUMMARY_BYTES, "rankings.csv": MAX_RANKINGS_BYTES,
                    "paths.csv.gz": MAX_PATHS_BYTES}
WINDOW_MEMBERS = ("summary.json", "rankings.csv", "paths.csv.gz")
# Flat consumer keys are published in this order, summary LAST: the summary
# is the commit record for the set, so an interrupted publish leaves the old
# summary (whose counts still describe the old rankings) rather than a new
# summary over old rankings. The revision members are immutable, so their
# order does not matter. (The consumer's pointer check is the load-bearing
# guard against a mixed revision; this order is defence in depth for
# readers that do not check the pointer.)
FLAT_PUBLISH_ORDER = ("paths.csv.gz", "rankings.csv", "summary.json")

WINDOW_ID = {
    "weekly_cheapest": re.compile(r"weekly-(\d{4})-W(\d{2})-cheapest\Z"),
    "weekly_fixed": re.compile(r"weekly-(\d{4})-W(\d{2})-fixed\Z"),
    "weekly_cheapest_best_exit": re.compile(r"weekly-(\d{4})-W(\d{2})-cheapest_best_exit\Z"),
    "overnight": re.compile(r"overnight-(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\Z"),
    "sessions_2": re.compile(r"sessions2-(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\Z"),
    "sessions_3": re.compile(r"sessions3-(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\Z"),
}
# Relative keys allowed under oa203/windows/<policy>/<kind>/<window_id>/.
# Anything else in an inventory listing is an unrecognized artifact and
# fails the publish closed (manual reconciliation required).
_MEMBER = r"(?:summary\.json|rankings\.csv|paths\.csv\.gz)"
WINDOW_KEY = re.compile(
    r"(?:rev_[0-9a-f]{64}/" + _MEMBER + r"|" + _MEMBER + r"|latest\.json|latest-attempt\.json|"
    r"budget/rev_[0-9a-f]{64}-[123]\.json)\Z")
REV_DIR = re.compile(r"rev_([0-9a-f]{64})\Z")
_OCC_SUFFIX = re.compile(r"(\d{6})([CP])(\d{8})\Z")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,5}\Z")
MARKET_TZ = ZoneInfo("America/New_York")
_EPOCH_2000_MS = 946684800000


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def window_prefix(kind: str, window_id: str) -> str:
    return f"oa203/windows/{POLICY}/{kind}/{window_id}"


def validate_window_identity(kind: str, window_id: str) -> None:
    """Kind/window_id agreement, mirroring the consumer allowlist exactly."""
    if kind not in w.KINDS:
        raise ValueError(f"unknown window kind: {kind}")
    match = WINDOW_ID[kind].match(window_id)
    if not match:
        raise ValueError(f"window_id {window_id!r} does not match kind {kind}")
    if kind.startswith("weekly"):
        year, week = int(match.group(1)), int(match.group(2))
        if not 1 <= week <= 53:
            raise ValueError(f"invalid ISO week in window_id: {window_id!r}")
        try:
            datetime.strptime(f"{year}-W{week:02d}-1", "%Y-W%W-%w")
        except ValueError:
            raise ValueError(f"invalid ISO week in window_id: {window_id!r}") from None
    else:
        start, end = match.group(1), match.group(2)
        try:
            start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            raise ValueError(f"invalid window span in window_id: {window_id!r}") from None
        if not start_d < end_d:
            raise ValueError(f"window entry day must precede its exit day: {window_id!r}")


def _is_int(v: Any) -> bool:
    return type(v) is int


def _is_num(v: Any) -> bool:
    return type(v) in (int, float)


def _parse_generated_at(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("summary generated_at is not a string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("summary generated_at is not parseable") from None


def _occ_parts(symbol: Any) -> tuple[str, str, str, float]:
    """Split an OCC symbol into (root, expiration, option_type, strike)."""
    if not isinstance(symbol, str) or len(symbol) < 16:
        raise ValueError(f"invalid OCC contract symbol: {symbol!r}")
    root, suffix = symbol[:-15], symbol[-15:]
    match = _OCC_SUFFIX.match(suffix)
    if not root or not match:
        raise ValueError(f"invalid OCC contract symbol: {symbol!r}")
    expiry = date(2000 + int(match.group(1)[:2]), int(match.group(1)[2:4]),
                  int(match.group(1)[4:6])).isoformat()
    return root, expiry, "call" if match.group(2) == "C" else "put", int(match.group(3)) / 1000


def _cell_num(cell: Any) -> float | int | None:
    if cell in (None, ""):
        return None
    try:
        value: float | int = int(cell) if re.fullmatch(r"[+-]?\d+", str(cell).strip()) else float(cell)
    except (TypeError, ValueError):
        raise ValueError(f"non-numeric cell: {cell!r}") from None
    return value


def _validate_summary(summary: Any, kind: str, window_id: str) -> dict[str, Any]:
    """Port of the consumer's windowSummary validator, plus id/span agreement."""
    if not isinstance(summary, dict):
        raise ValueError("summary.json is not an object")
    if (summary.get("schema_version") != 1 or summary.get("kind") != kind
            or summary.get("window_id") != window_id or not isinstance(summary.get("state"), str)
            or summary.get("state") not in ("final", "partial", "in_progress")
            or not isinstance(summary.get("state_reasons"), list)
            or not all(isinstance(r, str) for r in summary["state_reasons"])):
        raise ValueError("unsupported window summary identity or state")
    state = summary["state"]
    if state == "in_progress":
        # Refused entirely by the publisher (see module docstring); the
        # message names the decision so a caller never mistakes it for a bug.
        raise ValueError("in_progress window is not published (no ranking served; republish when final)")
    policy = summary.get("policy")
    if (not isinstance(policy, dict) or policy.get("version") != POLICY
            or policy.get("close_tolerance_s") != 900 or policy.get("open_tolerance_s") != 900
            or not isinstance(policy.get("qualified_endpoint_policy"), dict)
            or policy["qualified_endpoint_policy"].get("max_quote_age_s") != 1800):
        raise ValueError("unsupported window policy")
    sessions = summary.get("sessions")
    if (not isinstance(sessions, list) or not sessions
            or not all(isinstance(s, dict) and isinstance(s.get("day"), str)
                       and _is_int(s.get("open_ms")) and _is_int(s.get("close_ms"))
                       and s["open_ms"] < s["close_ms"] and isinstance(s.get("early_close"), bool)
                       and isinstance(s.get("final"), bool) and isinstance(s.get("assessment"), str)
                       for s in sessions)
            or any(sessions[i - 1]["day"] >= sessions[i]["day"] for i in range(1, len(sessions)))):
        raise ValueError("unsupported window sessions")
    days = [s["day"] for s in sessions]
    # Session bounds must agree with their exchange date (producer invariant).
    for s in sessions:
        try:
            w.Session(s["day"], s["open_ms"], s["close_ms"], early_close=s["early_close"])
        except ValueError:
            raise ValueError(f"session bounds disagree with exchange date: {s['day']}") from None
    if (not isinstance(summary.get("entry_sessions"), list) or not summary["entry_sessions"]
            or summary["entry_sessions"] != days[:len(summary["entry_sessions"])]
            or summary.get("exit_session") != days[-1]
            or not isinstance(summary.get("early_closes"), list)
            or not all(d in days for d in summary["early_closes"])):
        raise ValueError("unsupported window entry/exit sessions")
    pending = [s["day"] for s in sessions if not s["final"]]
    if state == "final":
        if pending or summary["state_reasons"] or not all(s["assessment"] == "complete" for s in sessions):
            raise ValueError("final window has pending or partial sessions")
    elif state == "partial":
        if pending or not summary["state_reasons"]:
            raise ValueError("partial window names no reason")
    counts = summary.get("counts")
    evaluated = summary.get("contracts_evaluated")
    if not _is_int(evaluated) or evaluated < 0 or not isinstance(counts, dict):
        raise ValueError("unsupported window per-basis counts")
    for basis in BASES:
        c = counts.get(basis)
        if (not isinstance(c, dict) or not _is_int(c.get("eligible")) or c["eligible"] < 0
                or not _is_int(c.get("negative")) or c["negative"] < 0 or c["negative"] > c["eligible"]
                or not _is_int(c.get("excluded")) or c["excluded"] < 0
                or c["eligible"] + c["excluded"] != evaluated
                or not _is_int(c.get("shortlist")) or c["shortlist"] < 0
                or c["shortlist"] != min(SHORTLIST, c["eligible"])
                or not isinstance(c.get("exclusion_reasons"), dict)
                or not all(_is_int(n) and n >= 0 for n in c["exclusion_reasons"].values())
                or sum(c["exclusion_reasons"].values()) != c["excluded"]):
            raise ValueError(f"unsupported window per-basis counts for {basis}")
    outputs_field = summary.get("outputs")
    labels = summary.get("labels")
    if (not isinstance(outputs_field, dict) or outputs_field.get("rankings") != "rankings.csv"
            or outputs_field.get("paths") != "paths.csv.gz"
            or not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels)
            or (kind in ("weekly_cheapest", "weekly_cheapest_best_exit")
                and not any("hindsight" in x.lower() for x in labels))
            or not isinstance(summary.get("scope"), dict) or not isinstance(summary.get("calendar"), str)
            or not isinstance(summary.get("sources"), list)
            or not all(isinstance(s, dict) and isinstance(s.get("key"), str) for s in summary["sources"])):
        raise ValueError("unsupported window outputs, labels or provenance")
    _parse_generated_at(summary.get("generated_at"))
    # Window-id / session agreement: a weekly id names its ISO week, a span
    # id names its first and last session days.
    if kind.startswith("weekly"):
        iso = date.fromisoformat(days[0]).isocalendar()
        match = WINDOW_ID[kind].match(window_id)
        assert match is not None
        if (iso.year, iso.week) != (int(match.group(1)), int(match.group(2))):
            raise ValueError("weekly window_id disagrees with its sessions")
        if any(date.fromisoformat(d).isocalendar()[:2] != (iso.year, iso.week) for d in days):
            raise ValueError("weekly window spans more than one ISO week")
        if len(days) < 2:
            raise ValueError("weekly window needs at least two sessions")
    else:
        match = WINDOW_ID[kind].match(window_id)
        assert match is not None
        if (days[0], days[-1]) != (match.group(1), match.group(2)):
            raise ValueError("window span disagrees with its sessions")
    return summary


def _f(cell: Any, what: str) -> float:
    """Parse a CSV numeric cell; garbage fails closed with a named error."""
    value = _cell_num(cell)
    if not _is_num(value):
        raise ValueError(f"invalid window rankings {what}: {cell!r}")
    return float(value)


def _validate_rankings(text: bytes, summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Rankings CSV must be exactly the summary's per-basis shortlists."""
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("rankings.csv is not UTF-8") from None
    try:
        rows = list(csv.DictReader(io.StringIO(decoded)))
        # splitlines() so a CRLF file is read as the producer's LF file.
        header = (decoded.splitlines() or [""])[0].split(",")
    except csv.Error:
        raise ValueError("rankings.csv is not parseable CSV") from None
    if header != RANKING_COLUMNS:
        raise ValueError("missing window rankings columns")
    for row in rows:
        if None in row or sorted(k for k in row if k is not None) != sorted(RANKING_COLUMNS):
            raise ValueError("rankings row does not match the expected columns")
    days = [s["day"] for s in summary["sessions"]]
    counts = summary["counts"]
    by_basis: dict[str, list[dict[str, Any]]] = {}
    order = 0
    for row in rows:
        basis = row.get("basis")
        if basis not in BASES:
            raise ValueError(f"rankings row has unknown basis: {basis!r}")
        index = BASES.index(basis)
        if index < order:
            raise ValueError("window rankings are not grouped by basis")
        order = index
        by_basis.setdefault(basis, []).append(row)
    for basis in BASES:
        brow = by_basis.get(basis, [])
        shortlist = counts[basis]["shortlist"]
        if len(brow) != shortlist:
            raise ValueError(
                f"window rankings hold {len(brow)} {basis} rows, not the published shortlist {shortlist}")
        for i, r in enumerate(brow):
            if _cell_num(r.get("rank")) != i + 1:
                raise ValueError(f"window {basis} ranks are not exactly 1..{shortlist}")
            if i:
                prev = brow[i - 1]
                pct, prev_pct = _f(r.get("pct"), "pct"), _f(prev.get("pct"), "pct")
                if not (pct < prev_pct or (pct == prev_pct and r["symbol"] > prev["symbol"])):
                    raise ValueError(f"window {basis} rows are not pct-descending with symbol tiebreak")
        negatives = sum(1 for r in brow if _f(r.get("pct"), "pct") < 0)
        if counts[basis]["eligible"] == shortlist:
            if negatives != counts[basis]["negative"]:
                raise ValueError(f"window {basis} negative count disagrees with its published count")
        elif negatives > counts[basis]["negative"]:
            raise ValueError(f"window {basis} shortlist holds more negative rows than its published count")
    for r in rows:
        try:
            root, expiry, otype, strike = _occ_parts(r["symbol"])
        except ValueError:
            raise ValueError(f"invalid window rankings row symbol: {r.get('symbol')!r}") from None
        if (not _SYMBOL.match(r["underlying"] or "") or r["option_type"] != otype
                or r["expiration"] != expiry or _f(r.get("strike"), "strike") != strike
                or not _is_num(_cell_num(r.get("pct"))) or not _is_num(_cell_num(r.get("entry")))
                or _f(r.get("entry"), "entry") <= 0 or not _is_num(_cell_num(r.get("exit")))
                or _f(r.get("exit"), "exit") <= 0
                or not _is_num(_cell_num(r.get("abs_change_per_contract")))
                or r["entry_session"] not in days or r["exit_session"] not in days
                or r["entry_session"] > r["exit_session"]
                or abs(_f(r.get("pct"), "pct")
                       - (_f(r.get("exit"), "exit") / _f(r.get("entry"), "entry") - 1)) >= 2e-6):
            raise ValueError(f"invalid window rankings row: {r.get('symbol')!r}")
    return by_basis


def _decompress_bounded(data: bytes, limit: int) -> str:
    """Gunzip with a hard expanded-size ceiling; over it fails closed."""
    try:
        raw = gzip.GzipFile(fileobj=io.BytesIO(data))
        chunks: list[bytes] = []
        total = 0
        while True:
            piece = raw.read(1024 * 1024)
            if not piece:
                break
            total += len(piece)
            if total > limit:
                raise ValueError("paths.csv.gz exceeds the expanded size ceiling")
            chunks.append(piece)
    except (EOFError, OSError) as exc:
        raise ValueError(f"paths.csv.gz is damaged: {exc}") from None
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("paths.csv.gz is not UTF-8") from None


def _weekly_path_lines(data: bytes) -> Iterator[str]:
    """Validate the entire gzip stream while keeping only one path line in memory."""
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as raw:
            while line := raw.readline(MAX_PATH_LINE_BYTES + 1):
                total += len(line)
                if total > MAX_WEEKLY_PATHS_EXPANDED_BYTES:
                    raise ValueError("weekly paths.csv.gz exceeds the expanded size ceiling")
                if len(line) > MAX_PATH_LINE_BYTES:
                    raise ValueError("weekly path line exceeds its byte ceiling")
                yield line.decode("utf-8")
    except (EOFError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"weekly paths.csv.gz is damaged: {exc}") from None


def _validate_paths(data: bytes, summary: dict[str, Any],
                    ranked: dict[str, list[dict[str, Any]]], *, weekly: bool) -> None:
    """Full-universe paths must prove the summary counts and the shortlist.

    Streams rows (only ranked symbols are retained): symbols unique
    (conflicting attributions for one symbol fail closed), row count equals
    contracts_evaluated, per-basis eligible/excluded counts and
    exclusion-reason sums recomputed from the full universe match the
    summary exactly, and every ranked row matches its paths row on basis
    status, pct, entry, exit and sessions.
    """
    lines = (_weekly_path_lines(data) if weekly
             else io.StringIO(_decompress_bounded(data, MAX_PATHS_EXPANDED_BYTES)))
    try:
        reader = csv.DictReader(lines)
        header = reader.fieldnames
    except csv.Error:
        raise ValueError("paths.csv.gz is not parseable CSV") from None
    expected_header = PATH_COLUMNS + [f"{b}_{c}" for b in BASES for c in BASIS_COLUMNS]
    if header != expected_header:
        raise ValueError("unsupported window paths header")
    sessions = {s["day"]: s for s in summary["sessions"]}
    ranked_symbols = {r["symbol"] for brow in ranked.values() for r in brow}
    ranked_cells = {(r["symbol"], r["basis"]): r for brow in ranked.values() for r in brow}
    seen: set[str] = set()
    kept: dict[str, dict[str, Any]] = {}
    per_basis: dict[str, dict[str, Any]] = {
        b: {"eligible": 0, "excluded": 0, "reasons": {}} for b in BASES}
    row_count = 0
    for row in reader:
        row_count += 1
        symbol = row.get("symbol")
        if not symbol or symbol in seen:
            raise ValueError("paths file carries conflicting attributions for a symbol; "
                             "no canonical path is asserted")
        seen.add(symbol)
        try:
            root, expiry, otype, strike = _occ_parts(symbol)
        except ValueError:
            raise ValueError(f"invalid window paths row symbol: {symbol!r}") from None
        if (row.get("root") != root or row.get("underlying") is None or not _SYMBOL.match(row["underlying"])
                or row.get("option_type") != otype or row.get("expiration") != expiry
                or _cell_num(row.get("strike")) != strike):
            raise ValueError(f"invalid window paths row identity: {symbol!r}")
        size = _cell_num(row.get("contract_size"))
        if not _is_int(size) or size <= 0:
            raise ValueError(f"invalid window paths contract size: {symbol!r}")
        exclusions = [x for x in (row.get("contract_exclusions") or "").split(";") if x]
        for basis in BASES:
            cell = row.get(f"{basis}_status")
            reasons = [x for x in (row.get(f"{basis}_reasons") or "").split(";") if x]
            if cell == "eligible":
                per_basis[basis]["eligible"] += 1
                if exclusions:
                    raise ValueError(f"eligible {basis} path carries contract exclusions: {symbol!r}")
                _validate_paths_leg(row, basis, sessions, size)
            elif cell == "excluded":
                per_basis[basis]["excluded"] += 1
                if not reasons:
                    raise ValueError(f"excluded {basis} path names no reason: {symbol!r}")
                key = reasons[0].split(":")[0]
                per_basis[basis]["reasons"][key] = per_basis[basis]["reasons"].get(key, 0) + 1
                if exclusions and sorted(reasons) != sorted(exclusions):
                    raise ValueError(f"contract exclusions disagree with {basis} reasons: {symbol!r}")
            else:
                raise ValueError(f"invalid window paths {basis} status: {symbol!r}")
        if exclusions and not all(row.get(f"{b}_status") == "excluded" for b in BASES):
            raise ValueError(f"contract exclusions without all-basis exclusion: {symbol!r}")
        if symbol in ranked_symbols:
            kept[symbol] = row
    if row_count != summary["contracts_evaluated"]:
        raise ValueError("paths row count disagrees with contracts_evaluated")
    for basis in BASES:
        counts = summary["counts"][basis]
        got = per_basis[basis]
        if got["eligible"] != counts["eligible"] or got["excluded"] != counts["excluded"]:
            raise ValueError(f"paths per-basis counts disagree with the summary for {basis}")
        if got["reasons"] != counts["exclusion_reasons"]:
            raise ValueError(f"paths exclusion reasons disagree with the summary for {basis}")
    for (symbol, basis), rank_row in ranked_cells.items():
        prow = kept.get(symbol)
        if prow is None:
            raise ValueError(f"ranked symbol is absent from paths: {symbol!r}")
        if (prow.get(f"{basis}_status") != "eligible"
                or _cell_num(prow.get(f"{basis}_pct")) != float(rank_row["pct"])
                or _cell_num(prow.get(f"{basis}_entry")) != float(rank_row["entry"])
                or _cell_num(prow.get(f"{basis}_exit")) != float(rank_row["exit"])
                or prow.get(f"{basis}_entry_session") != rank_row["entry_session"]
                or prow.get(f"{basis}_exit_session") != rank_row["exit_session"]
                or prow.get("underlying") != rank_row["underlying"]
                or prow.get("option_type") != rank_row["option_type"]
                or _cell_num(prow.get("strike")) != float(rank_row["strike"])
                or prow.get("expiration") != rank_row["expiration"]):
            raise ValueError(f"paths row disagrees with the pinned ranking row: {symbol!r}")


def _validate_paths_leg(row: dict[str, Any], basis: str, sessions: dict[str, dict[str, Any]],
                        contract_size: int) -> None:
    """Endpoint/session/quote/arithmetic checks for one eligible paths leg.

    Mirrors the consumer's detail verification for the pinned ranking row.
    """
    pct = _cell_num(row.get(f"{basis}_pct"))
    entry = _cell_num(row.get(f"{basis}_entry"))
    exit_ = _cell_num(row.get(f"{basis}_exit"))
    entry_ms = _cell_num(row.get(f"{basis}_entry_ms"))
    exit_ms = _cell_num(row.get(f"{basis}_exit_ms"))
    if (not _is_num(pct) or not _is_num(entry) or entry <= 0 or not _is_num(exit_) or exit_ <= 0
            or not _is_int(entry_ms) or not _is_int(exit_ms) or exit_ms <= entry_ms):
        raise ValueError(f"invalid window paths {basis} endpoints: {row.get('symbol')!r}")
    entry_session = sessions.get(row.get(f"{basis}_entry_session") or "")
    exit_session = sessions.get(row.get(f"{basis}_exit_session") or "")
    if (not entry_session or not exit_session
            or not entry_session["open_ms"] <= entry_ms < entry_session["close_ms"]
            or not exit_session["open_ms"] <= exit_ms < exit_session["close_ms"]):
        raise ValueError(f"window path endpoints fall outside their sessions: {row.get('symbol')!r}")
    for stamp_name, at in ((f"{basis}_entry_bid_ms", entry_ms), (f"{basis}_entry_ask_ms", entry_ms),
                           (f"{basis}_exit_bid_ms", exit_ms), (f"{basis}_exit_ask_ms", exit_ms)):
        stamp = _cell_num(row.get(stamp_name))
        if stamp is not None and (not _is_int(stamp) or stamp < _EPOCH_2000_MS or stamp > at):
            raise ValueError(f"window path quote stamp is missing, future, or before 2000: {row.get('symbol')!r}")
    for age_name in (f"{basis}_entry_quote_age_s", f"{basis}_exit_quote_age_s",
                     f"{basis}_entry_bid_quote_age_s", f"{basis}_entry_ask_quote_age_s",
                     f"{basis}_exit_bid_quote_age_s", f"{basis}_exit_ask_quote_age_s"):
        age = _cell_num(row.get(age_name))
        if age is not None and (not _is_num(age) or age < 0):
            raise ValueError(f"window path quote age is negative: {row.get('symbol')!r}")
    abs_change = _cell_num(row.get(f"{basis}_abs_change_per_contract"))
    if (abs(pct - (exit_ / entry - 1)) >= 2e-6 or not _is_num(abs_change)
            or abs(abs_change - (exit_ - entry) * contract_size) >= 1e-3):
        raise ValueError(f"window path arithmetic disagrees with its own endpoints: {row.get('symbol')!r}")


@dataclass
class ValidatedWindow:
    kind: str
    window_id: str
    state: str
    summary: dict[str, Any]
    summary_bytes: bytes
    member_bytes: dict[str, bytes]
    member_sha256: dict[str, str]
    revision: str

    @property
    def prefix(self) -> str:
        return window_prefix(self.kind, self.window_id)


def load_window(window_dir: Path) -> ValidatedWindow:
    """Read and fully validate one local window directory; nothing is written."""
    parts = window_dir.parts
    if (len(parts) < 5 or parts[-5] != "oa203" or parts[-4] != "windows"
            or parts[-3] != POLICY):
        raise ValueError("window directory is not under oa203/windows/<policy>/")
    kind, window_id = parts[-2], parts[-1]
    validate_window_identity(kind, window_id)
    member_bytes: dict[str, bytes] = {}
    for name in WINDOW_MEMBERS:
        path = window_dir / name
        if not path.is_file():
            raise ValueError(f"window is missing member: {name}")
        data = path.read_bytes()
        if len(data) > MEMBER_MAX_BYTES[name]:
            raise ValueError(f"window member exceeds its byte ceiling: {name}")
        member_bytes[name] = data
    try:
        summary = json.loads(member_bytes["summary.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("summary.json is not valid JSON") from None
    summary = _validate_summary(summary, kind, window_id)
    ranked = _validate_rankings(member_bytes["rankings.csv"], summary)
    try:
        _validate_paths(member_bytes["paths.csv.gz"], summary, ranked, weekly=kind.startswith("weekly_"))
    except ValueError as exc:
        raise ValueError(f"{kind}/{window_id}: {exc}") from exc
    revision = "rev_" + _sha256(member_bytes["summary.json"])
    return ValidatedWindow(kind=kind, window_id=window_id, state=summary["state"], summary=summary,
                           summary_bytes=member_bytes["summary.json"], member_bytes=member_bytes,
                           member_sha256={n: _sha256(b) for n, b in member_bytes.items()},
                           revision=revision)


# -- storage backends ------------------------------------------------------
#
# A backend moves bytes under oa203/windows/ keys. The publisher only ever
# calls put/get/put_if_absent/list_keys/delete, so a fake backend can stand
# in for R2 in tests with no network and no credentials.


class StorageBackend(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> dict[str, Any]:
        """Store bytes; returns {"sha256": ..., "bytes": ...} for what was sent."""
        ...

    def get(self, key: str) -> bytes:
        """Read bytes back; raises KeyError when the key is absent."""
        ...

    def put_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        """Store only when the key does not exist; False when already present."""
        ...

    def list_keys(self, prefix: str) -> list[dict[str, Any]]:
        """Objects under prefix: {"key", "size", "last_modified" (aware datetime)}.

        Must raise ValueError when the listing is truncated or unbounded.
        """
        ...

    def delete(self, key: str) -> None:
        """Remove a key; absent keys are ignored."""
        ...


class LocalFilesystemBackend:
    """A StorageBackend rooted at a local directory (tests, dry runs)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents and path != self.root.resolve():
            raise ValueError(f"key escapes backend root: {key}")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> dict[str, Any]:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        return {"sha256": _sha256(data), "bytes": len(data)}

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def put_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        return True

    def list_keys(self, prefix: str) -> list[dict[str, Any]]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        found = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.endswith(".tmp"):
                found.append({"key": path.relative_to(self.root).as_posix(),
                              "size": path.stat().st_size,
                              "last_modified": datetime.fromtimestamp(
                                  path.stat().st_mtime, tz=timezone.utc)})
        return found

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


class R2Backend:
    """The scanner's existing R2 client (no new credentials, host, or secrets).

    Uploads reuse ``put_bytes_verified`` (PUT + HEAD length check); the
    publisher additionally verifies every member with a full GET + SHA-256
    readback before the pointer is written.
    """

    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def put(self, key: str, data: bytes, content_type: str) -> dict[str, Any]:
        from market_data.tradier.probe import put_bytes_verified
        return put_bytes_verified(self.client, self.bucket, key, data, content_type)

    def get(self, key: str) -> bytes:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            return body.read()
        except Exception as exc:  # boto3 ClientError: NoSuchKey -> KeyError
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise KeyError(key) from None
            raise

    def get_stream(self, key: str) -> tuple[Any, int]:
        """Open a bounded streaming read: (body, content_length).

        The caller reads the body in chunks and always closes it;
        this method itself reads nothing. Added for the archive-cache
        bounded reader (archive_cache._bounded_get); get() is
        unchanged and the publisher's reads stay as they are.
        Raises KeyError when the key is absent (same mapping as get).
        A missing ContentLength is reported as -1 (unknown).
        """
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"], int(resp.get("ContentLength", -1))
        except Exception as exc:  # boto3 ClientError: NoSuchKey -> KeyError
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise KeyError(key) from None
            raise

    def put_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                   ContentType=content_type, IfNoneMatch="*")
        except Exception as exc:
            status = (getattr(exc, "response", {}) or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if status in (409, 412) or code in ("PreconditionFailed", "412"):
                return False
            raise
        return True

    def list_keys(self, prefix: str) -> list[dict[str, Any]]:
        result = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=1000)
        if result.get("IsTruncated"):
            raise ValueError("window inventory exceeds reviewed bound")
        return [{"key": obj["Key"], "size": obj["Size"], "last_modified": obj["LastModified"]}
                for obj in result.get("Contents", [])]

    def delete(self, key: str) -> None:
        result = self.client.delete_objects(
            Bucket=self.bucket, Delete={"Objects": [{"Key": key}], "Quiet": True})
        if result.get("Errors"):
            raise ValueError("window retention deletion failed")


def r2_backend() -> R2Backend:
    """Build the real backend from the scanner's existing R2 environment."""
    from market_data.options_scanner._probe import r2_client
    client, bucket = r2_client()
    return R2Backend(client, bucket)


# -- retention / budget ----------------------------------------------------
#
# Proposal (not a live approval): mirrors active_budget.py's shape with
# numbers sized for windows instead of intraday snapshots. A window
# revision reserves 20 MiB (flat members up to 9 MiB + the immutable
# revision copy + pointer/ledger); the 8 GiB global cap matches the
# scanner's reviewed bound; 90-day retention deletes expired revisions,
# flat members, pointers and ledgers together. The byte cap binds before
# retention for full-universe windows near the per-member ceilings
# (~400 revisions at the cap), so exhaustion refuses new publications
# until cleanup or an approved raise -- it never silently deletes live data.

@dataclass(frozen=True)
class WindowBudget:
    retention_days: int = 90
    max_bytes: int = 8 * 1024**3
    reservation_bytes: int = 20 * 1024**2
    max_attempts_per_revision: int = 3
    max_revisions_per_window: int = 16
    max_keys_per_prefix: int = 1000
    max_inventory_keys: int = 32000


def _inventory(backend: StorageBackend, budget: WindowBudget) -> list[dict[str, Any]]:
    """Every object under oa203/windows/, fail-closed on unknown keys."""
    objects: list[dict[str, Any]] = []
    for kind in w.KINDS:
        prefix = f"oa203/windows/{POLICY}/{kind}/"
        listed = backend.list_keys(prefix)
        if len(listed) > budget.max_keys_per_prefix:
            raise ValueError("window inventory exceeds reviewed bound")
        for obj in listed:
            if not obj["key"].startswith(prefix):
                raise ValueError("unexpected storage prefix")
            rel = obj["key"][len(prefix):]
            # rel is <window_id>/<member>; both segments must be known.
            window_id, _, member = rel.partition("/")
            if not member or not WINDOW_ID[kind].match(window_id):
                raise ValueError("unrecognized window artifact; manual reconciliation required")
            if not WINDOW_KEY.fullmatch(member):
                raise ValueError("unrecognized window artifact; manual reconciliation required")
            objects.append(obj)
        if len(objects) > budget.max_inventory_keys:
            raise ValueError("window inventory exceeds reviewed bound")
    return objects


def _is_token_key(key: str) -> bool:
    """A reservation token is <any window prefix>/budget/rev_<sha>-<1..3>.json."""
    parts = key.split("/")
    return (len(parts) == 7 and parts[0] == "oa203" and parts[1] == "windows"
            and parts[5] == "budget"
            and re.fullmatch(r"rev_[0-9a-f]{64}-[123]\.json", parts[6]) is not None)


_TOKEN_REV = re.compile(r"rev_([0-9a-f]{64})-[123]\.json\Z")


def _window_revisions(window_objects: list[dict[str, Any]], prefix: str) -> set[str]:
    """Distinct revisions for one window: immutable rev dirs plus revisions
    with a pending budget token. A token counts because its attempt will
    create at most one revision; retries of the same revision share one
    entry, so a failed attempt does not consume an extra slot for its own
    revision -- but a failed attempt's revision does occupy a slot until
    retention cleanup, which is the fail-closed side of this bound."""
    revisions: set[str] = set()
    for obj in window_objects:
        rel = obj["key"][len(prefix) + 1:]
        first, _, rest = rel.partition("/")
        match = REV_DIR.match(first)
        if match:
            revisions.add(match.group(1))
        elif first == "budget":
            token = _TOKEN_REV.match(rest)
            if token:
                revisions.add(token.group(1))
    return revisions


def _verify_reservation(backend: StorageBackend, kind: str, window_id: str, revision: str,
                        budget: WindowBudget) -> None:
    """Re-read the inventory after reserving: another publisher may have
    reserved or published while this one was deciding, so the budget and
    revision caps are checked against the inventory as it stands now, with
    this reservation counted. Raises ValueError when a cap is exceeded;
    the caller releases the token it just created. This is what makes the
    global cap hold under a race: the decision is no longer based on a
    possibly stale inventory read. It assumes the backend's listing
    reflects a just-created token (true for the local backend and for
    R2/S3 strong read-after-write consistency on list)."""
    objects = _inventory(backend, budget)
    tokens = [o for o in objects if _is_token_key(o["key"])]
    charged = sum(o["size"] for o in objects) + len(tokens) * budget.reservation_bytes
    if charged > budget.max_bytes:
        raise ValueError("window remote storage budget exhausted")
    prefix = window_prefix(kind, window_id)
    window_objects = [o for o in objects if o["key"].startswith(prefix + "/")]
    if len(_window_revisions(window_objects, prefix)) > budget.max_revisions_per_window:
        raise ValueError("window revision budget exhausted")


def clean_and_reserve(backend: StorageBackend, kind: str, window_id: str, revision: str,
                      budget: WindowBudget, now: datetime, dry_run: bool = False) -> dict[str, Any]:
    """Delete expired windows, charge the budget, and reserve one attempt.

    The reservation token is conditional (put-if-absent), so a retry after a
    crash cannot double-charge the same attempt slot. A dry run reports
    what would be deleted and reserved without changing anything.
    """
    prefix = window_prefix(kind, window_id)
    objects = _inventory(backend, budget)
    cutoff = now - timedelta(days=budget.retention_days)
    expired = [o for o in objects if o["last_modified"] < cutoff]
    if expired and not dry_run:
        for obj in expired:
            backend.delete(obj["key"])
        remaining = _inventory(backend, budget)
        if any(o["key"] in {x["key"] for x in expired} for o in remaining):
            raise ValueError("window retention deletion not verified")
        objects = remaining
    tokens = [o for o in objects if _is_token_key(o["key"])]
    charged = sum(o["size"] for o in objects) + len(tokens) * budget.reservation_bytes
    if charged + budget.reservation_bytes > budget.max_bytes:
        raise ValueError("window remote storage budget exhausted")
    window_objects = [o for o in objects if o["key"].startswith(prefix + "/")]
    revisions = _window_revisions(window_objects, prefix)
    if revision.removeprefix("rev_") not in revisions and len(revisions) >= budget.max_revisions_per_window:
        raise ValueError("window revision budget exhausted")
    if dry_run:
        return {"reservation": None, "attempt": None, "retention_days": budget.retention_days,
                "charged_bytes_before": charged, "max_bytes": budget.max_bytes,
                "expired_objects": len(expired), "would_delete": [o["key"] for o in expired]}
    for attempt in range(1, budget.max_attempts_per_revision + 1):
        key = f"{prefix}/budget/{revision}-{attempt}.json"
        body = (json.dumps({"schema_version": 1, "kind": kind, "window_id": window_id,
                            "revision": revision, "reserved_at": now.isoformat(),
                            "reserved_bytes": budget.reservation_bytes},
                           sort_keys=True) + "\n").encode()
        if backend.put_if_absent(key, body, "application/json"):
            try:
                _verify_reservation(backend, kind, window_id, revision, budget)
            except ValueError:
                try:
                    backend.delete(key)
                except Exception:
                    pass
                raise
            return {"reservation": key, "attempt": attempt, "retention_days": budget.retention_days,
                    "charged_bytes_before": charged, "max_bytes": budget.max_bytes,
                    "expired_objects": len(expired)}
    raise ValueError("window remote retry budget exhausted")


# -- publication -----------------------------------------------------------


def _verify_readback(backend: StorageBackend, key: str, expected_sha256: str) -> None:
    try:
        body = backend.get(key)
    except KeyError:
        raise ValueError(f"published member is missing on readback: {key}") from None
    if _sha256(body) != expected_sha256:
        raise ValueError(f"readback digest mismatch for {key}; pointer not published")


def _read_pointer(backend: StorageBackend, prefix: str) -> dict[str, Any] | None:
    try:
        raw = backend.get(f"{prefix}/latest.json")
    except KeyError:
        return None
    try:
        pointer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("published window pointer is damaged") from None
    if (not isinstance(pointer, dict) or pointer.get("schema_version") != 1
            or not isinstance(pointer.get("revision"), str)
            or not isinstance(pointer.get("members"), dict)):
        raise ValueError("published window pointer is damaged")
    return pointer


def _write_attempt(backend: StorageBackend, prefix: str, validated: ValidatedWindow,
                   status: str, reason: str | None, attempt: int | None, now: datetime) -> None:
    payload = {"schema_version": 1, "kind": validated.kind, "window_id": validated.window_id,
               "revision": validated.revision, "state": validated.state, "status": status,
               "reason": reason, "attempt": attempt, "at": now.isoformat()}
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    backend.put(f"{prefix}/latest-attempt.json", body, "application/json")
    _verify_readback(backend, f"{prefix}/latest-attempt.json", _sha256(body))


def publish_window(source_root: Path, kind: str, window_id: str, backend: StorageBackend,
                   *, budget: WindowBudget | None = None, now: datetime | None = None,
                   dry_run: bool = False) -> dict[str, Any]:
    """Validate and publish one local window. Returns the pointer payload.

    Raises ValueError before any upload when the window is invalid, the
    budget is exhausted, the inventory is truncated or holds unknown keys,
    or a readback digest mismatches. A failed attempt after reservation is
    recorded in latest-attempt.json (best effort); a validation refusal
    uploads nothing at all, including no ledger entry.
    """
    budget = budget or WindowBudget()
    now = now or datetime.now(timezone.utc)
    validate_window_identity(kind, window_id)
    window_dir = Path(source_root) / "oa203" / "windows" / POLICY / kind / window_id
    validated = load_window(window_dir)
    prefix = validated.prefix
    pointer = _read_pointer(backend, prefix)
    if pointer is not None and pointer["revision"] == validated.revision:
        if pointer["members"] != validated.member_sha256:
            raise ValueError("published window pointer digest changed")
        for name, digest in validated.member_sha256.items():
            _verify_readback(backend, f"{prefix}/{validated.revision}/{name}", digest)
        return {"status": "already_published", "kind": kind, "window_id": window_id,
                "state": validated.state, "revision": validated.revision, "prefix": prefix,
                "members": dict(validated.member_sha256), "pointer": pointer, "attempt": None}
    if dry_run:
        report = clean_and_reserve(backend, kind, window_id, validated.revision, budget, now,
                                   dry_run=True)
        return {"status": "dry_run", "kind": kind, "window_id": window_id,
                "state": validated.state, "revision": validated.revision, "prefix": prefix,
                "members": dict(validated.member_sha256), "pointer": None,
                "budget": report, "attempt": None}
    reservation = clean_and_reserve(backend, kind, window_id, validated.revision, budget, now)
    attempt = reservation["attempt"]
    try:
        # Immutable revision members first, then the flat consumer keys
        # (paths, rankings, summary last), then the pointer last.
        for name in WINDOW_MEMBERS:
            data = validated.member_bytes[name]
            key = f"{prefix}/{validated.revision}/{name}"
            backend.put(key, data, _content_type(name))
            _verify_readback(backend, key, validated.member_sha256[name])
        for name in FLAT_PUBLISH_ORDER:
            key = f"{prefix}/{name}"
            backend.put(key, validated.member_bytes[name], _content_type(name))
            _verify_readback(backend, key, validated.member_sha256[name])
        published_at = now.isoformat()
        new_pointer = {"schema_version": 1, "publisher": "windows_publish/1",
                       "policy": POLICY, "kind": kind, "window_id": window_id,
                       "state": validated.state, "state_reasons": validated.summary["state_reasons"],
                       "revision": validated.revision, "members": dict(validated.member_sha256),
                       "generated_at": validated.summary["generated_at"],
                       "published_at": published_at,
                       "previous_published_at": pointer.get("published_at") if pointer else None,
                       "budget": {"reservation": reservation["reservation"],
                                  "retention_days": budget.retention_days,
                                  "max_bytes": budget.max_bytes}}
        body = (json.dumps(new_pointer, indent=2, sort_keys=True) + "\n").encode()
        backend.put(f"{prefix}/latest.json", body, "application/json")
        _verify_readback(backend, f"{prefix}/latest.json", _sha256(body))
    except Exception as exc:
        try:
            _write_attempt(backend, prefix, validated, "failed", str(exc)[:300], attempt, now)
        except Exception:
            pass
        raise
    ledger_error: str | None = None
    try:
        _write_attempt(backend, prefix, validated, "published", None, attempt, now)
    except Exception as exc:  # the publication stands; the ledger is a record, not the gate
        ledger_error = str(exc)[:300]
    result: dict[str, Any] = {"status": "published", "kind": kind, "window_id": window_id,
                              "state": validated.state, "revision": validated.revision,
                              "prefix": prefix, "members": dict(validated.member_sha256),
                              "pointer": new_pointer, "attempt": attempt,
                              "reservation": reservation["reservation"]}
    if ledger_error is not None:
        result["attempt_ledger_error"] = ledger_error
    return result
