// Shared average-cost accounting for current and as-of journal projections.
// Callers supply only eligible executions in deterministic event order.
export function journalBookState(executions) {
  const m=executions[0].instrument.multiplier;
  let pos=0,avg=0,realized=0,maxQty=0;
  for(const e of executions) {
    const q=e.side==='buy'?e.qty:-e.qty;
    if(pos===0 || Math.sign(pos)===Math.sign(q)) {
      avg=(avg*Math.abs(pos)+e.price*Math.abs(q))/(Math.abs(pos)+Math.abs(q));pos+=q;
    } else {
      const closing=Math.min(Math.abs(q),Math.abs(pos));
      realized+=(e.price-avg)*closing*m*Math.sign(pos);pos+=q;
      if(pos===0)avg=0;
      else if(Math.sign(pos)===Math.sign(q))avg=e.price;
    }
    maxQty=Math.max(maxQty,Math.abs(pos));
  }
  return {pos,avg,realized,maxQty,m};
}
