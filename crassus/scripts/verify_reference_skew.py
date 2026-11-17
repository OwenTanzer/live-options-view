"""Exercise the reference strategies without a runner or order execution."""
from verify_oi_skew import FLAT_ROWS, SKEWED_BULLISH_ROWS, fresh_quote, make_ctx
from crassus.replay import reset_strategy_state
from crassus.strategy import get


def context(root, rows, timestamp, widened=False):
    board = []
    for row in rows:
        strike = row["Strike"]
        if widened:
            strike = 400 + (strike - 400) * 3
        symbol = f"{root}240101{row['OptionSymbol'][9]}{round(strike * 1000):08d}"
        board.append(dict(row, Strike=strike, OptionSymbol=symbol))
    quotes = {r["OptionSymbol"]: fresh_quote(r["OptionSymbol"]) for r in board}
    return make_ctx(rows=board, timestamp=timestamp, quote_map=quotes)


for root, name in (("SPY", "oi_skew_spy"), ("IBIT", "oi_skew_ibit_adaptive")):
    decide = get(name)
    reset_strategy_state(name)
    anchor = decide(context(root, FLAT_ROWS, "2024-01-01T15:00:00Z", root == "IBIT"))
    assert anchor.action == "no_trade" and anchor.metadata["imbalance_ratio"] == 0
    changed = context(root, SKEWED_BULLISH_ROWS, "2024-01-01T15:01:00Z", root == "IBIT")
    entry = decide(changed)
    assert entry.action == "buy" and entry.symbol.startswith(root) and entry.quantity == 1
    assert entry.strategy_id == name and entry.metadata["band_strikes"] == 5
    assert decide(changed).action == "no_trade"
    assert decide(context("QQQ", FLAT_ROWS, "2024-01-01T15:02:00Z")).action == "no_trade"
    thin = decide(context(root, FLAT_ROWS[:2], "2024-01-01T15:03:00Z"))
    assert thin.action == "no_trade"
    print(root, "anchor, drift, exact contract, repeat, mismatched board and thin book passed")
