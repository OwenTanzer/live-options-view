"""Python binding of the One Clock envelope/selection contract.

No transport, timer, storage or player clock lives here. Trainer retains its
forward-only clock and passes a cutoff; the same fixtures exercise the JS core.
"""
from __future__ import annotations

from copy import deepcopy
from bisect import bisect_right
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import json
import math
import heapq
import hashlib
import re

ORIGINS = {"recorded", "reconstructed", "synthetic"}


def instant(value):
    if type(value) is int and 10**12 <= value <= 2**53 - 1:
        return value
    if isinstance(value, str) and re.search(r'T.*(?:Z|[+-]\d{2}:\d{2})$',value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            delta=parsed-datetime(1970,1,1,tzinfo=timezone.utc)
            return delta.days*86400000+delta.seconds*1000+delta.microseconds//1000
    raise ValueError("time must be epoch milliseconds or offset-qualified ISO")


def observation(value):
    row = deepcopy(value)
    for key in ("source", "id", "instrument", "session", "evidence"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"{key} is required")
    if row.get("origin") not in ORIGINS or type(row.get("revision")) is not int or not 1 <= row["revision"] <= 2**53 - 1:
        raise ValueError("explicit origin and positive integer revision required")
    row["eventAt"] = instant(row["eventAt"])
    row["availableAt"] = None if row.get("availableAt") is None else instant(row["availableAt"])
    if row["availableAt"] is not None and row["availableAt"] < row["eventAt"]:
        raise ValueError("availability precedes observation")
    if "value" not in row:
        raise ValueError("value is required")
    return row


def identity(row):
    return tuple(row[k] for k in ("source", "session", "instrument", "origin", "id"))


def _key(row):
    # Same deterministic UTF-16 code-unit ordering used by the JS core.
    return json.dumps(identity(row), ensure_ascii=False, separators=(",", ":")).encode("utf-16-be")


def _apply(row, identities, seen):
    key = identity(row)
    revision = key, row["revision"]
    if revision in seen and _canonical(seen[revision]) != _canonical(row):
        raise ValueError("conflicting immutable observation revision")
    seen[revision] = row
    if key not in identities or row["revision"] > identities[key]["revision"]:
        identities[key] = row


def _canonical(value):
    if value is None or isinstance(value,float) and not math.isfinite(value):
        return ('null',)
    if isinstance(value,bool):
        return ('boolean',value)
    if isinstance(value,(int,float)):
        return ('number',value)
    if isinstance(value,str):
        return ('string',value)
    if isinstance(value,list):
        return ('array',tuple(_canonical(v) for v in value))
    return ('object',tuple((k,_canonical(v)) for k,v in sorted(value.items())))


def _ordered(rows):
    return tuple(sorted(rows, key=lambda r: (r["eventAt"], r["availableAt"], _key(r))))


class _Latest:
    def __init__(self,row):
        self.row=row
        self.rank=(row['eventAt'],row['availableAt'],_key(row))

    def __lt__(self,other):
        return self.rank>other.rank


def select_as_of(inputs, selection):
    cutoff = instant(selection["cutoff"])
    origin = selection.get("origin", "recorded")
    if origin not in ORIGINS or any(not isinstance(selection.get(k), str) or not selection[k].strip() for k in ("session", "instrument")):
        raise ValueError("invalid scope")
    identities, seen = {}, {}
    for value in inputs:
        row = observation(value)
        if any(row[k] != selection[k] for k in ("session", "instrument")) or row["origin"] != origin:
            continue
        if row["eventAt"] > cutoff or row["availableAt"] is None or row["availableAt"] > cutoff:
            continue
        _apply(row, identities, seen)
    return _ordered(identities.values())


def reading_as_of(inputs,selection,max_age_ms):
    if type(max_age_ms) is not int or max_age_ms<0:
        raise ValueError('explicit nonnegative max age required')
    rows=select_as_of(inputs,selection)
    if not rows:
        return dict(state='unavailable',row=None,ageMs=None,publicationAgeMs=None)
    row=rows[-1]
    age=instant(selection['cutoff'])-row['eventAt']
    return dict(state='unavailable' if row['value'] is None else 'stale' if age>max_age_ms else 'fresh',
        row=row,ageMs=age,publicationAgeMs=instant(selection['cutoff'])-row['availableAt'])


class TrainerSource:
    """Adapt an immutable server-side pack without disclosing its date/keys.

    Legacy historical packs explicitly reconstruct availability at each snapshot
    boundary; they are never labelled recorded publication evidence. An explicit
    available_s=None stays unavailable. Simulations remain a separate cohort.
    """
    def __init__(self, pack, instrument):
        self.session, self.instrument = pack["day"], instrument
        self.origin = pack.get("clock_origin", "synthetic" if pack.get("mode") == "simulation" else "reconstructed")
        if self.origin not in ORIGINS or self.session.startswith('SIM:') and self.origin!='synthetic':
            raise ValueError('invalid Trainer evidence cohort')
        self.basis = "explicit-publication" if self.origin == "recorded" else "modelled-availability"
        # A private coordinate origin, not an actual date for synthetic packs.
        self.base = 1_700_000_000_000
        if not self.session.startswith('SIM:'):
            self.base = round(datetime.fromisoformat(self.session).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()*1000)
        self.rows, ready = [], set()
        payload_indices = {}
        payload_digests = {}
        for i, snap in enumerate(pack["snapshots"]):
            # A repeated immutable payload has one representative index. Pack
            # order is not payload identity; content_t is a full-pack derivation.
            payload = _canonical({k:v for k,v in snap.items() if k != 'content_t'})
            payload_index = payload_indices.setdefault(payload, i)
            if payload_index == i:
                payload_digests[i] = hashlib.sha256(repr(payload).encode()).hexdigest()
            at = snap["t"]
            available = snap.get("available_s", None if self.origin == "recorded" else at)
            row = observation(dict(source="trainer-chain", id=snap.get("observation_id", snap.get("key", str(i))),
                revision=snap.get("revision", 1), session=self.session, instrument=instrument, origin=self.origin,
                evidence=snap.get("key", f"generated:{snap.get('observation_id',i)}"), eventAt=self.base+round(at*1000),
                availableAt=None if available is None else self.base+round(available*1000),
                payloadDigest=payload_digests[payload_index],
                value=None if snap.get("retracted") else payload_index))
            self.rows.append(row)
            if available is not None:
                ready.add(math.ceil(available))
        self.ready = sorted(ready)
        # Compile the immutable pack once. Views and dark-stretch boundaries
        # use binary search, not a whole-pack selection per displayed second.
        self.boundaries, self.latest_rows, self.content_times, self.conflict_at = [], [], [], None
        identities, seen = {}, {}
        pending = sorted((row for row in self.rows if row['availableAt'] is not None),key=lambda row:row['availableAt'])
        # Segment tree over event order: each node describes its active suffix
        # of identical content. Corrections remove the old revision first. This
        # derives frozen age from eligible evidence in O(log n) per publication,
        # without retaining every prefix or trusting archive-wide content_t.
        ordered = sorted(pending,key=lambda r:(r['eventAt'],r['availableAt'],_key(r)))
        slots = {id(row):i for i,row in enumerate(ordered)}
        size = 1 << max(0,(len(ordered)-1).bit_length())
        tree = [None] * (2*size)
        def update(row, active):
            at = size + slots[id(row)]
            snap = pack['snapshots'][row['value']] if row['value'] is not None else None
            sig = _canonical([snap.get('spot'),snap.get('q')]) if snap is not None else ('retracted',identity(row))
            tree[at] = (sig,True,row['eventAt']) if active else None
            while at > 1:
                at //= 2
                left,right = tree[at*2:at*2+2]
                if left is None or right is None:
                    tree[at] = left if right is None else right
                else:
                    joins = right[1] and left[0] == right[0]
                    tree[at] = (right[0],left[1] and joins,left[2] if joins else right[2])
        heap=[]
        for i,row in enumerate(pending):
            previous = identities.get(identity(row))
            try:
                _apply(row, identities, seen)
            except ValueError:
                self.conflict_at = row['availableAt']
                break
            if identities[identity(row)] is row:
                if previous is not None:
                    update(previous,False)
                update(row,True)
            heapq.heappush(heap,_Latest(row))
            while heap and identities[identity(heap[0].row)] is not heap[0].row:
                heapq.heappop(heap)
            if i+1 == len(pending) or pending[i+1]['availableAt'] != row['availableAt']:
                self.boundaries.append(row['availableAt'])
                self.latest_rows.append(heap[0].row)
                self.content_times.append((tree[1][2]-self.base)/1000)
        self._last_cutoff,self._last_rows=None,()

    def latest(self,seconds):
        cutoff=self.base+round(seconds*1000)
        if self.conflict_at is not None and cutoff >= self.conflict_at:
            raise ValueError('conflicting immutable observation revision')
        i=bisect_right(self.boundaries,cutoff)-1
        return self.latest_rows[i] if i>=0 else None

    def at(self, seconds):
        cutoff=self.base+round(seconds*1000)
        if cutoff!=self._last_cutoff:
            self._last_rows=select_as_of(self.rows,dict(session=self.session,instrument=self.instrument,origin=self.origin,cutoff=cutoff))
            self._last_cutoff=cutoff
        return self._last_rows

    def content_time(self, seconds):
        self.latest(seconds)  # retain the same conflict gate
        i=bisect_right(self.boundaries,self.base+round(seconds*1000))-1
        return self.content_times[i] if i>=0 else None
