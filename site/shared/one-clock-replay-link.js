import {instant} from './one-clock.js';
import {validSession,sessionAt} from './one-clock-session.js';
export function replaySelection(search) {
  const query=new URLSearchParams(search);
  if(!['one_clock_session','one_clock_instrument','one_clock_at'].some(k=>query.has(k)))return null;
  if(['one_clock_session','one_clock_instrument','one_clock_at'].some(k=>query.getAll(k).length!==1))throw new Error('Invalid event replay link.');
  const session=query.get('one_clock_session'),instrument=query.get('one_clock_instrument'),cutoff=instant(Number(query.get('one_clock_at')));
  if(!validSession(session)||!['QQQ','SPY','IBIT'].includes(instrument)||sessionAt(cutoff)!==session)throw new Error('Invalid event replay scope.');
  return {session,instrument,cutoff,mode:'replay',origin:'recorded'};
}
