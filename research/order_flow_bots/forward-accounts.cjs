'use strict';

const {DatabaseSync} = require('node:sqlite');

// All slots start with $10,000 in the paper engine. A closed-P&L balance
// excludes unrealized movement on a currently open long or short share.
function readAccounts(path) {
  const db = new DatabaseSync(path,{readOnly:true});
  try {
    db.exec('PRAGMA busy_timeout=5000; BEGIN');
    const latestDecision = db.prepare('SELECT * FROM decisions WHERE id=? ORDER BY t DESC LIMIT 1');
    const active = db.prepare("SELECT * FROM trades WHERE id=? AND status IN ('watch','pending','open')");
    const lastTrade = db.prepare('SELECT * FROM trades WHERE id=? ORDER BY decision_t DESC LIMIT 1');
    const totals = db.prepare(`SELECT COUNT(CASE WHEN status='closed' THEN 1 END) AS closed_trades,
      COUNT(CASE WHEN status='skipped' THEN 1 END) AS skipped_episodes,
      COALESCE(SUM(pnl),0) AS realized_pnl FROM trades WHERE id=?`);
    const accounts = Array.from({length:11},(_,i) => {
      const slot = i+1;
      const total = totals.get(slot);
      const order = active.get(slot) ?? null;
      let position = 0;
      if (order?.status === 'open') position = order.direction === 'long' ? 1 : -1;
      return {slot, position_shares:position, realized_balance:10000+total.realized_pnl,
        ...total, active_order:order, last_trade:lastTrade.get(slot) ?? null,
        last_decision:latestDecision.get(slot) ?? null};
    });
    const session = db.prepare('SELECT * FROM sessions ORDER BY open DESC LIMIT 1').get() ?? null;
    const quote = db.prepare('SELECT * FROM quotes ORDER BY t DESC LIMIT 1').get() ?? null;
    db.exec('COMMIT');
    return {instrument:'QQQ', mode:'isolated_forward_paper', latest_session:session, latest_quote:quote, accounts};
  } finally {
    db.close();
  }
}

if (require.main === module) {
  if (process.argv.length !== 3) {
    console.error('Usage: node forward-accounts.cjs PATH_TO_PAPER_DATABASE');
    process.exitCode = 2;
  } else {
    try {
      console.log(JSON.stringify(readAccounts(process.argv[2]),null,2));
    } catch (error) {
      console.error('Cannot read paper accounts: '+error.message);
      process.exitCode = 1;
    }
  }
}

module.exports = {readAccounts};
