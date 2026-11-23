import {verifiedNativeChain} from './one-clock-chain.js';
import {observation} from '../site/shared/one-clock.js';

import {readOpeningOi,openingIndex} from './one-clock-opening-oi.js';
export {calibratedLevels} from './one-clock-opening-oi.js';
export const levelIngestion={
  id:'calibrated-oi-levels',version:'1',owner:'market_data.live_market',demandDriven:true,reuseObjects:true,reportGapReasons:true,
  instruments:['QQQ'],outputs:['oi-levels','level-spot'],fields:{'level-spot':{spot:'Qualified underlying observation'},'oi-levels':{qualified:'Calibrated QQQ strike levels'}},
  async discover(bucket,{instrument,session,cutoff},db) {
    const stream='collector-chain/'+instrument;
    const status=await db.prepare('SELECT coverage FROM one_clock_publication_status INDEXED BY one_clock_publication_status_cutoff WHERE stream=? AND checked_at<=? ORDER BY checked_at DESC,generation DESC LIMIT 1').bind(stream,cutoff).first();
    const progress=status && JSON.parse(status.coverage);
    if(progress?.reason!=='tail-at-check')return {artifacts:[],complete:false,reason:'native-price-index-incomplete'};
    const indexed=(await db.prepare(`SELECT payload FROM one_clock_publication_records INDEXED BY one_clock_native_chain_replay
      WHERE stream=? AND session=? AND source='price' AND event_at<=? AND available_at<=? AND received_at<=? AND sequence<?
      ORDER BY event_at,available_at,record_id,revision LIMIT 1001`).bind(stream,session,cutoff,cutoff,cutoff,progress.nextSequence).all()).results;
    if(indexed.length>1000)return {artifacts:[],complete:false,reason:'native-session-index-bound'};
    if(!indexed.length)return {artifacts:[],complete:false,reason:'no-native-chain-publications'};
    const artifacts=indexed.map(item=>{
      const price=JSON.parse(item.payload);
      return {key:JSON.stringify([price.id,price.revision]),version:String(price.details.sequence),eventAt:price.eventAt,price};
    });
    const opening=await openingIndex(bucket,session,cutoff);
    if(!opening.items.length)return {artifacts:[],complete:false,reason:opening.reason};
    artifacts.push(...opening.items.map(item=>({...item,eventAt:item.availableAt,opening:true})));
    return {artifacts,complete:true};
  },
  async read(bucket,selection,artifact) {
    if(artifact.opening) {
      const map=await readOpeningOi(bucket,selection.session,selection.cutoff,artifact);
      if(map.etag && map.etag!==artifact.version)return {rows:[],retry:true};
      if(!map.levels)return {rows:[],gap:true,reason:map.reason};
      return {rows:[observation({source:'oi-levels',id:artifact.key,revision:1,session:selection.session,instrument:'QQQ',
        origin:'recorded',eventAt:map.capturedAt,availableAt:map.availableAt,evidence:artifact.key,
        value:{qualified:true,levels:map.levels,calibration:map.calibration}})],gap:map.gaps>0,reason:map.gaps?'partial-opening-calibration':null};
    }
    const {price}=artifact;
    let spotRow=observation({...price,source:'level-spot',value:null});
    const gap=reason=>({rows:[spotRow],gap:true,reason});
    if(!Number.isFinite(price.value?.spot))return gap('spot-unavailable');
    const result=await verifiedNativeChain(bucket,selection,price);
    const chain=result.rows[0]?.value;
    if(chain?.spotFreshness!=='live' || !Number.isSafeInteger(chain.spotAt) || chain.spotAt>price.eventAt ||
      price.eventAt-chain.spotAt>150000)return gap('spot-observation-time-unqualified');
    spotRow=observation({...price,source:'level-spot',valueAt:chain.spotAt});
    return {rows:[spotRow]};
  }
};
