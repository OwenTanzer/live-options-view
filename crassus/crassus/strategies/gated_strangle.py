"""OA-233 forward paper variant, using shared completed five-minute QQQ bars."""
import math
from datetime import datetime, timedelta

from ..strategy import Decision, register
from .looking_glass_straddle import _leg_fill_time, _traded_today

ID = "qqq_gated_strangle"
CUTOFFS = ((10, 5), (10, 30), (11, 0), (12, 0), (13, 0))
LEVEL_SCALE = {10: (18.158266, 29.918556), 11: (13.922548, 21.251469),
               12: (10.849404, 15.542231), 13: (7.849231, 12.899183)}


def forecast(rows, cutoff, now):
    by_end = {datetime.fromisoformat(row["event_end"]): row["close"] for row in rows
              if datetime.fromisoformat(row["available_at"]) <= now}
    closes = [by_end.get(cutoff - timedelta(minutes=5*i)) for i in range(7)]
    if not all(x and x > 0 and math.isfinite(x) for x in closes):
        return None
    rv = 10000 * math.sqrt(sum(math.log(a/b)**2 for a, b in zip(closes, closes[1:])))
    level, scale = LEVEL_SCALE[cutoff.hour]
    return level * (max(rv, 4.293039) / scale)**0.69


def clean(q):
    return (q and q.is_executable and 0 <= q.age_seconds <= 15 and
            0 < q.bid <= q.ask and q.ask-q.bid <=
            (0.05 if (q.bid+q.ask)/2 < 0.5 else 0.05*(q.ask+q.bid)) + 1e-9)


def decide(ctx):
    def intent(action, reason, symbol=None, **metadata):
        return Decision(action, reason, ID, "1", symbol, 1 if symbol else None, metadata=metadata)
    now = ctx.now_et
    positions = ctx.book.positions
    if ctx.session_phase != "open":
        return intent("no_trade", "Market closed")
    if positions:
        first = min(filter(None, (_leg_fill_time(ctx, s) for s in positions)), default=None)
        calls = [s for s in positions if s[-9] == "C"]
        if not first or not calls or now-first >= timedelta(minutes=30):
            return intent("sell", "Close timed strangle", next(iter(positions)))
        if len(positions) == 2:
            return intent("no_trade", "Holding pair until timed exit")
        call = calls[0]
        put = call[:-9] + "P" + f"{int(call[-8:])-4000:08d}"
        prior_put = any((ctx.book._normalize(t) or (None,))[0] == put and
                        str(t.get("ts", ""))[:10] == now.date().isoformat() for t in ctx.book.trades)
        quote = ctx.quotes([put]).get(put)
        if prior_put or now-first >= timedelta(minutes=10) or not clean(quote):
            return intent("sell", "Unwind unmatched call", call)
        cost = 100*(positions[call].average_cost + quote.ask)+2.60
        if cost > min(250, budget(ctx, exclude=call)):
            return intent("sell", "Paired debit exceeds budget", call)
        return intent("buy", "Complete strangle at original lower strike", put)
    if _traded_today(ctx) or not ctx.snapshot or ctx.snapshot.expiration != now.date().isoformat():
        return intent("no_trade", "Already traded or no same-day chain")
    for h, m in CUTOFFS:
        cutoff = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if timedelta(minutes=1) <= now-cutoff < timedelta(minutes=6):
            break
    else:
        return intent("no_trade", "Outside entry window")
    score = forecast(ctx.params.get("_completed_bars", []), cutoff, now)
    if score is None or score < 13.352715:
        return intent("no_trade", "Gate closed or missing completed bars", forecast_bp=score)
    center = math.floor(ctx.snapshot.underlying_price + 0.5)
    prefix = "QQQ"+now.strftime("%y%m%d")
    call, put = prefix+"C"+f"{1000*(center+2):08d}", prefix+"P"+f"{1000*(center-2):08d}"
    quotes = ctx.quotes([call, put])
    if not all(clean(quotes.get(s)) for s in (call, put)):
        return intent("no_trade", "Missing tight executable pair")
    cost = 100*(quotes[call].ask+quotes[put].ask)+2.60
    if cost > min(250, budget(ctx)):
        return intent("no_trade", "Pair exceeds paper budget", quoted_cost=cost)
    return intent("buy", "Volatility gate open", call, forecast_bp=score, put=put, quoted_cost=cost)


def budget(ctx, exclude=None):
    cash = 300.0
    for trade in ctx.book.trades:
        fill = ctx.book._normalize(trade)
        if fill:
            symbol, side, qty, price = fill
            if symbol == exclude and side == "buy":
                continue
            cash += qty*((-1 if side == "buy" else 1)*price*100-0.65)
    return cash


decide.strategy_id = ID
decide.strategy_version = "1"
register(decide)
