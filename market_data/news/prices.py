"""Read-only price records for the news/price join (M3).

Every record carries two times:

  price_time    when the price applies at the source: `quote_ts` for a quote,
                `bar_end` for a bar (a bar is not a price until it has closed)
  available_at  when our system had the record (`collector_received_at` for a
                quote; for a bar, when it was finalized, never before `bar_end`)

The join only ever uses a record at or after its `available_at`. Underlying
quotes, option quotes and bars are separate kinds and are never mixed: an
option quote is not evidence of the underlying price.

This module reads local JSONL files only. M3 uses the synthetic QQQ fixture in
tests/fixtures/news/prices/; real price paths and hashes are pinned later.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .normalize import parse_iso

KINDS = ("underlying_quote", "option_quote", "bar")
SESSIONS = ("pre", "regular", "post")
GRANULARITIES = {"underlying_quote": ("tick",), "option_quote": ("tick",), "bar": ("1m", "1d")}
HEADER_KIND = "fixture_header"


class PriceRecordError(ValueError):
    pass


@dataclass(frozen=True)
class PriceRecord:
    record_id: str
    record_kind: str
    instrument_id: str
    underlying_instrument_id: str | None
    granularity: str
    price: float
    price_basis: str
    session: str
    price_time: datetime
    available_at: datetime
    source: str
    synthetic: bool
    raw: dict = field(repr=False, compare=False, hash=False)

    def summary(self) -> dict:
        r = self.raw
        out = {"record_id": self.record_id, "record_kind": self.record_kind, "instrument_id": self.instrument_id,
               "granularity": self.granularity, "price": self.price, "price_basis": self.price_basis,
               "session": self.session, "available_at": r["available_at"], "source": self.source,
               "synthetic": self.synthetic}
        if self.record_kind == "bar":
            out.update(bar_start=r["bar_start"], bar_end=r["bar_end"])
        else:
            out.update(quote_ts=r["quote_ts"], collector_received_at=r["collector_received_at"])
        return out


@dataclass
class PriceFile:
    path: str
    sha256: str
    header: dict | None
    records: list[PriceRecord]


def _time(rec: dict, key: str) -> datetime:
    value = rec.get(key)
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PriceRecordError(f"{rec.get('record_id')}: {key} must be an ISO UTC time ending in Z, got {value!r}")
    t = parse_iso(value)
    if t is None:
        raise PriceRecordError(f"{rec.get('record_id')}: {key} is not a time: {value!r}")
    return t


def parse_record(rec: dict) -> PriceRecord:
    rid = rec.get("record_id")
    if not rid:
        raise PriceRecordError(f"record without record_id: {rec}")
    kind = rec.get("record_kind")
    if kind not in KINDS:
        raise PriceRecordError(f"{rid}: unknown record_kind {kind!r}")
    gran = rec.get("granularity")
    if gran not in GRANULARITIES[kind]:
        raise PriceRecordError(f"{rid}: granularity {gran!r} not valid for {kind}")
    if rec.get("session") not in SESSIONS:
        raise PriceRecordError(f"{rid}: session must be one of {SESSIONS}")
    if not isinstance(rec.get("price"), (int, float)) or isinstance(rec.get("price"), bool):
        raise PriceRecordError(f"{rid}: price must be a number")
    if not math.isfinite(rec["price"]):
        raise PriceRecordError(f"{rid}: price must be finite (NaN and infinities are rejected)")
    if not rec.get("instrument_id") or not rec.get("price_basis"):
        raise PriceRecordError(f"{rid}: instrument_id and price_basis are required")
    available = _time(rec, "available_at")
    if kind == "bar":
        start, end = _time(rec, "bar_start"), _time(rec, "bar_end")
        if end <= start:
            raise PriceRecordError(f"{rid}: bar_end must be after bar_start")
        if available < end:
            raise PriceRecordError(f"{rid}: a bar cannot be available before it closes")
        price_time = end
    else:
        price_time = _time(rec, "quote_ts")
        if _time(rec, "collector_received_at") != available:
            raise PriceRecordError(f"{rid}: a quote's available_at is its collector_received_at")
        if available < price_time:
            raise PriceRecordError(f"{rid}: received before its quote time")
    underlying = rec.get("underlying_instrument_id")
    if kind == "option_quote" and not underlying:
        raise PriceRecordError(f"{rid}: an option quote names its underlying_instrument_id")
    if kind != "option_quote" and underlying:
        raise PriceRecordError(f"{rid}: only option quotes have an underlying_instrument_id")
    return PriceRecord(rid, kind, rec["instrument_id"], underlying, gran, float(rec["price"]), rec["price_basis"],
                       rec["session"], price_time, available, rec.get("source") or "", rec.get("synthetic") is True,
                       rec)


def load_price_file(path) -> PriceFile:
    path = Path(path)
    data = path.read_bytes()
    header, records, seen = None, [], set()
    for n, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PriceRecordError(f"{path.name}:{n}: {exc}") from exc
        if rec.get("record_kind") == HEADER_KIND:
            if header is not None or records:
                raise PriceRecordError(f"{path.name}:{n}: the header must be the first record")
            header = rec
            continue
        r = parse_record(rec)
        if r.record_id in seen:
            raise PriceRecordError(f"{path.name}:{n}: duplicate record_id {r.record_id}")
        seen.add(r.record_id)
        records.append(r)
    return PriceFile(str(path), hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest(), header, records)
