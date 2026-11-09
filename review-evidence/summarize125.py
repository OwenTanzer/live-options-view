import json
from pathlib import Path
P=Path(__file__).parent
Q='qualified_first_ask_to_later_bid'
data=[json.loads(p.read_text()) for p in sorted(P.glob('2026-*.json'))]
assert len(data)==8
assert all(not d['legacy_mismatches'] and not d['damaged'] for d in data)
out=['# Issue 125 acceptance review','',
'Reviewed November 9, 2026 UTC by Chej. Implementation PR #126 is merged. Production-code baseline: 762efafe88425c73dcecb9aefa9963ea80c29781; replay revision 65830835b0c45dd56601fe93e4d885a258eafcc2 adds only review evidence.','',
'## Verification','',
'94 scanner tests and 63 consumer tests pass. See scanner-tests.txt and mcp-consumer-tests.txt. Current connector also returned the real finalized November 6 qualified view (57,908 contracts; 15,508 eligible and 42,400 excluded). This historical replay is separate from live cadence acceptance under #131.','',
'The current production build_contract_rows function replayed all archived sweeps for each of the eight originally reviewed sessions. No production collection or archive was changed. Per-session JSON includes source URLs via the base in replay125.py, SHA-256 digests, counts, rank changes, selected controls and both endpoint evidence. The existing doc describes the historical lack of access at implementation time; permitted public raw archives were accessible for this later review.','',
'| Session | Sweeps | Contracts | Eligible | Excluded | Unknown | Eligible <=0 | New top 200 outside old shortlist | Legacy rows compared |',
'|---|---:|---:|---:|---:|---:|---:|---:|---:|']
for d in data:
 s=d['statuses']
 out.append('| '+' | '.join(map(str,[d['day'],d['sweeps'],d['contracts'],s.get('eligible',0),s.get('excluded',0),s.get('unknown',0),d['qualified_negative_or_zero'],d['qualified_top200_outside_legacy'],d['legacy_rows_compared']]))+' |')
out+=['','All replay comparisons match the archived midpoint first-to-max, ask-to-later-bid, all rank, clean rank and clean classification (numeric tolerance 1e-6). No damaged sweeps reported. This comparison concerns quote-derived fields; trade-bar backfill was not downloaded or claimed revalidated.','',
'## Named controls','',
'Return values below are ratios (multiply by 100 for percent). Clean is the legacy artifact predicate, not liquidity. Full endpoint evidence and reasons appear in each session JSON.','',
'| Session | Contract | Legacy clean/rank | Qualified status/rank | Qualified return |',
'|---|---|---|---|---:|']
for d in data:
 for r in d['controls']:
  sym=r['symbol']
  if (sym.startswith('SOXL') and d['day'] in ('2026-09-29','2026-10-01')) or sym in ('GS261002P00600000','GS261009C01160000','GS261009C00940000') or (sym.startswith(('MU','SPCX')) and r.get(Q+'_status')=='eligible'):
   out.append(f"| {d['day']} | {sym} | {r['clean']}/{r.get('clean_rank')} | {r.get(Q+'_status')}/{r.get(Q+'_rank')} | {r.get(Q+'_pct')} |")
out+=['','## Interpretation and boundaries','',
'The separately versioned first-qualifying-ask metric uses entry-time evidence, strictly later qualified bid exits and full-universe ranks. It preserves eligible losses and does not redefine legacy exec fields. Unknown-input behavior is covered deterministically even where a historical session has zero unknown statuses.','',
'The replay supports implementation acceptance, not threshold calibration or trading profitability. Defaults remain provisional. Maximum later bids are hindsight observations; samples and displayed sizes do not establish fills, slippage, queue position or capacity. September 24-25 are outside this eight-session coverage. No deployment, producer scan, polling change or historical overwrite occurred.','']
(P/'REVIEW-125.md').write_text('\n'.join(out),encoding='utf-8')
print('\n'.join(out))
