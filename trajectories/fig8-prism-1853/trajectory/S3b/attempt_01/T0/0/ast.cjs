
const fs = require('fs');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const parser = require(cfg.parser);
const text = fs.readFileSync(cfg.file, 'utf8');
const plugins = ['jsx', 'classProperties', 'objectRestSpread', 'optionalChaining', 'dynamicImport', 'decorators-legacy'];
if (/\.tsx?$/.test(cfg.file)) plugins.push('typescript');
const ast = parser.parse(text, {sourceType:'unambiguous', plugins});
const nodes = [];
function walk(n, parent, key, ancestors) {
  if (!n || typeof n !== 'object') return;
  if (Array.isArray(n)) { n.forEach(x => walk(x, parent, key, ancestors)); return; }
  if (!n.type) return;
  nodes.push({n, parent, key, ancestors});
  for (const k of Object.keys(n)) if (!['loc','tokens','comments'].includes(k))
    walk(n[k], n, k, ancestors.concat(n));
}
walk(ast, null, '', []);
function name(n) {
  if (!n) return '';
  if (n.type === 'Identifier') return n.name;
  if (n.type === 'ThisExpression') return 'this';
  if (n.type === 'StringLiteral') return n.value;
  if (n.type === 'MemberExpression' || n.type === 'OptionalMemberExpression') {
    if (n.computed && n.property.type !== 'StringLiteral') return '';
    return name(n.object) + '.' + name(n.property);
  }
  return '';
}
function functionName(r) {
  const n=r.n, p=r.parent;
  if (n.id) return name(n.id);
  if (p && p.type === 'VariableDeclarator') return name(p.id);
  if (p && p.type === 'AssignmentExpression') return name(p.left);
  if (n.key) {
    const cls=r.ancestors.slice().reverse().find(x => /Class(Declaration|Expression)/.test(x.type));
    return cls && cls.id ? name(cls.id)+'.'+name(n.key) : name(n.key);
  }
  if (p && p.type === 'ObjectProperty') return name(p.key);
  return '';
}
const funcs=nodes.filter(r => /Function|Method/.test(r.n.type) && r.n.body);
const declarations=new Set();
const members=new Set();
for (const r of nodes) {
  const n=r.n;
  if (/Declaration$/.test(n.type) && n.id) declarations.add(name(n.id));
  if (n.type === 'VariableDeclarator') declarations.add(name(n.id));
  if (/Import.*Specifier/.test(n.type)) declarations.add(name(n.local));
  if (n.type === 'AssignmentExpression') declarations.add(name(n.left));
  if (/Method$|Property$/.test(n.type) && n.key) declarations.add(name(n.key));
  if (/MemberExpression$/.test(n.type) && (!n.computed || n.property.type === 'StringLiteral'))
    members.add(name(n.property));
}
funcs.forEach(r => declarations.add(functionName(r)));
const edits=[], probes=[];
for (const h of cfg.hypotheses || []) {
  const symbol=String(h.symbol || ''), line=Number(h.line || 0);
  const matches=funcs.filter(r => functionName(r) === symbol || functionName(r).endsWith('.'+symbol));
  const fn=matches.find(r => line && r.n.loc.start.line <= line && r.n.loc.end.line >= line) || matches[0];
  const exists=!!symbol && (declarations.has(symbol) || funcs.some(r => functionName(r) === symbol));
  let expression=String((h.question || {}).expression || '').trim();
  if (!/^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$/.test(expression)) expression='';
  const root=expression.split('.')[0];
  const read=expression ? `(typeof ${root} === 'undefined' ? undefined : ${expression})` : 'undefined';
  const hit=`globalThis[${JSON.stringify(cfg.hook)}]&&globalThis[${JSON.stringify(cfg.hook)}].hit(${JSON.stringify(h.id)},()=>${read})`;
  let candidates=nodes.filter(r => /Statement$|VariableDeclaration$/.test(r.n.type)
    && !/BlockStatement|EmptyStatement|FunctionDeclaration/.test(r.n.type)
    && r.parent && (Array.isArray(r.parent[r.key]) || ['consequent','alternate','body'].includes(r.key))
    && line && r.n.loc.start.line <= line && r.n.loc.end.line >= line
    && (!fn || (r.n.start >= fn.n.body.start && r.n.end <= fn.n.body.end)));
  candidates.sort((a,b) => (a.n.end-a.n.start)-(b.n.end-b.n.start));
  let point=candidates[0], position=null, reason='no_executable_ast_location', certificate=null;
  if (point) {
    position=point.n.loc.start.line;
    const assignment=point.n.type === 'ExpressionStatement' && point.n.expression.type === 'AssignmentExpression' ? point.n.expression : null;
    const left=assignment && assignment.left;
    const exactWrite=left && /MemberExpression$/.test(left.type) && name(left.property) === h.target_property
      && (!left.computed || left.property.type === 'StringLiteral') && expression
      && text.slice(left.start,left.end).trim() === expression;
    if (exactWrite) {
      if (!Array.isArray(point.parent[point.key])) edits.push({pos:point.n.start,text:'{'});
      edits.push({pos:point.n.end,text:`;${hit};` + (Array.isArray(point.parent[point.key])?'':'}')});
      certificate={kind:'executed_target_write',target_property:h.target_property,expression};
      reason='after_exact_target_assignment';
    } else {
      if (Array.isArray(point.parent[point.key])) edits.push({pos:point.n.start,text:`;${hit};`});
      else { edits.push({pos:point.n.start,text:`{;${hit};`}); edits.push({pos:point.n.end,text:'}'}); }
    }
    if (!certificate) reason='ast_statement';
  } else if (fn && fn.n.body.type === 'BlockStatement') {
    position=fn.n.body.loc.start.line;
    const directives=fn.n.body.directives || [];
    const pos=directives.length ? directives[directives.length-1].end : fn.n.body.start+1;
    edits.push({pos,text:`;${hit};`}); reason='function_entry';
  } else if (fn) {
    position=fn.n.body.loc.start.line;
    edits.push({pos:fn.n.body.start,text:`(${hit},(`});
    edits.push({pos:fn.n.body.end,text:'))'}); reason='arrow_expression';
  }
  probes.push({id:h.id,symbol_exists:exists,instrumented_line:position,expression,
    reason,ambiguous_symbol:matches.length>1,static_reachable:null,certificate});
}
if (edits.length) {
  const load=`;globalThis[${JSON.stringify(cfg.hook)}]&&globalThis[${JSON.stringify(cfg.hook)}].loaded(${JSON.stringify(cfg.relative)});`;
  edits.push({pos:ast.program.body.length ? ast.program.body[0].start : text.length,text:load});
}
let output=text;
edits.sort((a,b) => b.pos-a.pos).forEach(e => { output=output.slice(0,e.pos)+e.text+output.slice(e.pos); });
if (cfg.instrument && edits.length) fs.writeFileSync(cfg.file,output);
process.stdout.write(JSON.stringify({probes,declarations:[...declarations].filter(Boolean),members:[...members].filter(Boolean)}));
