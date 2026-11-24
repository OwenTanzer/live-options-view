import assert from 'node:assert/strict';
import {model,flowResponse} from '../site/shared/intraminute.js';
const minute=1794580560000;
const row=(kind,offset,rest)=>({kind,at:minute+offset,...rest});
const quote=(at,mid)=>row('quote',at,{bid:mid-.01,ask:mid+.01});
const print=(seq,type,size,label,basis,strike)=>row('option',80,{seq,type,size,label,basis,strike,symbol:'QQQ-'+type+strike});
const rows=[quote(50,670),print(1,'C',10,'buy','q1',670),print(2,'P',3,'sell','q5',670),
 print(3,'P',4,'buy','tick',671),print(1,'C',10,'buy','q1',670),quote(1050,670.5)];
const opts={minute,cutoff:minute+1099,width:100};
const tape=model(rows,opts);
assert.deepEqual(tape.bins[0].C,[10,0,0]);
assert.deepEqual(tape.bins[0].P,[0,3,4]);
assert.deepEqual(flowResponse(tape,0,100,minute,opts.cutoff),{pressure:13,imbalance:1,response:.5});
assert.equal(flowResponse(tape,0,100,minute,minute+1098).response,null);
assert.equal(model(rows,{...opts,strike:670}).unknown,0);
const gap=row('gap',900,{start:minute+400,reason:'stream_reconnect_resumed'});
assert.equal(flowResponse(model([...rows,gap],opts),0,100,minute,opts.cutoff).response,null);
assert.equal(model(rows,{...opts,cutoff:minute+79}).calls,0);
console.log('Intraminute signing, receipts, strike scope, gaps, response and duplicate checks passed');
