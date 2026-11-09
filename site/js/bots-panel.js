// Reads /api/bots, a public read-only roster of the Crassus paper-trading
// accounts. Positions come back netted but unmarked; they're marked here
// against the shared liveQuotes service so a bot's mark is by construction the
// same number the Live tab shows for that contract.
const BOTS_POLL_MS = 30_000;
// A mark older than this no longer counts as a mark. LiveQuoteService keeps
// whatever it last received until the symbol leaves the interest set, so
// without an age check a quote frozen by an upstream outage would keep every
// account looking "complete" and keep ranking them on prices from minutes ago
// -- exactly the confidently-wrong total the completeness state exists to
// prevent. The quote poller runs on a 5s cycle while this tab is open, so 60s
// is a dozen missed cycles: an outage, not jitter. Matches the spirit of the
// 30s staleness bound TickerStateStore already applies to the price strip.
const BOT_MARK_MAX_AGE_MS = 60_000;
let botsTimer = null;
let botsData  = null;
let botsInFlight = false;
let botsError = null;

function setBotsPolling(on) {
  if (on) {
    if (botsTimer) return;
    fetchBots();
    botsTimer = setInterval(fetchBots, BOTS_POLL_MS);
  } else {
    if (botsTimer) clearInterval(botsTimer);
    botsTimer = null;
    // Stop paying for bot-only quotes once the tab is off screen.
    liveQuotes.setBotPositions([]);
  }
}

async function fetchBots() {
  if (botsInFlight) return;
  botsInFlight = true;
  try {
    const res = await fetch('/api/bots', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    botsData = await res.json();
    botsError = null;
    renderBots();
  } catch (err) {
    // Held in state rather than written straight to the status line: a quote
    // tick fires renderBots() every few seconds, which would otherwise paint
    // a fresh-looking "updated 12:34:56" over the warning while still showing
    // the last successful roster. The balances on screen are real but no
    // longer current, so the failure has to survive until a fetch succeeds.
    botsError = err.message;
    renderBots();
  } finally {
    botsInFlight = false;
  }
}

// Equity = cash + mark value of open positions, but only when every position
// has a mark.
//
// Skipping an unmarked leg and returning the remaining sum is worse than
// returning nothing: an unmarked *short* has its liability omitted, which
// inflates equity and could crown that account leader on the strength of a
// number nobody computed. So an account with any unmarked position reports
// complete:false and equity:null, and is excluded from leader selection and
// from strategy aggregation rather than ranked on a partial figure.
// A quote counts only while it is fresh. An absent quote and a frozen one are
// the same thing for ranking purposes: in both cases nobody knows what the leg
// is worth right now.
function botMark(sym) {
  const q = liveQuotes.get(sym);
  if (!q || q.mid == null) return null;
  const age = Date.now() - Date.parse(q.observedAt);
  // A non-finite age means no usable timestamp; a large negative one means
  // clock skew. Neither is evidence of freshness, so neither is trusted.
  if (!Number.isFinite(age) || age > BOT_MARK_MAX_AGE_MS || age < -30_000) return null;
  return q.mid;
}

function botEquity(bot) {
  let openValue = 0;
  let unmarked = 0;
  for (const p of bot.positions) {
    const mark = botMark(p.sym);
    if (mark == null) { unmarked++; continue; }
    openValue += mark * p.qty * (p.multiplier ?? 100);
  }
  const complete = unmarked === 0;
  return {
    complete,
    unmarked,
    openValue: complete ? openValue : null,
    equity: complete ? bot.balance_cash + openValue : null,
  };
}

// Aggregate PnL per strategy. With more than one strategy in play (#22 added
// reddit_sentiment_qqq alongside smoke_atm_roundtrip) the interesting
// comparison is strategy-vs-strategy, not just account-vs-account -- six
// accounts running one strategy only tell you about variance.
//
// A strategy with any unmarked leg is reported as partial rather than given a
// confident total, for the same reason individual cards are.
function renderStrategyRollup(scored) {
  const el = document.getElementById('bots-by-strategy');
  const byStrategy = new Map();
  for (const s of scored) {
    const id = s.bot.strategy_id || 'unassigned';
    const agg = byStrategy.get(id) || { id, accounts: 0, rankable: 0, pnl: 0, staked: 0, pending: 0 };
    agg.accounts++;
    // Only complete accounts contribute to the aggregate. Folding in an
    // account whose equity nobody could compute would rank a strategy on a
    // number that is partly missing.
    if (s.complete) {
      agg.rankable++;
      agg.pnl += s.equity - s.bot.starting_balance;
      agg.staked += s.bot.starting_balance;
    } else {
      agg.pending++;
    }
    byStrategy.set(id, agg);
  }

  // One strategy across the board means the rollup restates the cards below.
  if (byStrategy.size < 2) { el.innerHTML = ''; return; }

  // A strategy is only ranked when *every* one of its accounts is marked.
  // Ranking a subtotal from the complete accounts would order strategies on
  // figures covering different fractions of their books -- a 2-of-3 subtotal
  // sitting above a fully-marked rival says nothing about which is ahead.
  // Partial strategies still show their subtotal, explicitly labelled with the
  // coverage it represents, but are excluded from ordering and parked after
  // the ranked ones.
  el.innerHTML = [...byStrategy.values()]
    .sort((a, b) => {
      const aFull = a.pending === 0;
      const bFull = b.pending === 0;
      if (aFull !== bFull) return aFull ? -1 : 1;
      if (!aFull) return a.id.localeCompare(b.id);
      return b.pnl - a.pnl;
    })
    .map(a => {
      const full = a.pending === 0;
      const pct = a.staked ? (a.pnl / a.staked) * 100 : 0;
      const accounts = `${a.accounts} account${a.accounts === 1 ? '' : 's'}`;
      if (full) {
        return `<div class="strat-tile">
          <span class="strat-name">${escapeHtml(a.id)}</span>
          <span class="${pnlClass(a.pnl)}"> ${fmtMoney(a.pnl)}</span>
          <span class="strat-meta"> (${pct >= 0 ? '+' : '−'}${Math.abs(pct).toFixed(2)}%) · ${accounts}</span>
        </div>`;
      }
      // Nothing marked at all: there is no subtotal to show, partial or not.
      const subtotal = a.rankable
        ? `<span class="${pnlClass(a.pnl)}"> ${fmtMoney(a.pnl)}</span>
           <span class="strat-meta"> (${pct >= 0 ? '+' : '−'}${Math.abs(pct).toFixed(2)}%) subtotal</span>`
        : '';
      return `<div class="strat-tile">
        <span class="strat-name">${escapeHtml(a.id)}</span>
        ${subtotal}
        <span class="strat-meta bot-unmarked"> · ${a.rankable}/${a.accounts} marked — unranked</span>
      </div>`;
    }).join('');
}

// A roster fetch failure outranks anything the cards have to say: whatever is
// on screen came from an earlier successful poll and is no longer current.
function botsStatusLine(suffix) {
  return botsError ? `roster unavailable — ${botsError} · showing last known` : suffix;
}

// Display only: retain the original trade timestamp and financial history.
const botTradeDateFormat = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', year: 'numeric', month: 'short', day: 'numeric',
  hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true,
  timeZoneName: 'short',
});
function formatBotLastTrade(value) {
  if (value == null || (typeof value === 'string' && !value.trim())) return 'Not recorded';
  if (typeof value !== 'string' && typeof value !== 'number') return 'Invalid timestamp';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'Invalid timestamp';
  return `${botTradeDateFormat.format(date)} (New York)`;
}

function renderBots() {
  if (!botsData) {
    if (botsError) {
      document.getElementById('bots-status').textContent = botsStatusLine('');
    }
    return;
  }
  const grid = document.getElementById('bots-grid');
  const bots = botsData.bots || [];

  liveQuotes.setBotPositions(bots.flatMap(b => b.positions.map(p => p.sym)));

  if (!bots.length) {
    grid.innerHTML = '';
    // Clear the rollup too, or tiles from the last populated render survive
    // the liquidation of the final bot and read as live.
    document.getElementById('bots-by-strategy').innerHTML = '';
    document.getElementById('bots-status').textContent =
      botsStatusLine('no automated accounts registered yet');
    return;
  }

  // Rank by equity descending, with accounts that have no complete equity
  // parked after them -- they are unranked, not last-placed.
  const scored = bots.map(b => ({ bot: b, ...botEquity(b) }))
    .sort((a, b) => {
      if (a.complete !== b.complete) return a.complete ? -1 : 1;
      if (!a.complete) return a.bot.alias.localeCompare(b.bot.alias);
      return b.equity - a.equity;
    });
  const rankable = scored.filter(s => s.complete);
  const best = rankable.length ? rankable[0].equity : null;
  renderStrategyRollup(scored);

  grid.innerHTML = scored.map(({ bot, equity, complete, unmarked }) => {
    const pnl = complete ? equity - bot.starting_balance : null;
    const pct = complete && bot.starting_balance ? (pnl / bot.starting_balance) * 100 : 0;
    const dir = !complete ? 'flat' : pnl > 0 ? 'up' : pnl < 0 ? 'down' : 'flat';
    // Only a complete account can lead, and only when something else could
    // have beaten it.
    const isLeader = complete && rankable.length > 1 && equity === best;

    const posRows = bot.positions.map(p => {
      const mark = botMark(p.sym);
      const mult = p.multiplier ?? 100;
      const ppnl = mark != null && p.avg_price != null
        ? (mark - p.avg_price) * p.qty * mult : null;
      const label = p.instrument_type === 'share'
        ? escapeHtml(p.sym)
        : `${occRoot(p.sym || p.symbol) || p.underlying || 'QQQ'} ${p.strike} ${p.type === 'call' ? 'C' : 'P'}`;
      return `<tr>
        <td>${label}</td>
        <td>${p.qty > 0 ? '+' : ''}${p.qty}</td>
        <td>${p.avg_price != null ? fmtQ(p.avg_price) : '—'}</td>
        <td>${mark != null ? fmtQ(mark) : '—'}</td>
        <td class="${ppnl != null ? pnlClass(ppnl) : ''}">${ppnl != null ? fmtMoney(ppnl) : '—'}</td>
      </tr>`;
    }).join('');

    const state = bot.strategy_state && typeof bot.strategy_state === 'object'
      ? bot.strategy_state : null;
    const stateText = state
      ? `<div class="bot-strategy-state">${[
          state.signal_date ? `Bar ${escapeHtml(String(state.signal_date))}` : '',
          state.signal ? `Signal ${escapeHtml(String(state.signal))}` : '',
          state.close != null ? `Close ${Number(state.close).toFixed(2)}` : '',
          state.sma != null ? `SMA ${Number(state.sma).toFixed(2)}` : '',
          state.target ? `Target ${escapeHtml(String(state.target))}` : '',
          state.position ? `Position ${escapeHtml(String(state.position))}` : '',
          state.reason ? escapeHtml(String(state.reason)) : '',
        ].filter(Boolean).join(' · ')}</div>`
      : '';

    return `<div class="bot-card${isLeader ? ' leader' : ''}">
      <h4><span>${escapeHtml(bot.alias)}</span><span class="bot-user">${escapeHtml(bot.username)}</span></h4>
      <div class="bot-strategy">${bot.strategy_id ? escapeHtml(bot.strategy_id) : 'no strategy recorded'}</div>
      ${bot.account_closed ? '<div class="bot-strategy">Closed — trading stopped; history retained</div>' : ''}
      ${stateText}
      <div class="bot-equity">${complete ? `$${equity.toFixed(2)}` : '<span class="bot-unmarked">pending</span>'}</div>
      <div class="bot-pnl ${dir}">${complete
        ? `${fmtMoney(pnl)} (${pct >= 0 ? '+' : '−'}${Math.abs(pct).toFixed(2)}%)`
        : '<span class="bot-unmarked">awaiting marks</span>'}</div>
      <div class="bot-meta">
        Cash ${bot.balance_cash < 0 ? '−$' + Math.abs(bot.balance_cash).toFixed(2) : '$' + bot.balance_cash.toFixed(2)} · ${bot.trade_count} trade${bot.trade_count === 1 ? '' : 's'}
        <br><span class="bot-last-trade">Last trade: ${escapeHtml(formatBotLastTrade(bot.last_trade_ts))}</span>
        ${unmarked ? `<br><span class="bot-unmarked">${unmarked} position${unmarked === 1 ? '' : 's'} without a mark — equity and PnL withheld, and this account is unranked</span>` : ''}
      </div>
      ${bot.positions.length
        ? `<table><tr><th>Contract</th><th>Pos</th><th>Avg</th><th>Mark</th><th>PnL</th></tr>${posRows}</table>`
        : '<div class="bot-nopos">flat — no open positions</div>'}
    </div>`;
  }).join('');

  const asOf = botsData.as_of ? new Date(botsData.as_of).toLocaleTimeString() : '—';
  document.getElementById('bots-status').textContent =
    botsStatusLine(`${bots.length} account${bots.length === 1 ? '' : 's'} · updated ${asOf}`);
}
