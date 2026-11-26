import { jsonResponse } from './common.js';
import { sessionAt, validSession } from '../site/shared/one-clock-session.js';

export async function regimeFlow(request, env, url) {
  if (request.method !== 'GET') return jsonResponse({error:'GET required'}, 405);
  const params = url.searchParams;
  const cutoff = Number(params.get('cutoff')), session = params.get('session');
  if ([...params.keys()].some(k => !['session','cutoff'].includes(k) || params.getAll(k).length !== 1)
      || !validSession(session) || !Number.isSafeInteger(cutoff) || cutoff < 1e12
      || cutoff > Date.now() + 60000 || sessionAt(cutoff) !== session) {
    return jsonResponse({error:'Invalid regime flow scope'}, 400);
  }
  try {
    const current = Math.floor(cutoff / 60000) * 60000;
    for (const minute of [current, current - 60000]) {
      if (sessionAt(minute) !== session) continue;
      const key = 'order-flow/qqq/regime/v1/' + minute + '.json';
      const object = await env.PAPER_TRADES.get(key);
      if (!object) continue;
      const published = object.uploaded?.getTime();
      if (!Number.isSafeInteger(published) || published > cutoff) continue;
      if (!Number.isSafeInteger(object.size) || object.size > 2048) throw new Error('Invalid sample size');
      const s = await object.json();
      if (s.session !== session || !Number.isSafeInteger(s.capturedAt)
          || s.capturedAt < minute || s.capturedAt >= minute + 60000
          || !Number.isSafeInteger(s.lastPrintAt) || sessionAt(s.lastPrintAt) !== session
          || s.lastPrintAt > s.capturedAt || typeof s.partial !== 'boolean'
          || [s.bullish,s.bearish,s.unclassified].some(n => !Number.isSafeInteger(n) || n < 0)) {
        throw new Error('Invalid sample');
      }
      const availableAt = Math.max(s.capturedAt, published);
      if (availableAt > cutoff) continue;
      return jsonResponse({rows:[{source:'regime-flow',id:key,revision:1,origin:'recorded',
        instrument:'QQQ',session,eventAt:s.capturedAt,valueAt:s.lastPrintAt,availableAt,
        evidence:key,value:{bullish:s.bullish,bearish:s.bearish,unclassified:s.unclassified,partial:s.partial}}]}, 200);
    }
    return jsonResponse({rows:[]}, 200);
  } catch {
    return jsonResponse({error:'Regime flow evidence unavailable'}, 503);
  }
}
