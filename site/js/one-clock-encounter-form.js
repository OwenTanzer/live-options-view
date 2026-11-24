import {headlineDefinition,headlineTickers,hasHeadlinePriceRoute} from '../shared/one-clock-headlines.js';
import {candleDefinition} from '../shared/one-clock-candle-breach.js';
import {encounterGroups} from '../shared/one-clock-level-encounters.js';

export function encounterForm(root) {
  const read=id=>root.querySelector('#'+id);
  const mode=read('study-kind'), fieldset=read('level-fields');
  const ticker=read('headline-instrument');
  for(const symbol of headlineTickers) {
    const option=document.createElement('option');option.value=symbol;option.textContent=hasHeadlinePriceRoute(symbol)?symbol:symbol+" · headlines only, no price route";ticker.append(option);
  }
  ticker.value='NVDA';
  function visibility() {
    const level=mode.value==='level';
    const candle=mode.value==='candle', headline=mode.value==='headline', specialized=level || candle || headline;
    read('headline-fields').hidden=!headline;
    read('candle-description').hidden=!candle;
    fieldset.hidden=!level;
    for(const id of ['combine','transition','missing'])read(id).closest('label').hidden=specialized;
    read('scalar-fields').hidden=specialized;
    read('instrument').disabled=specialized;
    if(specialized)read('instrument').value=headline?ticker.value:'QQQ';
  }
  ticker.addEventListener('change',visibility);
  mode.addEventListener('change',visibility);
  return {
    open(rule) {
      const config=rule.levelEncounter;
      if(rule.transition==='per-headline')ticker.value=rule.instruments[0];
      mode.value=rule.transition==='per-headline'?'headline':config?'level':rule.transition==='per-candle-pair'?'candle':'scalar';
      read('level-bucket').value=String(config?.minimumBucket??5);
      read('level-contact').value=config?.contact??'exact-or-bracketed';
      read('level-approach').value=config?.approach??'either';
      read('level-gap').value=String((config?.maxGapMs??150000)/1000);
      for(const g of encounterGroups)read(g).checked=(config?.groups??encounterGroups).includes(g);
      read('level-horizons').value=config?rule.outcomes.map(o=>o.horizonMs/60000).join(', '):'1, 3, 9, 27';
      read('level-tolerance').value=config?String(rule.outcomes[0].toleranceMs/1000):'30';
      visibility();
    },
    definition() {
      if(mode.value==='headline')return headlineDefinition(ticker.value);
      if(mode.value==='candle')return candleDefinition();
      if(mode.value!=='level')return null;
      const groups=encounterGroups.filter(g=>read(g).checked);
      const horizons=read('level-horizons').value.split(',').map(v=>Number(v.trim()));
      if(!horizons.length || horizons.length>8 || horizons.some(h=>!Number.isFinite(h)||h<=0||h>1440) || new Set(horizons).size!==horizons.length)throw new Error('Choose one to eight distinct positive minute horizons');
      const maxGapMs=Number(read('level-gap').value)*1000;
      const toleranceMs=Number(read('level-tolerance').value)*1000;
      return {instruments:['QQQ'],combine:'all',transition:'rising',missingInput:'reset',
        conditions:[{source:'oi-levels',field:'qualified',op:'==',value:true,maxAgeMs:maxGapMs}],
        levelEncounter:{minimumBucket:Number(read('level-bucket').value),groups,approach:read('level-approach').value,contact:read('level-contact').value,maxGapMs},
        outcomes:horizons.map(h=>({id:'direction-'+h,source:'level-spot',field:'spot',measure:'category',horizonMs:h*60000,maxAgeMs:maxGapMs,toleranceMs}))};
    }
  };
}
