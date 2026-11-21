// Source session calendar: existing US-equity archive keys use New York time.
// This is wall-clock conversion, not a claim that a date is an exchange session.
const formatter = new Intl.DateTimeFormat('en-US', { timeZone:'America/New_York',
  hourCycle:'h23',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit' });
const parts = at => Object.fromEntries(formatter.formatToParts(new Date(at)).map(p=>[p.type,p.value]));
export function validSession(day) {
  return typeof day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(day) &&
    Number.isFinite(Date.parse(day+'T00:00:00Z')) && new Date(day+'T00:00:00Z').toISOString().slice(0,10) === day;
}
export function sessionAt(at) { const p=parts(at); return `${p.year}-${p.month}-${p.day}`; }
export function sessionInstant(day,hour,minute=0,second=0,ms=0) {
  if(!validSession(day) || ![hour,minute,second,ms].every(Number.isInteger) || hour<0 || hour>23 || minute<0 || minute>59 || second<0 || second>59 || ms<0 || ms>999) throw new Error('invalid session time');
  const local=Date.parse(`${day}T00:00:00Z`)+hour*3600000+minute*60000+second*1000+ms;
  const offset=at=>{ const p=parts(at); return Date.UTC(+p.year,+p.month-1,+p.day,+p.hour,+p.minute,+p.second)-Math.floor(at/1000)*1000; };
  return local-offset(local-offset(local));
}
export function snapshotInstant(key,session) {
  const match=/\/snapshot_(\d{2})(\d{2})(\d{2})(\d{6})\.(?:csv|provenance\.json)$/.exec(key);
  return match ? sessionInstant(session,+match[1],+match[2],+match[3],Math.floor(+match[4]/1000)) : null;
}
