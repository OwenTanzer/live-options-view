import {archivedChainSource} from './one-clock-chain-source.js';
import {sessionAt, validSession} from './one-clock-session.js';

const MINUTE = 60_000;
const validTime = n => Number.isSafeInteger(n) && n >= Date.UTC(2000, 0) && n <= 8.64e15;

function validTarget(t) {
  return validSession(t.session) && validTime(t.fillAt) && validTime(t.cutoff) &&
    t.fillAt <= t.cutoff && sessionAt(t.fillAt) === t.session &&
    /^pos:[0-9a-f]{16}$/.test(t.positionId) &&
    typeof t.executionId === 'string' && /^[\w:. -]{1,256}$/.test(t.executionId);
}

// The fragment stays in the browser; private identity is retrieved through the journal session.
export function journalFillReplayHref(event, cutoff) {
  const fill = event?.journalFill;
  if (!fill) return null;
  const {positionId, executionId, fillAt} = fill;
  const session = validTime(fillAt) ? sessionAt(fillAt) : null;
  if (!validTarget({positionId, executionId, fillAt, cutoff, session})) return null;
  return '/trade-journal#' + new URLSearchParams({journal_fill:executionId, position:positionId, session,
    fill_at:String(fillAt), as_of:String(cutoff)});
}

export function journalFillReplayTarget(hash) {
  const p = new URLSearchParams(hash.replace(/^#/, ''));
  if (!p.has('journal_fill')) return null;
  const keys = ['journal_fill', 'position', 'session', 'fill_at', 'as_of'];
  const target = {executionId:p.get('journal_fill'), positionId:p.get('position'), session:p.get('session'),
    fillAt:Number(p.get('fill_at')), cutoff:Number(p.get('as_of'))};
  if (keys.some(k => p.getAll(k).length !== 1) || !validTarget(target)) throw new Error('Invalid journal fill replay link.');
  return target;
}

export function locateJournalFill(positions, target) {
  const position = positions.find(p => p.position_id === target.positionId && p.source_status === 'active');
  const fill = position?.executions.find(e => e.execution_id === target.executionId && Date.parse(e.ts) === target.fillAt);
  return fill ? {position, fill} : null;
}

// Each sample has its own publication cutoff. Older values outside the source's declared
// freshness window are gaps. Repeated receipts appear once at their actual observation time.
export async function journalFillMarketContext(fill, target, signal, source=archivedChainSource()) {
  const instrument = fill.instrument.underlying;
  const context = {underlying:null, option:null, gaps:[], coverage:[], notes:[]};
  if (!['QQQ','SPY','IBIT'].includes(instrument)) {
    context.gaps.push(instrument + ' underlying and contract: no qualified chain archive');
    return context;
  }
  const cutoffs = [...new Set([-4,-2,0,2,4].map(n => Math.min(target.fillAt + n * MINUTE, target.cutoff)))]
    .filter(at => sessionAt(at) === target.session);
  const underlying = new Map(), option = new Map();
  let uncovered = 0;
  for (const cutoff of cutoffs) {
    signal.throwIfAborted();
    let row;
    try {
      [row] = await source.read({session:target.session,instrument,cutoff,signal});
    } catch (error) {
      if (signal.aborted) throw error;
    }
    if (!row || row.eventAt > cutoff || row.availableAt === null || row.availableAt > cutoff ||
        cutoff - row.eventAt > source.maxAgeMs || !row.value) { uncovered++; continue; }
    const at = new Date(row.eventAt).toISOString();
    if (row.value.underlying_price > 0) underlying.set(at,[at,row.value.underlying_price]);
    const symbol = fill.instrument.occ_symbol?.replaceAll(' ','');
    const contract = symbol && row.value.rows.find(c => c.OptionSymbol?.replaceAll(' ','') === symbol);
    if (contract && Number.isFinite(contract.Bid) && Number.isFinite(contract.Ask) && contract.Bid >= 0 && contract.Ask >= contract.Bid)
      option.set(at,[at,contract.Bid,contract.Ask]);
  }
  const series = (map, label) => map.size ? {source:label, segments:[{kind:'points', points:[...map.values()].sort((a,b)=>a[0].localeCompare(b[0]))}]} : null;
  context.underlying = series(underlying, 'One Clock · sampled underlying prices');
  context.option = series(option, 'One Clock · sampled exact-contract bid/ask');
  context.coverage.push(`${underlying.size} underlying and ${option.size} exact-contract observations across ${cutoffs.length} sampling instants; ${uncovered} without a fresh chain.`);
  if (!context.underlying) context.gaps.push(instrument + ' underlying: no qualified observations in this window');
  if (!context.option) context.gaps.push('No qualified exact-contract quotes in this window');
  return context;
}
