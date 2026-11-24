// Read-only projection. A minute names exactly one derived QQQ object.
export async function intraminute(request, env, url) {
  const error = (message, status) => Response.json({error:message},{status,headers:{'Cache-Control':'no-store'}});
  if (request.method !== 'GET') return error('GET required',405);
  const minute = url.searchParams.get('minute');
  if (minute !== null) {
    if (!/^\d{13}$/.test(minute) || Number(minute)%60000) return error('Invalid UTC minute',400);
    const object = await env.PAPER_TRADES.get('order-flow/qqq/intraminute/v1/'+minute+'.json');
    return object ? new Response(object.body,{headers:{'Content-Type':'application/json','Cache-Control':'no-store'}}) : error('Minute has no published replay',404);
  }
  const cursor=url.searchParams.get('cursor') ?? '0', epoch=url.searchParams.get('epoch') ?? '';
  if (!/^\d{1,15}$/.test(cursor) || !/^(?:[0-9a-f]{32})?$/.test(epoch)) return error('Invalid cursor',400);
  if (!env.QQQ_TAPE_ORIGIN || !env.QQQ_TAPE_KEY) return error('Live QQQ tape is offline',503);
  try {
    const target=new URL('/tape',env.QQQ_TAPE_ORIGIN);
    if(target.protocol!=='https:') return error('Invalid tape origin',503);
    target.search=new URLSearchParams({cursor,epoch});
    const res=await fetch(target,{headers:{Authorization:'Bearer '+env.QQQ_TAPE_KEY},redirect:'error',signal:AbortSignal.timeout(3000)});
    if(!res.ok) return error('Live QQQ tape unavailable',503);
    return new Response(res.body,{headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
  } catch { return error('Live QQQ tape unavailable',503); }
}
