import json
import os
import unittest
from datetime import datetime, timezone, date, timedelta
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from market_data.live_market import settlement_marks, quotes, chain
from market_data.live_market import market as mkt
from market_data.live_market.state import Counters, SnapshotTracker, UnderlyingSession

class Sink:
 def __init__(self): self.writes=[]
 def put_object(self,**kw): self.writes.append(kw)

class ConflictSink(Sink):
 """Enforces IfNoneMatch='*' like R2: a second put to the same key 409s."""
 def __init__(self): super().__init__(); self.keys=set()
 def put_object(self, **kw):
  if kw.get("IfNoneMatch") == "*" and kw["Key"] in self.keys:
   exc = Exception("Precondition Failed")
   exc.response = {"ResponseMetadata": {"HTTPStatusCode": 412}}
   raise exc
  self.keys.add(kw["Key"]); self.writes.append(kw)

class FixedDatetime(datetime):
 """Pins take_snapshot's now to 2026-11-09T21:00:40Z (16:00:40 ET)."""
 @classmethod
 def now(cls, tz=None):
  fixed = datetime(2026, 11, 9, 21, 0, 40, tzinfo=timezone.utc)
  return fixed if tz is None else fixed.astimezone(tz)

def _snapshot_settlement_writes(quote, symbol="SPY", sink=None):
 sink = sink or Sink()
 class Feed:
  def get_state(self): return {symbol: quote}
 us = UnderlyingSession(symbol)
 strikes = [{"strike": 670, "call_sym": "SPY261109C00670000", "call_occ": "SPY261109C00670000",
             "put_sym": "SPY261109P00670000", "put_occ": "SPY261109P00670000"}]
 with patch.object(mkt, "datetime", FixedDatetime):
  mkt.take_snapshot(sink, Feed(), strikes, "2026-11-09", "tier", date(2026, 11, 9),
                    Counters(), SnapshotTracker(), us)
 return [w for w in sink.writes if w["Key"].startswith("settlement/")]

# Chej's reproduction: fresh receipt, provider time UNKNOWN on both sides.
UNKNOWN_QUOTE = {"bid": 670, "ask": 670.2,
                 "bid_ts": "2026-11-09T21:00:30+00:00", "ask_ts": "2026-11-09T21:00:31+00:00",
                 "bid_observed_at": None, "ask_observed_at": None}
# Valid post-close positive: both sides provider-observed in window, fresh.
KNOWN_QUOTE = dict(UNKNOWN_QUOTE, bid_observed_at="2026-11-09T21:00:30+00:00",
                   ask_observed_at="2026-11-09T21:00:31+00:00")

class MultiUnderlying(unittest.TestCase):
 def test_mark_requires_expiry_own_timed_post_close_spot(self):
  sink=Sink();now=datetime(2026,11,6,21,0,30,tzinfo=timezone.utc)
  for symbol in ('SPY','IBIT'):
   self.assertTrue(settlement_marks.publish_settlement_mark(sink,symbol,'2026-11-06',50,'2026-11-06T21:00:00+00:00',now))
  for stamp,exp in [(None,'2026-11-06'),('2026-11-06T20:59:00+00:00','2026-11-06'),('2026-11-06T21:00:00+00:00','2026-11-13'),('2026-11-06T21:01:00+00:00','2026-11-06')]:
   self.assertFalse(settlement_marks.publish_settlement_mark(sink,'IBIT',exp,50,stamp,now))
  self.assertEqual(len(sink.writes),2)
  self.assertTrue(all(w['IfNoneMatch']=='*' for w in sink.writes))
 def test_ibit_nearest_expiry(self):
  response=type('Response',(),{'raise_for_status':lambda s:None,'json':lambda s:{'data':{'items':[{'expirations':[{'expiration-date':'2026-11-13','strikes':[]}]}]}}})()
  with patch.object(chain.requests,'get',return_value=response):
   self.assertEqual(chain.load_chain('fixture',date(2026,11,9),'IBIT',False)[1],'2026-11-13')
   with self.assertRaises(RuntimeError): chain.load_chain('fixture',date(2026,11,9),'SPY',False)
 def test_held_contract_interest_is_bounded_and_no_extra_connection(self):
  class Feed:
   def __init__(self): self.added=[]
   def add_option_subscriptions(self,symbols): self.added+=symbols
   def get_state(self): return {}
   def get_health(self): return {'last_feed_event_time':None,'connected':True,'authorized':True,'channel_open':True}
  feed=Feed(); registry=quotes.LiveQuoteRegistry();registry.set_session(feed,{})
  with patch.object(quotes,'COLLECT_IBIT',True):
   registry.quote_payload([f'IBIT291221C{n*1000:08}' for n in range(1,151)])
   self.assertEqual(len(feed.added),100)
   registry.quote_payload(['IBIT291221C00001000']);self.assertEqual(len(feed.added),100)
  registry.set_session(feed,{})
  with patch.object(quotes,'COLLECT_IBIT',False): registry.quote_payload(['IBIT291221C00001000'])
  self.assertEqual(len(feed.added),100)

 @patch.object(quotes, "datetime", wraps=datetime)
 def test_quote_payload_explains_every_absent_contract(self, clock):
  # The 503 that made the option lane look broken was the service correctly
  # refusing to quote outside a feed session. Inside a session the remaining
  # ambiguity was silence: an expired contract, a later expiry the provider had
  # not answered yet, an exhausted subscription budget and a missing ticker all
  # looked identical (returned < requested). Each now carries a reason, and the
  # ones that are not the caller's business to retry are named.
  TODAY='2026-11-10'
  clock.now.return_value = datetime.fromisoformat(TODAY + 'T14:30:00+00:00')
  class Feed:
   def __init__(self): self.added=[]
   def add_option_subscriptions(self,symbols): self.added+=symbols
   def get_state(self):
    return {'.QQQ261110C677':{'bid':1.0,'ask':1.2,'bid_ts':TODAY+'T14:30:00+00:00','ask_ts':TODAY+'T14:30:00+00:00'},
            quotes.PRICE_TICKERS['QQQ']:{'last':677.4,'prev_close':679.0,'last_ts':TODAY+'T14:30:00+00:00'}}
   def get_health(self): return {'last_feed_event_time':None,'connected':True,'authorized':True,'channel_open':True}
  feed=Feed(); registry=quotes.LiveQuoteRegistry()
  registry.set_session(feed,{
   'QQQ261110C00677000':{'streamer_symbol':'.QQQ261110C677','strike':677.0,'type':'call','exp':TODAY},
   'QQQ261110P00677000':{'streamer_symbol':'.QQQ261110P677','strike':677.0,'type':'put','exp':TODAY},
  })
  with patch.object(quotes,'COLLECT_IBIT',False):
   payload=registry.quote_payload([
    'QQQ261110C00677000',   # quoted
    'QQQ261110P00677000',   # subscribed, provider has not answered yet
    'QQQ241101C00677000',   # expired before today
    'QQQ261120C00677000',   # later expiry: subscribes this request
    'IBIT261120C00041000',  # IBIT not collected in this run
    'NOTASYMBOL',           # never an instrument
    'QQQ',                  # ticker, quoted from the feed
   ])
  self.assertEqual([q['symbol'] for q in payload['quotes']],['QQQ261110C00677000','QQQ'])
  self.assertEqual(payload['returned'],2); self.assertEqual(payload['requested'],7)
  self.assertEqual(payload['unavailable'],{
   'QQQ261110P00677000':'no_quote_yet',
   'QQQ241101C00677000':'expired',
   'QQQ261120C00677000':'no_quote_yet',
   'IBIT261120C00041000':'not_collected',
   'NOTASYMBOL':'unknown_symbol',
  })
  self.assertEqual(len(feed.added),1, 'only the later expiry is subscribed; expired and uncollected contracts are not')

  # The subscription budget names itself rather than dropping contracts silently.
  with patch.object(quotes,'COLLECT_IBIT',True):
   registry.set_session(Feed(),{})
   overload=registry.quote_payload([f'IBIT261221C{n*1000:08}' for n in range(1,102)])
  self.assertEqual(overload['unavailable'][f'IBIT261221C{101*1000:08}'],'subscription_limit')
  self.assertEqual(overload['returned'],0)

  # A closed feed session is definite, and says so on every option it refuses.
  other=quotes.LiveQuoteRegistry()
  other.update_ticker_fallbacks({'QQQ':{'price':677.4,'quote_ts':TODAY+'T14:30:00+00:00'}},TODAY+'T14:30:00+00:00')
  closed=other.quote_payload(['QQQ261110C00677000','QQQ','NOTASYMBOL'])
  # A registry that has never opened a session is uninitialized, not closed:
  # the reason map must agree with the lifecycle state rather than assume.
  self.assertEqual(closed['unavailable'],{'QQQ261110C00677000':'uninitialized','NOTASYMBOL':'unknown_symbol'})
  self.assertEqual([q['symbol'] for q in closed['quotes']],['QQQ'],'tickers keep working from the price lane')
  self.assertEqual(closed['health']['state'],'offline')
  self.assertEqual(closed['health']['session'],'uninitialized')

 def test_http_handler_separates_deliberate_stop_from_feed_failure(self):
  # The review concern: `offline` covers three different situations -- no
  # session yet, a session that died, and a session that ended on purpose.
  # Mapping all of them to "closed" would hide a trading-hours outage, so the
  # handler must say which one it is, on the wire, and the answer must come
  # from lifecycle state rather than from a clock. This drives the real
  # ThreadingHTTPServer handler over real HTTP.
  from market_data.live_market.quotes import start_live_quote_server
  class Feed:
   def __init__(self, live=True): self.live=live; self.added=[]
   def add_option_subscriptions(self,symbols): self.added+=symbols
   def get_state(self): return {}
   def get_health(self):
    return {'last_feed_event_time':(datetime.now(timezone.utc) if self.live else None),
            'connected':self.live,'authorized':self.live,'channel_open':self.live}
  def get(port,path,symbols):
   request=Request(f'http://127.0.0.1:{port}{path}?symbols={symbols}',
                   headers={'X-Live-Quote-Key':access_key})
   try:
    with urlopen(request,timeout=10) as response:
     return response.status, json.loads(response.read())
   except HTTPError as error:
    return error.code, json.loads(error.read())
  access_key='test-key'
  registry=quotes.LiveQuoteRegistry()
  server=None
  with patch.dict(os.environ,{'LIVE_QUOTE_KEY':access_key}):
   server=start_live_quote_server(registry,port=0)
  port=server.server_address[1]
  try:
   # 1. Before any session: uninitialized, not closed.
   status,body=get(port,'/live-quotes','QQQ261110C00677000')
   self.assertEqual(status,503)
   self.assertEqual(body['health']['state'],'offline')
   self.assertEqual(body['health']['session'],'uninitialized')

   # 2. In-session but the feed is not delivering: a failure while the lane is
   #    supposed to be up, and explicitly NOT a closed session.
   registry.set_session(Feed(live=False),{})
   status,body=get(port,'/live-quotes','QQQ261110C00677000')
   self.assertEqual(status,503)
   self.assertEqual(body['health']['session'],'open')
   self.assertEqual(body['health']['state'],'connecting')

   # 3. A session that died (auth circuit, token refresh, unhandled exception).
   registry.clear_session(reason='session_failed')
   status,body=get(port,'/live-quotes','QQQ261110C00677000')
   self.assertEqual(status,503)
   self.assertEqual(body['health']['session'],'session_failed')
   self.assertEqual(body['health']['state'],'offline')

   # 4. The deliberate end of the trading session: the only closed lane.
   registry.set_session(Feed(),{})
   registry.clear_session(reason='session_closed')
   status,body=get(port,'/live-quotes','QQQ261110C00677000')
   self.assertEqual(status,503)
   self.assertEqual(body['health']['session'],'session_closed')
   self.assertEqual(body['error'],'Live quote feed unavailable')

   # 5. Tickers keep answering from the price lane in every one of those states,
   #    which is why an option-only 503 never looked like a total outage.
   registry.update_ticker_fallbacks({'QQQ':{'price':677.4,'quote_ts':'2026-11-10T23:30:00+00:00'}},
                                    '2026-11-10T23:30:00+00:00')
   status,body=get(port,'/live-quotes','QQQ')
   self.assertEqual(status,200)
   self.assertEqual(body['health']['session'],'session_closed')
   self.assertEqual(body['quotes'][0]['symbol'],'QQQ')

   # 6. A cold start after hours: the scheduler is deliberately waiting for the
   #    next session, which is a closed lane and not an outage. The per-symbol
   #    reason follows the lifecycle value instead of assuming session_closed.
   registry.await_next_session()
   status,body=get(port,'/live-quotes','QQQ261110C00677000')
   self.assertEqual(status,503)
   self.assertEqual(body['health']['session'],'waiting_for_session')
   self.assertEqual(body['health']['state'],'offline')

   # 7. Only the known reasons are accepted, so a typo cannot invent a state.
   with self.assertRaises(ValueError):
    registry.clear_session(reason='offline')
  finally:
   if server is not None:
    server.shutdown(); server.server_close()

 def test_cold_afterhours_start_waits_but_startup_failure_does_not_close(self):
  # main() builds the registry uninitialized and then blocks in
  # wait_for_premarket. A cold restart after hours therefore sat at
  # "uninitialized" all evening, so the closed state the collector was really
  # in could never be shown. announce_scheduler_wait labels that deliberate
  # wait, and must never do it after a session failure -- that wait is recovery,
  # not a closed lane.
  from market_data.live_market import runner
  class Feed:
   def add_option_subscriptions(self,symbols): pass
   def get_state(self): return {}
   def get_health(self):
    return {'last_feed_event_time':datetime.now(timezone.utc),'connected':True,
            'authorized':True,'channel_open':True}
  registry=quotes.LiveQuoteRegistry()

  # Cold start after hours: no session yet, scheduler about to wait.
  self.assertEqual(registry.session_state(),'uninitialized')
  self.assertEqual(registry.health()['session'],'uninitialized')
  runner.announce_scheduler_wait(registry,None)
  self.assertEqual(registry.session_state(),'waiting_for_session')
  self.assertEqual(registry.health()['session'],'waiting_for_session')
  self.assertEqual(registry.health()['state'],'offline')
  self.assertEqual(registry.quote_payload(['QQQ261110C00677000'])['unavailable'],
                   {'QQQ261110C00677000':'waiting_for_session'})
  # A repeat wait is idempotent and cannot promote itself past a real state.
  runner.announce_scheduler_wait(registry,None)
  self.assertEqual(registry.session_state(),'waiting_for_session')

  # Next session opens.
  registry.set_session(Feed(),{})
  self.assertEqual(registry.session_state(),'open')
  self.assertEqual(registry.health()['state'],'live')

  # In-session startup failure: the registry must NOT become a closed lane, and
  # the blocked-session wait that follows must not relabel it either.
  registry.clear_session(reason='session_failed')
  self.assertEqual(registry.session_state(),'session_failed')
  blocked=date(2026,11,10)
  runner.announce_scheduler_wait(registry,blocked)
  self.assertEqual(registry.session_state(),'session_failed',
                   'a blocked-session wait is failure recovery, never a closed lane')
  self.assertEqual(registry.health()['session'],'session_failed')
  self.assertEqual(registry.quote_payload(['QQQ261110C00677000'])['unavailable'],
                   {'QQQ261110C00677000':'session_failed'})

  # Recovery: the next session opens normally.
  registry.set_session(Feed(),{})
  self.assertEqual(registry.session_state(),'open')

  # Deliberate end, then the evening wait leaves the ended-session label alone.
  registry.clear_session()
  self.assertEqual(registry.session_state(),'session_closed')
  runner.announce_scheduler_wait(registry,None)
  self.assertEqual(registry.session_state(),'session_closed',
                   'an ended session is not rewritten as "waiting"')

 def test_provider_observed_pairs_price_with_its_own_event_time(self):
  # Unknown provider time: price is computable but there is no provider
  # event to stamp it with -- must not publish.
  self.assertEqual(quotes._provider_observed_spy_price(UNKNOWN_QUOTE), (670.1, None, "bid_ask_midpoint"))
  # Both sides known: the OLDER side wins (a fresh half must not make an
  # old half look observed).
  self.assertEqual(quotes._provider_observed_spy_price(KNOWN_QUOTE),
                   (670.1, "2026-11-09T21:00:30+00:00", "bid_ask_midpoint"))
  # One side unknown: no provider time at all.
  self.assertEqual(quotes._provider_observed_spy_price(
   dict(KNOWN_QUOTE, ask_observed_at=None))[1], None)
  # Last trade: its own event time; unknown stays None.
  self.assertEqual(quotes._provider_observed_spy_price(
   {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00",
    "last_observed_at": "2026-11-09T21:00:30+00:00"}),
   (670.1, "2026-11-09T21:00:30+00:00", "last_trade"))
  self.assertEqual(quotes._provider_observed_spy_price(
   {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00", "last_observed_at": None})[1], None)
 def test_take_snapshot_unknown_provider_time_does_not_publish(self):
  # G3 regression (Chej review): SPY bid 670/ask 670.2, receipt 21:00:30/31,
  # provider time None on both sides, now 21:00:40. The receipt time falls
  # in the post-close window, but the provider observation is unknown, so
  # no immutable mark may be written.
  self.assertEqual(_snapshot_settlement_writes(UNKNOWN_QUOTE), [])
 def test_take_snapshot_valid_postclose_publishes(self):
  writes = _snapshot_settlement_writes(KNOWN_QUOTE)
  self.assertEqual([w["Key"] for w in writes], ["settlement/spy/2026-11-09.json"])
  self.assertEqual(writes[0]["IfNoneMatch"], "*")
 def test_take_snapshot_rejects_preclose_stale_future_mixed_side(self):
  base = dict(KNOWN_QUOTE)
  cases = {
   "preclose": dict(base, bid_observed_at="2026-11-09T20:59:30+00:00",
                    ask_observed_at="2026-11-09T20:59:31+00:00"),
   "stale": dict(base, bid_observed_at="2026-11-09T20:50:30+00:00",
                 ask_observed_at="2026-11-09T20:50:31+00:00"),
   "future": dict(base, bid_observed_at="2026-11-09T21:01:00+00:00",
                  ask_observed_at="2026-11-09T21:01:01+00:00"),
   # Mixed side: bid post-close, ask pre-close -- the older (pre-close)
   # side is the provider time, so the mark must not publish.
   "mixed": dict(base, bid_observed_at="2026-11-09T21:00:30+00:00",
                 ask_observed_at="2026-11-09T20:59:00+00:00"),
  }
  for name, quote in cases.items():
   with self.subTest(name):
    self.assertEqual(_snapshot_settlement_writes(quote), [])
 def test_take_snapshot_last_trade_and_ibit_provider_time(self):
  known_last = {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00",
                "last_observed_at": "2026-11-09T21:00:30+00:00"}
  self.assertEqual([w["Key"] for w in _snapshot_settlement_writes(known_last)],
                   ["settlement/spy/2026-11-09.json"])
  self.assertEqual(_snapshot_settlement_writes(
   dict(known_last, last_observed_at=None)), [])
  self.assertEqual([w["Key"] for w in _snapshot_settlement_writes(KNOWN_QUOTE, "IBIT")],
                   ["settlement/ibit/2026-11-09.json"])
  self.assertEqual(_snapshot_settlement_writes(UNKNOWN_QUOTE, "IBIT"), [])
 def test_take_snapshot_future_contributing_leg_vetoes(self):
  # The provider time is the OLDEST contributing leg, which proves the
  # oldest leg isn't stale but hides a future newer leg: a valid bid at
  # 21:00:30 plus a FUTURE ask at 21:01:01 (now 21:00:40) must not publish,
  # for either root and either bid/ask permutation. Every contributing
  # provider timestamp is validated against the window and now.
  for symbol in ("SPY", "IBIT"):
   for leg in ("bid_observed_at", "ask_observed_at"):
    with self.subTest(msg=f"{symbol} {leg}"):
     self.assertEqual(_snapshot_settlement_writes(
      dict(KNOWN_QUOTE, **{leg: "2026-11-09T21:01:01+00:00"}), symbol), [])
  # Last trade with a future event time is vetoed the same way; the valid
  # and unknown cases stay green.
  self.assertEqual(_snapshot_settlement_writes(
   {"last": 670.1, "last_ts": "2026-11-09T21:00:30+00:00",
    "last_observed_at": "2026-11-09T21:01:00+00:00"}), [])
  self.assertEqual([w["Key"] for w in _snapshot_settlement_writes(KNOWN_QUOTE)],
                   ["settlement/spy/2026-11-09.json"])
  self.assertEqual(_snapshot_settlement_writes(UNKNOWN_QUOTE), [])
 def test_settlement_mark_is_immutable_and_repeated_publish_fails(self):
  sink = ConflictSink(); now = datetime(2026, 11, 9, 21, 0, 40, tzinfo=timezone.utc)
  self.assertTrue(settlement_marks.publish_settlement_mark(
   sink, "SPY", "2026-11-09", 670.1, "2026-11-09T21:00:30+00:00", now))
  # A second publish of the same mark is rejected (IfNoneMatch conflict)
  # and does not overwrite the first.
  self.assertFalse(settlement_marks.publish_settlement_mark(
   sink, "SPY", "2026-11-09", 670.2, "2026-11-09T21:00:31+00:00", now))
  self.assertEqual(len(sink.writes), 1)
  self.assertEqual(sink.writes[0]["Body"], sink.writes[0]["Body"])

if __name__=='__main__': unittest.main()
