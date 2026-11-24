'use strict';

// Pair chronological episodes within the same part of the session. Match without inspecting labels.
// The activity signal forecasts magnitude; compare both cohorts on the same magnitude outcome.
function matchedActivity(rows, outcomes, open) {
  const byId = new Map();
  for (const outcome of outcomes) byId.set(outcome.id+'/'+outcome.t,outcome);
  const buckets = new Map();
  for (const r of rows) {
    if ((r.id!==2 && r.id!==6) || !r.episode) continue;
    const index = Math.floor((r.t-open)/1800000);
    if (!buckets.has(index)) buckets.set(index,{2:[],6:[]});
    buckets.get(index)[r.id].push(r);
  }
  const pairs = [];
  for (const [bucket, group] of buckets) {
    for (let i=0;i<Math.min(group[2].length, group[6].length);i++) {
      const a=group[2][i], b=group[6][i];
      pairs.push({bucket,signedAt:a.t,activityAt:b.t,
        signedRangeBps:byId.get('2/'+a.t)?.sampledRangeBps??null,
        activityRangeBps:byId.get('6/'+b.t)?.sampledRangeBps??null});
    }
  }
  return pairs;
}
module.exports={matchedActivity};
