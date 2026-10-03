
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

function flush(){let e=document.getElementById("__cg_c53f301c8f2aaa7f_result");if(!e){e=document.createElement('script');e.id="__cg_c53f301c8f2aaa7f_result";e.type='application/json';document.documentElement.appendChild(e);}e.textContent=JSON.stringify(state).replace(/</g,'\u003c');}
state.console=[];for(const level of ['log','warn','error']){const original=console[level];console[level]=(...args)=>{state.console.push({level,text:args.map(String).join(' ').slice(0,2000)});state.console=state.console.slice(-60);flush();original.apply(console,args);};}
window.addEventListener('error',e=>{state.errors.push(String(e.message));state.scenario_complete=false;flush();});window.addEventListener('unhandledrejection',e=>{state.errors.push(String(e.reason));state.scenario_complete=false;flush();});
