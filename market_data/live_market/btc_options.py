"""Private, ephemeral standard BTC monthlies on the existing price-lane feed.

No archive writer, login, order API, or independent connection. Provider identity
is resolved, never manufactured from an OCC parser. Disabled by default.
"""
import hashlib
import math
import os
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode

import requests

API = 'https://api.tastyworks.com'
BUDGET = 64
WINDOW_STRIKES = 11
SNAPSHOT_BATCH_SIZE = 64
SNAPSHOT_BATCHES_PER_CYCLE = 4
SNAPSHOT_MIN_AGE_SECONDS = 30


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


def is_standard_monthly(row):
    product = row.get('future-option-product') or {}
    return (product.get('code') in ('BTC', 'WM') and product.get('exchange') == 'CME'
            and product.get('expiration-type') == 'End-Of-Month')


def normalize(row, future):
    """Fail closed if monthly/exercise/deliverable facts are missing."""
    try:
        exp = row['expiration-date']
        strike = float(row['strike-price'])
        micro = (row.get('future-option-product') or {}).get('code') == 'WM'
        root, multiplier = ('/MBT', 0.1) if micro else ('/BTC', 5)
        if (row['root-symbol'] != root or not future['symbol'].startswith(root) or row['exchange'] != 'CME'
                or not is_standard_monthly(row)
                or row['settlement-type'].lower() != 'future'
                or row['option-type'] not in ('C', 'P')
                or not math.isfinite(strike) or strike <= 0
                or future['symbol'] != row['underlying-symbol']
                or future['expiration-date'] != exp
                or float(future['notional-multiplier']) != multiplier
                or not row['streamer-symbol'].endswith(':XCME')
                or not instant(row['expires-at']) or not instant(row['stops-trading-at'])):
            return None
        key = 'BTCOPT:' + hashlib.sha256(row['symbol'].encode()).hexdigest()[:40]
        return {'contract_key': key, 'instrument_type': 'futures_option', 'product': root,
                'series': 'monthly', 'exchange': 'CME', 'provider_symbol': row['symbol'],
                'security_id': row.get('security-id'),
                'streamer_symbol': row['streamer-symbol'], 'future_symbol': future['symbol'],
                'future_streamer_symbol': future.get('streamer-symbol'),
                'future_month': (future.get('maturity-date') or exp)[:7], 'future_expiry': exp,
                'exp': exp, 'expires_at': row['expires-at'], 'stops_trading_at': row['stops-trading-at'],
                'strike': strike, 'type': 'call' if row['option-type'] == 'C' else 'put', 'multiplier': multiplier}
    except (KeyError, TypeError, ValueError):
        return None


class BtcOptions:
    def __init__(self, get=requests.get):
        self._get = get
        self._lock = threading.RLock()
        self.contracts = {}
        self.entries = []
        self.listed = set()
        self.expiries = []
        self.viewport_expiry = 'front'
        self.viewport_product = '/BTC'
        self.viewport_keys = ()
        self.held = {}  # provider identity -> last requested time (bounded TTL)
        self.feed = None
        self.checked = None
        self.status = 'uninitialized'
        self.subscription_count = 0
        self._worker = None
        self.display_checked = {}
        self.display_prices = {}

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
        self._worker = threading.Thread(target=self._refresh_cycle, args=(feed, token, now),
                                        name='btc-discovery', daemon=True)
        self._worker.start()

    def _refresh_cycle(self, feed, token, now):
        self.refresh(feed, token, now)
        # Read every listed expiry independently of browser activity. Oldest
        # attempts go first so repeated provider omissions cannot starve a month.
        with self._lock:
            never = datetime.min.replace(tzinfo=timezone.utc)
            due = sorted((self.display_checked.get(k, never), self.contracts[k]['exp'],
                          self.contracts[k]['provider_symbol'], k) for k in self.listed
                         if k not in self.display_checked or
                         (now - self.display_checked[k]).total_seconds() >= SNAPSHOT_MIN_AGE_SECONDS)
        limit = SNAPSHOT_BATCH_SIZE * SNAPSHOT_BATCHES_PER_CYCLE
        for start in range(0, min(len(due), limit), SNAPSHOT_BATCH_SIZE):
            symbols = {symbol: key for _, _, symbol, key in due[start:start + SNAPSHOT_BATCH_SIZE]}
            with self._lock:
                self.display_checked.update(dict.fromkeys(symbols.values(), now))
            try:
                self._read_display_batch(symbols, token)
            except (ValueError, KeyError, requests.RequestException):
                # Preserve the previous timestamped batch on a failed request.
                break

    def _read_display_batch(self, symbols, token):
        query = urlencode([('future-option', symbol) for symbol in symbols])
        rows = self._read('/market-data/by-type?' + query, token)['items']
        prices = {}
        for row in rows:
            key = symbols.get(row.get('symbol'))
            if key is None or not instant(row.get('updated-at')):
                continue
            snapshot = {'as_of': row['updated-at']}
            for side in ('bid', 'ask', 'mark'):
                try:
                    value = float(row.get(side))
                except (ValueError, TypeError):
                    continue
                if math.isfinite(value) and value > 0:
                    snapshot[side] = value
            prices[key] = snapshot
        with self._lock:
            for k in symbols.values():
                self.display_prices.pop(k, None)
            self.display_prices.update(prices)

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
                if os.getenv('BTC_MICRO_OPTIONS') == '1':
                    try:
                        micro_rows = []
                        for page in range(10):
                            try:
                                page_data = self._read('/futures-option-chains/MBT?page-offset=' + str(page), token)
                            except ValueError as exc:
                                if str(exc) != 'provider_http_404':
                                    raise
                                page_data = self._read('/instruments/future-options?option-root-symbol=WM&exchange=CME&page-offset=' + str(page), token)
                            micro_rows.extend(page_data.get('items', []))
                            if page + 1 >= page_data.get('_pagination', {}).get('total-pages', 1):
                                rows.extend(micro_rows)
                                break
                    except (ValueError, KeyError, requests.RequestException):
                        pass  # Standard quotes stay available when Micro discovery is unavailable.
            chain_symbols = {r.get('symbol') for r in rows}
            for symbol in missing[:4]:
                rows.append(self._read('/instruments/future-options/' + quote(symbol, safe=''), token))
            monthly = [r for r in rows if is_standard_monthly(r)]
            rows = [r for r in monthly if (instant(r.get('stops-trading-at')) or now) > now
                    or r.get('symbol') in held_snapshot]
            futures, resolved, closing_only = {}, {}, set()
            for row in rows:
                fs = row.get('underlying-symbol', '')
                if fs not in futures:
                    futures[fs] = self._read('/instruments/futures/' + quote(fs, safe=''), token)
                c = normalize(row, futures[fs])
                if c:
                    resolved[c['contract_key']] = c
                    if row.get('is-closing-only') or row.get('active') is False:
                        closing_only.add(c['contract_key'])
            with self._lock:
                if due:
                    self.listed = {k for k, c in resolved.items() if c['provider_symbol'] in chain_symbols
                                   and instant(c['stops_trading_at']) > now}
                    self.display_prices = {k: v for k, v in self.display_prices.items() if k in self.listed}
                    self.display_checked = {k: v for k, v in self.display_checked.items() if k in self.listed}
                    self.expiries = sorted({resolved[k]['exp'] for k in self.listed})
                prior = {k: c for k, c in self.contracts.items()
                         if c['provider_symbol'] in self.held or k in self.listed}
                prior.update(resolved)
                self.contracts = prior
                if due:
                    self.entries = [k for k in resolved if k in self.listed and k not in closing_only]
                self.status = 'ok' if self.listed else 'no_verified_monthly'
        except Exception as exc:
            reason = str(exc)
            self.status = reason if reason.startswith('provider_http_') or reason in ('chain_too_large', 'chain_page_budget', 'future_budget') else 'discovery_unavailable'
        with self._lock:
            try:
                self._subscribe(now)
            except Exception:
                self.status = 'subscription_unavailable'

    def _subscribe(self, now, symbols=()):
        held = [c for c in self.contracts.values() if c['provider_symbol'] in self.held
                and instant(c['stops_trading_at']) > now]
        expiries = sorted({self.contracts[k]['exp'] for k in self.listed if self.contracts[k]['product'] == self.viewport_product})
        self.expiry = self.viewport_expiry if self.viewport_expiry in expiries else next(iter(expiries), None)
        active = [c for k, c in self.contracts.items() if k in self.listed and c['exp'] == self.expiry and c['product'] == self.viewport_product]
        inspected = [self.contracts[k] for k in self.viewport_keys
                     if k in self.listed and self.contracts[k]['exp'] == self.expiry]
        exact = [self.contracts[k] for k in symbols if k in self.contracts]
        state = self.feed.get_state()
        # Center on the deliverable future, never on spot BTC or another month.
        center = None
        self.center_from_future = False
        if active:
            # Median strike is a bounded discovery fallback until a future quote arrives.
            strikes = sorted({c['strike'] for c in active})
            center = strikes[len(strikes) // 2]
            future_quote = state.get(active[0].get('future_streamer_symbol'), {})
            bid, ask = future_quote.get('bid'), future_quote.get('ask')
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and 0 < bid <= ask:
                center = (bid + ask) / 2
                self.center_from_future = True
        self.center = center
        window = sorted(active, key=lambda c: (abs(c['strike'] - center), c['strike'], c['type']))[:WINDOW_STRIKES * 2] if center else []
        chosen = list({c['contract_key']: c for c in held + exact + inspected + window}.values())[:BUDGET]
        streams = [c['streamer_symbol'] for c in chosen]
        if active and active[0].get('future_streamer_symbol'):
            streams.append(active[0]['future_streamer_symbol'])
        self.feed.replace_btc_subscriptions(streams, option_symbols=[c['streamer_symbol'] for c in chosen])
        self.selected = {c['contract_key'] for c in chosen}
        self.subscription_count = len(chosen)

    def payload(self, symbols, held, now=None, expiry=None, product='cme'):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            for s in held:
                if isinstance(s, str) and len(s) <= 128 and (s in self.held or len(self.held) < BUDGET):
                    self.held[s] = now
            if expiry is not None:
                self.viewport_expiry = expiry
                self.viewport_product = '/MBT' if product == 'micro' else '/BTC'
                self.viewport_keys = tuple(symbols)
            if self.feed:
                try: self._subscribe(now, symbols if expiry is None else ())
                except Exception: self.status = 'subscription_unavailable'
            selected = getattr(self, 'selected', set())
            contracts = [c for k, c in self.contracts.items()
                         if (k in self.listed and c['exp'] == getattr(self, 'expiry', None) and c['product'] == self.viewport_product)
                         or (expiry is None and c['provider_symbol'] in self.held)]
            requested = list(dict.fromkeys([*symbols, *sorted(selected)]))
            ready, state, diagnostics = self.feed.get_btc_snapshot() if self.feed else (False, {}, {})
            quotes, unavailable = [], {}
            for key in requested:
                c = self.contracts.get(key)
                if c is None or key not in selected:
                    unavailable[key] = 'identity_pending_or_subscription_limit'
                    continue
                raw = state.get(c['streamer_symbol'], {})
                q = {'symbol': key, 'contract': c, 'source': 'tastytrade/dxlink',
                     'stream_epoch': raw.get('btc_stream_epoch'),
                     'available': ready and session(now) == 'open'
                     and raw.get('btc_stream_epoch') == diagnostics.get('stream_epoch')}
                for side in ('bid', 'ask'):
                    q[side] = raw.get(side)
                    q[side + '_size'] = raw.get(side + '_size')
                    q[side + '_ts'] = raw.get(side + '_observed_at')
                    q[side + '_received_ts'] = raw.get(side + '_ts')
                    q[side + '_change'] = raw.get(side + '_change')
                quotes.append(q)
            return {'display_prices': {c['contract_key']: self.display_prices[c['contract_key']] for c in contracts
                                       if c['contract_key'] in self.display_prices},
                    'contracts': contracts, 'quotes': quotes, 'entry_keys': self.entries,
                    'unavailable': unavailable, 'session': session(now), 'source_status': self.status,
                    'expiries': sorted({self.contracts[k]['exp'] for k in self.listed if self.contracts[k]['product'] == self.viewport_product}),
                    'expiry': getattr(self, 'expiry', None), 'center': getattr(self, 'center', None),
                    'center_from_future': getattr(self, 'center_from_future', False),
                    'subscribed_keys': sorted(selected), 'subscription_count': self.subscription_count, 'subscription_budget': BUDGET,
                    'wire_time_diagnostics': diagnostics,
                    'server_ts': now.isoformat()}
