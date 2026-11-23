'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { minuteFrames } = require('./frames.cjs');
const evaluateA = require('./strategies-a.cjs');
const evaluateB = require('./strategies-b.cjs');

function decisions(frames) {
  const history = [], rows = [], last = new Map();
  for (const frame of frames) {
    history.push(frame);
    for (const rule of [...evaluateA(history),...evaluateB(history)]) {
      const state = last.get(rule.id) || { signal:null, until:0 };
      const changed = rule.signal && rule.signal !== state.signal;
      const episode = Boolean(changed && frame.t >= state.until);
      last.set(rule.id,{signal:rule.signal,until:episode ? frame.t+rule.horizon*60000 : state.until});
      rows.push({version:1,session:frame.session,t:frame.t,origin:'reconstructed',snapshotTime:frame.snapshotTime,
        complete:frame.complete,minuteComplete:frame.minuteComplete,spot:frame.spot,...rule,episode});
    }
  }
  return rows;
}

const { labels, anchors } = require('./outcomes.cjs');
const { matchedActivity } = require('./comparison.cjs');
const { eventStudy } = require('./event-builder.cjs');
const { paper } = require('./paper.cjs');

function run() {
  const input=process.argv[2] || path.join(__dirname,'../../site/order-flow-replay.json');
  const bytes=fs.readFileSync(input), hash=crypto.createHash('sha256').update(bytes).digest('hex');
  const replay=JSON.parse(bytes);
  if (replay.session==='2026-09-25' && hash!=='39c0926059972f38cdb3d81a4ca1942f1c9ea614017c92a1660ad8c54b6000b9') throw Error('Audited replay checksum mismatch');
  const oiEvidence=process.argv[4] ? JSON.parse(fs.readFileSync(process.argv[4],'utf8')) : null;
  const frames=[...minuteFrames(replay,oiEvidence)], rows=decisions(frames);
  const allAnchors=anchors(rows,replay), outcomes=labels(rows,replay,allAnchors);
  const forecastMap=new Map(allAnchors.map(a=>[a.t+'/'+a.horizon,a]));
  const paired=matchedActivity(rows,outcomes.map(o=>({...o,
    sampledRangeBps:forecastMap.get(o.t+'/15')?.sampledRangeBps})),replay.coverage.open);
  const simulated=paper(rows,replay);
  const summary=Array.from({length:11},(_,i)=>{
    const rule=rows.filter(r=>r.id===i+1), scored=outcomes.filter(r=>r.id===i+1&&r.valueBps!==null);
    return {id:i+1,decisions:rule.length,activeMinutes:rule.filter(r=>r.signal).length,
      episodes:rule.filter(r=>r.episode).length,scored:scored.length};
  });
  if (process.argv[3]) {
    // Create a fresh directory to preserve earlier runs and prevent partial overwrites.
    const dir=process.argv[3]; fs.mkdirSync(dir);
    for (const [name,data] of Object.entries({decisions:rows,outcomes,anchors:allAnchors,frames})) {
      fs.writeFileSync(path.join(dir,name+'.jsonl'),data.map(r=>JSON.stringify(r)).join('\n')+'\n');
    }
    for (const [name,data] of Object.entries({paired,paper:simulated,oneClock:eventStudy(rows,replay.session)})) {
      fs.writeFileSync(path.join(dir,name+'.json'),JSON.stringify(data));
    }
    fs.writeFileSync(path.join(dir,'summary.json'),JSON.stringify({session:replay.session,hash,summary},null,2));
  }
  console.log(JSON.stringify({session:replay.session,hash,summary},null,2));
}
if(require.main===module) run();
module.exports={decisions,labels};
