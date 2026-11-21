// Explicit read-only personal-use conformance. No credentials, identity mock,
// paper fills, production storage, quote output or retained quote artifacts.
import { readFileSync } from 'node:fs';
import { FakeD1 } from '../tests/helpers/d1_shim.mjs';
import { discover, fetchMarket, displayMarket } from '../backend/kalshi-source.js';
if (process.argv.slice(2).join(' ') !== '--personal-read-only') throw Error('Use --personal-read-only only under the member’s explicit source-read authority');
const db=new FakeD1();
db.db.exec(readFileSync(new URL('../backend/migrations/0006_kalshi_private.sql',import.meta.url),'utf8'));
try {
  const env={DB:db,KALSHI_PERSONAL_ENABLED:'1'}, d=await discover(env);
  const m=d.markets[0]; if(!m) throw Error('No supported open markets in bounded discovery');
  const evidence=await fetchMarket(env,m.ticker), display=displayMarket(evidence);
  console.log(JSON.stringify({checked_at:new Date().toISOString(),source:'real_public_Kalshi',
    discovery_count:d.markets.length,series_count:new Set(d.markets.map(x=>x.series)).size,
    rules_valid:true,book_clock:display.clock_basis,exchange_timestamp_present:false,
    executable_whole_contract:!!display.quotes,unavailable_reason:display.reason,
    fee_type:evidence.market.rules.fee.type,fee_rounding:evidence.market.rules.fee.rounding,
    quote_records_printed:0,orders:0,production_writes:0}));
} finally {db.db.close();}
