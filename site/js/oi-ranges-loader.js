// OIranges loader — extracted from docs/index.html (B2, readability quest,
// Nov 2026). Classic script, loaded by index.html in the same position the
// inline section occupied, so the page's script order — and the shared global
// scope — is unchanged. No ESM conversion.
// ── OIranges loader ───────────────────────────────────────────────────────────
// parseRangesCsv / parseCsv / getThresh / oiBucket / buildHeatmapRows / DISPLAY /
// TIER_COLORS / fmt*/quoteHtml/flowHtml live in shared/heatmap.js (also used by diag.html).
async function loadRanges() {
  try {
    const resp = await fetch(`${BROWSER_DATA}/derived/OIranges.csv`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    ranges = parseRangesCsv(await resp.text());
    document.getElementById('cal-error').style.display = 'none';
  } catch (e) {
    console.warn('OIranges load failed:', e);
    // No periodic timer of its own (unlike the heatmap/prices/health fetches) —
    // a single transient failure here used to mean the calibration banner (and
    // "wall" coloring for every nonzero cell) stayed broken for the rest of the
    // session. Retried from the visibility handler below and a slow safety-net
    // interval so a one-off blip self-heals instead of needing a manual reload.
    document.getElementById('cal-error').style.display = 'block';
  }
}

// All calibrated QQQ views ask for the same immutable opening reference.
// Cache successes per session. A known reference remains invisible on rewind
// until its recorded publication time, including when it was seen previously.
const openingOiCache=new Map(), openingOiReads=new Map();
async function loadOpeningOi(session,cutoff) {
  if(!/^\d{4}-\d{2}-\d{2}$/.test(session) || !Number.isSafeInteger(cutoff))return {reason:'invalid-opening-scope'};
  const cached=openingOiCache.get(session);
  if(cached && cached.availableAt<=cutoff && cached.complete)return cached;
  const query=new URLSearchParams({session,cutoff:String(cutoff)}).toString();
  if(!openingOiReads.has(query))openingOiReads.set(query,(async()=>{
    try {
      const response=await fetch('/api/one-clock/opening-oi?'+query,{cache:'no-store'});
      if(!response.ok)return {reason:'opening-evidence-unavailable'};
      const map=await response.json();
      if(!Array.isArray(map.levels)||map.session!==session||map.availableAt>cutoff)return {reason:map.reason??'opening-evidence-unavailable'};
      if(openingOiCache.size>=32)openingOiCache.delete(openingOiCache.keys().next().value);
      openingOiCache.set(session,map);return map;
    }catch{return {reason:'opening-evidence-unavailable'};}
  })().finally(()=>openingOiReads.delete(query)));
  return openingOiReads.get(query);
}
function openingOiNote(opening) {
  return opening?.levels?'OI colors fixed at the regular-session open · opening ATM $'+opening.calibration.openingAtm+
    ' · settled '+opening.calibration.settledSession+(opening.gaps?' · partial coverage':''):
    'Opening OI unavailable · unqualified cells are uncolored';
}
