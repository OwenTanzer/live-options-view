import {candleDefinition,candleSource,validBar} from './one-clock-candle-breach.js';

export const candleSessions=['2026-10-29','2026-10-30','2026-11-02'];
export async function loadCandleStudy(session) {
  if(!candleSessions.includes(session))throw new Error('Unknown candle replay session');
  const path='/candle-replay/QQQ-'+session+'.txt';
  const response=await fetch(path,{credentials:'omit'});
  if(!response.ok)throw new Error('QQQ replay bars unavailable');
  return candleStudy(session,await response.text(),path);
}
export function candleStudy(session,csv,path) {
  const selection={session,instrument:'QQQ',origin:'reconstructed'};
  const lines=csv.trim().split(/\r?\n/);
  if(lines.shift()!=='timestamp,open,high,low,close' || lines.length!==390)throw new Error('Incomplete QQQ fixture');
  const rows=lines.map(line=>{
    const [stamp,...values]=line.split(',');
    const [open,high,low,close]=values.map(Number), value={open,high,low,close};
    const at=Date.parse(stamp)+60000;
    if(!stamp.startsWith(session+'T') || !validBar(value))throw new Error('Invalid QQQ fixture bar');
    return {...selection,source:candleSource,id:'QQQ/'+(at-60000),revision:1,eventAt:at,availableAt:at,
      evidence:path+'#'+stamp,value};
  });
  return {selection,rows,start:rows[0].eventAt-60000,close:rows.at(-1).eventAt,
    rules:[{id:'qqq.1m.opposite_color_breach.v1',version:1,name:'QQQ opposite-color candle breach',...candleDefinition()}],
    sources:[{id:candleSource,label:'QQQ one-minute candles',fields:{open:'Open',high:'High',low:'Low',close:'Close'}}]};
}
