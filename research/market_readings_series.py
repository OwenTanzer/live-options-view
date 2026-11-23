#!/usr/bin/env python3
"""Rebuild the reference market-reading series from archived snapshot sidecars.

The collector writes `market_readings` (and the older `market` block) into each
snapshot's `snapshot_provenance.v1` sidecar. That makes a durable per-snapshot
series keyed by the exact CSV key, without adding a read-modify-write object to
the collection loop. This reader turns a tree of sidecars -- or a listing plus
the public origin -- into tidy rows for replay: one row per reading per
snapshot, carrying units, parameters, status, freshness, observation and
publication times.

It reads both the current `market_readings` list and the older `market` block:
a sidecar written before `market_reading.v1` existed yields one explicitly
labelled `legacy_underlying_market` row instead of pretending it had readings.
A digest mismatch is reported per snapshot, never silently repaired.

    python research/market_readings_series.py --dir research/archive_evidence
    python research/market_readings_series.py --listing keys.json --origin https://...
    python research/market_readings_series.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from market_data.overview.readings import readings_digest  # noqa: E402


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sidecar_from_key(origin: str, key: str) -> dict | None:
    key = key[:-4] + ".provenance.json" if key.endswith(".csv") else key
    try:
        with urllib.request.urlopen(f"{origin}/{key}", timeout=40) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None


def _rows(sidecar: dict, digest_status: str) -> list[dict]:
    snapshot_key = sidecar.get("snapshot_key")
    readings = sidecar.get("market_readings")
    base = {
        "snapshot_key": snapshot_key,
        "symbol": sidecar.get("symbol"),
        "digest_status": digest_status,
        "publication": sidecar.get("publication"),
        "published_at": sidecar.get("latest_published_at"),
        "snapshot_constructed_at": sidecar.get("constructed_at"),
    }
    queued = sidecar.get("market_readings_availability_basis") == "confirmed_latest_publication"
    confirmed = (sidecar.get("latest_published_at")
                 if (sidecar.get("publication") or {}).get("status") == "published" else None)
    rows = []
    if isinstance(readings, list) and readings:
        for reading in readings:
            rows.append({
                **base,
                "reading_id": reading.get("reading_id"),
                "schema_version": reading.get("schema_version"),
                "calculation_version": (reading.get("calculation") or {}).get("version"),
                "status": (reading.get("status") or {}).get("code"),
                "status_reason": (reading.get("status") or {}).get("reason"),
                "freshness": (reading.get("freshness") or {}).get("status"),
                "observed_at": reading.get("observed_at"),
                "calculated_at": reading.get("calculated_at"),
                "reading_published_at": confirmed if queued else reading.get("published_at"),
                "values": reading.get("values"),
                "units": reading.get("units"),
                "parameters": (reading.get("calculation") or {}).get("parameters"),
                "caveats": reading.get("caveats"),
                "coverage": reading.get("coverage"),
            })
        return rows
    # Older sidecars: preserve the durable market block, but label it honestly.
    market = sidecar.get("market")
    if not isinstance(market, dict):
        return []
    return [{
        **base,
        "reading_id": "legacy_underlying_market",
        "schema_version": "pre-market_reading.v1",
        "calculation_version": None,
        "status": market.get("freshness"),
        "status_reason": "sidecar predates market_readings",
        "freshness": market.get("freshness"),
        "observed_at": market.get("spot_ts"),
        "calculated_at": sidecar.get("constructed_at"),
        "reading_published_at": None,
        "values": {k: market.get(k) for k in ("spot", "vwap", "rvol", "momentum")},
        "units": None,
        "parameters": None,
        "caveats": ["legacy_market_block_only"],
        "coverage": {"partial": market.get("vwap_partial_session")},
    }]


def collect(sidecars: list[tuple[str, dict]]) -> list[dict]:
    rows = []
    for key, sidecar in sidecars:
        published_digest = sidecar.get("market_readings_digest")
        readings = sidecar.get("market_readings")
        if isinstance(readings, list) and readings:
            status = "ok" if published_digest == readings_digest(readings) else "digest_mismatch"
        else:
            status = "legacy_no_readings" if published_digest is None else "missing_readings"
        rows.extend(_rows(sidecar, status))
    return rows


def from_directory(root: Path) -> list[tuple[str, dict]]:
    found = []
    for path in sorted(root.rglob("*.provenance.json")):
        sidecar = _read(path)
        if sidecar is not None:
            found.append((str(path), sidecar))
    return found


def from_listing(listing: Path, origin: str) -> list[tuple[str, dict]]:
    payload = json.loads(listing.read_text(encoding="utf-8"))
    entries = payload.get("Contents", payload) if isinstance(payload, dict) else payload
    found = []
    for entry in entries:
        key = entry.get("Key") or entry.get("key")
        if not key:
            continue
        sidecar = _sidecar_from_key(origin, key)
        if sidecar is not None:
            found.append((key, sidecar))
    return found


def _write(rows: list[dict], target: Path) -> None:
    if target.suffix.lower() == ".csv":
        fields = list(rows[0].keys()) if rows else [
            "snapshot_key", "reading_id", "status", "values", "parameters"]
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v)
                                 for k, v in row.items()})
        return
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reading = {
            "reading_id": "reference_pcr", "schema_version": "market_reading.v1",
            "observed_at": None, "calculated_at": "2026-11-09T15:00:00+00:00",
            "published_at": None, "status": {"code": "ok", "reason": None},
            "freshness": {"status": "live"},
            "calculation": {"version": "reference_pcr.v1", "parameters": {"formula": "x"}},
            "values": {"put_call_oi_ratio": 1.2}, "units": {"put_call_oi_ratio": "dimensionless"},
            "caveats": ["oi_zero_or_missing_indistinguishable"], "coverage": {"partial": False},
        }
        good = {
            "schema_version": "snapshot_provenance.v1", "snapshot_key": "intraday/20261109/snapshot_1.csv",
            "symbol": "QQQ", "constructed_at": "2026-11-09T15:00:00+00:00",
            "publication": {"status": "published"}, "latest_published_at": "2026-11-09T15:00:01+00:00",
            "market_readings": [reading], "market_readings_digest": readings_digest([reading]),
        }
        legacy = {
            "schema_version": "snapshot_provenance.v1", "snapshot_key": "intraday/20261109/snapshot_2.csv",
            "symbol": "QQQ", "constructed_at": "2026-11-09T15:01:00+00:00",
            "publication": {"status": "archived_unpublished"},
            "market": {"spot": 600.1, "spot_ts": "2026-11-09T15:00:59+00:00", "vwap": 599.9,
                       "rvol": {"status": "ok"}, "momentum": {"status": "ok"},
                       "vwap_partial_session": False, "freshness": "live"},
        }
        tampered = {**good, "snapshot_key": "intraday/20261109/snapshot_3.csv",
                    "market_readings_digest": "0" * 64}
        (root / "a.provenance.json").write_text(json.dumps(good))
        (root / "b.provenance.json").write_text(json.dumps(legacy))
        (root / "c.provenance.json").write_text(json.dumps(tampered))
        rows = collect(from_directory(root))
        assert len(rows) == 3, rows
        by_key = {r["snapshot_key"]: r for r in rows}
        assert by_key["intraday/20261109/snapshot_1.csv"]["digest_status"] == "ok", rows
        legacy_row = by_key["intraday/20261109/snapshot_2.csv"]
        assert legacy_row["status"] == "live", rows
        assert "legacy_market_block_only" in legacy_row["caveats"], rows
        assert by_key["intraday/20261109/snapshot_3.csv"]["digest_status"] == "digest_mismatch", rows
        out = root / "series.jsonl"
        _write(rows, out)
        assert len(out.read_text().splitlines()) == 3
        csv_out = root / "series.csv"
        _write(rows, csv_out)
        assert "reading_id" in csv_out.read_text().splitlines()[0]
    print("market readings series self-test passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="tree containing *.provenance.json sidecars")
    ap.add_argument("--listing", help="JSON list of snapshot keys (S3/R2 listing)")
    ap.add_argument("--origin", default="https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev")
    ap.add_argument("--out", default="market_readings_series.jsonl")
    ap.add_argument("--strict", action="store_true", help="exit non-zero on any digest mismatch")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.dir:
        sidecars = from_directory(Path(args.dir))
    elif args.listing:
        sidecars = from_listing(Path(args.listing), args.origin)
    else:
        ap.error("one of --dir, --listing or --self-test is required")
    rows = collect(sidecars)
    _write(rows, Path(args.out))
    mismatches = sum(1 for r in rows if r["digest_status"] == "digest_mismatch")
    print(f"{len(sidecars)} sidecars -> {len(rows)} reading rows -> {args.out}")
    print(f"digest mismatches: {mismatches}")
    return 1 if args.strict and mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
