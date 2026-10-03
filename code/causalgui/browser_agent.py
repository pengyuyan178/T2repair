from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path


SEED = 42
SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
EXCLUDED = {"node_modules", ".git", "test", "tests", "__tests__", "fixtures", "dist", "build"}
REGRESSION_CACHE = {}
OWNED_SNAPSHOTS = {}
AST_SCRIPT = r"""
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
"""

HOOK_SCRIPT = r"""
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
globalThis[HOOK]={
  loaded:file=>{if(!state.loaded.includes(file))state.loaded.push(file);flush();},
  hit:(id,read)=>{
    if (state.events.filter(x=>x.id===id).length<32) state.events.push({id,value:scalar(read()),read_succeeded:true});
    flush();
  }
};
"""


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _environment(repo: Path) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=str(SEED), TZ="UTC", CI="true")
    node = _node()
    env["PATH"] = os.pathsep.join([str(repo / "node_modules/.bin"), str(Path(node).parent), env.get("PATH", "")])
    return env


def _node() -> str:
    return os.environ.get("CAUSALGUI_NODE") or shutil.which("node") or "node"


def _run(command: list[str], cwd: Path, output: Path, seconds: int = 90) -> dict:

    output.mkdir(parents=True, exist_ok=True)
    cwd = Path(cwd).resolve()
    environment = _environment(cwd)
    environment["PATH"] = os.pathsep.join(
        str(cwd) if not part else str(cwd / part) if not os.path.isabs(part) else part
        for part in environment["PATH"].split(os.pathsep)
    )
    name = str(cwd / command[0]) if os.path.dirname(command[0]) else command[0]
    program = shutil.which(name, path=environment["PATH"])
    if program is None:
        result = {"command": command, "returncode": 127, "reason": "executable_unavailable"}
        _write(output / "process.json", result)
        return result
    executable = ["timeout", "--signal=TERM", "--kill-after=5", str(seconds), program, *command[1:]]
    with (output / "stdout.log").open("w", encoding="utf-8") as stdout, (output / "stderr.log").open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(executable, cwd=cwd, env=environment, stdout=stdout, stderr=stderr, check=False)
    result = {"command": command, "returncode": completed.returncode,
              "stdout": str(output / "stdout.log"), "stderr": str(output / "stderr.log")}
    _write(output / "process.json", result)
    return result


def _parser(repo: Path) -> str:

    from .front import _parser_root

    return _parser_root(Path(repo))


def _ast(repo: Path, path: Path, output: Path, hypotheses: list | None = None, hook: str = "", instrument: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    parser = _parser(repo)
    if not parser or not path.is_file():
        return {"ok": False, "reason": "babel_parser_or_source_unavailable", "probes": []}
    script = output / "ast.cjs"
    script.write_text(AST_SCRIPT, encoding="utf-8")
    _write(output / "request.json", {"parser": parser, "file": str(path), "relative": str(path.relative_to(repo)),
                                    "hypotheses": hypotheses or [], "hook": hook, "instrument": instrument})
    result = _run([_node(), str(script), str(output / "request.json")], repo, output)
    if result["returncode"]:
        return {"ok": False, "reason": "ast_parse_failed", "process": result, "probes": []}
    return {"ok": True, **json.loads((output / "stdout.log").read_text(encoding="utf-8"))}


def _files(repo: Path) -> list[str]:
    metadata = repo / ".git/causalgui_dependencies.json"
    declared = json.loads(metadata.read_text()) if metadata.exists() else {}
    preserved = declared.get("snapshot_files", []) if isinstance(declared, dict) else []
    listed = subprocess.run(["git", "-C", str(repo), "ls-files", "--cached", "-z"], capture_output=True, check=False)
    if listed.returncode:
        return preserved
    untracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"])
    tracked = [p for p in listed.stdout.decode().split("\0") if p] + preserved
    extra = [p for p in untracked.decode().split("\0") if p and "node_modules" not in Path(p).parts]
    return list(dict.fromkeys([*tracked, *extra]))


def _release_snapshot(target: Path, owner: Path, identity: tuple[int, int], output: Path) -> None:

    assert OWNED_SNAPSHOTS.get(str(target)) == (owner, identity)
    assert target.is_absolute() and not target.is_symlink()
    assert target.resolve(strict=True) == target
    assert target.parent == owner and target.is_relative_to(owner) and target.name.startswith("cg-runtime-")
    stat = target.stat()
    assert (stat.st_dev, stat.st_ino) == identity
    assert shutil.rmtree.avoids_symlink_attacks
    shutil.rmtree(target)
    del OWNED_SNAPSHOTS[str(target)]
    _write(output / "runtime_cleanup.json", {"runtime_repo": str(target), "owner": str(owner), "released": True,
                                             "shared_dependencies_followed": False})


def link_dependencies(source: Path, target: Path, paths: list[str], repo_links=None) -> None:

    source, target = source.resolve(), target.resolve()
    roots = [Path(name) for name in paths]
    assert len(set(roots)) == len(roots)
    assert all(not path.is_absolute() and path.name == "node_modules"
               and not {"..", ".git"}.intersection(path.parts) for path in roots)
    assert all(not a.is_relative_to(b) for a in roots for b in roots if a != b)
    repo_links = {Path(name): Path(location) for name, location in (repo_links or {}).items()}
    assert all(any(name.is_relative_to(root) for root in roots) for name in repo_links)
    assert all(not name.is_absolute() and not {"..", ".git"}.intersection(name.parts) for name in repo_links)
    assert all(not location.is_absolute() and not {"..", ".git"}.intersection(location.parts)
               for location in repo_links.values())

    def bind(relative):

        origin, destination = source / relative, target / relative
        if any(name != relative and name.is_relative_to(relative) for name in repo_links):
            assert origin.is_dir() and not destination.is_symlink(), str(relative)
            children = sorted(origin.iterdir())
            if destination.exists():
                assert destination.is_dir() and {p.name for p in destination.iterdir()} == {p.name for p in children}
            else:
                destination.mkdir()
            for child in children:
                bind(relative / child.name)
            return
        location = target / repo_links[relative] if relative in repo_links else origin
        if relative in repo_links:
            assert location.resolve().is_relative_to(target), str(relative)
        if destination.is_symlink():
            assert os.readlink(destination) == str(location), str(relative)
        else:
            assert not destination.exists(), str(relative)
            destination.symlink_to(location, target_is_directory=location.is_dir())

    for relative in roots:
        dependency, destination = source / relative, target / relative
        assert dependency.is_dir(), str(dependency)
        assert destination.parent.resolve().is_relative_to(target), str(destination)
        if any(name.is_relative_to(relative) for name in repo_links):
            destination.parent.mkdir(parents=True, exist_ok=True)
            bind(relative)
            continue
        if destination.is_symlink():
            assert destination.resolve(strict=True) == dependency.resolve(strict=True), str(destination)
            continue
        assert not destination.exists(), f"Declared dependency conflicts with repository content: {relative}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(dependency.resolve(strict=True), target_is_directory=True)


def _snapshot(repo: Path, output: Path, resources: ExitStack) -> Path:

    scratch = Path(os.environ.get("CAUSALGUI_SCRATCH", str(output))).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="cg-runtime-", dir=scratch))
    stat = target.stat()
    OWNED_SNAPSHOTS[str(target)] = (scratch, (stat.st_dev, stat.st_ino))
    resources.callback(_release_snapshot, target, scratch, (stat.st_dev, stat.st_ino), output)
    copied = []
    for name in _files(repo):
        source, destination = repo / name, target / name
        if Path(name).parts[0] in {"dist", "build"}:
            continue
        if source.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
            copied.append(name)
            continue
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        shutil.copymode(source, destination)
        copied.append(name)
    metadata = repo / ".git/causalgui_dependencies.json"
    declared = json.loads(metadata.read_text()) if metadata.exists() else []
    paths = declared["paths"] if isinstance(declared, dict) else declared
    links = declared.get("repo_links", {}) if isinstance(declared, dict) else {}
    link_dependencies(repo, target, paths, links)
    _write(target / ".git/causalgui_dependencies.json", {"paths": paths, "repo_links": links, "snapshot_files": copied})
    _write(output / "runtime_source.json", {"source": str(repo), "runtime_repo": str(target), "old_dist_copied": False})
    return target


def _profile(repo: Path, draft: dict) -> str:
    package = json.loads((repo / "package.json").read_text(encoding="utf-8")) if (repo / "package.json").is_file() else {}
    return str(draft.get("profile") or package.get("name") or "unknown")


def preflight(repo: Path, output: Path, profile: str = "") -> dict:

    repo, output = Path(repo).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    node = _run([_node(), "--version"], repo, output / "node", 15)
    chromium = os.environ.get("CAUSALGUI_CHROMIUM") or os.environ.get("TRACE_REPAIR_CHROMIUM_EXECUTABLE", "")
    browser = _run([chromium, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-background-networking", "--dump-dom", "about:blank"], repo, output / "chromium", 20) if chromium else {"returncode": 127}
    modules = {name: (repo / "node_modules" / name / "package.json").is_file()
               for name in ["browserify", "rollup", "esbuild", "webpack", "jsdom"]}
    package = json.loads((repo / "package.json").read_text(encoding="utf-8")) if (repo / "package.json").is_file() else {}
    entry = (repo / str(package.get("main") or "index.js")).resolve()
    load = _run([_node(), "-e", "require(process.argv[1]);process.stdout.write('target_loaded');", str(entry)], repo,
                output / "entry_load", 20) if entry.is_file() and entry.is_relative_to(repo) else {"returncode": 127, "reason": "package_entry_unavailable_before_build"}
    result = {"profile": profile or _profile(repo, {}), "node_available": node["returncode"] == 0,
              "chromium_available": browser["returncode"] == 0, "chromium": chromium,
              "babel_parser": _parser(repo), "dependencies_available": (repo / "node_modules").exists(),
              "modules": modules, "seed": SEED, "viewport": [1280, 720], "device_scale_factor": 1,
              "entry": str(entry), "entry_load": load,
              "T0_available": True, "T1_available": node["returncode"] == 0 and load["returncode"] == 0,
              "T2_available": browser["returncode"] == 0,
              "availability_note": "Runtime presence is not successful target loading or mechanism activation."}
    _write(output / "preflight.json", result)
    return result


def _build(repo: Path, draft: dict, output: Path) -> dict:
    profile = _profile(repo, draft).lower()
    package = json.loads((repo / "package.json").read_text(encoding="utf-8")) if (repo / "package.json").is_file() else {}
    command = draft.get("build_command") or []
    source_entry = str(draft.get("entry") or "")
    entry = ""
    if command:
        result = _run([str(x) for x in command], repo, output, 150)
    elif "chart" in profile and (repo / "node_modules/rollup/dist/bin/rollup").is_file():
        result = _run([_node(), "node_modules/rollup/dist/bin/rollup", "-c"], repo, output, 150)
        entry = str(package.get("main") or "dist/chart.js")
    elif ("p5" in profile) and (repo / "node_modules/browserify/bin/cmd.js").is_file() and (repo / "src/app.js").is_file():
        entry = ".cg_bundle.js"
        result = _run([_node(), "node_modules/browserify/bin/cmd.js", source_entry or "src/app.js", "--standalone", "p5", "--outfile", entry], repo, output, 150)
    else:
        result = {"returncode": 0, "reason": "no_build_declared"}
    if not entry:
        entry = str(package.get("browser") if isinstance(package.get("browser"), str) else package.get("main", ""))
        if not entry or not (repo / entry).is_file():
            entry = source_entry
    path = (repo / entry).resolve() if entry else repo
    exists = path.is_file() and path.is_relative_to(repo)
    return {**result, "entry": str(path), "entry_exists": exists,
            "entry_sha256": _sha(path) if exists else "", "entry_bytes": path.stat().st_size if exists else 0}


def _hook_text(hook: str) -> str:
    return HOOK_SCRIPT.replace("HOOK", json.dumps(hook))


def _node_probe(repo: Path, draft: dict, output: Path, hook: str) -> dict:
    script = str(draft.get("node_script") or "")
    if not script.strip():
        return {"ok": False, "reason": "no_node_reproducer", "state": {}}
    output.mkdir(parents=True, exist_ok=True)
    events = output / "events.json"
    entry = repo / ".cg_node.cjs"
    bootstrap = "const fs=require('fs');\n" + _hook_text(hook)
    bootstrap += "\nfunction flush(){fs.writeFileSync(" + json.dumps(str(events)) + ",JSON.stringify(state));}\n"
    bootstrap += "process.on('exit',flush);process.on('uncaughtExceptionMonitor',e=>{state.errors.push(String(e));flush();});\n"
    bootstrap += "(async()=>{\n" + script + "\n})().then(()=>{state.scenario_complete=true;flush();});\n"
    entry.write_text(bootstrap, encoding="utf-8")
    shutil.copyfile(entry, output / "reproducer.cjs")
    result = _run([_node(), str(entry)], repo, output, 35)
    state = json.loads(events.read_text(encoding="utf-8")) if events.is_file() else {}
    return {"ok": result["returncode"] == 0 and bool(state.get("scenario_complete")) and bool(state.get("loaded")) and not state.get("errors"),
            "reason": "node_execution", "state": state, "process": result, "artifact": str(events)}


def _prepare_browser_page(repo: Path, draft: dict, build: dict, output: Path, hook: str):
    chromium = os.environ.get("CAUSALGUI_CHROMIUM") or os.environ.get("TRACE_REPAIR_CHROMIUM_EXECUTABLE", "")
    script = str(draft.get("browser_script") or "")
    if not chromium or not script.strip() or not build["entry_exists"] or build["returncode"]:
        return {"ok": False, "reason": "browser_reproducer_or_built_entry_unavailable", "state": {}}
    output.mkdir(parents=True, exist_ok=True)
    tag = hook + "_result"
    bootstrap = _hook_text(hook)
    bootstrap += "\nfunction flush(){let e=document.getElementById(" + json.dumps(tag) + ");if(!e){e=document.createElement('script');e.id=" + json.dumps(tag) + ";e.type='application/json';document.documentElement.appendChild(e);}e.textContent=JSON.stringify(state).replace(/</g,'\\u003c');}\n"
    bootstrap += "state.console=[];for(const level of ['log','warn','error']){const original=console[level];console[level]=(...args)=>{state.console.push({level,text:args.map(String).join(' ').slice(0,2000)});state.console=state.console.slice(-60);flush();original.apply(console,args);};}\n"
    bootstrap += "window.addEventListener('error',e=>{state.errors.push(String(e.message));state.scenario_complete=false;flush();});window.addEventListener('unhandledrejection',e=>{state.errors.push(String(e.reason));state.scenario_complete=false;flush();});\n"
    (repo / ".cg_boot.js").write_text(bootstrap, encoding="utf-8")
    suffix = "\n})().then(()=>new Promise(r=>setTimeout(r,250))).then(()=>{state.scenario_complete=state.errors.length===0;state.canvas_count=document.querySelectorAll('canvas').length;state.body_text=document.body.innerText.slice(0,200);state.dom_elements=document.body.querySelectorAll(':not(script)').length;flush();});\n"
    source = Path(build["entry"]).relative_to(repo).as_posix()
    entry_text = Path(build["entry"]).read_text(encoding="utf-8")
    module = source.endswith(".mjs") or draft.get("entry_type") == "module" or bool(re.search(r"^\s*(?:import\s|export\s)", entry_text, re.M))
    setup = "globalThis.CausalGUITarget=await import(" + json.dumps("./" + source) + ");\n" if module else ""
    (repo / ".cg_browser.js").write_text("(async()=>{\n" + setup + script + suffix, encoding="utf-8")
    loader = '' if module else '<script src="' + html.escape(source, quote=True) + '"></script>'
    target = repo / ".cg_page.html"
    target.write_text('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:1280px;min-height:720px}</style></head><body><script src=".cg_boot.js"></script>' + loader + '<script src=".cg_browser.js"></script></body></html>', encoding="utf-8")
    for name in [".cg_boot.js", ".cg_browser.js", ".cg_page.html"]:
        shutil.copyfile(repo / name, output / name)
    return {"ok": True, "target": target, "tag": tag}


def _prepare_witnesses(repo: Path, hypotheses: list[dict], output: Path, mode: str, resources: ExitStack):

    repo, output = Path(repo).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    hook = "__cg_" + hashlib.sha256(json.dumps(hypotheses, sort_keys=True).encode()).hexdigest()[:16]
    runtime = _snapshot(repo, output, resources)
    by_file = {}
    for hypothesis in hypotheses:
        by_file.setdefault(str(hypothesis.get("file") or ""), []).append(hypothesis)
    records = {}
    for index, (name, items) in enumerate(by_file.items()):
        path = (runtime / name).resolve()
        valid = path.is_relative_to(runtime) and path.is_file()
        info = _ast(runtime, path, output / "T0" / str(index), items, hook, mode not in {"off", "static"}) if valid and path.suffix in SOURCE_SUFFIXES else {"ok": False, "probes": [], "reason": "non_javascript_t0" if valid else "source_unavailable"}
        if valid:
            preserved = output / "instrumented_sources" / path.relative_to(runtime)
            preserved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, preserved)
        probes = {str(p["id"]): p for p in info["probes"]}
        for h in items:
            point = probes.get(str(h["id"]), {})
            records[str(h["id"])] = {"id": h["id"], "tier": "T0", "symbol_exists": point.get("symbol_exists"),
                "reached": None, "values": [], "execution_ok": False, "artifact": str(output / "T0" / str(index)),
                "reason": point.get("reason", info.get("reason", "source_unavailable")), "source_sha256": _sha(repo / name) if valid else "",
                "instrumented_line": point.get("instrumented_line"), "static_reachable": None,
                "scenario_complete": False, "coverage": {"complete": False, "scenarios": 0}, "tiers": [],
                "static_certificate": point.get("certificate")}
    return runtime, hook, records


def _merge_layers(records, layers):

    for tier, result in layers:
        state = result.get("state", {})
        for record in records.values():
            events = [event for event in state.get("events", []) if str(event.get("id")) == str(record["id"])]
            reached = bool(events) if result["ok"] and record["instrumented_line"] is not None else (True if events else None)
            observation = {"tier": tier, "execution_ok": result["ok"], "reached": reached,
                           "values": [event["value"] for event in events], "scenario_complete": bool(state.get("scenario_complete")),
                           "target_loaded": bool(state.get("loaded")), "artifact": result.get("artifact", ""),
                           "reason": result["reason"], "errors": state.get("errors", [])}
            if result["ok"] and record["static_certificate"] and any(e.get("read_succeeded") for e in events):
                observation["evidence"] = {**record["static_certificate"], "verified": True}
            record["tiers"].append(observation)
            if result["ok"] and (events or not record["execution_ok"]):
                record.update(observation)
                record["coverage"] = {"complete": False, "scenarios": 1, "reason": "single_issue_reproducer_is_not_exhaustive"}
                if record["static_certificate"] and any(e.get("read_succeeded") for e in events):
                    record["evidence"] = {**record["static_certificate"], "verified": True}


def _run_witnesses(repo: Path, hypotheses: list[dict], draft: dict, output: Path, mode: str, resources: ExitStack):
    runtime, hook, records = _prepare_witnesses(repo, hypotheses, output, mode, resources)
    if mode not in {"off", "static"}:
        build = _build(runtime, draft, output / "build")
        _write(output / "build.json", build)
        layers = [("T1", _node_probe(runtime, draft, output / "T1", hook))]
        if mode == "probe+render":
            layers.append(("T2", _browser_probe(runtime, draft, build, output / "T2", hook)))
        _merge_layers(records, layers)
    ordered = [records[str(h["id"])] for h in hypotheses]
    _write(output / "witnesses.json", ordered)
    return ordered


def run_witnesses(repo: Path, hypotheses: list[dict], draft: dict, output: Path, mode: str = "probe+render") -> list[dict]:

    with ExitStack() as resources:
        return _run_witnesses(repo, hypotheses, draft, output, mode, resources)


PLAYWRIGHT_ACTIONS = r'''
import json, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
cfg = json.loads(Path(sys.argv[1]).read_text())
out = Path(cfg['output'])
observed = {'completed_actions': [], 'state': {}, 'dom': '', 'url': ''}
def save():
    observed['state'] = page.evaluate('(tag)=>{const n=document.getElementById(tag);return n?JSON.parse(n.textContent):{}}', cfg['tag'])
    observed['dom'] = page.locator('body').inner_text()[:12000]
    observed['html'] = page.locator('body').inner_html()[:24000]
    observed['url'] = page.url
    observed['actual_scene'] = page.locator('body :not(script)').count() > 0 or bool(observed['dom'])
    screenshot = out / ('step_%02d.png' % len(observed['completed_actions']))
    page.screenshot(path=str(screenshot))
    observed['screenshot'] = str(screenshot)
    (out / 'observation.json').write_text(json.dumps(observed, ensure_ascii=False))
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(cfg['endpoint'])
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size({'width': 1280, 'height': 720})
    page.set_default_timeout(5000)
    for action in cfg['actions']:
        kind, target, value = action['kind'], action['target'], action['value']
        if kind == 'open':
            path = (Path(cfg['repo']) / target).resolve() if target else Path(cfg['target'])
            assert path.is_relative_to(Path(cfg['repo'])) and path.is_file(), 'local_page_required'
            page.goto(path.as_uri(), wait_until='load', timeout=15000)
            page.wait_for_timeout(350)
        elif kind == 'click':
            page.locator(target).click()
            page.wait_for_timeout(100)
        elif kind == 'fill':
            page.locator(target).fill(value)
            page.wait_for_timeout(100)
        elif kind == 'scroll':
            page.mouse.wheel(0, int(value))
            page.wait_for_timeout(150)
        elif kind == 'dom':
            observed['query'] = page.locator(target or 'body').evaluate_all(
                '(nodes)=>nodes.slice(0,30).map(n=>({tag:n.tagName,text:n.innerText,value:n.value,html:n.outerHTML.slice(0,4000)}))')
        elif kind == 'screenshot':
            page.screenshot(path=str(out / 'screenshot.png'))
        observed['completed_actions'].append(action)
        save()
    observed['state'] = page.evaluate('(tag)=>{const n=document.getElementById(tag);return n?JSON.parse(n.textContent):{}}', cfg['tag'])
    observed['dom'] = page.locator('body').inner_text()[:12000]
    observed['html'] = page.locator('body').inner_html()[:24000]
    observed['url'] = page.url
    observed['actual_scene'] = page.locator('body :not(script)').count() > 0 or bool(observed['dom'])
    page.screenshot(path=str(out / 'screenshot.png'))
    observed['screenshot'] = str(out / 'screenshot.png')
    save()
'''


class BrowserSession:


    def __init__(self, repo, page, output, resources):
        self.repo, self.page, self.output = repo, page, Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        chromium = os.environ.get("CAUSALGUI_CHROMIUM") or os.environ.get("TRACE_REPAIR_CHROMIUM_EXECUTABLE", "")
        self.endpoint = ""
        if not chromium:
            return
        profile = repo / ".cg_chrome"
        command = [chromium, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                   "--disable-background-networking", "--disable-component-update", "--allow-file-access-from-files",
                   "--window-size=1280,720", "--force-device-scale-factor=1", "--hide-scrollbars",
                   "--remote-debugging-port=0", "--user-data-dir=" + str(profile), "about:blank"]
        stdout = resources.enter_context((self.output / "chrome.stdout.log").open("w"))
        stderr = resources.enter_context((self.output / "chrome.stderr.log").open("w"))
        process = subprocess.Popen(command, cwd=repo, env=_environment(repo), stdout=stdout, stderr=stderr,
                                   start_new_session=True)

        def close():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()

        resources.callback(close)
        deadline = time.monotonic() + 10
        port = profile / "DevToolsActivePort"
        while not port.is_file() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if port.is_file():
            self.endpoint = "http://127.0.0.1:" + port.read_text().splitlines()[0]
        _write(self.output / "session.json", {"command": command, "pid": process.pid, "endpoint": self.endpoint})

    def perform(self, actions, output):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        if not self.endpoint:
            return {"ok": False, "reason": "chrome_cdp_unavailable", "state": {}, "completed_actions": []}
        _write(output / "request.json", {"endpoint": self.endpoint, "repo": str(self.repo),
                "target": str(self.page["target"]), "tag": self.page["tag"], "actions": actions, "output": str(output)})
        script = output / "actions.py"
        script.write_text(PLAYWRIGHT_ACTIONS, encoding="utf-8")
        python = os.environ.get("CAUSALGUI_TOOL_PYTHON", "")
        if not python:
            return {"ok": False, "reason": "playwright_python_unconfigured", "state": {}, "completed_actions": []}
        process = _run([python, str(script), str(output / "request.json")], self.repo, output / "process", 75)
        path = output / "observation.json"
        observed = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"completed_actions": [], "state": {}}
        observed.update(ok=process["returncode"] == 0, process=process,
                        reason="browser_actions" if process["returncode"] == 0 else "browser_action_failed")
        if process.get("stderr"):
            observed["stderr"] = Path(process["stderr"]).read_text(encoding="utf-8")[-6000:]
        return observed


def _browser_result(observed):

    state = observed.get("state", {})
    ok = (observed.get("ok") and state.get("scenario_complete") and state.get("loaded")
          and not state.get("errors") and observed.get("actual_scene") and observed.get("screenshot"))
    return {"ok": bool(ok), "reason": "pinned_chromium_scene" if ok else "target_render_not_demonstrated",
            "state": state, "artifact": observed.get("screenshot", "")}


def _browser_probe(repo, draft, build, output, hook):

    page = _prepare_browser_page(repo, draft, build, output, hook)
    if not page["ok"]:
        return page
    with ExitStack() as resources:
        session = BrowserSession(repo, page, output / "session", resources)
        actions = [{"kind": "open", "target": "", "value": ""}] + draft.get("actions", [])
        observed = session.perform(actions, output / "replay")
        _write(output / "browser_observation.json", observed)
        return _browser_result(observed)


def discover_test_runner(repo):

    result = subprocess.run(["git", "-C", str(repo), "show", "HEAD:package.json"], capture_output=True, text=True)
    if result.returncode:
        return {"available": False, "reason": "base_package_missing"}
    package = json.loads(result.stdout)
    scripts = package.get("scripts", {})
    names = sorted((name for name in scripts if name == "test" or name.startswith("test:")),
                   key=lambda name: (name != "test:unit", name != "test", name))
    names = [name for name in names if not any(part in name.lower() for part in ("watch", "update", "coverage"))]
    if not names or shutil.which("npm") is None:
        return {"available": False, "reason": "base_test_script_unavailable"}
    script = names[0]
    match = re.search(r"\b(jest|mocha|jasmine|karma|ava|tap|tape)\b", scripts[script])
    framework = match[1] if match else "package_script"
    return {"available": True, "framework": framework, "script": script,
            "script_body": scripts[script],
            "command": [shutil.which("npm"), "run", script, "--"],
            "provenance": "base_package_scripts", "package_sha256": hashlib.sha256(result.stdout.encode()).hexdigest()}


def test_summary(text):

    text = re.sub(r"\x1b\[[0-9;]*m", "", text).replace("\r", "\n")
    jest = re.findall(r"^Tests:\s+(.+)$", text, re.M)
    if jest:
        row = jest[-1]
        total = re.search(r"(\d+) total", row)
        counts = {kind: int(count) for count, kind in re.findall(r"(\d+) (passed|failed|skipped|todo)", row)}
        complete = bool(total and int(total[1]) > 0 and sum(counts.values()) == int(total[1]))
        return {"complete": complete, "total": int(total[1]) if total else 0,
                "passed": counts.get("passed", 0), "failed": counts.get("failed", 0)}
    jasmine = re.findall(r"(?:^|\n)\s*(\d+) specs?, (\d+) failures?(?:, (\d+) pending specs?)?", text)
    if jasmine:
        total, failed, pending = (int(value or 0) for value in jasmine[-1])
        return {"complete": total > 0, "total": total, "passed": max(0, total - failed - pending), "failed": failed}
    karma = re.findall(r"Executed (\d+) of (\d+)(?: \(skipped (\d+)\))?([^\n]*)", text)
    if karma:
        done, total, skipped, tail = karma[-1]
        failure = re.search(r"\((\d+) FAILED\)", tail)
        failed = int(failure[1]) if failure else 0
        return {"complete": int(total) > 0 and int(done) + int(skipped or 0) == int(total),
                "total": int(total), "passed": int(done) - failed, "failed": failed}
    mocha = re.findall(r"^\s*(\d+) passing(?: \([^\n]+\))?\s*$", text, re.M)
    failures = re.findall(r"^\s*(\d+) failing\s*$", text, re.M)
    if mocha:
        passed, failed = int(mocha[-1]), int(failures[-1]) if failures else 0
        return {"complete": passed + failed > 0, "total": passed + failed, "passed": passed, "failed": failed}
    total = re.findall(r"^# tests\s+(\d+)", text, re.M)
    passed = re.findall(r"^# pass\s+(\d+)", text, re.M)
    failed = re.findall(r"^# fail\s+(\d+)", text, re.M)
    if total and passed:
        n, ok, bad = int(total[-1]), int(passed[-1]), int(failed[-1]) if failed else 0
        return {"complete": n > 0 and n == ok + bad, "total": n, "passed": ok, "failed": bad}
    return {"complete": False, "total": 0, "passed": 0, "failed": 0}


def run_base_test(repo, runner, path, output):

    if not runner.get("available"):
        return {"ok": False, "status": "UNKNOWN", "reason": runner["reason"]}
    package = json.loads((Path(repo) / "package.json").read_text())
    if package.get("scripts", {}).get(runner["script"]) != runner["script_body"]:
        return {"ok": False, "status": "UNKNOWN", "reason": "base_test_command_changed"}
    if path and (not (Path(repo) / path).is_file() or not
                 (set(Path(path).parts) & {"test", "tests", "__tests__", "spec", "specs"}
                  or re.search(r"\.(test|spec)\.", path))):
        return {"ok": False, "status": "UNKNOWN", "reason": "existing_test_file_required"}
    framework = runner["framework"]
    options = {"jest": ["--runInBand", "--watch=false"], "mocha": [],
               "jasmine": ["--random=true", "--seed=42"], "karma": ["--single-run", "--auto-watch=false"]}.get(framework, [])
    if path and framework not in {"jest", "mocha", "jasmine"}:
        return {"ok": False, "status": "UNKNOWN", "reason": "runner_has_no_verified_file_selection"}
    command = runner["command"] + options + ([path] if path else [])
    with ExitStack() as resources:
        runtime = _snapshot(Path(repo), Path(output) / "source", resources)
        process = _run(command, runtime, Path(output) / "process", 90)
    text = "\n".join(Path(process[key]).read_text(encoding="utf-8", errors="replace")
                     for key in ("stdout", "stderr") if process.get(key))
    summary = test_summary(text)
    status = ("PASS" if process["returncode"] == 0 and summary["complete"] and summary["passed"] > 0 and summary["failed"] == 0
              else "FAIL" if summary["complete"] and summary["failed"] > 0 else "UNKNOWN")
    return {"ok": summary["complete"], "status": status, "runner": runner, "selected_test": path,
            "process": process, "summary": summary, "output_tail": text[-12000:], "seed": SEED,
            "provenance": "base_commit_existing_tests"}


class BrowserTools:


    def __init__(self, repo, hypotheses, structure, output, resources, mode="probe+render", *, evidence=False):
        from .code_agent import SourceTools
        self.repo, self.hypotheses, self.output, self.mode = Path(repo), hypotheses, Path(output), mode
        self.sources = SourceTools(repo, structure, evidence=evidence)
        self.resources = resources.enter_context(ExitStack())
        self.session, self.records, self.node, self.latest = None, {}, {}, {}
        self.draft = {"profile": "", "entry": "", "node_script": "", "browser_script": "", "build_command": [], "actions": []}
        self.attempts = 0

    def execute(self, tool, output):
        if tool["name"] in {"read_source", "search_source", "dependencies", "symbols", "list_sources", "project_info", "run_tests"}:
            return self.sources.execute(tool, output)
        if tool["name"] not in {"reproduce", "interact"}:
            return {"ok": False, "error": "browser_tool_unavailable"}
        if len(tool["actions"]) > 8:
            return {"ok": False, "error": "browser_action_limit_8"}
        if tool["name"] == "reproduce":
            if tool["draft"] is None:
                return {"ok": False, "error": "draft_required"}
            self.resources.close()
            self.attempts += 1
            self.draft = {**tool["draft"], "actions": [], "witness_mode": self.mode}
            folder = self.output / f"attempt_{self.attempts:02d}"
            self.runtime, self.hook, self.records = _prepare_witnesses(self.repo, self.hypotheses, folder, self.mode, self.resources)
            self.session, self.latest, self.node = None, {}, {}
            if self.mode in {"off", "static"}:
                return {"ok": False, "reason": "runtime_disabled", "witnesses": list(self.records.values())}
            build = _build(self.runtime, self.draft, folder / "build")
            _write(folder / "build.json", build)
            self.node = _node_probe(self.runtime, self.draft, folder / "T1", self.hook)
            page = _prepare_browser_page(self.runtime, self.draft, build, folder / "T2", self.hook)
            if self.mode == "probe+render" and page["ok"]:
                self.session = BrowserSession(self.runtime, page, folder / "session", self.resources)
                self.latest = self.session.perform([{"kind": "open", "target": "", "value": ""}] + tool["actions"], Path(output) / "browser")
                completed = self.latest.get("completed_actions", [])[1:]
                self.draft["actions"].extend(completed)
            else:
                self.latest = page
            feedback = {"build": build, "node": self.node, "browser": self.latest}
            for key in ("stdout", "stderr"):
                path = build.get(key)
                if path:
                    feedback["build_" + key] = Path(path).read_text(encoding="utf-8")[-6000:]
            process = self.node.get("process", {})
            if process.get("stderr"):
                feedback["node_stderr"] = Path(process["stderr"]).read_text(encoding="utf-8")[-6000:]
        else:
            if self.session is None:
                return {"ok": False, "error": "reproduce_before_interact"}
            self.latest = self.session.perform(tool["actions"], Path(output) / "browser")
            self.draft["actions"].extend(self.latest.get("completed_actions", []))
            feedback = {"browser": self.latest}
        return {"ok": bool(self.latest.get("ok") or self.node.get("ok")), **feedback,
                "screenshot": self.latest.get("screenshot"), "witnesses": self.witnesses()}

    def witnesses(self):
        records = json.loads(json.dumps(self.records))
        layers = [("T1", self.node)] if self.node else []
        if self.latest:
            layers.append(("T2", _browser_result(self.latest)))
        _merge_layers(records, layers)
        return list(records.values())

    def freeze(self):
        witnesses = self.witnesses()
        _write(self.output / "replay_plan.json", self.draft)
        _write(self.output / "witnesses.json", witnesses)
        _write(self.output / "replay_status.json", {"attempts": self.attempts,
                "browser_actions_completed": len(self.draft["actions"]), "last_browser_ok": self.latest.get("ok"),
                "last_browser_reason": self.latest.get("reason"), "model_calls_in_replay": 0})
        return self.draft, witnesses


def _run_regression(repo: Path, candidate: Path, changed_files: list[str], output: Path, resources: ExitStack) -> dict:

    if os.environ.get("CAUSALGUI_EVIDENCE_POLICY") == "base":
        runner = discover_test_runner(repo)
        key = (str(repo), subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(), "base_suite_v2")
        if key not in REGRESSION_CACHE:
            REGRESSION_CACHE[key] = run_base_test(repo, runner, "", output / "base")
        baseline = REGRESSION_CACHE[key]
        if baseline["status"] != "PASS":
            return {"regression_status": "UNKNOWN", "regression_reason": "base_suite_not_verified_passing", "base": baseline}
        tested = run_base_test(candidate, runner, "", output / "patched")
        comparable = tested.get("summary", {}).get("total") == baseline["summary"]["total"]
        return {"regression_status": tested["status"] if comparable else "UNKNOWN",
                "regression_reason": "frozen_base_suite" if comparable else "test_count_changed",
                "base": baseline, "patched": tested, "seed": SEED}
    unknown = {"regression_status": "UNKNOWN", "regression_reason": "no_verified_nearby_base_jasmine_runner"}
    manifest = subprocess.run(["git", "-C", str(repo), "show", "HEAD:package.json"], capture_output=True, text=True, check=False)
    if manifest.returncode or not (repo / "node_modules/jasmine/bin/jasmine.js").is_file():
        return unknown
    package = json.loads(manifest.stdout)
    commands = [shlex.split(value) for value in package.get("scripts", {}).values() if isinstance(value, str) and value.startswith("jasmine ")]
    command = next((parts for parts in commands if parts and all(part.startswith("--config=") for part in parts[1:])), None)
    if command is None:
        return unknown
    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True, check=False)
    stems = {Path(name).stem.lower() for name in changed_files}
    specs = [name for name in listed.stdout.splitlines() if set(Path(name).parts) & {"test", "tests", "spec", "__tests__"}
             and Path(name).suffix in {".js", ".cjs", ".mjs"}
             and re.sub(r"[._-](spec|test)$", "", Path(name).stem.lower()) in stems
             and re.search(r"[._-](spec|test)$", Path(name).stem, re.I)]
    if not specs or len(specs) > 4:
        return {**unknown, "regression_reason": "no_unambiguous_bounded_neighbor_spec_mapping", "specs": specs}
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    key = (str(repo), head, tuple(command), tuple(specs))
    if key not in REGRESSION_CACHE:
        base = _snapshot(repo, output / "base_source", resources)
        help_result = _run([_node(), "node_modules/jasmine/bin/jasmine.js", "help"], base, output / "help", 20)
        help_text = Path(help_result["stdout"]).read_text(encoding="utf-8") if help_result.get("stdout") else ""
        if "--seed=" not in help_text or "--random=" not in help_text:
            return {**unknown, "regression_reason": "installed_jasmine_seed_flags_not_verified"}
        args = [_node(), "node_modules/jasmine/bin/jasmine.js", *command[1:], "--random=true", "--seed=42", *specs]
        baseline = _run(args, base, output / "base", 90)
        text = Path(baseline["stdout"]).read_text(encoding="utf-8") if baseline.get("stdout") else ""
        count = re.search(r"(\d+) specs?, (\d+) failures?", text)
        baseline["spec_count"] = int(count[1]) if count else 0
        baseline["verified_pass"] = bool(count and int(count[1]) > 0 and int(count[2]) == 0 and baseline["returncode"] == 0)
        REGRESSION_CACHE[key] = {"base": baseline, "args": args}
        resources.close()
    cached = REGRESSION_CACHE[key]
    if not cached["base"]["verified_pass"]:
        return {**unknown, "regression_reason": "base_neighbors_not_verified_passing", "base": cached["base"], "specs": specs}
    changed = _run(cached["args"], candidate, output / "patched", 90)
    text = Path(changed["stdout"]).read_text(encoding="utf-8") if changed.get("stdout") else ""
    count = re.search(r"(\d+) specs?, (\d+) failures?", text)
    complete = bool(count and int(count[1]) == cached["base"]["spec_count"])
    status = "PASS" if complete and int(count[2]) == 0 and changed["returncode"] == 0 else ("FAIL" if complete and int(count[2]) > 0 else "UNKNOWN")
    return {"regression_status": status, "regression_reason": "explicit_base_neighbor_specs", "specs": specs,
            "base": cached["base"], "patched": changed, "seed": SEED}


def _regression(repo: Path, candidate: Path, changed_files: list[str], output: Path) -> dict:

    with ExitStack() as resources:
        return _run_regression(repo, candidate, changed_files, output, resources)


def _validate_patch(repo: Path, patch: str, changed_files: list[str], hypotheses, draft, base_witnesses, output: Path, resources: ExitStack) -> dict:

    repo, output = Path(repo).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "candidate.diff").write_text(patch, encoding="utf-8")
    candidate = _snapshot(repo, output / "candidate", resources)
    _run(["git", "init", "--quiet"], candidate, output / "git_init", 15)
    applied = _run(["git", "apply", "--whitespace=nowarn", str(output / "candidate.diff")], candidate, output / "apply", 20)
    if applied["returncode"]:
        result = {"patch_applied": False, "syntax_ok": None, "validation_failed": True,
                  "missing_symbols": [], "witnesses": [], "probe_preserved": False,
                  "target_improved": False, "render_artifacts": [], "apply": applied}
        _write(output / "validation.json", result)
        return result
    checked, missing, new_members, defined = [], [], set(), set()
    for index, name in enumerate(changed_files):
        path = (candidate / name).resolve()
        if not path.is_relative_to(candidate) or path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        info = _ast(candidate, path, output / "syntax" / str(index))
        base = _ast(repo, repo / name, output / "base_syntax" / str(index)) if (repo / name).is_file() else {"ok": bool(_parser(repo))}
        syntax = info["ok"] if info["ok"] or base["ok"] else None
        checked.append({"file": name, "ok": syntax, "reason": info.get("reason", "parsed"), "base_parse_ok": base["ok"]})
        defined.update(info.get("declarations", []))
        new_members.update(info.get("members", []))
    patterns = output / "member_names.txt"
    patterns.write_text("\n".join(sorted(new_members)) + "\n", encoding="utf-8")
    pathspec = ["*" + suffix for suffix in SOURCE_SUFFIXES]
    pathspec += [":(exclude)**/" + name + "/**" for name in EXCLUDED]
    original = subprocess.run(["git", "-C", str(repo), "grep", "-I", "-w", "-h", "-o", "-F", "-f", str(patterns), "HEAD", "--", *pathspec], capture_output=True, check=False) if new_members else subprocess.CompletedProcess([], 0, b"", b"")
    base_spellings = set(original.stdout.decode("utf-8", errors="replace").splitlines())
    for name in sorted(new_members - base_spellings - defined) if original.returncode in {0, 1} else []:
        missing.append({"symbol": name, "evidence": "member_name_absent_from_base_source_and_patch_definitions",
                        "verdict": "UNKNOWN", "scope": "lexical_absence_is_not_receiver_type_resolution"})
    syntax_ok = False if any(item["ok"] is False for item in checked) else (True if checked and all(item["ok"] is True for item in checked) else None)
    replay_disabled = draft.get("replay_disabled_by_ablation", False)
    witnesses = (run_witnesses(candidate, hypotheses, draft, output / "replay", draft.get("witness_mode", "probe+render"))
                 if syntax_ok is not False and not replay_disabled else [])
    baseline = {str(w["id"]): w for w in base_witnesses}
    changes = [{"id": w["id"], "reached_before": baseline.get(str(w["id"]), {}).get("reached"), "reached_after": w["reached"],
                "value_changed": baseline.get(str(w["id"]), {}).get("values") != w["values"],
                "execution_ok": w["execution_ok"]} for w in witnesses]
    reached_before = [w for w in base_witnesses if w.get("execution_ok") and w.get("reached") is True]
    reached_after = {str(w["id"]) for w in witnesses if w.get("execution_ok") and w.get("reached") is True}
    regression = _regression(repo, candidate, changed_files, output / "regression") if syntax_ok is not False else {"regression_status": "UNKNOWN", "regression_reason": "syntax_failed"}
    result = {"patch_applied": True, "syntax_ok": syntax_ok, "syntax": checked, "missing_symbols": missing, "symbol_validation": "UNKNOWN",
              "witnesses": witnesses, "probe_changes": changes, "probe_improved": False,
              "probe_preserved": bool(reached_before) and all(str(w["id"]) in reached_after for w in reached_before), "target_improved": False,
              **regression,
              "render_artifacts": [w["artifact"] for w in witnesses if w["tier"] == "T2" and w["execution_ok"]],
              "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()}
    if replay_disabled:
        result["witness_replay"] = "disabled_by_ablation"
    _write(output / "validation.json", result)
    return result


def validate_patch(repo: Path, patch: str, changed_files: list[str], hypotheses, draft, base_witnesses, output: Path) -> dict:

    with ExitStack() as resources:
        return _validate_patch(repo, patch, changed_files, hypotheses, draft, base_witnesses, output, resources)
