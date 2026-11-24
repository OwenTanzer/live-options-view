// Private Yahoo chart receipts projected onto the shared One Clock cursor.
import {readFile,readdir} from 'node:fs/promises';
import {join} from 'node:path';
import {gunzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {selectAsOf,instant} from '../../site/shared/one-clock.js';
import {sessionAt,validSession} from '../../site/shared/one-clock-session.js';

export async function readYahooCandles(root,symbol,interval,session,cutoff) {
  if (!/^[A-Z0-9][A-Z0-9.-]{0,11}$/.test(symbol) || !['1m','1d'].includes(interval) || !validSession(session))
    throw new Error('Invalid Yahoo candle selection');
  const directory=join(root,symbol,interval);
  const latest=new Map();
  const receipts=(await readdir(directory)).filter(name=>name.endsWith('.receipt.json')).sort();
  let previous='';
  for (const name of receipts) {
    const receipt=JSON.parse(await readFile(join(directory,name),'utf8'));
    if (receipt.symbol!==symbol || receipt.interval!==interval || receipt.source!=='yahoo_finance_chart' ||
        !/^[a-f0-9]{64}$/.test(receipt.sha256)) throw new Error('Invalid Yahoo receipt');
    // Round sub-millisecond times up to the first eligible One Clock millisecond.
    const availableAt=instant(receipt.received_at)+(/\.\d{3}\d*[1-9]\d*(?:Z|[+-]\d\d:\d\d)$/.test(receipt.received_at)?1:0);
    if (availableAt>cutoff || receipt.sha256===previous) continue;
    const bytes=gunzipSync(await readFile(join(directory,receipt.sha256+'.json.gz')));
    if (createHash('sha256').update(bytes).digest('hex')!==receipt.sha256) throw new Error('Yahoo response digest mismatch');
    const chart=JSON.parse(bytes).chart?.result?.[0];
    if (chart?.meta?.symbol!==symbol || chart.meta.dataGranularity!==interval) throw new Error('Yahoo response identity mismatch');
    previous=receipt.sha256;
    const quotes=chart.indicators?.quote?.[0]??{};
    for (const [i,seconds] of (chart.timestamp??[]).entries()) {
      const eventAt=seconds*1000;
      if (!Number.isSafeInteger(eventAt) || eventAt>availableAt || sessionAt(eventAt)!==session) continue;
      const value=Object.fromEntries(['open','high','low','close','volume'].map(field=>[field,quotes[field]?.[i]??null]));
      const measured=['open','high','low','close'].every(field=>Number.isFinite(value[field]))?value:null;
      const old=latest.get(eventAt);
      if (old && JSON.stringify(old.value)===JSON.stringify(measured)) continue;
      latest.set(eventAt,{source:'yahoo-finance-candles',id:symbol+':'+interval+':'+seconds,
        revision:(old?.revision??0)+1,instrument:symbol,session,origin:'recorded',evidence:name,eventAt,availableAt,value:measured});
    }
  }
  return selectAsOf([...latest.values()],{session,instrument:symbol,origin:'recorded',cutoff});
}

if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  const [root,symbol,interval,session,cutoff]=process.argv.slice(2);
  console.log(JSON.stringify(await readYahooCandles(root,symbol,interval,session,instant(cutoff))));
}
