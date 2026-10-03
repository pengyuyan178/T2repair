const fs=require('fs');

const state={events:[],loaded:[],scenario_complete:false,errors:[],seed:42};
let randomState=42;
Math.random=()=>{randomState=(Math.imul(1664525,randomState)+1013904223)>>>0;return randomState/4294967296;};
function scalar(x,depth=0) {
  if (x === undefined) return {type:'undefined'};
  if (typeof x === 'number' && !Number.isFinite(x)) return {type:'nonfinite_number',value:String(x)};
  if (x === null || typeof x === 'boolean' || typeof x === 'number') return x;
  if (typeof x === 'string') return x.length<=1000 ? x : {type:'truncated_string',prefix:x.slice(0,1000),length:x.length};
  if (typeof x !== 'object' || depth>2) return {type:typeof x};
  const out=Array.isArray(x)?[]:{};
  for (const k of Object.keys(x).slice(0,16)) {
    const d=Object.getOwnPropertyDescriptor(x,k);
    if (d && 'value' in d) out[k]=scalar(d.value,depth+1);
  }
  return out;
}
globalThis["__cg_c53f301c8f2aaa7f"]={
  loaded:file=>{if(!state.loaded.includes(file))state.loaded.push(file);flush();},
  hit:(id,read)=>{
    if (state.events.filter(x=>x.id===id).length<32) state.events.push({id,value:scalar(read()),read_succeeded:true});
    flush();
  }
};

function flush(){fs.writeFileSync("/task/trajectory/candidates/05/validation/replay/T1/events.json",JSON.stringify(state));}
process.on('exit',flush);process.on('uncaughtExceptionMonitor',e=>{state.errors.push(String(e));flush();});
(async()=>{
var Prism = require('./prism.js'); if (!global.Prism) { global.Prism = Prism; } require('./components/prism-json.js'); console.log('NODE keys=' + Object.keys(Prism.languages.json).join(',') + ' COMMENT=' + String(Prism.languages.json['comment']) + ' EXEC=' + String(Prism.languages.json['comment'].exec('{"A":"/*","B":"B","C":"C"}')))
})().then(()=>{state.scenario_complete=true;flush();});
