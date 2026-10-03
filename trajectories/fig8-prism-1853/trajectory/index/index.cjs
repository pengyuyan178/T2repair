
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const parser = require(cfg.parser);
const ROOT = cfg.root;
function relative(file) { return path.relative(ROOT, file).split(path.sep).join('/'); }
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
function own(node) {
  const out = {};
  for (const k of Object.keys(node)) {
    if (['loc','tokens','comments','leadingComments','trailingComments'].includes(k)) continue;
    out[k] = node[k];
  }
  return out;
}
const files = [];
for (const item of cfg.files) {
  let text, sourceHash;
  try { const raw = fs.readFileSync(item); text = raw.toString('utf8'); sourceHash = crypto.createHash('sha256').update(raw).digest('hex'); } catch (error) { files.push({path: relative(item), error: 'unreadable'}); continue; }
  if (text.length > cfg.maxBytes) { files.push({path: relative(item), error: 'too_large'}); continue; }
  if (text.split('\n').some(line => line.length > 1200)) { files.push({path: relative(item), error: 'minified'}); continue; }
  const plugins = ['jsx', 'classProperties', 'objectRestSpread', 'optionalChaining',
                   'dynamicImport', 'decorators-legacy', 'nullishCoalescingOperator'];
  if (/\.tsx?$/.test(item)) plugins.push('typescript');
  let ast;
  try { ast = parser.parse(text, {sourceType: 'unambiguous', plugins, errorRecovery: false}); }
  catch (error) {
    if (!/\.tsx?$/.test(item)) {
      try { ast = parser.parse(text, {sourceType: 'unambiguous', plugins: plugins.concat(['flow', 'flowComments']), errorRecovery: false}); }
      catch (flowError) { files.push({path: relative(item), error: 'parse_failed'}); continue; }
    } else { files.push({path: relative(item), error: 'parse_failed'}); continue; }
  }
  const functions = [], classes = [], scopes = [], tests = [], assignments = [], literals = [], calls = [];
  const declarations = [], decisions = [];
  const covered = new Set();
  function walk(node, parent, key, ancestors) {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(child => walk(child, parent, key, ancestors)); return; }
    if (!node.type) return;
    const record = {node, parent, key, ancestors};
    const start = node.start, end = node.end;
    const kind = node.type;
    if (/^(FunctionDeclaration|FunctionExpression|ArrowFunctionExpression|ClassMethod|ObjectMethod|ClassPrivateMethod)$/.test(kind) && node.body) {
      let label = name(node.id);
      if (!label && parent && parent.type === 'VariableDeclarator') label = name(parent.id);
      if (!label && parent && parent.type === 'AssignmentExpression') label = name(parent.left);
      if (!label && node.key) {
        const owner = ancestors.slice().reverse().find(x => /Class(Declaration|Expression)/.test(x.type));
        label = (owner && owner.id ? name(owner.id) + '.' : '') + name(node.key);
      }
      if (!label && parent && parent.type === 'ObjectProperty') label = name(parent.key);
      functions.push({name: label, start, end, line: node.loc.start.line, end_line: node.loc.end.line,
                      params: (node.params || []).map(p => name(p)).filter(Boolean)});
      if (node.body && node.body.type === 'BlockStatement') {
        scopes.push({kind: 'function_body', name: label, start: node.body.start, end: node.body.end,
                     line: node.body.loc.start.line, end_line: node.body.loc.end.line});
      }
    }
    if (kind === 'ClassDeclaration' || kind === 'ClassExpression') {
      classes.push({name: name(node.id), start, end, line: node.loc.start.line, end_line: node.loc.end.line});
    }
    if (kind === 'BlockStatement' || kind === 'ForStatement' || kind === 'ForOfStatement' ||
        kind === 'ForInStatement' || kind === 'WhileStatement' || kind === 'SwitchStatement') {
      scopes.push({kind: kind === 'BlockStatement' ? 'block' : 'loop', name: '', start, end,
                   line: node.loc.start.line, end_line: node.loc.end.line});
    }
    if (kind === 'RegExpLiteral') {
      const span = start + ':' + end;
      if (!covered.has(span)) {
        tests.push({form: 'regexp', value: node.pattern || '', flags: node.flags || '', start, end,
                    line: node.loc.start.line, end_line: node.loc.end.line, owner: ownerFunction(ancestors)});
      }
    }
    if ((kind === 'Literal' || kind === 'StringLiteral') && typeof node.value === 'string') {
      literals.push({value: node.value, start, end, line: node.loc.start.line, end_line: node.loc.end.line,
                     owner: ownerFunction(ancestors)});
    }
    if (kind === 'CallExpression' || kind === 'OptionalCallExpression') {
      const method = name(node.callee).split('.').pop();
      calls.push({callee: name(node.callee), method, start, end, line: node.loc.start.line,
                  end_line: node.loc.end.line, args: (node.arguments || []).length});
      if (/^(match|matchAll|search|replace|replaceAll|split|test|exec)$/.test(method)) {
        const first = (node.arguments || [])[0];
        const isRegExp = first && first.type === 'RegExpLiteral';
        const literalString = first && (first.type === 'Literal' || first.type === 'StringLiteral') && typeof first.value === 'string';
        if (first) covered.add(first.start + ':' + first.end);
        tests.push({form: isRegExp ? 'regexp_argument' : literalString ? 'string_argument' : 'dynamic',
                    value: isRegExp ? (first.pattern || '') : (literalString ? first.value : ''),
                    flags: isRegExp ? (first.flags || '') : '', method, start: node.start, end: node.end,
                    line: node.loc.start.line, end_line: node.loc.end.line, owner: ownerFunction(ancestors)});
      }
    }
    if (kind === 'AssignmentExpression' && node.left && node.operator === '=') {
      assignments.push({target: name(node.left), member: /MemberExpression$/.test(node.left.type),
                        property: node.left.property ? name(node.left.property) : '',
                        start, end, line: node.loc.start.line, end_line: node.loc.end.line,
                        owner: ownerFunction(ancestors), inLoop: ancestors.some(a => /^For|^While/.test(a.type))});
    }
    if (kind === 'VariableDeclarator' && node.id) {
      declarations.push({name: name(node.id), start, end, line: node.loc.start.line,
                         end_line: node.loc.end.line, owner: ownerFunction(ancestors),
                         init: node.init ? node.init.type : '',
                         guard: node.init && /BinaryExpression|ConditionalExpression|LogicalExpression|CallExpression/.test(node.init.type)});
    }
    if ((kind === 'IfStatement' || kind === 'ConditionalExpression') && node.test) {
      decisions.push({test: text.slice(node.test.start, node.test.end).replace(/\s+/g, ' ').slice(0, 160),
                      start: node.test.start, end: node.test.end, line: node.test.loc.start.line,
                      end_line: node.test.loc.end.line, owner: ownerFunction(ancestors),
                      hasElse: kind === 'ConditionalExpression' || Boolean(node.alternate)});
    }
    if (kind === 'ReturnStatement' && node.argument) {
      declarations.push({name: '', start, end, line: node.loc.start.line, end_line: node.loc.end.line,
                         owner: ownerFunction(ancestors), init: node.argument.type, guard: true,
                         isReturn: true});
    }
    if (kind === 'ArrayExpression' || kind === 'NewExpression' || kind === 'ObjectExpression') {
      literals.push({form: kind, value: '', start, end, line: node.loc.start.line,
                     end_line: node.loc.end.line, owner: ownerFunction(ancestors),
                     size: node.type === 'ArrayExpression' && Array.isArray(node.elements) ? node.elements.length : null});
    }
    for (const child of Object.keys(node)) {
      if (['loc','tokens','comments'].includes(child)) continue;
      walk(node[child], node, child, ancestors.concat(node));
    }
  }
  function ownerFunction(ancestors) {
    for (let index = ancestors.length - 1; index >= 0; index -= 1) {
      if (/^(FunctionDeclaration|FunctionExpression|ArrowFunctionExpression|ClassMethod|ObjectMethod)$/.test(ancestors[index].type)) {
        return ancestors[index].loc ? ancestors[index].loc.start.line : 0;
      }
    }
    return 0;
  }
  walk(ast, null, '', []);
  files.push({path: relative(item), source_sha256: sourceHash, lines: text.split('\n').length, functions, classes, scopes, tests,
              assignments, literals, calls, declarations, decisions});
}
process.stdout.write(JSON.stringify({files}));
