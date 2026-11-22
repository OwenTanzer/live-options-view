"""Serve private CME Micro Bitcoin quotes from an authenticated IB Gateway."""
import asyncio
import hmac
import json
import math
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiohttp import web
from ib_async import IB, Future, FuturesOption
from market_data.live_market.btc_quote_clock import BtcQuoteClock
from market_data.live_market.btc_options import session


def identity(detail, future):
    opt, fut = detail.contract, future.contract
    date = detail.realExpirationDate
    if not (opt.secType == 'FOP' and opt.symbol == 'MBT' and opt.exchange == 'CME'
            and opt.currency == 'USD' and opt.multiplier in ('0.1', '0.10')
            and opt.right in ('C', 'P') and opt.conId > 0 and opt.strike > 0
            and detail.underConId == fut.conId and date and date == future.realExpirationDate
            and re.fullmatch(r'MBT[A-Z]\d{1,4}', fut.localSymbol)):
        return None
    try:
        at = datetime.strptime(date+' '+detail.lastTradeTime, '%Y%m%d %H:%M:%S').replace(
            tzinfo=ZoneInfo(detail.timeZoneId)).astimezone(timezone.utc)
    except (KeyError, ValueError):
        return None
    exp = datetime.strptime(date,'%Y%m%d').date().isoformat()
    return dict(contract_key='MBTOPT:'+str(opt.conId), provider='IBKR', conId=opt.conId,
                provider_symbol=opt.localSymbol, product='/MBT', exchange='CME', multiplier=0.1,
                series='monthly', instrument_type='futures_option', exp=exp, future_expiry=exp,
                future_symbol='/'+fut.localSymbol, future_month=exp[:7], expires_at=at.isoformat(),
                stops_trading_at=at.isoformat(), strike=opt.strike, type='call' if opt.right=='C' else 'put')


class Books:
    def __init__(self, ib):
        self.ib = ib
        self.registry = {}
        self.streams = {}
        self.books = {}
        self.clock = BtcQuoteClock()
        self.lock = asyncio.Lock()
        self.refreshed = 0
        self.ib.pendingTickersEvent += self.update

    def update(self, tickers):
        now = datetime.now(timezone.utc)
        self.clock.next_frame()
        for t in tickers:
            key = 'MBTOPT:'+str(t.contract.conId)
            if key not in self.streams or key not in self.registry:
                continue
            if t.marketDataType != 1:
                self.clock.forget(key)
                self.books.pop(key,None)
                continue
            def number(v):
                return v if math.isfinite(v) and v >= 0 else None
            prices = {s:number(getattr(t,s)) for s in ('bid','ask')}
            sizes = {s:number(getattr(t,s+'Size')) for s in prices}
            proof = self.clock.observe(key,{},now,prices,sizes)
            row = dict(symbol=key,contract=self.registry[key][0],source='IBKR',available=True,stream_epoch=self.clock.epoch)
            for side in prices:
                row.update({side:prices[side],side+'_size':sizes[side],side+'_received_ts':now.isoformat(),
                            side+'_ts':None,side+'_change':proof[side+'_change']})
            self.books[key] = row

    async def discover(self):
        async with self.lock:
            now = datetime.now(timezone.utc)
            if now.timestamp()-self.refreshed < 300:
                return
            futures = await self.ib.reqContractDetailsAsync(Future('MBT',exchange='CME',currency='USD'))
            upcoming = sorted((d for d in futures if d.realExpirationDate >= now.strftime('%Y%m%d')),key=lambda d:d.realExpirationDate)
            found = {}
            for fut in upcoming[:6]:
                query = FuturesOption(symbol='MBT',lastTradeDateOrContractMonth=fut.realExpirationDate,
                                      exchange='CME',currency='USD',multiplier='0.1')
                for d in await self.ib.reqContractDetailsAsync(query):
                    c = identity(d,fut)
                    if c and datetime.fromisoformat(c['expires_at'])>now:
                        found[c['contract_key']] = (c,d.contract)
            self.registry = found
            self.refreshed = now.timestamp()

    def payload(self, expiry, keys, held):
        now = datetime.now(timezone.utc)
        active = [c for c,_ in self.registry.values() if datetime.fromisoformat(c['expires_at'])>now]
        expiries = sorted({c['exp'] for c in active})
        if expiry not in expiries:
            expiry = next(iter(expiries),None)
        listed = [c for c in active if c['exp']==expiry]
        strikes = sorted({c['strike'] for c in listed})
        center = strikes[len(strikes)//2] if strikes else None
        window = sorted(listed,key=lambda c:abs(c['strike']-center))
        targets = [k for k in keys if k in self.registry]
        targets.extend(c['contract_key'] for c in active if c['provider_symbol'] in held)
        targets.extend(c['contract_key'] for c in window)
        targets = list(dict.fromkeys(targets))[:32]
        for k in list(self.streams):
            if k not in targets:
                self.ib.cancelMktData(self.streams.pop(k).contract)
                self.clock.forget(k)
                self.books.pop(k,None)
        for k in targets:
            if k not in self.streams:
                self.streams[k] = self.ib.reqMktData(self.registry[k][1])
        return dict(contracts=listed,quotes=[self.books[k] for k in targets if k in self.books],
                    entry_keys=[c['contract_key'] for c in active],subscribed_keys=targets,
                    expiries=expiries,expiry=expiry,center=center,center_from_future=False,
                    display_prices={},session=session(now))


async def main():
    token = os.environ['IBKR_MBT_QUOTE_KEY']
    if not token.strip():
        raise ValueError('Missing private quote key')
    ib = IB()
    await ib.connectAsync(os.getenv('IBKR_HOST','127.0.0.1'),int(os.getenv('IBKR_PORT','4002')),
                          clientId=int(os.getenv('IBKR_CLIENT_ID','73')),readonly=True,timeout=8)
    ib.reqMarketDataType(1)
    books = Books(ib)
    async def handle(request):
        if not hmac.compare_digest(request.headers.get('X-Live-Quote-Key',''),token):
            raise web.HTTPForbidden()
        if not ib.isConnected():
            raise web.HTTPServiceUnavailable()
        keys = list(filter(None,request.query.get('symbols','').split(',')))
        try:
            held = json.loads(request.query.get('held','[]'))
        except ValueError:
            raise web.HTTPBadRequest()
        if (len(keys)>64 or any(not re.fullmatch(r'MBTOPT:[1-9]\d{0,14}',k) for k in keys)
                or not isinstance(held,list) or len(held)>64 or any(not isinstance(s,str) for s in held)):
            raise web.HTTPBadRequest()
        await asyncio.wait_for(books.discover(),60)
        return web.json_response(books.payload(request.query.get('expiry'),keys,held))
    app = web.Application()
    app.router.add_get('/btc-options',handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner,'127.0.0.1',int(os.getenv('IBKR_HTTP_PORT','8789'))).start()
        await ib.disconnectedEvent
    finally:
        await runner.cleanup()
        ib.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
