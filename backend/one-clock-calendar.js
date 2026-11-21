import calendar from './one-clock-nyse-calendar.json' with {type:'json'};
import {validSession,sessionAt} from '../site/shared/one-clock-session.js';
const days=Object.keys(calendar.sessions).sort();
export function equitySession(day) {
  if(!validSession(day)||day<calendar.start||day>calendar.end)throw new Error('NYSE calendar coverage unavailable');
  const bounds=calendar.sessions[day];
  return bounds?{open:bounds[0],close:bounds[1],policy:`NYSE:${calendar.library}:${calendar.version}`} : null;
}
export function observerSessions(now) {
  const today=sessionAt(now);equitySession(today);
  let low=0,high=days.length;
  while(low<high){const mid=(low+high)>>1;if(days[mid]<=today)low=mid+1;else high=mid;}
  if(low<2)throw new Error('NYSE prior session coverage unavailable');
  return [days[low-1],days[low-2]];
}
