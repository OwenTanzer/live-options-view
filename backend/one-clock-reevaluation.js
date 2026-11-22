import { equitySession } from './one-clock-calendar.js';
const stmt = (db, sql, ...args) => db.prepare(sql).bind(...args);
const rows = async statement => (await statement.all()).results;
const BATCH = 24;

export async function bootstrapHistory(db) {
  const checkpoint = await stmt(db,'SELECT * FROM one_clock_interval_bootstrap WHERE id=1').first();
  if (!checkpoint?.done) {
    const cursor = JSON.parse(checkpoint?.cursor ?? '["","",-1,-1]');
    const page = await rows(stmt(db,`SELECT namespace,rule_id,version,started_at FROM one_clock_activations
      WHERE (namespace,rule_id,version,started_at)>(?,?,?,?) ORDER BY namespace,rule_id,version,started_at LIMIT ?`,...cursor,BATCH));
    const last = page.at(-1);
    const next = last ? JSON.stringify([last.namespace,last.rule_id,last.version,last.started_at]) : JSON.stringify(cursor);
    // Reread intervals inside the transaction, so a pause between the page
    // read and the index build cannot resurrect an open-ended interval.
    await db.batch([
      ...page.map(a=>stmt(db,`INSERT INTO one_clock_interval_build SELECT namespace,rule_id,version,started_at,ended_at
        FROM one_clock_activations WHERE namespace=? AND rule_id=? AND version=? AND started_at=?`,a.namespace,a.rule_id,a.version,a.started_at)),
      stmt(db,`INSERT INTO one_clock_interval_bootstrap VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,done=excluded.done
        WHERE one_clock_interval_bootstrap.cursor=? AND one_clock_interval_bootstrap.done=0`,next,Number(page.length<BATCH),checkpoint?.cursor??'')
    ]);
    if (page.length===BATCH) return false;
  }
  const work=await stmt(db,'SELECT * FROM one_clock_change_bootstrap WHERE last_sequence<end_sequence LIMIT 1').first();
  if (work) {
    const end=Math.min(work.end_sequence,work.last_sequence+BATCH);
    await db.batch([
      stmt(db,`INSERT OR IGNORE INTO one_clock_native_changes(stream,sequence,session,instrument,received_at,bootstrap)
        SELECT DISTINCT e.stream,e.sequence,json_extract(j.value,'$.session'),substr(e.stream,instr(e.stream,'/')+1),e.received_at,1
        FROM one_clock_publication_entries e,json_each(e.payload,'$.records') j WHERE e.stream=? AND e.sequence>? AND e.sequence<=?
        ORDER BY e.sequence`,work.stream,work.last_sequence,end),
      stmt(db,'UPDATE one_clock_change_bootstrap SET last_sequence=? WHERE stream=? AND last_sequence=?',end,work.stream,work.last_sequence)
    ]);
    return false;
  }
  return true;
}

export async function fanoutChanges(db,now) {
  if (!await bootstrapHistory(db)) return {state:'indexing'};
  const work=await stmt(db,'SELECT * FROM one_clock_fanout WHERE retry_at<=? ORDER BY retry_at,enqueued_at LIMIT 1',now).first();
  if (!work) return {state:'idle'};
  let bounds;
  try {bounds=equitySession(work.session);} catch(error) {
    await stmt(db,'UPDATE one_clock_fanout SET retry_at=?,reason=? WHERE session=? AND instrument=? AND target=?',now+3600000,error.message,work.session,work.instrument,work.target).run();
    return {state:'calendar-unavailable',session:work.session,instrument:work.instrument};
  }
  const buckets=[];
  if(bounds) for(let b=131072+Math.floor(bounds.open/86400000);b>=1;b=Math.floor(b/2)) buckets.push(b);
  const cursor=JSON.parse(work.cursor);
  let index=cursor[0]??0, key=cursor.slice(1),remaining=BATCH;
  const selected=[];
  while(index<buckets.length && remaining) {
    const page=await rows(stmt(db,`SELECT i.namespace,i.rule_id,i.version,i.started_at,a.ended_at
      FROM one_clock_interval_index i CROSS JOIN one_clock_activations a
      ON (a.namespace,a.rule_id,a.version,a.started_at)=(i.namespace,i.rule_id,i.version,i.started_at)
      WHERE i.instrument=? AND i.bucket=? AND (i.namespace,i.rule_id,i.version,i.started_at)>(?,?,?,?)
      ORDER BY i.namespace,i.rule_id,i.version,i.started_at LIMIT ?`,
      work.instrument,buckets[index],...(key.length?key:['','',-1,-1]),remaining));
    for(const a of page) if(a.started_at<=bounds.close && (a.ended_at===null || a.ended_at>=bounds.open)) selected.push(a);
    const more=page.length===remaining;
    remaining-=page.length;
    if(more) {const a=page.at(-1);key=[a.namespace,a.rule_id,a.version,a.started_at];}
    else {index++;key=[];}
  }
  const args=[work.session,work.instrument,work.target,work.cursor];
  const writes=selected.map(a=>stmt(db,`INSERT INTO one_clock_historical_rules
    (namespace,session,instrument,rule_id,version,target,next_target,enqueued_at,retry_at) SELECT ?,?,?,?,?,?,?,?,?
    WHERE EXISTS(SELECT 1 FROM one_clock_fanout WHERE session=? AND instrument=? AND target=? AND cursor=?)
    AND NOT EXISTS(SELECT 1 FROM one_clock_generation_receipts WHERE namespace=? AND session=? AND instrument=? AND rule_id=? AND version=? AND generation>=?)
    ON CONFLICT(namespace,session,instrument,rule_id,version) DO UPDATE SET next_target=MAX(next_target,excluded.next_target)`,
    a.namespace,work.session,work.instrument,a.rule_id,a.version,work.target,work.target,work.enqueued_at,work.enqueued_at,...args,
    a.namespace,work.session,work.instrument,a.rule_id,a.version,work.target));
  if(index<buckets.length) writes.push(stmt(db,'UPDATE one_clock_fanout SET cursor=?,retry_at=? WHERE session=? AND instrument=? AND target=? AND cursor=?',JSON.stringify([index,...key]),now,...args));
  else {
    writes.push(stmt(db,'DELETE FROM one_clock_fanout WHERE session=? AND instrument=? AND target=? AND cursor=? AND target=next_target',...args));
    writes.push(stmt(db,"UPDATE one_clock_fanout SET target=next_target,cursor='[]',retry_at=? WHERE session=? AND instrument=? AND target=? AND cursor=? AND next_target>target",now,...args));
  }
  await db.batch(writes);
  return {state:'discovered',generation:work.target,queueLagMs:Math.max(0,now-work.enqueued_at)};
}

export async function snapshotVersion(db,session,instrument,cutoff,target=null) {
  // Each stream's forward and migration scans are individually ordered by
  // sequence and time. Two index seeks per lane intersect both boundaries,
  // even when the migration imports an older entry after new traffic arrives.
  const eligible=await rows(stmt(db,`SELECT c.stream,c.sequence,c.generation,c.received_at
    FROM one_clock_change_lanes l CROSS JOIN one_clock_native_changes c
    WHERE l.session=? AND l.instrument=? AND c.generation=MIN(
      (SELECT x.generation FROM one_clock_native_changes x WHERE
        (x.session,x.instrument,x.stream,x.bootstrap)=(l.session,l.instrument,l.stream,l.bootstrap) AND x.received_at<=?
        ORDER BY x.received_at DESC,x.generation DESC LIMIT 1),
      (SELECT x.generation FROM one_clock_native_changes x WHERE
        (x.session,x.instrument,x.stream,x.bootstrap)=(l.session,l.instrument,l.stream,l.bootstrap) AND x.generation<=?
        ORDER BY x.generation DESC LIMIT 1))`,session,instrument,cutoff,target??Number.MAX_SAFE_INTEGER));
  const sequences={};
  for(const row of eligible)sequences[row.stream]=Math.max(sequences[row.stream]??0,row.sequence);
  return {generation:Math.max(0,...eligible.map(r=>r.generation)),sequences,
    receivedAt:Math.max(0,...eligible.map(r=>r.received_at))};
}

export function generationWrites(db,job,rules,generation,now) {
  return rules.map(r=>stmt(db,`INSERT INTO one_clock_generation_receipts(namespace,session,instrument,rule_id,version,generation,committed_at,frozen)
    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING`,job.namespace,job.session,job.instrument,r.rule_id,r.version,generation,now,Number(job.target!==undefined)));
}

export async function finishHistory(db,job,now,failure=null) {
  const key=[job.namespace,job.session,job.instrument,job.rule_id,job.version,job.target];
  const where='WHERE namespace=? AND session=? AND instrument=? AND rule_id=? AND version=? AND target=?';
  if(failure) {
    const delay=Math.min(3600000,60000*2**Math.min(6,job.failures));
    await stmt(db,'UPDATE one_clock_historical_rules SET failures=failures+1,retry_at=?,reason=? '+where,now+delay,failure,...key).run();
  } else await db.batch([
    stmt(db,'DELETE FROM one_clock_historical_rules '+where+' AND target=next_target',...key),
    stmt(db,'UPDATE one_clock_historical_rules SET target=next_target,retry_at=?,failures=0,reason=NULL '+where+' AND target<next_target',now,...key)
  ]);
}
