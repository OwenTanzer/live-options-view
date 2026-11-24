'use strict';

// Paper only. All strategies simulate one QQQ share at the observed market
// side; buy/cover at ask and sell/short at bid. No broker dependency.
const { DatabaseSync } = require('node:sqlite');
const evaluateA = require('./strategies-a.cjs');
const evaluateB = require('./strategies-b.cjs');
const minute = 60_000;
const maxAge = 120_000;
const ids = Array.from({length: 11}, (_, i) => i + 1);

function openPaper(filename) {
  const db = new DatabaseSync(filename);
  db.exec(`
    PRAGMA busy_timeout=10000;
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS sessions (session TEXT PRIMARY KEY, open INTEGER, close INTEGER);
    CREATE TABLE IF NOT EXISTS frames (session TEXT, t INTEGER, body TEXT, PRIMARY KEY(session,t));
    CREATE TABLE IF NOT EXISTS decisions (
      session TEXT, t INTEGER, id INTEGER, evaluated_at INTEGER, signal TEXT, until_t INTEGER,
      reason TEXT, episode INTEGER, PRIMARY KEY(session,t,id));
    CREATE TABLE IF NOT EXISTS quotes (t INTEGER PRIMARY KEY, received_at INTEGER, spot REAL,
      bid REAL, ask REAL, bid_at INTEGER, ask_at INTEGER);
    CREATE TABLE IF NOT EXISTS trades (
      session TEXT, id INTEGER, decision_t INTEGER, accepted_at INTEGER, due INTEGER,
      status TEXT, direction TEXT, range_low REAL, range_high REAL, trigger_at INTEGER,
      entry_t INTEGER, entry_price REAL, exit_t INTEGER, exit_price REAL, pnl REAL, reason TEXT,
      PRIMARY KEY(session,id,decision_t));
    CREATE UNIQUE INDEX IF NOT EXISTS one_order_per_slot ON trades(id)
      WHERE status IN ('watch','pending','open');
  `);
  return db;
}

function transaction(db, fn) {
  db.exec('BEGIN IMMEDIATE');
  try {
    const result = fn();
    db.exec('COMMIT');
    return result;
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }
}

function fresh(t, received, now) {
  return [t, received, now].every(Number.isSafeInteger) &&
    t <= received && received <= now && now - t <= maxAge;
}

function registerSession(db, {session,open,close}) {
  if (!/^\d{4}-\d\d-\d\d$/.test(session) || ![open,close].every(Number.isSafeInteger) || close <= open)
    throw Error('Invalid paper session');
  db.prepare('INSERT OR IGNORE INTO sessions VALUES (?,?,?)').run(session,open,close);
  const existing = db.prepare('SELECT open,close FROM sessions WHERE session=?').get(session);
  if (existing.open !== open || existing.close !== close) throw Error('Conflicting paper session bounds');
}

function frozenRange(db, t, open, now) {
  if (t - 5 * minute < open) return null;
  const rows = db.prepare('SELECT t,spot FROM quotes WHERE t BETWEEN ? AND ? AND received_at <= ? ORDER BY t')
    .all(t - 5 * minute, t, now);
  if (rows.length < 2 || rows[0].t - (t - 5 * minute) > maxAge ||
      t - rows.at(-1).t > maxAge || rows.some((r, i) => i && r.t - rows[i-1].t > maxAge)) return null;
  return {low: Math.min(...rows.map(r => r.spot)), high: Math.max(...rows.map(r => r.spot))};
}

function expireOrders(db, now) {
  db.prepare(`UPDATE trades SET status='skipped', reason='expired before observed fill'
    WHERE (status IN ('watch','pending') AND due<=?)
       OR (status='pending' AND COALESCE(trigger_at,accepted_at)+?<?)`).run(now,maxAge,now);
}

function applyFrame(db, {frame, open, close, receivedAt, eligibleSlots}, now = Date.now()) {
  if (!frame || !/^\d{4}-\d\d-\d\d$/.test(frame.session) ||
      ![frame.t,open,close,receivedAt,now].every(Number.isSafeInteger) ||
      frame.t < open || frame.t > close || (frame.t-open)%minute !== 0 ||
      receivedAt < frame.t || receivedAt > now || !Array.isArray(eligibleSlots) ||
      eligibleSlots.some(id => !ids.includes(id))) throw Error('Invalid paper frame envelope');
  return transaction(db, () => {
    registerSession(db,{session:frame.session,open,close});
    const prior = db.prepare('SELECT t FROM frames WHERE session=? ORDER BY t DESC LIMIT 1').get(frame.session);
    if (prior && prior.t >= frame.t) return {accepted:false,reason:'repeated or late minute'};
    expireOrders(db,now);
    db.prepare('INSERT INTO frames VALUES (?,?,?)').run(frame.session,frame.t,JSON.stringify(frame));
    const history = db.prepare('SELECT body FROM frames WHERE session=? ORDER BY t').all(frame.session)
      .map(row => JSON.parse(row.body));
    const last = prior ? db.prepare('SELECT * FROM decisions WHERE session=? AND t=?').all(frame.session,prior.t) : [];
    const decisions = [];
    for (const rule of [...evaluateA(history),...evaluateB(history)]) {
      const prev = last.find(r => r.id === rule.id);
      let reason = rule.reason;
      let signal = rule.signal;
      if (!eligibleSlots.includes(rule.id) || (rule.id === 9 && history[0].t !== open)) {
        reason = 'required forward input unavailable';
        signal = null;
      } else if (!fresh(frame.t,receivedAt,now) || now-frame.t >= minute) {
        reason = 'late decision minute';
        signal = null;
      }
      const due = frame.t + rule.horizon * minute;
      const episode = Boolean(signal && signal !== prev?.signal && frame.t >= (prev?.until_t || 0));
      db.prepare('INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)')
        .run(frame.session,frame.t,rule.id,now,signal,episode ? due : prev?.until_t || 0,reason,Number(episode));
      decisions.push({id:rule.id,reason,signal,episode});
      if (!episode) continue;
      const busy = db.prepare("SELECT 1 FROM trades WHERE id=? AND status IN ('watch','pending','open')").get(rule.id);
      let status = signal === 'range' ? 'watch' : 'pending';
      const range = signal === 'range' ? frozenRange(db,frame.t,open,now) : null;
      if (due > close || now >= due || busy || (signal === 'range' && !range)) {
        status = 'skipped';
        reason = busy ? 'slot occupied' : due > close || now >= due ? 'outside session horizon' : 'incomplete pre-signal range';
      }
      db.prepare(`INSERT INTO trades(session,id,decision_t,accepted_at,due,status,direction,range_low,range_high,reason)
        VALUES (?,?,?,?,?,?,?,?,?,?)`).run(frame.session,rule.id,frame.t,now,due,status,signal,range?.low??null,range?.high??null,reason);
    }
    return {accepted:true,decisions};
  });
}

function hasQuoteWork(db) {
  return Boolean(db.prepare("SELECT 1 FROM trades WHERE status IN ('watch','pending','open') LIMIT 1").get());
}

function applyQuote(db, {symbol,t,spot,receivedAt,bid,ask,bidAt,askAt}, now = Date.now()) {
  if (symbol !== 'QQQ' || !fresh(t,receivedAt,now) || !Number.isFinite(spot) || spot <= 0)
    return {accepted:false,reason:'invalid or stale QQQ quote'};
  if (![bid,ask].every(Number.isFinite) || bid<=0 || ask<bid ||
      ![bidAt,askAt].every(at=>Number.isSafeInteger(at) && at <= receivedAt && now-at<=15_000) ||
      t!==Math.min(bidAt,askAt))
    return {accepted:false,reason:'required fresh two-sided QQQ quote unavailable'};
  const fillPrice = side => side>0 ? ask : bid;
  return transaction(db, () => {
    const inSession = db.prepare('SELECT session FROM sessions WHERE open<=? AND close>=?').get(t,t);
    if (!inSession) return {accepted:false,reason:'outside registered paper session'};
    const last = db.prepare('SELECT t FROM quotes ORDER BY t DESC LIMIT 1').get();
    if (last && t <= last.t) return {accepted:false,reason:'repeated or late quote'};
    db.prepare('INSERT INTO quotes VALUES (?,?,?,?,?,?,?)').run(t,receivedAt,spot,bid,ask,bidAt,askAt);
    expireOrders(db,now);
    const orders = db.prepare("SELECT rowid,* FROM trades WHERE status IN ('watch','pending','open')").all();
    for (const order of orders) {
      const side = order.direction === 'long' ? 1 : -1;
      if (order.status === 'open') {
        if (t < order.due || t <= order.entry_t) continue;
        const price = fillPrice(-side);
        const reason = now-order.due > maxAge ? 'late recovery exit at observed price' : 'fixed-horizon exit';
        db.prepare("UPDATE trades SET status='closed',exit_t=?,exit_price=?,pnl=?,reason=? WHERE rowid=?")
          .run(t,price,side*(price-order.entry_price),reason,order.rowid);
      } else if (t > (order.trigger_at ?? order.accepted_at) && t < order.due) {
        if (order.status === 'watch') {
          if (!last || t-last.t > maxAge) {
            db.prepare("UPDATE trades SET status='skipped',reason='gap during breakout watch' WHERE rowid=?").run(order.rowid);
          } else if (spot > order.range_high || spot < order.range_low) {
            db.prepare("UPDATE trades SET status='pending',direction=?,trigger_at=? WHERE rowid=?")
              .run(spot>order.range_high ? 'long':'short',now,order.rowid);
          }
        } else {
          const price = fillPrice(side);
          const account = db.prepare("SELECT 10000+COALESCE(SUM(pnl),0) AS equity FROM trades WHERE id=?").get(order.id);
          if (price > account.equity) {
            db.prepare("UPDATE trades SET status='skipped',reason='insufficient one-share collateral' WHERE rowid=?").run(order.rowid);
          } else {
            db.prepare("UPDATE trades SET status='open',entry_t=?,entry_price=?,reason='synthetic one-share entry' WHERE rowid=?")
              .run(t,price,order.rowid);
          }
        }
      }
    }
    return {accepted:true};
  });
}

module.exports = {openPaper,registerSession,applyFrame,applyQuote,hasQuoteWork};
