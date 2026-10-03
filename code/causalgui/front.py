from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess

SCRIPT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
VISUAL = (".css", ".scss", ".sass", ".glsl", ".vert", ".frag")
EXCLUDED = {".git", "node_modules", "test", "tests", "__tests__", "__fixtures__",
            "dist", "build", "out", "coverage", "docs", "examples", ".yarn", ".github",
            "vendor", "third_party", "min", "esm", "umd", "coverage-tmp"}
MINIFIED_NAME = re.compile(r"\.min\.|\.bundle\.|[-.]min\.js$", re.IGNORECASE)

MECHANISM = {"matching": "matching_boundary", "value_decision": "output_path",
             "output_path": "output_path", "identity_binding": "identity_binding",
             "render_group": "render_group"}
EDIT_UNIT = {"matching": "matching_expression", "value_decision": "value_expression",
             "output_path": "function_body", "identity_binding": "assignment_expression",
             "render_group": "array_expression"}

INDEX_SCRIPT = r"""
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
"""


class IndexError_(RuntimeError):
    pass


def _parser_root(repo: Path) -> str:

    roots = [repo / "node_modules"]
    roots += [Path(entry) for entry in os.environ.get("CAUSALGUI_NODE_MODULES", "").split(os.pathsep) if entry]
    for root in roots:
        if (root / "@babel/parser/package.json").is_file():
            return str(root / "@babel/parser")
    return ""


def _node() -> str:
    return os.environ.get("CAUSALGUI_NODE_BINARY") or "node"


def _source_files(repo: Path) -> list[str]:

    files = []
    for directory, children, names in os.walk(repo):
        children[:] = sorted(name for name in children
                             if name not in EXCLUDED and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(repo).as_posix()
            if path.is_symlink() or not name.endswith(SCRIPT + VISUAL):
                continue
            if MINIFIED_NAME.search(name):
                continue
            if any(part in EXCLUDED for part in PurePosixPath(relative).parts[:-1]):
                continue
            files.append(relative)
    return sorted(files)


def parse_tree(repo: Path, output: Path) -> dict:

    repo, output = Path(repo).resolve(), Path(output)
    parser = _parser_root(repo)
    if not parser:
        raise IndexError_("babel_parser_unavailable")
    output.mkdir(parents=True, exist_ok=True)
    files = [name for name in _source_files(repo) if name.endswith(SCRIPT)]
    request = {"parser": parser, "root": str(repo), "files": [str(repo / name) for name in files],
               "maxBytes": 4000000}
    script = output / "index.cjs"
    script.write_text(INDEX_SCRIPT, encoding="utf-8")
    (output / "index_request.json").write_text(
        json.dumps({**request, "files": files}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    process = subprocess.run(
        [_node(), str(script), str(_write_request(output, request))],
        capture_output=True, text=True, cwd=str(repo), check=False,
    )
    (output / "index_stdout.log").write_text(process.stdout, encoding="utf-8", errors="replace")
    (output / "index_stderr.log").write_text(process.stderr, encoding="utf-8", errors="replace")
    if process.returncode:
        raise IndexError_(f"index_process_failed:{process.returncode}")
    return json.loads(process.stdout)


def _write_request(output: Path, request: dict) -> Path:
    path = output / "index_node_request.json"
    path.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def build_index(repo: Path, output: Path) -> dict:

    repo, output = Path(repo).resolve(), Path(output)
    parsed = parse_tree(repo, output)
    index, failed = {}, []
    for entry in parsed["files"]:
        name = PurePosixPath(entry["path"]).as_posix()
        if entry.get("error"):
            failed.append({"file": name, "error": entry["error"]})
            continue
        raw = (repo / name).read_bytes()
        source_sha256 = hashlib.sha256(raw).hexdigest()
        assert source_sha256 == entry["source_sha256"], f"Source changed during indexing: {name}"
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines()
        index[name] = {"text": lines, "masked": mask(text), "line_count": len(lines), "source_sha256": source_sha256,
                       "functions": entry["functions"], "classes": entry["classes"],
                       "scopes": entry["scopes"], "tests": entry["tests"],
                       "assignments": entry["assignments"], "literals": entry["literals"],
                       "calls": entry["calls"], "declarations": entry["declarations"],
                       "decisions": entry["decisions"], "import_bindings": imported_symbols(text)}
    for name in _source_files(repo):
        if name.endswith(VISUAL) and name not in index:
            raw = (repo / name).read_bytes()
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            lines = text.splitlines()
            index[name] = {"text": lines, "masked": mask(text), "line_count": len(lines),
                            "source_sha256": hashlib.sha256(raw).hexdigest(),
                           "functions": [], "classes": [], "scopes": [], "tests": [],
                           "assignments": [], "literals": [], "calls": [], "declarations": [],
                           "decisions": [], "import_bindings": imported_symbols(text)}
    return {"files": index, "failed": failed, "parsed": len(index)}


def shortlist(pool, issue, entities, limit=80):

    stop = {"the", "and", "for", "with", "this", "that", "from", "when", "should", "does",
            "not", "are", "was", "use", "using", "into", "but", "its", "have", "has"}
    issue_terms = {word.lower() for word in re.findall(r"[A-Za-z_$][\w$]{2,}", issue or "")} - stop
    entity_terms = {word.lower() for entity in entities or ()
                    for word in re.findall(r"[A-Za-z_$][\w$]{2,}", str(entity))} - stop

    def key(item):
        haystack = " ".join([item["file"], item["text"], item["expression"],
                             " ".join(item["read_symbols"]), str(item["detail"].get("rule_owner", "")),
                             str(item["detail"].get("named_callable", ""))]).lower()
        role_hits = sum(word in haystack for word in entity_terms)
        issue_hits = sum(word in haystack for word in issue_terms)
        return (-role_hits, -issue_hits, -item["specificity"], item["file"], item["line"], item["id"])

    ordered = sorted(pool, key=key)
    chosen, counts = [], defaultdict(int)
    for item in ordered:
        if counts[item["file"]] >= 12:
            continue
        chosen.append(item)
        counts[item["file"]] += 1
        if len(chosen) == limit:
            return chosen
    selected = {item["id"] for item in chosen}
    chosen.extend(item for item in ordered if item["id"] not in selected)
    return chosen[:limit]


NONCODE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/', re.S)


def mask(text: str) -> str:

    return NONCODE.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)


IMPORT = re.compile(
    r"\b(?:import|export)\s+(?:(?:[^;'\"]*?)\s+from\s*)?['\"]([^'\"]+)['\"]"
    r"|\b(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"|@(?:import|use|forward)\s+['\"]([^'\"]+)['\"]"
    r"|#include\s+[\"<]([^\">]+)[\">]", re.M,
)
ES_IMPORT_BINDING = re.compile(
    r"\bimport\s+(?!['\"])(?:type\s+)?([^;'\"]*?)\s+from\s*['\"]([^'\"]+)['\"]", re.M,
)
ES_REEXPORT_BINDING = re.compile(
    r"\bexport\s+(\*|\{[^}]*\})\s+from\s*['\"]([^'\"]+)['\"]", re.M | re.S,
)
REQUIRE_BINDING = re.compile(
    r"\b(?:const|let|var)\s+(\{[^}]*\}|[$A-Za-z_][$\w]*)\s*=\s*"
    r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", re.M | re.S,
)
MATCH_METHODS = {"match", "matchAll", "search", "replace", "replaceAll", "split", "test", "exec"}
IDENTITY_PROPERTY = re.compile(
    r"(?:^|[a-z])(?:cache|index|cached|context|identity|reference|ref|id|state|store|prev|last)$",
    re.IGNORECASE,
)
CACHE_ASSIGNMENT = re.compile(r"(?<![\w$.])(\w*(?:cache|cached|prev|last|current|index)\w*)\s*=", re.IGNORECASE)


def _cache_scope_before(entry, line, window=12):

    start = max(0, line - window - 1)
    return bool(CACHE_ASSIGNMENT.search("\n".join(entry["masked"].splitlines()[start:line - 1])))


def _imports(name, text):

    found = []
    for match in IMPORT.finditer(text):
        request = next((group for group in match.groups() if group is not None), "")
        if request:
            found.append(request)
    return found


def _named_imports(clause, alias_word):

    found = []
    body = clause.strip().removeprefix("{").removesuffix("}")
    for item in body.split(","):
        item = re.sub(r"^\s*type\s+", "", item.strip())
        if not item:
            continue
        pieces = re.split(alias_word, item, maxsplit=1)
        imported = pieces[0].strip()
        local = pieces[-1].strip()
        if re.fullmatch(r"[$A-Za-z_][$\w]*", imported) and re.fullmatch(r"[$A-Za-z_][$\w]*", local):
            found.append((imported, local))
    return found


def imported_symbols(text):

    found = []
    for clause, request in ES_IMPORT_BINDING.findall(text):
        clause = clause.strip()
        default = re.match(r"([$A-Za-z_][$\w]*)\s*(?:,|$)", clause)
        if default and not clause.startswith(("{", "*")):
            found.append({"request": request, "imported": "default", "local": default.group(1)})
        braced = re.search(r"\{([^}]*)\}", clause, re.S)
        if braced:
            found.extend({"request": request, "imported": imported, "local": local}
                         for imported, local in _named_imports("{" + braced.group(1) + "}", r"\s+as\s+"))
        namespace = re.search(r"\*\s+as\s+([$A-Za-z_][$\w]*)", clause)
        if namespace:
            found.append({"request": request, "imported": "*", "local": namespace.group(1)})
    for clause, request in ES_REEXPORT_BINDING.findall(text):
        if clause.strip() == "*":
            found.append({"request": request, "imported": "*", "local": ""})
        else:
            found.extend({"request": request, "imported": imported, "local": local}
                         for imported, local in _named_imports(clause, r"\s+as\s+"))
    for clause, request in REQUIRE_BINDING.findall(text):
        clause = clause.strip()
        if clause.startswith("{"):
            found.extend({"request": request, "imported": imported, "local": local}
                         for imported, local in _named_imports(clause, r"\s*:\s*"))
            continue
        properties = sorted(set(re.findall(
            r"(?<![\w$])" + re.escape(clause) + r"\.([$A-Za-z_][$\w]*)", text)))
        if properties:
            found.extend({"request": request, "imported": prop, "local": clause + "." + prop}
                         for prop in properties)
        if re.search(r"(?<![\w$.])" + re.escape(clause) + r"\s*\(", text):
            found.append({"request": request, "imported": "default", "local": clause})
    unique = {(item["request"], item["imported"], item["local"]): item for item in found}
    return [unique[key] for key in sorted(unique)]


def export_names(entry):

    text = entry["masked"]
    names = defaultdict(set)
    declaration = re.compile(
        r"\bexport\s+(default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+([$A-Za-z_][$\w]*)"
    )
    for match in declaration.finditer(text):
        names["default" if match.group(1) else match.group(2)].add(match.group(2))
    for match in re.finditer(r"\bexport\s*\{([^}]*)\}(?!\s*from)", text, re.S):
        for local, public in _named_imports("{" + match.group(1) + "}", r"\s+as\s+"):
            names[public].add(local)
    for match in re.finditer(r"\bexport\s+default\s+([$A-Za-z_][$\w]*)\s*;", text):
        names["default"].add(match.group(1))
    for match in re.finditer(
            r"(?:\bexports|\bmodule\.exports)\.([$A-Za-z_][$\w]*)\s*=\s*([$A-Za-z_][$\w]*)", text):
        names[match.group(1)].add(match.group(2))
    for match in re.finditer(r"\bmodule\.exports\s*=\s*([$A-Za-z_][$\w]*)\s*;", text):
        names["default"].add(match.group(1))
    for match in re.finditer(r"\bmodule\.exports\s*=\s*\{([^}]*)\}", text, re.S):
        for item in match.group(1).split(","):
            pieces = item.strip().split(":", 1)
            public, local = pieces[0].strip(), pieces[-1].strip()
            if re.fullmatch(r"[$A-Za-z_][$\w]*", public) and re.fullmatch(r"[$A-Za-z_][$\w]*", local):
                names[public].add(local)
    return names


def _resolve(name, request, index):

    request = request.split("!")[-1].split("?")[0]
    if not request.startswith(".") and not name.endswith(VISUAL):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(name), request))
    if base.startswith("../") or base.startswith("/"):
        return None
    candidates = [base, *(base + suffix for suffix in SCRIPT + VISUAL)]
    candidates.extend(posixpath.join(base, "index" + suffix) for suffix in SCRIPT + VISUAL)
    stem = posixpath.join(posixpath.dirname(base), "_" + posixpath.basename(base))
    candidates.extend([stem, *(stem + suffix for suffix in VISUAL)])
    return next((candidate for candidate in candidates if candidate in index), None)


def dependency_graph(index):

    by_file, demanded, unresolved, edges = defaultdict(set), defaultdict(set), [], []
    exports = {name: export_names(entry) for name, entry in index.items() if "masked" in entry}
    for name in sorted(index):
        entry = index[name]
        if "masked" not in entry:
            continue
        text = "\n".join(entry["text"])
        bindings = entry.get("import_bindings")
        if bindings is None:
            bindings = imported_symbols(text)
        by_request = defaultdict(list)
        for binding in bindings:
            by_request[binding["request"]].append(binding)
        for request in _imports(name, text):
            target = _resolve(name, request, index)
            if target is None:
                if request.startswith("."):
                    unresolved.append({"from": name, "request": request})
                continue
            by_file[target].add(name)
            target_exports = exports.get(target, {})
            local_demand = set()
            for binding in by_request.get(request, []):
                imported = binding["imported"]
                if imported == "*":
                    local_demand.update(symbol for values in target_exports.values() for symbol in values)
                else:
                    local_demand.update(target_exports.get(imported, ()))
            demanded[target].update(local_demand)
            edges.append({"from": name, "to": target, "request": request,
                          "demanded_symbols": sorted(local_demand)})
    return by_file, demanded, unresolved, edges


def _function_at(entry, line, offset=None):

    candidates = [item for item in entry["functions"]
                  if item["line"] <= line <= item["end_line"]
                  and (offset is None or item["start"] <= offset <= item["end"])]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (
        item["end"] - item["start"] if offset is not None else item["end_line"] - item["line"],
        item["start"],
    ))


def read_set(entry, line, limit=24, offset=None):

    owner = _function_at(entry, line, offset)
    cache = entry.setdefault("_read_set_cache", {})
    cache_key = (owner["start"], owner["end"]) if owner else (0, 0)
    if cache_key in cache:
        symbols, basis = cache[cache_key]
        return list(symbols[:limit]), basis
    reachable = []
    bindings = entry.get("import_bindings")
    if bindings is None:
        bindings = imported_symbols("\n".join(entry["text"]))
    imported = [item["local"].split(".", 1)[0] for item in bindings if item["local"]]
    if owner:
        basis = "function_candidate_upper_bound"
        reachable.extend(owner.get("params") or [])
        masked = "\n".join(entry["masked"].splitlines()[owner["line"] - 1:owner["end_line"]])
        reachable.extend(re.findall(r"\b(?:const|let|var|function|class)\s+([$A-Za-z_][$\w]*)", masked))
        reachable.extend(imported)
        reachable.append("this")
    else:
        basis = "module_candidate_upper_bound"
        masked = "\n".join(entry["masked"].splitlines()[:40])
        reachable.extend(re.findall(r"\b(?:const|let|var|function|class)\s+([$A-Za-z_][$\w]*)", masked))
        reachable.extend(imported)
    seen, ordered = set(), []
    for symbol in reachable:
        if symbol and symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    cache[cache_key] = (tuple(ordered), basis)
    return ordered[:limit], basis


def enumerate_boundaries(index, consumers, demanded, limit=400):

    boundaries, seen = [], set()
    for name in sorted(index):
        entry = index[name]
        if "masked" not in entry or name.endswith(VISUAL):
            continue
        lines = entry["text"]
        masked_lines = entry["masked"].splitlines()

        def add(kind, line, end_line, start, end, expression, detail):
            key = (name, kind, start, end)
            if key in seen:
                return
            seen.add(key)
            boundaries.append(_boundary(name, kind, line, end_line, start, end, expression, index, lines,
                                        consumers, detail))
        for test in entry["tests"]:
            if test["form"] == "dynamic" or not test.get("value"):
                continue
            if test["form"] == "string_argument" and test.get("method") not in MATCH_METHODS:
                continue
            add("matching", test["line"], test["end_line"], test["start"], test["end"], test["value"],
                {"form": test["form"], "flags": test.get("flags", ""),
                 "method": test.get("method", ""),
                 "rule_owner": _rule_owner(masked_lines, test["line"])})
        for item in entry["assignments"]:
            if not item["member"] or not item["property"]:
                continue
            if not IDENTITY_PROPERTY.search(item["property"]):
                continue
            if item["inLoop"] or _cache_scope_before(entry, item["line"]):
                add("identity_binding", item["line"], item["end_line"], item["start"], item["end"], item["target"],
                    {"property": item["property"], "in_loop": item["inLoop"],
                     "owner_line": item.get("owner", 0)})
        for item in entry["declarations"]:
            if item.get("isReturn"):
                add("value_decision", item["line"], item["end_line"], item["start"], item["end"], "return",
                    {"init": item.get("init", ""), "owner_line": item.get("owner", 0),
                     "is_return": True})
                continue
            name_ = item.get("name", "")
            if not name_ or not item.get("guard"):
                continue
            readers = _symbol_reads(masked_lines, name_, item["line"])
            add("value_decision", item["line"], item["end_line"], item["start"], item["end"], name_,
                {"init": item.get("init", ""), "owner_line": item.get("owner", 0),
                 "reader_count": readers})
        for item in entry["decisions"]:
            if not item.get("test"):
                continue
            add("value_decision", item["line"], item["end_line"], item["start"], item["end"], item["test"],
                {"branches": 2 if item.get("hasElse") else 1, "owner_line": item.get("owner", 0),
                 "is_guard": True})
        for item in entry["literals"]:
            if item.get("form") != "ArrayExpression" or (item.get("size") or 0) < 2:
                continue
            add("render_group", item["line"], item["end_line"], item["start"], item["end"], "",
                {"form": item["form"], "size": item.get("size"),
                 "owner_line": item.get("owner", 0)})
        for symbol in sorted(demanded.get(name, ())):
            for item in entry["functions"]:
                if (item["name"] != symbol and item["name"].split(".")[-1] != symbol
                        and not item["name"].startswith(symbol + ".")):
                    continue
                add("output_path", item["line"], item["end_line"], item["start"], item["end"], item["name"],
                    {"named_callable": item["name"],
                     "consumer_count": len(consumers.get(name, ())),
                     "demanded_symbol": symbol})
    ranked = sorted(boundaries, key=lambda item: (-item["specificity"], item["file"], item["line"]))
    return ranked[:limit], ranked


def _symbol_reads(masked_lines, symbol, declaration_line, window=120):

    if not symbol or re.fullmatch(r"[$A-Za-z_][\w$]*", symbol) is None:
        return 0
    pattern = re.compile(r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])")
    horizon = min(len(masked_lines), declaration_line + window)
    return sum(len(pattern.findall(line)) for line in masked_lines[declaration_line:horizon])


def _rule_owner(masked_lines, line, window=30):

    for number in range(line - 1, max(-1, line - window - 1), -1):
        if number >= len(masked_lines):
            continue
        match = re.match(r"\s*([$A-Za-z_][\w$]*)\s*:\s*", masked_lines[number])
        if match:
            return match.group(1)
        if re.search(r"\b(?:function|class)\s+[$A-Za-z_]", masked_lines[number]):
            found = re.search(r"\b(?:function|class)\s+([$A-Za-z_][\w$]*)", masked_lines[number])
            return found.group(1) if found else ""
    return ""


def _boundary(name, kind, line, end_line, start, end, expression, index, lines, consumers, detail):

    entry = index[name]
    symbols, basis = read_set(entry, line, offset=start)
    return {
        "id": f"{kind}:{name}:{line}:{start}-{end}",
        "kind": kind,
        "mechanism": MECHANISM[kind],
        "edit_unit": EDIT_UNIT[kind],
        "file": name,
        "line": line,
        "end_line": end_line,
        "span": [start, end],
        "expression": expression,
        "text": lines[line - 1].strip()[:240] if 0 < line <= len(lines) else "",
        "read_symbols": symbols,
        "read_basis": basis,
        "consumers": sorted(consumers.get(name, []))[:8],
        "specificity": _specificity(kind, expression, detail, symbols),
        "detail": detail,
    }


def _specificity(kind, expression, detail, symbols):

    score = {"matching": 30, "identity_binding": 34, "output_path": 26,
             "render_group": 16, "value_decision": 22}[kind]
    if kind == "matching":
        if detail.get("rule_owner"):
            score += 12
        if detail.get("method") in {"match", "matchAll", "test", "exec", "replace", "replaceAll"}:
            score += 6
        if re.search(r"\([^)]*\)|\[[^\]]*\]|\|", expression):
            score += 8
        groups = len(re.findall(r"\((?!\?)", expression))
        score += min(6, groups * 2)
    if kind == "output_path":
        if detail.get("demanded_symbol"):
            score += 10
        score += min(8, int(detail.get("consumer_count") or 0))
    if kind == "identity_binding":
        if detail.get("in_loop"):
            score += 8
        if detail.get("property"):
            score += 4
    if kind == "value_decision":
        if detail.get("is_return"):
            score += 2
        if detail.get("is_guard"):
            score += 10
        score += min(10, int(detail.get("reader_count") or 0))
        if re.search(r"Math\.|\.length|\.slice|\?\?|\|\||&&", expression or ""):
            score += 8
    score += min(6, len(symbols) // 4)
    return score


def expressivity(boundary, constraints):

    relevant = [item for item in constraints
                if item.get("entity") and item.get("provenance_valid") is not False]
    symbols = sorted(set(boundary.get("read_symbols", ())))[:16]
    if not relevant:
        reason = "no_grounded_constraint"
    elif boundary.get("read_basis") != "function_candidate_upper_bound":
        reason = "static_read_set_incomplete"
    else:
        reason = "report_entities_not_bound_to_runtime_objects"
    return {"verdict": "UNKNOWN", "certificate": None, "reason": reason,
            "colliding_objects": [], "read_intersection": symbols,
            "excludes_candidate": False,
            "required_runtime_evidence": "object_identity_and_equal_z_b_observations"}


def obligation_summary(index, boundary):

    entry = index.get(boundary["file"])
    if entry is None:
        return []
    property_name = boundary["detail"].get("property", "")
    if not property_name or re.fullmatch(r"[$A-Za-z_][$\w]*", property_name) is None:
        return []
    pattern = re.compile(r"(?<![\w$])" + re.escape(property_name) + r"\s*(?:=(?!=)|\.|\()")
    found = []
    boundary_end = boundary.get("end_line", boundary["line"])
    for number, line in enumerate(entry["masked"].splitlines(), 1):
        if pattern.search(line):
            found.append({"file": boundary["file"], "line": number,
                          "text": entry["text"][number - 1].strip()[:200],
                          "outside_boundary": not (boundary["line"] <= number <= boundary_end)})
    return found[:12]
