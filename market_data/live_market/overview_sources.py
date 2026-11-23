"""Bounded source discovery and official daily Treasury observations."""
import threading
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import requests

from market_data.overview import INSTRUMENTS
from .config import (
    CONTRACT_RESOLVE_RETRY_MAX_SECS, CONTRACT_RESOLVE_RETRY_MIN_SECS,
    PRICE_TICKERS, TASTY_BASE, log,
)
from .oauth import _bearer_headers

RESOLVED_CONTRACTS = {}
_RESOLVE_LOCK = threading.Lock()
_RESOLVED_FOR_DATE = None
_RESOLVE_ATTEMPTS = 0
_RESOLVE_RETRY_AT = None
_RESOLVE_STATE = {
    "state": "never_attempted", "last_attempt_at": None, "last_success_at": None,
    "last_error": None, "attempts": 0, "retry_at": None, "carried": [],
}
TREASURY_URL = 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml'


def _resolution_failed(reason, now):
    """Record a resolution gap without touching the published/subscribed map."""
    global _RESOLVE_ATTEMPTS, _RESOLVE_RETRY_AT
    _RESOLVE_ATTEMPTS += 1
    delay = min(CONTRACT_RESOLVE_RETRY_MAX_SECS,
                CONTRACT_RESOLVE_RETRY_MIN_SECS * (2 ** (_RESOLVE_ATTEMPTS - 1)))
    _RESOLVE_RETRY_AT = now + timedelta(seconds=delay)
    _RESOLVE_STATE.update(
        state="degraded", last_error=reason, attempts=_RESOLVE_ATTEMPTS,
        last_attempt_at=now.isoformat(), retry_at=_RESOLVE_RETRY_AT.isoformat(),
        carried=[])


def resolution_status():
    """Resolution state for the lane health record (never mutates anything)."""
    with _RESOLVE_LOCK:
        return {
            **_RESOLVE_STATE,
            "resolved_for_date": _RESOLVED_FOR_DATE,
            "labels": {label: contract.get("symbol")
                       for label, contract in RESOLVED_CONTRACTS.items()},
        }


def resolve_futures(access_token, *, force=False, now=None):
    """Resolve provider-designated active months; never discard a good mapping.

    The provider is the only authority for the active month. A request that
    fails, returns no items, or leaves a product without an eligible active
    contract is a resolution gap: the last good contract stays published and
    subscribed, and the retry is bounded. Only a genuine provider answer
    replaces the shared mapping. Clearing it first -- the November 9 defect --
    unsubscribed every futures symbol while the market was still open.
    """
    global _RESOLVED_FOR_DATE, _RESOLVE_ATTEMPTS, _RESOLVE_RETRY_AT
    now = now or datetime.now(timezone.utc)
    futures = [r for r in INSTRUMENTS if r['profile'] == 'FU' and r['enabled']]
    today = now.date().isoformat()
    with _RESOLVE_LOCK:
        if not force and _RESOLVED_FOR_DATE == today and RESOLVED_CONTRACTS:
            return dict(RESOLVED_CONTRACTS)
        if (not force and _RESOLVE_RETRY_AT is not None
                and now < _RESOLVE_RETRY_AT and RESOLVED_CONTRACTS):
            return dict(RESOLVED_CONTRACTS)
        _RESOLVE_STATE['last_attempt_at'] = now.isoformat()
        try:
            response = requests.get(TASTY_BASE + '/instruments/futures',
                params={'product-code[]': [r['id'][4:].upper() for r in futures]},
                headers=_bearer_headers(access_token), timeout=15)
            response.raise_for_status()
            items = response.json()['data']['items']
            if not isinstance(items, list) or not items:
                raise ValueError('empty_futures_instrument_response')
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            reason = f'auth_rejected_{status}' if status in (401, 403) else type(exc).__name__
            _resolution_failed(reason, now)
            log.warning('overview futures discovery unavailable; keeping last known contracts')
            return dict(RESOLVED_CONTRACTS)
        resolved = {}
        carried = []
        unresolved = []
        for r in futures:
            root = r['id'][4:].upper()
            candidates = [i for i in items if i.get('active-month') and i.get('active')
                and not i.get('is-closing-only') and i.get('expiration-date', '') >= today
                and i.get('future-product', {}).get('code') == root]
            symbol = candidates[0].get('streamer-symbol') if len(candidates) == 1 else None
            if symbol and ':' in symbol:
                i = candidates[0]
                resolved[r['label']] = {'symbol': symbol, 'root': root,
                    'expiry': i['expiration-date'], 'exchange': symbol.split(':', 1)[1],
                    'selection': 'provider_active_month'}
                continue
            prior = RESOLVED_CONTRACTS.get(r['label'])
            if prior and prior.get('expiry', '') >= today:
                resolved[r['label']] = dict(prior)
                carried.append(r['label'])
            else:
                unresolved.append(root)
        if not resolved:
            _resolution_failed('no_active_contracts', now)
            return dict(RESOLVED_CONTRACTS)
        # Commit atomically, and only after a usable answer exists.
        RESOLVED_CONTRACTS.clear()
        RESOLVED_CONTRACTS.update(resolved)
        for r in futures:
            contract = resolved.get(r['label'])
            PRICE_TICKERS[r['label']] = contract['symbol'] if contract else None
        _RESOLVED_FOR_DATE = today
        _RESOLVE_ATTEMPTS = 0
        _RESOLVE_RETRY_AT = None
        _RESOLVE_STATE.update(
            state='ok' if not unresolved and not carried else 'partial',
            last_success_at=now.isoformat(), last_error=None, attempts=0,
            retry_at=None, carried=carried)
        if carried or unresolved:
            log.warning(f"overview futures resolution partial: carried={carried} unresolved={unresolved}")
        return dict(RESOLVED_CONTRACTS)


def futures_snapshot():
    """Atomic label->streamer-symbol view of the shared futures mapping.

    The price lane compares this each cycle so a chain-session resolution (or a
    daily roll) is reconciled into its own subscription instead of being read
    only at authentication.
    """
    with _RESOLVE_LOCK:
        return {r['label']: PRICE_TICKERS.get(r['label'])
                for r in INSTRUMENTS if r['profile'] == 'FU' and r['enabled']}


def treasury_observation(xml, now=None):
    """2-year constant-maturity yield, percent; official dated observation, not a trade."""
    now = now or datetime.now(timezone.utc).isoformat()
    values = {}
    for node in ElementTree.fromstring(xml).iter():
        if node.tag.split('}')[-1] != 'properties':
            continue
        fields = {c.tag.split('}')[-1]: c.text for c in node}
        try:
            date = datetime.fromisoformat(fields['NEW_DATE']).date().isoformat()
            value = float(fields['BC_2YEAR'])
            import math
            if math.isfinite(value):
                values[date] = value
        except (TypeError, ValueError, KeyError):
            continue
    ordered = sorted(values.items())
    if not ordered:
        raise ValueError('no dated Treasury observations')
    def pair(item):
        date, value = item
        return {'instrument_id': 'yld.us02y.cmt', 'value': value, 'source': 'treasury',
            'source_symbol': 'BC_2YEAR', 'price_basis': 'daily_close',
            'observed_at': None, 'observation_time_reason': 'official_daily_series_has_no_intraday_time',
            'observation_date': date, 'observation_timezone': 'America/New_York',
            'observation_granularity': 'day', 'session': 'daily',
            'adjustment_basis': 'unadjusted', 'resolved_contract': None}
    row = pair(ordered[-1])
    row['price'] = row.pop('value')
    row.update(bid=None, ask=None, volume=None, prev_close=None, quote_ts=None,
        collector_received_at=None, acquired_at=now, last_success_at=now,
        last_attempt_at=now, refresh_error=None,
        reference=pair(ordered[-2]) if len(ordered)>1 else None)
    return row


def fetch_treasury():
    now = datetime.now(timezone.utc)
    # Include December at the year boundary so a prior observation is retained.
    years = [now.year] + ([now.year-1] if now.month == 1 else [])
    rows = []
    for year in years:
        response = requests.get(TREASURY_URL, params={'data': 'daily_treasury_yield_curve',
            'field_tdr_date_value': str(year)}, timeout=20)
        response.raise_for_status()
        rows.append(treasury_observation(response.content, now.isoformat()))
    current = max(rows, key=lambda r: r['observation_date'])
    if current['reference'] is None:
        earlier = [r for r in rows if r['observation_date'] < current['observation_date']]
        if earlier:
            ref = max(earlier, key=lambda r: r['observation_date']).copy()
            ref['value'] = ref.pop('price')
            ref.pop('reference', None)
            current['reference'] = ref
    return current
