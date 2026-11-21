"""Private, ephemeral standard BTC monthlies on the existing price-lane feed.

No archive writer, login, order API, or independent connection. Provider identity
is resolved, never manufactured from an OCC parser. Disabled by default.
"""
import hashlib
import math
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote

import requests

API = 'https://api.tastyworks.com'
BUDGET = 64
WINDOW_STRIKES = 11


def instant(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt if dt.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def session(now):
    ct = now.astimezone(ZoneInfo('America/Chicago'))
    minute = ct.hour * 60 + ct.minute
    # CME crypto 24/7 calendar, reviewed 2026-11-21. Holidays continue trading.
    return 'maintenance' if (ct.weekday() < 5 and 960 <= minute < 962) or (
        ct.weekday() == 5 and 120 <= minute < 240) else 'open'


def normalize(row, future):
    """Fail closed if monthly/exercise/deliverable facts are missing."""
    try:
        product = row['future-option-product']
        exp = row['expiration-date']
        strike = float(row['strike-price'])
        if (row['root-symbol'] != '/BTC' or row['exchange'] != 'CME'
                or product['expiration-type'].lower() not in ('regular', 'monthly')
                or row['exercise-style'].lower() != 'european'
                or row['settlement-type'].lower() != 'future'
                or row.get('is-exercisable-weekly') is True
                or row['option-type'] not in ('C', 'P')
                or not math.isfinite(strike) or strike <= 0
                or future['symbol'] != row['underlying-symbol']
                or future['expiration-date'] != exp
                or float(future['notional-multiplier']) != 5
                or not row['streamer-symbol'].endswith(':XCME')
                or not instant(row['expires-at']) or not instant(row['stops-trading-at'])):
            return None
        key = 'BTCOPT:' + hashlib.sha256(row['symbol'].encode()).hexdigest()[:40]
        return {'contract_key': key, 'instrument_type': 'futures_option', 'product': '/BTC',
                'series': 'monthly', 'exchange': 'CME', 'provider_symbol': row['symbol'],
                'security_id': row.get('security-id'),
                'streamer_symbol': row['streamer-symbol'], 'future_symbol': future['symbol'],
                'future_streamer_symbol': future.get('streamer-symbol'),
                'future_month': future.get('maturity-date', exp)[:7], 'future_expiry': exp,
                'exp': exp, 'expires_at': row['expires-at'], 'stops_trading_at': row['stops-trading-at'],
                'strike': strike, 'type': 'call' if row['option-type'] == 'C' else 'put', 'multiplier': 5}
    except (KeyError, TypeError, ValueError):
        return None


class BtcOptions:
    def __init__(self, get=requests.get):
        self._get = get
        self._lock = threading.RLock()
        self.contracts = {}
        self.entries = []
        self.held = {}  # provider identity -> last requested time (bounded TTL)
        self.feed = None
        self.checked = None
        self.status = 'uninitialized'
        self.subscription_count = 0
        self._worker = None

    def _read(self, path, token):
        r = self._get(API + path, headers={'Authorization': 'Bearer ' + token,
                     'User-Agent': 'OptionsView-BTC-paper/1.0'}, timeout=10)
        if r.status_code != 200:
            # Status only: response bodies can contain account/credential data.
            raise ValueError('provider_http_' + str(r.status_code))
        body = r.json()
        data = body['data']
        if 'items' in data:
            data = {**data, '_pagination': body.get('pagination', {})}
        return data

    def schedule_refresh(self, feed, token, now):
        # Discovery must never block the price publisher. One bounded worker;
        # it reuses credentials/connection and never authenticates independently.
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self.refresh, args=(feed, token, now),
                                        name='btc-discovery', daemon=True)
        self._worker.start()

    def refresh(self, feed, token, now):
        """Called by price lane; discovery failure never tears down its feed."""
        with self._lock:
            changed = self.feed is not feed
            self.feed = feed
            self.held = {s: t for s, t in self.held.items() if (now - t).total_seconds() < 900}
            missing = [s for s in self.held if not any(c['provider_symbol'] == s for c in self.contracts.values())]
            due = self.checked is None or (now - self.checked).total_seconds() >= 300
            if not (due or changed or missing):
                return
            # One bounded REST attempt per cadence, even on provider failure.
            if self.checked and (now - self.checked).total_seconds() < 30 and not changed:
                return
            self.checked = now
            held_snapshot = dict(self.held)
        # REST runs outside the registry lock: readers can report unavailable
        # immediately while discovery is in flight.
        try:
            rows = []
            if due:
                for page in range(10):
                    path = '/futures-option-chains/BTC' + (f'?page-offset={page}' if page else '')
                    data = self._read(path, token)
                    rows.extend(data.get('items', []))
                    if len(rows) > 10000:
                        raise ValueError('chain_too_large')
                    if page + 1 >= data.get('_pagination', {}).get('total-pages', 1):
                        break
                else:
                    raise ValueError('chain_page_budget')
            for symbol in missing[:4]:
                rows.append(self._read('/instruments/future-options/' + quote(symbol, safe=''), token))
            monthly = [r for r in rows if r.get('future-option-product', {}).get('expiration-type', '').lower() in ('regular', 'monthly')]
            nearest = min((r.get('expiration-date', '') for r in monthly if instant(r.get('stops-trading-at')) and instant(r['stops-trading-at']) > now), default=None)
            rows = [r for r in monthly if r.get('expiration-date') == nearest or r.get('symbol') in held_snapshot]
            futures, resolved, closing_only = {}, {}, set()
            for row in rows:
                fs = row.get('underlying-symbol', '')
                if fs not in futures:
                    if len(futures) >= 5:
                        raise ValueError('future_budget')
                    futures[fs] = self._read('/instruments/futures/' + quote(fs, safe=''), token)
                c = normalize(row, futures[fs])
                if c:
                    resolved[c['contract_key']] = c
                    if row.get('is-closing-only') or row.get('active') is False:
                        closing_only.add(c['contract_key'])
            with self._lock:
                if due:
                    active = [c for c in resolved.values() if instant(c['stops_trading_at']) > now and c['contract_key'] not in closing_only]
                    nearest = min((c['exp'] for c in active), default=None)
                    self.entries = [c['contract_key'] for c in active if c['exp'] == nearest]
                prior = {k: c for k, c in self.contracts.items() if c['provider_symbol'] in self.held}
                prior.update(resolved)
                if not due:
                    prior.update({k: c for k, c in self.contracts.items() if k in self.entries})
                self.contracts = prior
                self.status = 'ok' if self.entries else 'no_verified_monthly'
        except Exception as exc:
            reason = str(exc)
            self.status = reason if reason.startswith('provider_http_') or reason in ('chain_too_large', 'chain_page_budget', 'future_budget') else 'discovery_unavailable'
        with self._lock:
            try:
                self._subscribe(now)
            except Exception:
                self.status = 'subscription_unavailable'

    def _subscribe(self, now):
        held = [c for c in self.contracts.values() if c['provider_symbol'] in self.held
                and instant(c['stops_trading_at']) > now]
        active = [self.contracts[k] for k in self.entries if k in self.contracts]
        state = self.feed.get_state()
        # Center on the deliverable future, never on spot BTC or another month.
        center = None
        if active:
            # Median strike is a bounded discovery fallback until a future quote arrives.
            strikes = sorted({c['strike'] for c in active})
            center = strikes[len(strikes) // 2]
            future_quote = state.get(active[0].get('future_streamer_symbol'), {})
            bid, ask = future_quote.get('bid'), future_quote.get('ask')
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and 0 < bid <= ask:
                center = (bid + ask) / 2
        window = sorted(active, key=lambda c: (abs(c['strike'] - center), c['strike'], c['type']))[:WINDOW_STRIKES * 2] if center else []
        chosen = list({c['contract_key']: c for c in held + window}.values())[:BUDGET]
        streams = [c['streamer_symbol'] for c in chosen]
        if active and active[0].get('future_streamer_symbol'):
            streams.append(active[0]['future_streamer_symbol'])
        self.feed.replace_btc_subscriptions(streams)
        self.selected = {c['contract_key'] for c in chosen}
        self.subscription_count = len(chosen)

    def payload(self, symbols, held, now=None):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            for s in held:
                if isinstance(s, str) and len(s) <= 128 and (s in self.held or len(self.held) < BUDGET):
                    self.held[s] = now
            if self.feed:
                try: self._subscribe(now)
                except Exception: self.status = 'subscription_unavailable'
            selected = getattr(self, 'selected', set())
            contracts = [self.contracts[k] for k in selected if k in self.contracts]
            requested = symbols or [c['contract_key'] for c in contracts]
            health = self.feed.get_health() if self.feed else {}
            ready = all(health.get(k) for k in ('connected', 'authorized', 'channel_open'))
            state = self.feed.get_state() if ready else {}
            quotes, unavailable = [], {}
            for key in requested:
                c = self.contracts.get(key)
                if c is None or key not in selected:
                    unavailable[key] = 'identity_pending_or_subscription_limit'
                    continue
                raw = state.get(c['streamer_symbol'], {})
                q = {'symbol': key, 'contract': c, 'source': 'tastytrade/dxlink',
                     'available': ready and session(now) == 'open'}
                for side in ('bid', 'ask'):
                    q[side] = raw.get(side)
                    q[side + '_size'] = raw.get(side + '_size')
                    q[side + '_ts'] = raw.get(side + '_observed_at')
                    q[side + '_received_ts'] = raw.get(side + '_ts')
                quotes.append(q)
            return {'contracts': contracts, 'quotes': quotes, 'entry_keys': self.entries,
                    'unavailable': unavailable, 'session': session(now), 'source_status': self.status,
                    'subscription_count': self.subscription_count, 'subscription_budget': BUDGET,
                    'server_ts': now.isoformat()}
