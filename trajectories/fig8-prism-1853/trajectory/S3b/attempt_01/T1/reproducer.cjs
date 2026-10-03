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

function flush(){fs.writeFileSync("/task/trajectory/S3b/attempt_01/T1/events.json",JSON.stringify(state));}
process.on('exit',flush);process.on('uncaughtExceptionMonitor',e=>{state.errors.push(String(e));flush();});
(async()=>{
var Prism = require('./prism.js');
if (!global.Prism) { global.Prism = Prism; }
require('./components/prism-json.js');
var out = {};
var s1 = '{"Accept":"*/*"}';
var m1 = Prism.languages.json['comment'].exec(s1);
out.H1 = m1 ? m1[0] : null;
var keys = Object.keys(Prism.languages.json);
out.key_order = keys.join(',');
out.H2 = keys.indexOf('comment') < keys.indexOf('string');
var sm = Prism.languages.json['string'].pattern.exec(s1);
out.H3 = sm ? sm[0] : null;
function plain(tokens) { return tokens.map(function (t) { return (typeof t === 'string') ? t : { type: t.type, content: t.content }; }); }
out.H4 = JSON.stringify(plain(Prism.tokenize(s1, Prism.languages.json)));
var s5 = '{"A":"/*","B":"B","C":"C"}';
out.H5 = Prism.tokenize(s5, Prism.languages.json).some(function (t) { return t && t.type === 'comment' && JSON.stringify(t.content).indexOf('B') !== -1; });
out.H6 = Prism.highlight(s5, Prism.languages.json, 'json');
out.H7 = [Prism.languages.json['string'].pattern.exec(s5).index, Prism.languages.json['comment'].exec(s5).index];
out.H8 = String(Prism.languages.json['comment'].lookbehind);
out.comment_source = String(Prism.languages.json['comment']);
out.string_source = String(Prism.languages.json['string'].pattern);
console.log('PROBE_RESULT ' + JSON.stringify(out, null, 1));

})().then(()=>{state.scenario_complete=true;flush();});
