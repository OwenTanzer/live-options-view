'use strict';

const MINUTE = 60000;
const FRAME_GRACE_MS = 15000;

// A watch starts when this service first attaches to the exchange session.
// The cursor records ownership even if the first poll never delivers a frame.
function createDeadlineTracker(db) {
  db.exec(`CREATE TABLE IF NOT EXISTS decision_watch (
    session TEXT PRIMARY KEY, attached_at INTEGER NOT NULL, next_t INTEGER NOT NULL, close INTEGER NOT NULL)`);
  const start = db.prepare('INSERT OR IGNORE INTO decision_watch VALUES (?,?,?,?)');
  const due = db.prepare('SELECT * FROM decision_watch WHERE next_t<=close AND next_t<? ORDER BY next_t');
  const advance = db.prepare('UPDATE decision_watch SET next_t=? WHERE session=?');
  const frameAt = db.prepare('SELECT 1 FROM frames WHERE session=? AND t=?');
  const insertFrame = db.prepare('INSERT INTO frames(session,t,body) VALUES (?,?,?)');
  const latest = db.prepare('SELECT body FROM frames WHERE session=? ORDER BY t DESC LIMIT 1');
  const insertDecision = db.prepare(`INSERT INTO decisions(session,t,id,evaluated_at,signal,until_t,reason,episode)
    VALUES (?,?,?, ?, NULL, COALESCE((SELECT until_t FROM decisions WHERE session=? AND id=? AND t<? ORDER BY t DESC LIMIT 1),0), ?,0)`);

  function attach({session,open,close}, now) {
    const first = open + Math.max(0, Math.ceil((now-open-FRAME_GRACE_MS)/MINUTE)) * MINUTE;
    start.run(session, now, first, close);
  }

  function expire(now, source = {}) {
    const cutoff = now - FRAME_GRACE_MS;
    if (!due.get(cutoff)) return 0;
    db.exec('BEGIN IMMEDIATE');
    let missed = 0;
    try {
      for (const watch of due.all(cutoff)) {
        let t = watch.next_t;
        for (; t <= watch.close && t < cutoff; t += MINUTE) {
          if (frameAt.get(watch.session, t)) continue;
          const reason = 'decision deadline expired without a timely flow frame';
          const frame = { session:watch.session, t, contracts:[], strikes:[], spot:null, snapshotTime:null,
            complete:false, minuteComplete:false, oiVerified:false, parityQualified:false,
            deadlineAt:t+FRAME_GRACE_MS, detectedAt:now, coverageReason:reason,
            observedSourceSession:source.session === watch.session ? source.flowDay ?? null : null,
            observedWatermark:source.session === watch.session ? source.watermark : null };
          insertFrame.run(watch.session, t, JSON.stringify(frame));
          for (let id = 1; id <= 11; id++) {
            insertDecision.run(watch.session,t,id,now,watch.session,id,t,reason);
          }
          missed++;
        }
        advance.run(t, watch.session);
      }
      db.exec('COMMIT');
      return missed;
    } catch (error) {
      db.exec('ROLLBACK');
      throw error;
    }
  }
  function latestCoverageError(session) {
    const row = latest.get(session);
    if (!row) return null;
    const f = JSON.parse(row.body);
    if (f.coverageReason) return f.coverageReason;
    return f.minuteComplete || f.openingReferenceComplete ? null : 'most recent decision frame has incomplete market coverage';
  }
  return {attach, expire, latestCoverageError};
}

module.exports = { createDeadlineTracker, FRAME_GRACE_MS };
