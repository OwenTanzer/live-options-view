// Private Yahoo chart receipts projected onto the shared One Clock cursor.
import {readFile, readdir} from 'node:fs/promises';
import {join} from 'node:path';
import {gunzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {selectAsOf, instant} from '../../site/shared/one-clock.js';
import {sessionAt, validSession} from '../../site/shared/one-clock-session.js';

const calendar = JSON.parse(await readFile(new URL('../../backend/one-clock-nyse-calendar.json', import.meta.url)));
const fields = ['open','high','low','close','volume'];

async function eligibleReceipts(directory, symbol, interval, cutoff) {
  const receipts = [];
  for (const name of await readdir(directory)) {
    if (!name.endsWith('.receipt.json')) continue;
    const receipt = JSON.parse(await readFile(join(directory,name),'utf8'));
    if (receipt.symbol !== symbol || receipt.interval !== interval || receipt.source !== 'yahoo_finance_chart' ||
        !/^[a-f0-9]{64}$/.test(receipt.sha256)) throw new Error('Invalid Yahoo receipt: '+name);
    const ms = instant(receipt.received_at);
    const fraction = receipt.received_at.match(/\.(\d+)(?:Z|[+-]\d\d:\d\d)$/)?.[1] ?? '';
    const subms = fraction.padEnd(9,'0').slice(3,9);
    const availableAt = ms + (Number(subms) > 0 ? 1 : 0);
    if (availableAt <= cutoff) receipts.push({name, digest:receipt.sha256, ms, subms, availableAt});
  }
  return receipts.sort((a,b)=>a.ms-b.ms || a.subms.localeCompare(b.subms) || a.name.localeCompare(b.name));
}

export async function readYahooCandles(root,symbol,interval,session,cutoff) {
  if (!/^[A-Z0-9][A-Z0-9.-]{0,11}$/.test(symbol) || !['1m','1d'].includes(interval) || !validSession(session))
    throw new Error('Invalid Yahoo candle selection');
  cutoff = instant(cutoff);
  if (interval === '1m' && (session < calendar.start || session > calendar.end))
    throw new Error('Exchange calendar does not cover '+session);
  const hours = calendar.sessions[session];
  if (interval === '1m' && !hours) return [];
  const directory = join(root,symbol,interval);
  const latest = new Map();
  let previousDigest;
  for (const receipt of await eligibleReceipts(directory,symbol,interval,cutoff)) {
    if (receipt.digest === previousDigest) continue;
    const bytes = gunzipSync(await readFile(join(directory,receipt.digest+'.json.gz')));
    if (createHash('sha256').update(bytes).digest('hex') !== receipt.digest) throw new Error('Yahoo response digest mismatch');
    const chart = JSON.parse(bytes).chart?.result?.[0];
    if (chart?.meta?.symbol !== symbol || chart.meta.dataGranularity !== interval) {
      console.warn('Skipping Yahoo response with mismatched identity:',receipt.name);
      continue;
    }
    previousDigest = receipt.digest;
    const quotes = chart.indicators?.quote?.[0] ?? {};
    const timestamps = chart.timestamp ?? [];
    const seen = new Map();
    let first = Infinity, last = -Infinity;
    for (const [i,seconds] of timestamps.entries()) {
      const eventAt = seconds * 1000;
      if (!Number.isSafeInteger(seconds) || !Number.isSafeInteger(eventAt) || eventAt > receipt.availableAt) continue;
      first = Math.min(first,eventAt);
      last = Math.max(last,eventAt);
      if (interval === '1m' ? eventAt < hours[0] || eventAt >= hours[1] : sessionAt(eventAt) !== session) continue;
      const value = Object.fromEntries(fields.map(field=>[field,quotes[field]?.[i] ?? null]));
      seen.set(eventAt,fields.slice(0,4).every(field=>Number.isFinite(value[field])) ? value : null);
    }
    // Omission inside this response's coverage retracts the prior reading.
    // Outside the two endpoints an older bar may merely have aged out of the query.
    for (const eventAt of latest.keys()) {
      if (eventAt >= first && eventAt <= last && !seen.has(eventAt)) seen.set(eventAt,null);
    }
    for (const [eventAt,value] of seen) {
      const old = latest.get(eventAt);
      if (old && JSON.stringify(old.value) === JSON.stringify(value)) continue;
      latest.set(eventAt,{source:'yahoo-finance-candles',id:symbol+':'+interval+':'+eventAt/1000,
        revision:(old?.revision ?? 0)+1,instrument:symbol,session,origin:'recorded',
        evidence:receipt.name,eventAt,availableAt:receipt.availableAt,value});
    }
  }
  return selectAsOf([...latest.values()],{session,instrument:symbol,origin:'recorded',cutoff});
}

if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  const [root,symbol,interval,session,cutoff]=process.argv.slice(2);
  console.log(JSON.stringify(await readYahooCandles(root,symbol,interval,session,instant(cutoff))));
}
