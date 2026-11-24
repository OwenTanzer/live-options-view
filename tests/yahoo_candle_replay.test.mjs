import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {createHash} from 'node:crypto';
import {gzipSync} from 'node:zlib';
import {readYahooCandles} from '../ops/yahoo-candles/replay.mjs';

async function fixture(t,day='2026-11-23') {
  const root = await mkdtemp(join(tmpdir(),'yahoo-replay-'));
  t.after(()=>rm(root,{recursive:true,force:true}));
  const dir = join(root,'AAPL','1m');
  await mkdir(dir,{recursive:true});
  const record = async (time, points, symbol='AAPL', interval='1m') => {
    const timestamp = points.map(([at])=>Date.parse(day+'T'+at+'Z')/1000);
    const prices = points.map(([,price])=>price);
    const chart = {meta:{symbol,dataGranularity:interval},timestamp,
      indicators:{quote:[{open:prices,high:prices,low:prices,close:prices}]}};
    const bytes = Buffer.from(JSON.stringify({chart:{result:[chart]}}));
    const digest = createHash('sha256').update(bytes).digest('hex');
    const received = day+'T'+time+'+00:00';
    await writeFile(join(dir,digest+'.json.gz'),gzipSync(bytes));
    const name = received.replaceAll(':','').replace('+','_')+'-'+digest+'.receipt.json';
    await writeFile(join(dir,name),JSON.stringify({source:'yahoo_finance_chart',symbol:'AAPL',interval:'1m',sha256:digest,received_at:received}));
  };
  const read = time=>readYahooCandles(root,'AAPL','1m',day,Date.parse(day+'T'+time+'Z'));
  return {record,read};
}

test('holes retract only covered rows and a later observation restores the bar',async t=>{
  const {record,read}=await fixture(t);
  const before=[['15:00',9],['15:01',10],['15:02',11],['15:03',12]];
  await record('16:00:00',before);
  await record('16:00:00.500',[before[1],['15:03',13]]);
  assert.deepEqual((await read('16:00:00.499')).map(r=>r.value.close),[9,10,11,12]);
  const after=await read('16:00:00.500');
  assert.deepEqual(after.map(r=>r.value?.close??null),[9,10,null,13]);
  assert.equal(after[2].revision,2);
  await record('16:01:00',before);
  assert.equal((await read('16:01:00'))[2].revision,3);
});

test('legacy identity mismatches are isolated and microsecond corrections use receipt order',async t=>{
  const {record,read}=await fixture(t);
  await record('16:00:00',[['15:00',10]]);
  await record('16:00:00.100',[['15:00',888]],'MSFT');
  await record('16:00:00.200',[['15:00',999]],'AAPL','1d');
  assert.equal((await read('16:00:00.300'))[0].value.close,10);
  await record('16:00:00.500799',[['15:00',12]]);
  await record('16:00:00.500222',[['15:00',11]]);
  assert.equal((await read('16:00:00.500'))[0].value.close,10);
  const [final]=await read('16:00:00.501');
  assert.equal(final.value.close,12);
  assert.equal(final.revision,3);
});

for (const [day,last,close] of [['2026-11-23','20:59','21:00'],['2026-11-27','17:59','18:00']]) {
  test('regular trading interval excludes the closing marker on '+day,async t=>{
    const {record,read}=await fixture(t,day);
    await record('22:00:00',[['14:29',8],['14:30',9],[last,10],[close,99]]);
    assert.deepEqual((await read('23:00:00')).map(r=>r.value.close),[9,10]);
  });
}
